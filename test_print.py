"""End-to-end tests for the virtual DICOM Print SCP.

Drives real pynetdicom Print SCUs against a live PrintSCP and checks the
captured film lands in the pending queue and approves into DICOM. Covers:

  1. grayscale + PDF layout (2-up film)         -> Encapsulated PDF (DOC)
  2. grayscale + image layout                   -> Secondary Capture (OT)
  3. colour print (colour meta + colour boxes)  -> captured PDF
  4. identity scraping (modality sends Patient/Study on the film session)

Run:  ./.venv/bin/python test_print.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE
from pynetdicom.sop_class import (
    BasicAnnotationBox,
    BasicColorImageBox,
    BasicColorPrintManagementMeta,
    BasicFilmBox,
    BasicFilmSession,
    BasicGrayscaleImageBox,
    BasicGrayscalePrintManagementMeta,
    PrintJob,
    Printer,
    PrinterInstance,
)

from pacs.config import Config
from pacs.server import PacsServer

_PORT = 11210
SCU_AET = "MODALITY01"


def _next_port() -> int:
    global _PORT
    _PORT += 1
    return _PORT


def _gray_item(value: int, rows: int = 24, cols: int = 24) -> Dataset:
    it = Dataset()
    it.SamplesPerPixel = 1
    it.PhotometricInterpretation = "MONOCHROME2"
    it.Rows, it.Columns = rows, cols
    it.BitsAllocated = it.BitsStored = 8
    it.HighBit = 7
    it.PixelRepresentation = 0
    it.PixelData = bytes([value]) * (rows * cols)
    return it


def _color_item(rgb: tuple, rows: int = 16, cols: int = 16) -> Dataset:
    it = Dataset()
    it.SamplesPerPixel = 3
    it.PhotometricInterpretation = "RGB"
    it.PlanarConfiguration = 0
    it.Rows, it.Columns = rows, cols
    it.BitsAllocated = it.BitsStored = 8
    it.HighBit = 7
    it.PixelRepresentation = 0
    it.PixelData = bytes(rgb) * (rows * cols)
    return it


def _start_server(**print_cfg):
    tmp = tempfile.mkdtemp(prefix="carinoprint-test-")
    cfg = Config(os.path.join(tmp, "config.json"))
    cfg.printer.update({"enabled": True, "port": _next_port(), **print_cfg})
    cfg.save()
    server = PacsServer(cfg)
    server.start_printer()
    time.sleep(0.4)
    assert server.print_scp and server.print_scp.running, "print SCP did not start"
    return server, cfg


def _drive(port, meta, box_cls, items, fmt="STANDARD\\1,1", session_attrs=None):
    """Run one full print conversation; returns the Film Box N-CREATE reply."""
    ae = AE(ae_title=SCU_AET)
    ae.add_requested_context(meta)
    assoc = ae.associate("127.0.0.1", port)
    assert assoc.is_established, "SCU could not associate"
    su, fu = generate_uid(), generate_uid()

    fs = Dataset()
    fs.FilmSessionLabel = "STUDY FILM"
    for k, v in (session_attrs or {}).items():
        setattr(fs, k, v)
    st, _ = assoc.send_n_create(fs, BasicFilmSession, su, meta_uid=meta)
    assert st.Status == 0x0000, "film session create failed"

    fb = Dataset()
    fb.ImageDisplayFormat = fmt
    st, created = assoc.send_n_create(fb, BasicFilmBox, fu, meta_uid=meta)
    assert st.Status == 0x0000, "film box create failed"
    boxes = created.ReferencedImageBoxSequence
    assert len(boxes) == len(items), f"expected {len(items)} boxes, got {len(boxes)}"

    seq_kw = "BasicColorImageSequence" if box_cls == BasicColorImageBox else "BasicGrayscaleImageSequence"
    for pos, (ib, item) in enumerate(zip(boxes, items), start=1):
        m = Dataset()
        m.ImageBoxPosition = pos
        setattr(m, seq_kw, [item])
        st, _ = assoc.send_n_set(m, box_cls, ib.ReferencedSOPInstanceUID, meta_uid=meta)
        assert st.Status == 0x0000, f"image box {pos} set failed"

    st, _ = assoc.send_n_action(None, 1, BasicFilmBox, fu, meta_uid=meta)
    assert st.Status == 0x0000, f"print action failed ({st.Status:#06x})"
    assoc.release()
    time.sleep(0.3)
    return created


def _pending(cfg):
    pdir = cfg.resolved("scu", "pending_dir")
    out = []
    for d in sorted(os.listdir(pdir)):
        entry = os.path.join(pdir, d)
        if os.path.isdir(entry) and os.path.isfile(os.path.join(entry, "meta.json")):
            meta = json.load(open(os.path.join(entry, "meta.json")))
            meta["_dir"] = entry
            out.append(meta)
    return out


# ------------------------------------------------------------------- scenarios
def test_grayscale_pdf():
    server, cfg = _start_server(layout="pdf")
    try:
        _drive(cfg.printer["port"], BasicGrayscalePrintManagementMeta,
               BasicGrayscaleImageBox, [_gray_item(80), _gray_item(160)], fmt="STANDARD\\1,2")
        items = _pending(cfg)
        assert len(items) == 1 and items[0]["kind"] == "pdf", "expected one PDF pending item"
        assert SCU_AET in items[0]["source"], "source missing SCU AE"
        head = open(os.path.join(items[0]["_dir"], items[0]["filename"]), "rb").read(5)
        assert head == b"%PDF-", "not a PDF"
        res = server.approve_pending(items[0]["id"], {"patient": "DOE^JANE", "patient_id": "P1"})
        assert res.get("ok"), res
        dcm = [f for f in os.listdir(cfg.resolved("scu", "watch_dir")) if f.endswith(".dcm")][0]
        ds = pydicom.dcmread(os.path.join(cfg.resolved("scu", "watch_dir"), dcm))
        assert ds.Modality == "DOC" and ds.PatientID == "P1"
        print("  [1] grayscale+PDF: 2-up film -> Encapsulated PDF (DOC) OK")
    finally:
        server.shutdown()


def test_grayscale_image():
    server, cfg = _start_server(layout="image")
    try:
        _drive(cfg.printer["port"], BasicGrayscalePrintManagementMeta,
               BasicGrayscaleImageBox, [_gray_item(120)])
        items = _pending(cfg)
        assert len(items) == 1 and items[0]["kind"] == "image", "expected one image pending item"
        assert items[0]["filename"].endswith(".png"), "expected a .png"
        res = server.approve_pending(items[0]["id"], {"patient_id": "P2"})
        assert res.get("ok"), res
        dcm = [f for f in os.listdir(cfg.resolved("scu", "watch_dir")) if f.endswith(".dcm")][0]
        ds = pydicom.dcmread(os.path.join(cfg.resolved("scu", "watch_dir"), dcm))
        assert ds.Modality == "OT" and int(ds.Rows) > 0 and int(ds.Columns) > 0
        print("  [2] grayscale+image: film -> Secondary Capture (OT) OK")
    finally:
        server.shutdown()


def test_color_pdf():
    server, cfg = _start_server(layout="pdf", color=True)
    try:
        created = _drive(cfg.printer["port"], BasicColorPrintManagementMeta,
                         BasicColorImageBox, [_color_item((220, 40, 40))])
        assert str(created.ReferencedImageBoxSequence[0].ReferencedSOPClassUID) == BasicColorImageBox, \
            "server did not hand back colour image box class on the colour meta"
        items = _pending(cfg)
        assert len(items) == 1 and items[0]["kind"] == "pdf", "colour print did not queue a PDF"
        head = open(os.path.join(items[0]["_dir"], items[0]["filename"]), "rb").read(5)
        assert head == b"%PDF-", "colour render is not a PDF"
        print("  [3] color print: colour meta + colour boxes -> captured PDF OK")
    finally:
        server.shutdown()


def test_identity_scrape():
    server, cfg = _start_server(layout="pdf")
    try:
        su_study = generate_uid()
        _drive(cfg.printer["port"], BasicGrayscalePrintManagementMeta,
               BasicGrayscaleImageBox, [_gray_item(90)],
               session_attrs={
                   "PatientName": "SMITH^JOHN",
                   "PatientID": "MRN-42",
                   "StudyInstanceUID": su_study,
                   "StudyDescription": "PORTABLE CHEST",
                   "AccessionNumber": "ACC-7",
               })
        meta = _pending(cfg)[0]
        assert meta["patient_id"] == "MRN-42", meta
        assert meta["patient"] == "JOHN SMITH", meta
        assert meta["study_uid"] == su_study, meta
        assert meta["study_desc"] == "PORTABLE CHEST", meta
        assert meta["accession"] == "ACC-7", meta
        print("  [4] identity scrape: Patient/Study pre-filled from print attrs OK")
    finally:
        server.shutdown()


def test_annotation_and_printjob():
    server, cfg = _start_server(layout="pdf")
    try:
        port = cfg.printer["port"]
        ae = AE(ae_title=SCU_AET)
        ae.add_requested_context(BasicGrayscalePrintManagementMeta)
        ae.add_requested_context(BasicAnnotationBox)
        ae.add_requested_context(PrintJob)
        a = ae.associate("127.0.0.1", port)
        assert a.is_established, "SCU could not associate (annotation/printjob contexts)"
        meta = BasicGrayscalePrintManagementMeta
        su, fu = generate_uid(), generate_uid()

        fs = Dataset()          # non-empty but no FilmSessionLabel → annotation drives study_desc
        fs.NumberOfCopies = "1"
        fs.PrintPriority = "MED"
        st, _ = a.send_n_create(fs, BasicFilmSession, su, meta_uid=meta)
        assert st.Status == 0x0000

        fb = Dataset()
        fb.ImageDisplayFormat = "STANDARD\\1,1"
        fb.AnnotationDisplayFormatID = "PATIENTINFO"
        st, created = a.send_n_create(fb, BasicFilmBox, fu, meta_uid=meta)
        assert st.Status == 0x0000
        assert "ReferencedBasicAnnotationBoxSequence" in created, "no annotation boxes handed back"
        annos = created.ReferencedBasicAnnotationBoxSequence
        assert len(annos) == 6, f"expected annotation pool of 6, got {len(annos)}"

        # image box
        ib = created.ReferencedImageBoxSequence[0]
        m = Dataset(); m.ImageBoxPosition = 1
        m.BasicGrayscaleImageSequence = [_gray_item(100)]
        st, _ = a.send_n_set(m, BasicGrayscaleImageBox, ib.ReferencedSOPInstanceUID, meta_uid=meta)
        assert st.Status == 0x0000

        # two annotation strings (on their own SOP class context)
        for pos, text in ((1, "SMITH^JOHN  MRN-42"), (2, "PORTABLE CHEST")):
            am = Dataset(); am.AnnotationPosition = pos; am.TextString = text
            st, _ = a.send_n_set(am, BasicAnnotationBox, annos[pos - 1].ReferencedSOPInstanceUID)
            assert st.Status == 0x0000, f"annotation {pos} set failed"

        # print → returns a Print Job reference
        st, reply = a.send_n_action(None, 1, BasicFilmBox, fu, meta_uid=meta)
        assert st.Status == 0x0000
        assert reply is not None and "ReferencedPrintJobSequence" in reply, "no print job handed back"
        pj_uid = reply.ReferencedPrintJobSequence[0].ReferencedSOPInstanceUID

        # poll the print job status
        st, info = a.send_n_get([0x21000020], PrintJob, pj_uid)
        assert st.Status == 0x0000 and info.ExecutionStatus == "DONE", "print job not DONE"
        a.release(); time.sleep(0.3)

        item = _pending(cfg)[0]
        assert item["study_desc"] == "SMITH^JOHN  MRN-42", \
            f"annotation not used as study desc: {item['study_desc']!r}"
        print("  [5] annotation box + print job: text captured, job reports DONE OK")
    finally:
        server.shutdown()


def test_bitdepth_and_robustness():
    from pacs.print_scp import _image_from_item
    # 12-bit stored in 16 bits: a max-value pixel must render near white, not ~15.
    it = Dataset()
    it.SamplesPerPixel = 1; it.PhotometricInterpretation = "MONOCHROME2"
    it.Rows = it.Columns = 2; it.BitsAllocated = 16; it.BitsStored = 12; it.HighBit = 11
    it.PixelRepresentation = 0
    it.PixelData = (0x0FFF).to_bytes(2, "little") * 4    # all pixels = 4095
    im = _image_from_item(it)
    px = im.convert("L").getpixel((0, 0))
    assert px >= 240, f"12-bit max pixel rendered too dark ({px}); scaling broken"

    # Truncated pixel data must not crash — it pads and still returns an image.
    it2 = Dataset()
    it2.SamplesPerPixel = 1; it2.PhotometricInterpretation = "MONOCHROME2"
    it2.Rows = it2.Columns = 4; it2.BitsAllocated = 8; it2.BitsStored = 8; it2.HighBit = 7
    it2.PixelRepresentation = 0; it2.PixelData = b"\xff\xff"   # far too short (need 16)
    assert _image_from_item(it2) is not None, "short pixel data should not fail decode"
    print("  [6] bit-depth + robustness: 12-bit scales bright, short pixels survive OK")


def test_empty_job_survives():
    # An N-ACTION with no image data returns a warning but must NOT abort — the
    # same association can then print a real film.
    server, cfg = _start_server(layout="pdf")
    try:
        port = cfg.printer["port"]
        ae = AE(ae_title=SCU_AET)
        ae.add_requested_context(BasicGrayscalePrintManagementMeta)
        a = ae.associate("127.0.0.1", port)
        meta = BasicGrayscalePrintManagementMeta
        su, fu = generate_uid(), generate_uid()
        fs = Dataset(); fs.NumberOfCopies = "1"
        a.send_n_create(fs, BasicFilmSession, su, meta_uid=meta)
        fb = Dataset(); fb.ImageDisplayFormat = "STANDARD\\1,1"
        a.send_n_create(fb, BasicFilmBox, fu, meta_uid=meta)
        st, _ = a.send_n_action(None, 1, BasicFilmBox, fu, meta_uid=meta)   # no image set
        assert st.Status == 0xB603, f"empty print should warn, got {st.Status:#06x}"
        assert a.is_established, "association aborted on an empty print"
        # now a real film on the SAME association
        fu2 = generate_uid()
        fb2 = Dataset(); fb2.ImageDisplayFormat = "STANDARD\\1,1"
        st, created = a.send_n_create(fb2, BasicFilmBox, fu2, meta_uid=meta)
        m = Dataset(); m.ImageBoxPosition = 1; m.BasicGrayscaleImageSequence = [_gray_item(70)]
        a.send_n_set(m, BasicGrayscaleImageBox, created.ReferencedImageBoxSequence[0].ReferencedSOPInstanceUID, meta_uid=meta)
        st, _ = a.send_n_action(None, 1, BasicFilmBox, fu2, meta_uid=meta)
        assert st.Status == 0x0000, "real film after empty print failed"
        a.release(); time.sleep(0.3)
        assert len(_pending(cfg)) == 1, "expected exactly one captured film"
        print("  [7] empty print warns without aborting; association still usable OK")
    finally:
        server.shutdown()


def main() -> int:
    tests = [test_grayscale_pdf, test_grayscale_image, test_color_pdf, test_identity_scrape,
             test_annotation_and_printjob, test_bitdepth_and_robustness, test_empty_job_survives]
    try:
        for t in tests:
            t()
    except AssertionError as exc:
        print(f"\nFAIL — {exc}", file=sys.stderr)
        return 1
    print("\nPASS — all print SCP scenarios (grayscale/color, PDF/image, identity) green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
