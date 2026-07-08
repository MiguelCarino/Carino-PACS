"""Storage SCP — the "receive" half of the PACS.

Listens for associations and handles C-STORE (store the incoming instance to
disk) and C-ECHO (verification ping).  Every storage SOP class is accepted
with every transfer syntax pynetdicom knows, so compressed objects (JPEG,
JPEG-LS, JPEG2000, RLE) are stored as-received without transcoding.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable, Optional

from pynetdicom import AE, evt, AllStoragePresentationContexts, ALL_TRANSFER_SYNTAXES
from pynetdicom.sop_class import Verification

from .logbuf import LogBuffer

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(component: str, fallback: str) -> str:
    """Turn a DICOM UID/ID into a safe single path component."""
    s = _UNSAFE.sub("_", str(component or "").strip())
    s = s.strip("._") or fallback
    return s[:120]


def _peer_addr(event) -> str:
    """Remote peer's IP address for an association event, or '?' if unavailable."""
    try:
        addr = event.assoc.requestor.address
        return str(addr) if addr else "?"
    except Exception:
        return "?"


def dest_path(base: str, ds, organize: bool) -> str:
    sop = _safe(getattr(ds, "SOPInstanceUID", ""), "unknown")
    if not organize:
        return os.path.join(base, sop + ".dcm")
    patient = _safe(getattr(ds, "PatientID", ""), "NOID")
    study = _safe(getattr(ds, "StudyInstanceUID", ""), "NOSTUDY")
    series = _safe(getattr(ds, "SeriesInstanceUID", ""), "NOSERIES")
    return os.path.join(base, patient, study, series, sop + ".dcm")


class StorageSCP:
    def __init__(
        self,
        aet: str,
        bind: str,
        port: int,
        storage_dir: str,
        organize: bool,
        log: LogBuffer,
        allowed_aets: Optional[list[str]] = None,
        on_received: Optional[Callable[[object, str], None]] = None,
        tls: bool = False,
        tls_cert: str = "",
        tls_key: str = "",
        tls_ca: str = "",
    ):
        self.aet = aet
        self.bind = bind
        self.port = port
        self.storage_dir = storage_dir
        self.organize = organize
        self.log = log
        self.allowed_aets = [a for a in (allowed_aets or []) if str(a).strip()]
        self.on_received = on_received
        self.tls = tls
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.tls_ca = tls_ca
        self._server = None
        self._lock = threading.Lock()
        self.received_count = 0
        self.error_count = 0

    # ---- DIMSE handlers ----------------------------------------------------
    def _handle_echo(self, event) -> int:
        who = event.assoc.requestor.ae_title
        self.log.info(f"C-ECHO from {who} @ {_peer_addr(event)}", kind="echo")
        return 0x0000

    def _handle_store(self, event) -> int:
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            path = dest_path(self.storage_dir, ds, self.organize)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            ds.save_as(path, write_like_original=False)
            with self._lock:
                self.received_count += 1
            who = event.assoc.requestor.ae_title
            self.log.info(
                f"Stored {getattr(ds, 'Modality', '?')} {os.path.basename(path)} "
                f"from {who} @ {_peer_addr(event)}",
                kind="store",
                path=path,
            )
            if self.on_received:
                try:
                    self.on_received(ds, path)
                except Exception:  # never let a callback kill the association
                    pass
            return 0x0000
        except Exception as exc:  # out of resources / cannot write
            with self._lock:
                self.error_count += 1
            self.log.error(f"Failed to store instance: {exc}", kind="store")
            return 0xA700

    # ---- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self.running:
            return
        os.makedirs(self.storage_dir, exist_ok=True)
        ae = AE(ae_title=self.aet)
        for ctx in AllStoragePresentationContexts:
            ae.add_supported_context(ctx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
        ae.add_supported_context(Verification)
        if self.allowed_aets:
            ae.require_calling_aet = list(self.allowed_aets)
        handlers = [
            (evt.EVT_C_STORE, self._handle_store),
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
            f"Receiver listening on {self.bind}:{self.port} as {self.aet} "
            f"[{proto}] (accept: {allow}) -> {self.storage_dir}",
            kind="scp",
        )

    def stop(self) -> None:
        if not self.running:
            return
        try:
            self._server.shutdown()
        finally:
            self._server = None
            self.log.info("Receiver stopped", kind="scp")
