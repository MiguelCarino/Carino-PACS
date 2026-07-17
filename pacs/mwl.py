"""Modality Worklist SCP — serve RIS orders to modalities (the "order OUT" half).

When the RIS is down (emergency failover) or simply to feed a worklist, a
modality needs to *pull* its schedule.  In DICOM you cannot push an order onto a
modality — it queries a **Modality Worklist** (C-FIND) and pulls matching items.
This module is that worklist provider: it answers
``ModalityWorklistInformationFind`` C-FIND queries out of the shared
:class:`~pacs.ris.OrderStore` — every **open** order is one worklist item.

Shape mirrors ``scp.py`` / ``print_scp.py``: a non-blocking ``pynetdicom`` server
with the same TLS / allowed-AET / counter / start-stop surface.  The DIMSE verb
is **C-FIND** against the worklist information model rather than C-STORE.

Matching (lenient by design — the goal is *keep imaging flowing*, so we would
rather over-show an order than hide one a tech needs):

  * The SCU sends a query identifier with some keys filled (match keys) and the
    rest empty (return keys).  We honour the common ones: PatientID, PatientName,
    AccessionNumber, StudyInstanceUID (top level) and Modality,
    ScheduledStationAETitle, ScheduledProcedureStepStartDate (inside the
    Scheduled Procedure Step Sequence).
  * An empty query key matches everything (universal matching).  Wildcards
    ``*``/``?`` are supported.
  * An order that leaves a field blank (e.g. no target modality on a hand-keyed
    emergency order) matches *any* value for that field — so an untargeted order
    appears on every modality's worklist.  Set ``station_aet`` on the order to
    target one modality.

The response carries the order's pre-generated **Study Instance UID**, so the
exam the modality produces is stamped with the same UID and reconciles back to
the order exactly (see ``OrderStore.match`` / ``_reconcile_study``).
"""

from __future__ import annotations

import datetime
import fnmatch
import threading
from typing import Callable, Optional

from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    ModalityWorklistInformationFind,
    Verification,
)

from .logbuf import LogBuffer


def _peer_addr(event) -> str:
    try:
        addr = event.assoc.requestor.address
        return str(addr) if addr else "?"
    except Exception:
        return "?"


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _order_date(order: dict) -> str:
    """The order's scheduled date as DICOM DA (YYYYMMDD), or '' if it has none."""
    d = _digits(order.get("scheduled_dt"))
    return d[:8] if len(d) >= 8 else ""


def _order_time(order: dict) -> str:
    """The order's scheduled time as DICOM TM (HHMMSS), or '' if it has none."""
    d = _digits(order.get("scheduled_dt"))
    return d[8:14] if len(d) >= 10 else ""


def _today() -> str:
    return datetime.date.today().strftime("%Y%m%d")


# --------------------------------------------------------------------- matching
def _match_text(query_value: str, order_value: str, lenient_blank_order: bool = False) -> bool:
    """One text key. Empty query = universal match. ``*``/``?`` = wildcard.
    Otherwise case-insensitive equality. When *lenient_blank_order* and the order
    leaves the field blank, it matches any queried value."""
    q = str(query_value or "").strip()
    o = str(order_value or "").strip()
    if not q:
        return True                     # universal / return-key
    if lenient_blank_order and not o:
        return True                     # untargeted order shows for any value
    if any(c in q for c in "*?"):
        return fnmatch.fnmatch(o.upper(), q.upper())
    return o.upper() == q.upper()


def _match_date(query_value: str, order_date: str) -> bool:
    """DICOM date matching (exact or ``a-b`` / ``a-`` / ``-b`` range). Empty query
    matches all. An order with NO scheduled date matches any query (lenient — a
    hand-keyed emergency order must not be hidden by a date filter)."""
    q = str(query_value or "").strip()
    if not q:
        return True
    if not order_date:
        return True                     # dateless order: never hide it
    if "-" in q:
        lo, _, hi = q.partition("-")
        lo, hi = lo.strip(), hi.strip()
        if lo and order_date < lo:
            return False
        if hi and order_date > hi:
            return False
        return True
    return order_date == q


def _query_sps_item(ds):
    """First item of the query's Scheduled Procedure Step Sequence, or None."""
    seq = getattr(ds, "ScheduledProcedureStepSequence", None)
    try:
        return seq[0] if seq else None
    except (TypeError, IndexError):
        return None


def order_matches_query(order: dict, ds) -> bool:
    """True if *order* satisfies every match key present in the C-FIND query *ds*."""
    if not _match_text(getattr(ds, "PatientID", ""), order.get("patient_id")):
        return False
    if not _match_text(getattr(ds, "PatientName", ""), order.get("patient_name") or order.get("patient")):
        return False
    if not _match_text(getattr(ds, "AccessionNumber", ""), order.get("accession")):
        return False
    if not _match_text(getattr(ds, "StudyInstanceUID", ""), order.get("study_uid")):
        return False
    q = _query_sps_item(ds)
    if q is not None:
        # Modality / station may be blank on the order → lenient (show everywhere).
        if not _match_text(getattr(q, "Modality", ""), order.get("modality"), lenient_blank_order=True):
            return False
        if not _match_text(getattr(q, "ScheduledStationAETitle", ""), order.get("station_aet"), lenient_blank_order=True):
            return False
        if not _match_date(getattr(q, "ScheduledProcedureStepStartDate", ""), _order_date(order)):
            return False
    return True


# ---------------------------------------------------------------- response build
def build_worklist_item(order: dict):
    """A full MWL C-FIND response dataset for one order. We return the standard
    attribute set regardless of which return keys the SCU asked for — extra
    attributes are harmless and save us guessing the SCU's return-key list."""
    from pydicom.dataset import Dataset

    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 192"          # UTF-8 → accented names survive

    # Patient / study identification
    ds.PatientName = order.get("patient_name") or order.get("patient") or ""
    ds.PatientID = order.get("patient_id", "")
    ds.PatientBirthDate = (_digits(order.get("patient_birthdate"))[:8]
                           if _digits(order.get("patient_birthdate")) else "")
    ds.PatientSex = order.get("patient_sex", "")
    ds.StudyInstanceUID = order.get("study_uid", "")     # burned into the exam → exact reconcile
    ds.AccessionNumber = order.get("accession", "")
    ds.ReferringPhysicianName = order.get("referring", "")
    ds.RequestedProcedureID = order.get("procedure_id", "")
    ds.RequestedProcedureDescription = order.get("study_desc", "")

    # Scheduled Procedure Step Sequence — one step per order
    step = Dataset()
    step.Modality = order.get("modality", "")
    step.ScheduledStationAETitle = order.get("station_aet", "")
    step.ScheduledStationName = order.get("station_name", "")
    step.ScheduledProcedureStepStartDate = _order_date(order) or _today()
    step.ScheduledProcedureStepStartTime = _order_time(order)
    step.ScheduledProcedureStepDescription = order.get("study_desc", "")
    step.ScheduledProcedureStepID = order.get("sps_id", "") or order.get("procedure_id", "")
    step.ScheduledPerformingPhysicianName = ""
    ds.ScheduledProcedureStepSequence = [step]
    return ds


class MwlSCP:
    def __init__(
        self,
        aet: str,
        bind: str,
        port: int,
        log: LogBuffer,
        get_orders: Callable[[], list],
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
        # Callable returning the current OPEN orders (decouples us from OrderStore).
        self.get_orders = get_orders
        self.allowed_aets = [a for a in (allowed_aets or []) if str(a).strip()]
        self.tls = tls
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.tls_ca = tls_ca
        self._server = None
        self._lock = threading.Lock()
        self.query_count = 0
        self.match_count = 0
        self.error_count = 0

    # ---- DIMSE handlers ----------------------------------------------------
    def _handle_echo(self, event) -> int:
        who = event.assoc.requestor.ae_title
        self.log.info(f"C-ECHO from {who} @ {_peer_addr(event)}", kind="mwl")
        return 0x0000

    def _handle_find(self, event):
        """Yield one worklist item per matching open order. Generator protocol:
        each yield is (status, dataset); pynetdicom sends Success when we return."""
        who = event.assoc.requestor.ae_title
        try:
            query = event.identifier
        except Exception as exc:
            with self._lock:
                self.error_count += 1
            self.log.error(f"MWL: bad query from {who}: {exc}", kind="mwl")
            yield 0xC000, None           # Unable to process
            return
        with self._lock:
            self.query_count += 1
        try:
            orders = [o for o in (self.get_orders() or []) if o.get("status") == "open"]
        except Exception:
            orders = []
        matches = [o for o in orders if order_matches_query(o, query)]
        self.log.info(
            f"MWL query from {who} @ {_peer_addr(event)} → {len(matches)} "
            f"of {len(orders)} open order(s)", kind="mwl")
        n = 0
        for order in matches:
            if event.is_cancelled:
                yield 0xFE00, None       # Cancel
                return
            try:
                item = build_worklist_item(order)
            except Exception as exc:
                with self._lock:
                    self.error_count += 1
                self.log.error(f"MWL: could not build item for order "
                               f"{order.get('accession') or order.get('id')}: {exc}", kind="mwl")
                continue
            n += 1
            yield 0xFF00, item           # Pending — one match
        with self._lock:
            self.match_count += n
        # Generator return → pynetdicom sends 0x0000 Success.

    # ---- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self.running:
            return
        ae = AE(ae_title=self.aet)
        ae.add_supported_context(ModalityWorklistInformationFind)
        ae.add_supported_context(Verification)
        if self.allowed_aets:
            ae.require_calling_aet = list(self.allowed_aets)
        handlers = [
            (evt.EVT_C_FIND, self._handle_find),
            (evt.EVT_C_ECHO, self._handle_echo),
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
        self.log.info(
            f"Worklist (MWL) listening on {self.bind}:{self.port} as {self.aet} "
            f"[{proto}] (accept: {allow})",
            kind="mwl",
        )

    def stop(self) -> None:
        if not self.running:
            return
        try:
            self._server.shutdown()
        finally:
            self._server = None
            self.log.info("Worklist (MWL) stopped", kind="mwl")
