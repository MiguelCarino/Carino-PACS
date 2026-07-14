"""Print SCP — a virtual DICOM film printer (the "capture print jobs" half).

Some modalities can only *print* a study (to a laser imager / film printer);
they never do a C-STORE.  This module pretends to be that printer: it accepts
the DICOM **Basic Grayscale Print Management** conversation (and optionally
Color), reassembles each film sheet from the image boxes the modality sends,
rasterises it to a **PDF**, and hands the result off to the pending-review
queue — exactly where a loose PDF would land, since a print job carries no
reliable patient identity for us to trust.

Shape mirrors ``scp.py`` (StorageSCP): a non-blocking ``pynetdicom`` server with
the same TLS / allowed-AET / counter / start-stop surface.  The difference is
the DIMSE verbs — print is DIMSE-**N** (N-CREATE/N-SET/N-ACTION/N-GET/N-DELETE)
driving a little Film Session -> Film Box -> Image Box object tree, rather than
a single C-STORE.  We also answer the optional **Basic Annotation Box** (text
strings the modality wants printed on the film — captured, rendered as a caption,
and used as an identity hint) and **Print Job** (a done-status object SCUs poll)
SOP classes, so a fuller-featured print SCU negotiates cleanly.

Identity gap (important): a film carries burned-in pixels, not a structured
PatientID / StudyInstanceUID.  So the render is staged into the review queue for
an operator to identify + approve, never auto-sent.  That reuses the whole
ingest/pending/approve pipeline already in the app.

pynetdicom note: its generic N-CREATE response echoes the *request's* Affected
SOP Instance UID, so we rely on the print SCU supplying the Film Session / Film
Box UIDs (pynetdicom's own SCU and the great majority of modalities do).  The
Image Box UIDs, by contrast, are ours to assign — we return them in the Film
Box N-CREATE response's Referenced Image Box Sequence.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import uuid
from typing import Callable, Optional

from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    BasicAnnotationBox,
    BasicColorImageBox,
    BasicColorPrintManagementMeta,
    BasicFilmBox,
    BasicFilmSession,
    BasicGrayscaleImageBox,
    BasicGrayscalePrintManagementMeta,
    PrintJob,
    PrinterInstance,
    Verification,
)

from .logbuf import LogBuffer

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# The image-box "content" sequences (grayscale / colour variants).
_GRAYSCALE_IMAGE_SEQ = "BasicGrayscaleImageSequence"   # (2020,0110)
_COLOR_IMAGE_SEQ = "BasicColorImageSequence"           # (2020,0111)

# A comfortable film page in pixels (portrait, ~200 dpi A4-ish). Only used as a
# rendering canvas — the actual film size the modality asked for is cosmetic
# once we are producing a document.
_PAGE_W, _PAGE_H = 1654, 2339
_MARGIN = 32

# How many Basic Annotation Boxes we create per film box when a modality asks
# for annotations (via AnnotationDisplayFormatID). The count is printer-defined
# in DICOM print; we publish a generous fixed pool and simply capture whichever
# positions the SCU fills in.
_ANNOTATION_POOL = 6


def _safe(component: str, fallback: str) -> str:
    s = _UNSAFE.sub("_", str(component or "").strip()).strip("._")
    return (s or fallback)[:64]


def _peer_addr(event) -> str:
    try:
        addr = event.assoc.requestor.address
        return str(addr) if addr else "?"
    except Exception:
        return "?"


# --------------------------------------------------------------------- layout
def _layout_cells(fmt: str) -> list[tuple[float, float, float, float]]:
    """Fractional (x, y, w, h) page rectangles, one per image box, for a DICOM
    Image Display Format.  Position N (1-based) uses cell N-1.  Handles the
    common STANDARD\\C,R plus ROW\\... and COL\\... ; anything else falls back to
    a single full-page cell."""
    parts = str(fmt or "").upper().split("\\")
    kind = parts[0].strip() if parts else ""
    spec = parts[1] if len(parts) > 1 else ""

    def _ints(s: str) -> list[int]:
        out = []
        for tok in s.split(","):
            tok = tok.strip()
            if tok.isdigit() and int(tok) > 0:
                out.append(int(tok))
        return out

    cells: list[tuple[float, float, float, float]] = []
    if kind == "STANDARD" and "," in spec:
        nums = _ints(spec)
        if len(nums) == 2:
            cols, rows = nums
            for r in range(rows):
                for c in range(cols):
                    cells.append((c / cols, r / rows, 1 / cols, 1 / rows))
            return cells
    if kind == "ROW":
        rows_spec = _ints(spec)
        if rows_spec:
            nrows = len(rows_spec)
            for r, ncols in enumerate(rows_spec):
                for c in range(ncols):
                    cells.append((c / ncols, r / nrows, 1 / ncols, 1 / nrows))
            return cells
    if kind in ("COL", "COLUMN"):
        cols_spec = _ints(spec)
        if cols_spec:
            ncols = len(cols_spec)
            for c, nrows in enumerate(cols_spec):
                for r in range(nrows):
                    cells.append((c / ncols, r / nrows, 1 / ncols, 1 / nrows))
            return cells
    # Unknown / SLIDE / SUPERSLIDE / CUSTOM — one full-page image.
    return [(0.0, 0.0, 1.0, 1.0)]


# --------------------------------------------------------------- image decode
def _image_from_item(item):
    """A PIL image (mode 'L' or 'RGB') from a Basic Grayscale/Color Image
    Sequence item, or None if it can't be decoded."""
    from PIL import Image, ImageOps

    try:
        rows = int(getattr(item, "Rows", 0))
        cols = int(getattr(item, "Columns", 0))
        pixel = bytes(getattr(item, "PixelData", b"") or b"")
        if rows <= 0 or cols <= 0 or not pixel:
            return None
        samples = int(getattr(item, "SamplesPerPixel", 1) or 1)
        bits = int(getattr(item, "BitsAllocated", 8) or 8)
        stored = int(getattr(item, "BitsStored", bits) or bits)
        photometric = str(getattr(item, "PhotometricInterpretation", "") or "").upper()

        if samples >= 3:
            need = rows * cols * 3
            pixel = (pixel + b"\x00" * need)[:need]
            planar = int(getattr(item, "PlanarConfiguration", 0) or 0)
            if planar == 1:
                # RRR…GGG…BBB → interleave into RGBRGB… so PIL reads it right
                n = rows * cols
                inter = bytearray(3 * n)
                inter[0::3], inter[1::3], inter[2::3] = pixel[:n], pixel[n:2 * n], pixel[2 * n:3 * n]
                pixel = bytes(inter)
            im = Image.frombytes("RGB", (cols, rows), pixel)
            return im

        # single-sample (grayscale)
        if bits > 8:
            # Scale the *used* range (BitsStored) down to 8 bits — a plain
            # ">>8" would render 10/12-bit film data almost black.
            import array
            import sys as _sys
            need = rows * cols * 2
            pixel = (pixel + b"\x00" * need)[:need]
            a = array.array("H")
            a.frombytes(pixel)
            if _sys.byteorder == "big":     # DICOM print is little-endian
                a.byteswap()
            shift = max(0, stored - 8)
            out = bytes(min(255, v >> shift) for v in a)
            im = Image.frombytes("L", (cols, rows), out)
        else:
            need = rows * cols
            pixel = (pixel + b"\x00" * need)[:need]
            im = Image.frombytes("L", (cols, rows), pixel)
        if photometric == "MONOCHROME1":          # 0 = white -> invert for display
            im = ImageOps.invert(im)
        return im
    except Exception:
        return None


# ---------------------------------------------------------- association state
class _FilmBox:
    __slots__ = ("uid", "fmt", "orientation", "cells", "box_uids", "images",
                 "is_color", "anno_uids", "annotations")

    def __init__(self, uid: str, fmt: str, orientation: str, is_color: bool):
        self.uid = uid
        self.fmt = fmt
        self.orientation = orientation
        self.is_color = is_color
        self.cells = _layout_cells(fmt)
        # one image-box UID per layout cell
        self.box_uids = [uid_gen() for _ in range(max(1, len(self.cells)))]
        self.images: dict[str, tuple[int, object]] = {}   # box_uid -> (position, PIL image)
        self.anno_uids: list[str] = []                    # allocated annotation-box UIDs
        self.annotations: dict[str, tuple[int, str]] = {} # anno_uid -> (position, text)

    def annotation_lines(self) -> list[str]:
        """Captured annotation text strings, ordered by AnnotationPosition."""
        return [t for _p, t in sorted(self.annotations.values(), key=lambda x: x[0]) if t]


# Print attributes a (non-conformant but common) modality may carry that let us
# pre-fill identity instead of leaving the operator a blank pending form.
_IDENTITY_TAGS = {
    "PatientName": "patient_name",
    "PatientID": "patient_id",
    "StudyInstanceUID": "study_uid",
    "StudyDescription": "study_desc",
    "StudyDate": "study_date",
    "AccessionNumber": "accession",
}


def _scrape_identity(ds, into: dict) -> None:
    """Copy any present identity attributes from a print dataset into *into*
    (best-effort — most print jobs carry none, so this is usually a no-op)."""
    if ds is None:
        return
    for tag, key in _IDENTITY_TAGS.items():
        val = getattr(ds, tag, None)
        if val:
            into[key] = str(val)
            if tag == "PatientName":
                from .history import _fmt_name
                into["patient"] = _fmt_name(val)


class _Job:
    """Everything a single association is building up."""

    def __init__(self):
        self.session_label = ""
        self.identity_hints: dict = {}                 # scraped Patient/Study fields (usually empty)
        self.film_boxes: dict[str, _FilmBox] = {}      # film-box uid -> _FilmBox
        self.box_to_film: dict[str, str] = {}          # image-box uid -> film-box uid
        self.anno_to_film: dict[str, str] = {}         # annotation-box uid -> film-box uid
        self.last_film: str = ""                       # most-recent film box (lenient annotation fallback)
        self.print_jobs: set = set()                   # print-job UIDs we handed back

    def film_of_anno(self, anno_uid: str) -> Optional[str]:
        """Film box an annotation box belongs to; fall back to the latest film
        box for SCUs that N-SET an annotation UID we didn't pre-reference."""
        return self.anno_to_film.get(anno_uid) or (self.last_film or None)


def uid_gen() -> str:
    from pydicom.uid import generate_uid
    return generate_uid()


class PrintSCP:
    def __init__(
        self,
        aet: str,
        bind: str,
        port: int,
        log: LogBuffer,
        on_output: Callable[[bytes, str, dict, str], None],
        color: bool = False,
        layout: str = "pdf",
        allowed_aets: Optional[list[str]] = None,
        tls: bool = False,
        tls_cert: str = "",
        tls_key: str = "",
        tls_ca: str = "",
    ):
        self.aet = aet
        self.bind = bind
        self.port = port
        self.log = log
        self.on_output = on_output
        self.color = bool(color)
        self.layout = "image" if str(layout).lower() in ("image", "secondary_capture", "sc") else "pdf"
        self.allowed_aets = [a for a in (allowed_aets or []) if str(a).strip()]
        self.tls = tls
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.tls_ca = tls_ca
        self._server = None
        self._lock = threading.Lock()
        self._jobs: dict[int, _Job] = {}
        self.printed_count = 0
        self.error_count = 0

    # ---- per-association state --------------------------------------------
    def _job(self, event) -> _Job:
        key = id(event.assoc)
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = _Job()
                self._jobs[key] = job
            return job

    def _drop_job(self, event) -> None:
        with self._lock:
            self._jobs.pop(id(event.assoc), None)

    # ---- DIMSE handlers ----------------------------------------------------
    def _handle_echo(self, event) -> int:
        who = event.assoc.requestor.ae_title
        self.log.info(f"C-ECHO from {who} @ {_peer_addr(event)}", kind="print")
        return 0x0000

    def _handle_n_get(self, event):
        """Status query — a healthy printer, or a finished print job."""
        from pydicom.dataset import Dataset
        req = event.request
        sop_class = str(getattr(req, "RequestedSOPClassUID", "") or "")
        ds = Dataset()
        if sop_class == PrintJob:
            # SCUs that poll the job they were handed back: report it's done.
            ds.ExecutionStatus = "DONE"
            ds.ExecutionStatusInfo = "NORMAL"
            ds.PrintJobID = str(getattr(req, "RequestedSOPInstanceUID", "") or "")[:16]
            ds.PrinterName = self.aet
        else:
            ds.PrinterStatus = "NORMAL"
            ds.PrinterStatusInfo = "NORMAL"
            ds.PrinterName = self.aet
            ds.Manufacturer = "Carino"
            ds.ManufacturerModelName = "Carino PACS Print"
        return 0x0000, ds

    def _handle_n_create(self, event):
        """Create a Film Session or Film Box.  For a Film Box we also allocate
        its Image Boxes and return their UIDs so the SCU can N-SET them."""
        from pydicom.dataset import Dataset
        req = event.request
        sop_class = str(getattr(req, "AffectedSOPClassUID", "") or "")
        job = self._job(event)
        try:
            attrs = event.attribute_list
        except Exception:
            attrs = Dataset()

        if sop_class == BasicFilmSession:
            job.session_label = str(getattr(attrs, "FilmSessionLabel", "") or "")
            _scrape_identity(attrs, job.identity_hints)
            return 0x0000, Dataset()

        if sop_class == BasicFilmBox:
            film_uid = str(getattr(req, "AffectedSOPInstanceUID", "") or "") or uid_gen()
            fmt = str(getattr(attrs, "ImageDisplayFormat", "") or "STANDARD\\1,1")
            orientation = str(getattr(attrs, "FilmOrientation", "") or "PORTRAIT")
            _scrape_identity(attrs, job.identity_hints)
            # Colour or grayscale is decided by which meta context this arrived
            # on, so the Referenced Image Box class we hand back is the matching
            # one even when the printer advertises both.
            try:
                is_color = str(event.context.abstract_syntax) == BasicColorPrintManagementMeta
            except Exception:
                is_color = False
            fb = _FilmBox(film_uid, fmt, orientation, is_color)
            box_cls = BasicColorImageBox if is_color else BasicGrayscaleImageBox
            seq = []
            for buid in fb.box_uids:
                job.box_to_film[buid] = film_uid
                item = Dataset()
                item.ReferencedSOPClassUID = box_cls
                item.ReferencedSOPInstanceUID = buid
                seq.append(item)
            job.film_boxes[film_uid] = fb
            job.last_film = film_uid
            resp = Dataset()
            resp.ImageDisplayFormat = fmt
            resp.ReferencedImageBoxSequence = seq
            # If the modality wants text annotations printed, allocate a pool of
            # Basic Annotation Boxes and hand back their UIDs for it to N-SET.
            if getattr(attrs, "AnnotationDisplayFormatID", None):
                anno_seq = []
                for _ in range(_ANNOTATION_POOL):
                    auid = uid_gen()
                    fb.anno_uids.append(auid)
                    job.anno_to_film[auid] = film_uid
                    a = Dataset()
                    a.ReferencedSOPClassUID = BasicAnnotationBox
                    a.ReferencedSOPInstanceUID = auid
                    anno_seq.append(a)
                resp.ReferencedBasicAnnotationBoxSequence = anno_seq
            self.log.info(
                f"Film box {fmt} ({len(fb.box_uids)} image(s)"
                f"{', +annotations' if fb.anno_uids else ''}) from "
                f"{event.assoc.requestor.ae_title}", kind="print")
            return 0x0000, resp

        # Print Job / other creatable objects — acknowledge without state.
        return 0x0000, Dataset()

    def _handle_n_set(self, event):
        """Populate an Image Box (the bitmap for film) or an Annotation Box
        (a text string to caption the film) with what the modality sends."""
        from pydicom.dataset import Dataset
        req = event.request
        box_uid = str(getattr(req, "RequestedSOPInstanceUID", "") or "")
        sop_class = str(getattr(req, "RequestedSOPClassUID", "") or "")
        try:
            mods = event.modification_list
        except Exception:
            mods = Dataset()

        job = self._job(event)

        # Annotation box — a text string to print on the film.
        if sop_class == BasicAnnotationBox or box_uid in job.anno_to_film:
            film_uid = job.film_of_anno(box_uid)
            if film_uid and film_uid in job.film_boxes:
                text = str(getattr(mods, "TextString", "") or "").strip()
                position = int(getattr(mods, "AnnotationPosition", 0) or 0)
                if text:
                    fb = job.film_boxes[film_uid]
                    fb.annotations[box_uid] = (position or (len(fb.annotations) + 1), text)
            return 0x0000, Dataset()

        # Image box — the bitmap the modality wants on film.
        film_uid = job.box_to_film.get(box_uid)
        if film_uid and film_uid in job.film_boxes:
            fb = job.film_boxes[film_uid]
            position = int(getattr(mods, "ImageBoxPosition", 0) or 0)
            seq = getattr(mods, _GRAYSCALE_IMAGE_SEQ, None) or getattr(mods, _COLOR_IMAGE_SEQ, None)
            item = seq[0] if seq else None
            if item is not None:
                img = _image_from_item(item)
                if img is not None:
                    fb.images[box_uid] = (position or (len(fb.images) + 1), img)
        # Echo back the modified attributes (SCUs may re-read them).
        return 0x0000, Dataset()

    def _handle_n_action(self, event):
        """The print trigger — render the referenced film box(es) to a PDF."""
        from pydicom.dataset import Dataset
        req = event.request
        sop_class = str(getattr(req, "RequestedSOPClassUID", "") or "")
        target = str(getattr(req, "RequestedSOPInstanceUID", "") or "")
        job = self._job(event)

        if sop_class == BasicFilmBox:
            films = [job.film_boxes[target]] if target in job.film_boxes else []
        else:  # Film Session (print the whole session) or anything else
            films = list(job.film_boxes.values())

        films = [f for f in films if f.images]
        if not films:
            self.log.warn("Print requested but no image data was received", kind="print")
            return 0xB603, Dataset()   # empty page warning (still a success-class status)

        who = event.assoc.requestor.ae_title
        identity = {
            "source": f"{who} @ {_peer_addr(event)}",
            "series_desc": "Printed film",
        }
        # Scraped Patient/Study fields (if the modality sent any) win over the
        # generic label fallback; captured annotation text is the next-best hint.
        identity.update({k: v for k, v in job.identity_hints.items() if v})
        anno = [ln for fb in films for ln in fb.annotation_lines()]
        study_desc = job.session_label or (anno[0] if anno else "") or "Printed film"
        identity.setdefault("study_desc", study_desc)
        try:
            stamp = uuid.uuid4().hex[:8]
            if self.layout == "image":
                # One Secondary-Capture image per film sheet.
                for i, fb in enumerate(films, start=1):
                    png = _render_png(fb)
                    suffix = f"-{i}" if len(films) > 1 else ""
                    name = f"Print-{_safe(who, 'FILM')}-{stamp}{suffix}.png"
                    self.on_output(png, "image", identity, name)
            else:
                # One (possibly multi-page) PDF document for the whole print.
                pdf_bytes = _render_pdf(films)
                name = f"Print-{_safe(who, 'FILM')}-{stamp}.pdf"
                self.on_output(pdf_bytes, "pdf", identity, name)
            with self._lock:
                self.printed_count += 1
            self.log.info(f"Captured print job from {who} → {len(films)} film(s) queued for review",
                          kind="print", path=name)
        except Exception as exc:
            with self._lock:
                self.error_count += 1
            self.log.error(f"Failed to render print job from {who}: {exc}", kind="print")
            return 0x0110, Dataset()   # processing failure

        # Clear the printed film boxes so a re-print in the same association is
        # clean, and hand back a Print Job reference (SCUs may N-GET its status).
        pj_uid = uid_gen()
        job.print_jobs.add(pj_uid)
        for f in films:
            job.film_boxes.pop(f.uid, None)
            for buid, fuid in list(job.box_to_film.items()):
                if fuid == f.uid:
                    job.box_to_film.pop(buid, None)
            for auid, fuid in list(job.anno_to_film.items()):
                if fuid == f.uid:
                    job.anno_to_film.pop(auid, None)
            if job.last_film == f.uid:
                job.last_film = ""
        reply = Dataset()
        pj = Dataset()
        pj.ReferencedSOPClassUID = PrintJob
        pj.ReferencedSOPInstanceUID = pj_uid
        reply.ReferencedPrintJobSequence = [pj]
        return 0x0000, reply

    def _handle_n_delete(self, event) -> int:
        req = event.request
        target = str(getattr(req, "RequestedSOPInstanceUID", "") or "")
        job = self._job(event)
        job.film_boxes.pop(target, None)
        for buid, fuid in list(job.box_to_film.items()):
            if fuid == target:
                job.box_to_film.pop(buid, None)
        return 0x0000

    def _handle_close(self, event) -> None:
        self._drop_job(event)

    # ---- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self.running:
            return
        ae = AE(ae_title=self.aet)
        ae.add_supported_context(BasicGrayscalePrintManagementMeta)
        if self.color:
            ae.add_supported_context(BasicColorPrintManagementMeta)
        # Annotation Box + Print Job are separate SOP classes some modalities
        # negotiate alongside the print meta (text overlays, job-status polling).
        ae.add_supported_context(BasicAnnotationBox)
        ae.add_supported_context(PrintJob)
        ae.add_supported_context(Verification)
        if self.allowed_aets:
            ae.require_calling_aet = list(self.allowed_aets)
        handlers = [
            (evt.EVT_C_ECHO, self._handle_echo),
            (evt.EVT_N_CREATE, self._handle_n_create),
            (evt.EVT_N_SET, self._handle_n_set),
            (evt.EVT_N_ACTION, self._handle_n_action),
            (evt.EVT_N_GET, self._handle_n_get),
            (evt.EVT_N_DELETE, self._handle_n_delete),
            (evt.EVT_RELEASED, self._handle_close),
            (evt.EVT_ABORTED, self._handle_close),
        ]
        ssl_context = None
        if self.tls:
            from .tlsutil import server_context
            ssl_context = server_context(self.tls_cert, self.tls_key, self.tls_ca)
        self._server = ae.start_server(
            (self.bind, self.port), block=False, evt_handlers=handlers, ssl_context=ssl_context
        )
        allow = ", ".join(self.allowed_aets) if self.allowed_aets else "any"
        proto = "DICOM-TLS" + (" (mutual)" if self.tls and self.tls_ca else "") if self.tls else "plain DICOM"
        mode = "grayscale+color" if self.color else "grayscale"
        out = "→ Secondary Capture" if self.layout == "image" else "→ PDF"
        self.log.info(
            f"Print receiver listening on {self.bind}:{self.port} as {self.aet} "
            f"[{proto}, {mode} {out}] (accept: {allow})",
            kind="print",
        )

    def stop(self) -> None:
        if not self.running:
            return
        try:
            self._server.shutdown()
        finally:
            self._server = None
            with self._lock:
                self._jobs.clear()
            self.log.info("Print receiver stopped", kind="print")


# ------------------------------------------------------------------ rendering
def _render_page(fb) -> "object":
    """Composite one film box's image boxes onto a single white page (PIL RGB),
    laid out per its Image Display Format, with any captured annotation text
    printed as a caption band at the foot of the page."""
    from PIL import Image

    landscape = str(fb.orientation).upper().startswith("LAND")
    pw, ph = (_PAGE_H, _PAGE_W) if landscape else (_PAGE_W, _PAGE_H)
    canvas = Image.new("RGB", (pw, ph), "white")

    lines = fb.annotation_lines()
    cap_h = min(len(lines) * 34 + 20, ph // 4) if lines else 0
    content_h = ph - cap_h            # reserve the foot for the caption band

    placed = sorted(fb.images.values(), key=lambda t: t[0])   # by ImageBoxPosition
    cells = fb.cells or [(0.0, 0.0, 1.0, 1.0)]
    for idx, (_pos, img) in enumerate(placed):
        cx, cy, cwf, chf = cells[idx] if idx < len(cells) else cells[-1]
        cell_w = int(pw * cwf) - 2 * _MARGIN
        cell_h = int(content_h * chf) - 2 * _MARGIN
        if cell_w <= 0 or cell_h <= 0:
            continue
        fitted = img.convert("RGB").copy()
        fitted.thumbnail((cell_w, cell_h), Image.LANCZOS)
        ox = int(pw * cx) + _MARGIN + (cell_w - fitted.width) // 2
        oy = int(content_h * cy) + _MARGIN + (cell_h - fitted.height) // 2
        canvas.paste(fitted, (ox, oy))

    if lines:
        _draw_caption(canvas, lines, content_h, pw, ph)
    return canvas


def _draw_caption(canvas, lines: list, top: int, pw: int, ph: int) -> None:
    """Draw annotation strings in the reserved foot band of the page."""
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=26)      # Pillow ≥10.1 sizes the builtin
    except TypeError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(canvas)
    draw.line([(_MARGIN, top + 4), (pw - _MARGIN, top + 4)], fill=(140, 140, 140), width=1)
    y = top + 12
    for line in lines:
        draw.text((_MARGIN, y), line, fill=(0, 0, 0), font=font)
        y += 34
        if y > ph - 20:
            break


def _render_pdf(films: list) -> bytes:
    """A (possibly multi-page) PDF — one page per film box — as bytes."""
    pages = [_render_page(fb) for fb in films]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", resolution=200.0, save_all=True, append_images=pages[1:])
    return buf.getvalue()


def _render_png(fb) -> bytes:
    """One film box as a PNG image (for the Secondary-Capture layout)."""
    buf = io.BytesIO()
    _render_page(fb).save(buf, format="PNG")
    return buf.getvalue()
