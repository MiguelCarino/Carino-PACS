"""Emergency RIS — order intake (HL7 v2 over MLLP + manual entry) and study
reconciliation.

Purpose is two-fold:

  * **Testing** — a lightweight endpoint that receives HL7 ``ORM^O01`` order
    messages (over MLLP) so you can exercise a RIS→PACS feed without a real RIS,
    and lets you hand-key orders in from the dashboard.
  * **Emergency fallback** — when the real RIS (or its worklist feed to the
    modality) is down, orders still land here.  A technician reads the order,
    types the demographics into the modality **by hand**, and — critically —
    keys the order's short **Accession Number** into the modality so the study
    that comes back can be matched to the order.

Flow (see the module design notes):

  1. Order arrives — via ``ORM^O01`` on the MLLP listener, or the manual "New
     order" form.  It lands in the :class:`OrderStore` with status ``open``.
  2. The dashboard displays open orders + the accession the tech must type in.
  3. When the study is C-STORE'd to the receiver, :meth:`OrderStore.match`
     looks for an open order by Accession Number (primary) or Patient ID
     (fallback).  On a hit the order is **closed and archived** — never erased,
     so there's an audit trail — and the match is logged.
  4. Image delivery is **never gated** on a match: the study is stored (and
     forwarded by the existing pipeline) regardless.  A no-match study simply
     leaves its order open, flagged for manual reconciliation.

HL7 note: parsing is a deliberately small, dependency-free pipe-delimited
reader (MSH / PID / ORC / OBR) — enough for order intake and a conformant ACK.
It is intentionally lenient (real-world ORMs vary wildly); swap in ``hl7apy`` /
``python-hl7`` later if you need full segment validation.  Framing is MLLP:
each message is wrapped ``<VT> … <FS><CR>`` (0x0B … 0x1C 0x0D) on a plain TCP
stream.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from typing import Callable, Optional

from .logbuf import LogBuffer

# ---- MLLP framing bytes (Minimal Lower Layer Protocol) --------------------
VT = 0x0B          # <SB> start-block, precedes the message
FS = 0x1C          # <EB> end-block, follows the message
CR = 0x0D          # carriage return, terminates the frame

# Fields we lift out of an HL7 order and keep on an order record.  Names match
# the identity dict used by the ingest/pending pipeline so a matched order can
# later coerce a study's tags with no translation layer.  The scheduling fields
# (station_aet … study_uid) feed the Modality Worklist and make study↔order
# matching exact — see docs/ris-emergency-design.md.
ORDER_FIELDS = (
    "accession", "patient_id", "patient", "patient_name",
    "patient_birthdate", "patient_sex",
    "study_desc", "modality", "scheduled_dt", "referring", "priority",
    "station_aet", "station_name", "sps_id", "procedure_id", "study_uid",
)


# --------------------------------------------------------------------- HL7
class HL7Message:
    """A parsed HL7 v2 message — segments split on their field separator.

    Deliberately minimal: it does not model repetitions/components beyond what
    order intake needs (``field(seg, n)`` returns the raw field; ``component``
    splits on ``^``).  Encoding characters from MSH-2 are honoured for the
    common ``^~\\&`` default.
    """

    def __init__(self, raw: str):
        self.raw = raw
        # HL7 segments are separated by <CR>; tolerate <LF>/<CRLF> too.
        self.segments: list[list[str]] = []
        self.field_sep = "|"
        self.comp_sep = "^"
        self.rep_sep = "~"
        for line in raw.replace("\r\n", "\r").replace("\n", "\r").split("\r"):
            if not line:
                continue
            if line[:3] == "MSH":
                # MSH is special: MSH-1 *is* the field separator character.
                self.field_sep = line[3]
                enc = line[4:line.find(self.field_sep, 4)] if self.field_sep in line[4:] else line[4:8]
                if len(enc) >= 1:
                    self.comp_sep = enc[0]
                if len(enc) >= 2:
                    self.rep_sep = enc[1]
                fields = line.split(self.field_sep)
                # Re-insert the separator as MSH-1 so field(2)=encoding chars,
                # field(9)=message type, etc. line up with the HL7 numbering.
                self.segments.append(["MSH", self.field_sep] + fields[1:])
            else:
                self.segments.append(line.split(self.field_sep))

    def seg(self, name: str) -> Optional[list[str]]:
        for s in self.segments:
            if s and s[0] == name:
                return s
        return None

    def field(self, seg_name: str, index: int) -> str:
        """HL7 field by 1-based position (MSH-9, PID-3, …); '' if absent."""
        s = self.seg(seg_name)
        if not s or index >= len(s):
            return ""
        return s[index].strip()

    def component(self, seg_name: str, index: int, comp: int = 1) -> str:
        """One ``^``-component of a field (1-based comp); '' if absent."""
        raw = self.field(seg_name, index)
        if not raw:
            return ""
        parts = raw.split(self.comp_sep)
        return parts[comp - 1].strip() if comp - 1 < len(parts) else ""

    @property
    def message_type(self) -> str:
        """MSH-9 message type, e.g. 'ORM^O01' (dropping the message-structure)."""
        return self.field("MSH", 9)

    @property
    def control_id(self) -> str:
        return self.field("MSH", 10)


def _fmt_hl7_name(raw_name: str, comp_sep: str = "^") -> str:
    """XPN 'Last^First^Middle' → 'First Middle Last' for display."""
    if not raw_name:
        return ""
    parts = [p.strip() for p in raw_name.split(comp_sep)]
    last = parts[0] if parts else ""
    first = parts[1] if len(parts) > 1 else ""
    middle = parts[2] if len(parts) > 2 else ""
    return " ".join(p for p in (first, middle, last) if p)


def parse_order(msg: HL7Message) -> dict:
    """Lift the order fields we care about out of a parsed HL7 message.

    Reads PID (patient), ORC/OBR (order + accession + procedure).  Every field
    is best-effort — a sparse ORM still yields a usable order the operator can
    complete by hand.
    """
    accession = msg.field("OBR", 3) or msg.field("ORC", 3) or msg.field("OBR", 2) or msg.field("ORC", 2)
    order = {
        "accession": accession.split(msg.comp_sep)[0].strip(),
        "patient_id": msg.component("PID", 3, 1),
        "patient_name": msg.field("PID", 5),
        "patient": _fmt_hl7_name(msg.field("PID", 5), msg.comp_sep),
        "patient_birthdate": msg.field("PID", 7),  # DICOM DA (YYYYMMDD)
        "patient_sex": msg.field("PID", 8),        # M | F | O
        "study_desc": msg.component("OBR", 4, 2) or msg.component("OBR", 4, 1),
        "modality": msg.field("OBR", 24),          # Diagnostic Serv Sect ID (US, CT, MR…)
        "scheduled_dt": msg.field("OBR", 7) or msg.field("ORC", 9),
        "referring": _fmt_hl7_name(msg.field("OBR", 16) or msg.field("ORC", 12), msg.comp_sep),
        "priority": msg.component("OBR", 27, 6) or msg.field("ORC", 7),
        # Placer/filler order numbers double as procedure/step ids for MWL.
        "procedure_id": msg.field("OBR", 2) or msg.field("ORC", 2),
        "sps_id": msg.field("OBR", 2) or msg.field("ORC", 2),
        # station_aet / station_name / study_uid: not carried by ORM — the
        # operator picks the target modality; study_uid is generated on add().
    }
    return order


def build_ack(msg: HL7Message, code: str = "AA", text: str = "") -> str:
    """A minimal HL7 ``ACK`` for a received message.

    AA = Application Accept.  Echoes MSH-10 as the MSA control id.  Sender/
    receiver app+facility are swapped so the ACK routes back sensibly.
    """
    sending_app = msg.field("MSH", 3)
    sending_fac = msg.field("MSH", 4)
    recv_app = msg.field("MSH", 5) or "CARINORIS"
    recv_fac = msg.field("MSH", 6)
    ctrl = msg.control_id or uuid.uuid4().hex[:12]
    fs = "|"
    # Swap sender/receiver: our app answers back to their app.
    msh = fs.join(["MSH", "^~\\&", recv_app, recv_fac, sending_app, sending_fac,
                   "", "", "ACK", ctrl, "P", "2.3"])
    msa = fs.join(["MSA", code, ctrl] + ([text] if text else []))
    return msh + "\r" + msa + "\r"


# --------------------------------------------------------------- order store
class OrderStore:
    """Thread-safe, JSON-file-backed list of RIS orders.

    Persisted to ``<store_dir>/orders.json`` so orders survive a restart (an
    emergency RIS that forgets its orders on a crash is worse than useless).
    Orders are dicts with the :data:`ORDER_FIELDS` plus ``id``/``status``/
    ``source``/``created``/``closed``/``matched_study``.

    ``status`` is ``open`` (awaiting a study) or ``closed`` (matched/cancelled).
    Closed orders are **kept** for the audit trail; :meth:`purge_closed` prunes
    them on demand.
    """

    def __init__(self, store_dir: str, log: Optional[LogBuffer] = None,
                 match_on: str = "accession", now: Callable[[], str] | None = None):
        self.store_dir = store_dir
        self.log = log
        # "accession" (exact accession only) | "accession_or_patient" (fall back
        # to Patient ID when the study carries no accession the tech typed in).
        self.match_on = match_on if match_on in ("accession", "accession_or_patient") else "accession"
        self._now = now or _utc_stamp
        self._lock = threading.Lock()
        self._orders: dict[str, dict] = {}
        self._load()

    # ---- persistence -------------------------------------------------------
    @property
    def _path(self) -> str:
        return os.path.join(self.store_dir, "orders.json")

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for o in data.get("orders", []):
                if o.get("id"):
                    self._orders[o["id"]] = o
        except (OSError, ValueError):
            pass

    def _save_locked(self) -> None:
        os.makedirs(self.store_dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"orders": list(self._orders.values())}, fh, indent=2)
        os.replace(tmp, self._path)

    # ---- CRUD --------------------------------------------------------------
    def add(self, fields: dict, source: str = "manual") -> dict:
        """Create an order from a partial field dict; returns the stored order."""
        oid = uuid.uuid4().hex[:12]
        order = {k: str(fields.get(k, "") or "").strip() for k in ORDER_FIELDS}
        if not order.get("patient") and order.get("patient_name"):
            order["patient"] = _fmt_hl7_name(order["patient_name"])
        # Generate the Study Instance UID up front: the modality burns THIS UID
        # into the exam (via MWL) and a wrapped capture inherits it, so study↔
        # order matching later is exact instead of fuzzy.
        if not order.get("study_uid"):
            order["study_uid"] = _gen_uid()
        order.update({
            "id": oid,
            "status": "open",
            "source": source,
            "created": self._now(),
            "closed": "",
            "matched_study": "",
        })
        with self._lock:
            self._orders[oid] = order
            self._save_locked()
        if self.log:
            self.log.info(
                f"RIS order queued: {order.get('patient') or '?'} "
                f"[acc {order.get('accession') or '—'}] {order.get('study_desc') or ''} "
                f"(via {source})",
                kind="ris",
            )
        return order

    def add_from_hl7(self, msg: HL7Message, source: str) -> dict:
        return self.add(parse_order(msg), source=source)

    def list(self, status: Optional[str] = None) -> list[dict]:
        with self._lock:
            out = list(self._orders.values())
        if status:
            out = [o for o in out if o.get("status") == status]
        # Newest first.
        return sorted(out, key=lambda o: o.get("created", ""), reverse=True)

    def get(self, oid: str) -> Optional[dict]:
        with self._lock:
            return self._orders.get(oid)

    def update(self, oid: str, fields: dict) -> Optional[dict]:
        with self._lock:
            o = self._orders.get(oid)
            if not o:
                return None
            for k in ORDER_FIELDS:
                if k in fields:
                    o[k] = str(fields.get(k, "") or "").strip()
            self._save_locked()
            return dict(o)

    def close(self, oid: str, reason: str = "cancelled", matched_study: str = "") -> Optional[dict]:
        """Mark an order closed (matched or cancelled). Keeps it for the audit
        trail — use :meth:`delete`/`purge_closed` to actually remove it."""
        with self._lock:
            o = self._orders.get(oid)
            if not o:
                return None
            o["status"] = "closed"
            o["closed"] = self._now()
            o["close_reason"] = reason
            if matched_study:
                o["matched_study"] = matched_study
            self._save_locked()
            return dict(o)

    def delete(self, oid: str) -> bool:
        with self._lock:
            existed = self._orders.pop(oid, None) is not None
            if existed:
                self._save_locked()
            return existed

    def purge_closed(self) -> int:
        with self._lock:
            closed = [oid for oid, o in self._orders.items() if o.get("status") == "closed"]
            for oid in closed:
                del self._orders[oid]
            if closed:
                self._save_locked()
        return len(closed)

    # ---- matching ----------------------------------------------------------
    def match(self, accession: str = "", patient_id: str = "", study_uid: str = "") -> Optional[dict]:
        """Find an OPEN order for an incoming study, strongest key first:

          1. **Study Instance UID** — exact, since we generate it on the order
             and the exam (via MWL) or a wrapped capture carries it. Always used.
          2. **Accession Number** — the value the tech typed into the modality.
          3. **Patient ID** — opt-in fallback (``match_on='accession_or_patient'``).

        Returns a copy of the matched order or None. Does not close it — the
        caller decides (so a dry 'would this match?' check is possible)."""
        su = _norm(study_uid)
        acc = _norm(accession)
        pid = _norm(patient_id)
        with self._lock:
            opens = [o for o in self._orders.values() if o.get("status") == "open"]
        if su:
            for o in opens:
                if _norm(o.get("study_uid")) == su:
                    return dict(o)
        if acc:
            for o in opens:
                if _norm(o.get("accession")) == acc:
                    return dict(o)
        if pid and self.match_on == "accession_or_patient":
            for o in opens:
                if _norm(o.get("patient_id")) == pid:
                    return dict(o)
        return None

    def counts(self) -> dict:
        with self._lock:
            vals = list(self._orders.values())
        return {
            "open": sum(1 for o in vals if o.get("status") == "open"),
            "closed": sum(1 for o in vals if o.get("status") == "closed"),
            "total": len(vals),
        }


def _gen_uid() -> str:
    """A valid DICOM Study Instance UID. Lazy pydicom import keeps this module's
    top level dependency-free (HL7/MLLP needs no DICOM stack)."""
    from pydicom.uid import generate_uid
    return str(generate_uid())


def _norm(v) -> str:
    """Case/whitespace-insensitive key for matching accession / patient id."""
    return str(v or "").strip().upper()


def _utc_stamp() -> str:
    """ISO-ish UTC timestamp. Isolated so it's trivial to stub in tests."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------- MLLP listener
class RisListener:
    """A non-blocking MLLP server that receives HL7 order messages and files
    them into an :class:`OrderStore`.

    Shape mirrors :class:`~pacs.print_scp.PrintSCP` / ``StorageSCP``: a
    background-threaded socket server with the same start/stop, counter and
    ``allowed_hosts`` surface.  Each accepted message is ACK'd on the same
    connection (MLLP is request/response over a persistent stream).
    """

    def __init__(
        self,
        bind: str,
        port: int,
        store: OrderStore,
        log: LogBuffer,
        allowed_hosts: Optional[list[str]] = None,
        accept_types: Optional[list[str]] = None,
    ):
        self.bind = bind
        self.port = port
        self.store = store
        self.log = log
        self.allowed_hosts = [h for h in (allowed_hosts or []) if str(h).strip()]
        # Message types we file as orders; others are ACK'd but ignored.
        self.accept_types = [t.upper() for t in (accept_types or ["ORM", "OMG", "OMI"])]
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.received_count = 0
        self.order_count = 0
        self.error_count = 0

    @property
    def running(self) -> bool:
        return self._sock is not None

    def start(self) -> None:
        if self.running:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(8)
        srv.settimeout(0.5)
        self._sock = srv
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="pacs-ris", daemon=True)
        self._thread.start()
        allow = ", ".join(self.allowed_hosts) if self.allowed_hosts else "any host"
        self.log.info(
            f"RIS listener on {self.bind}:{self.port} [HL7/MLLP] "
            f"(accept {', '.join(self.accept_types)} from {allow})",
            kind="ris",
        )

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer = addr[0] if addr else "?"
            if self.allowed_hosts and peer not in self.allowed_hosts:
                self.log.warn(f"RIS: refused connection from {peer} (not allowed)", kind="ris")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle_conn, args=(conn, peer),
                             name="pacs-ris-conn", daemon=True).start()

    def _handle_conn(self, conn: socket.socket, peer: str) -> None:
        """Read MLLP-framed messages off one connection, ACK each. A sender may
        pipeline several messages on the same stream, so we loop until close."""
        conn.settimeout(30)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                # Drain every complete <VT>…<FS><CR> frame currently in buffer.
                while True:
                    start = buf.find(VT)
                    if start == -1:
                        buf.clear()
                        break
                    end = buf.find(bytes([FS, CR]), start + 1)
                    if end == -1:
                        # Incomplete frame — keep the tail, wait for more bytes.
                        if start > 0:
                            del buf[:start]
                        break
                    payload = bytes(buf[start + 1:end])
                    del buf[:end + 2]
                    ack = self._process(payload, peer)
                    conn.sendall(bytes([VT]) + ack.encode("utf-8") + bytes([FS, CR]))
        except OSError as exc:
            self.log.warn(f"RIS: connection from {peer} dropped: {exc}", kind="ris")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _process(self, payload: bytes, peer: str) -> str:
        """Parse one HL7 message, file it if it's an order, return an ACK string."""
        with self._lock:
            self.received_count += 1
        try:
            raw = payload.decode("utf-8", errors="replace")
            msg = HL7Message(raw)
        except Exception as exc:
            with self._lock:
                self.error_count += 1
            self.log.error(f"RIS: could not parse HL7 from {peer}: {exc}", kind="ris")
            # Best-effort AR (Application Reject) with an empty control id.
            return "MSH|^~\\&|CARINORIS|||||ACK|1|P|2.3\rMSA|AR|1|parse error\r"

        mtype = (msg.message_type.split("^")[0] or "").upper()
        if mtype in self.accept_types:
            try:
                order = self.store.add_from_hl7(msg, source=f"HL7 {peer}")
                with self._lock:
                    self.order_count += 1
                return build_ack(msg, "AA")
            except Exception as exc:
                with self._lock:
                    self.error_count += 1
                self.log.error(f"RIS: failed to store order from {peer}: {exc}", kind="ris")
                return build_ack(msg, "AE", "could not store order")
        # Not an order we handle — acknowledge so the sender isn't left hanging.
        self.log.info(f"RIS: received {mtype or 'unknown'} from {peer} (not an order — ignored)", kind="ris")
        return build_ack(msg, "AA")

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        try:
            self._sock.close()
        finally:
            self._sock = None
            self.log.info("RIS listener stopped", kind="ris")
