"""Storage SCU — the "send" half.

C-STORE a single .dcm file to a remote node, and C-ECHO to test connectivity.
We request the instance's own transfer syntax (compressed objects are sent
as-is; pynetdicom does not transcode), so if a remote refuses that syntax the
store fails loudly rather than silently corrupting data.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Optional

from pydicom import dcmread
from pydicom.uid import ImplicitVRLittleEndian
from pynetdicom import AE
from pynetdicom.sop_class import Verification

# C-STORE statuses that are "stored, with a caveat" — treat as success.
_WARNING_STATUSES = {0xB000, 0xB006, 0xB007}


@dataclass
class Destination:
    name: str
    host: str
    port: int
    aet: str
    tls: bool = False  # connect to this node over TLS

    @classmethod
    def from_dict(cls, d: dict) -> "Destination":
        return cls(name=d.get("name", d["host"]), host=d["host"], port=int(d["port"]),
                   aet=d["aet"], tls=bool(d.get("tls", False)))


def _tls_args(dest: "Destination", tls_context: Optional[ssl.SSLContext]):
    """pynetdicom associate() tls_args tuple, or None for a plaintext link."""
    if dest.tls and tls_context is not None:
        return (tls_context, dest.host)
    return None


@dataclass
class SendResult:
    ok: bool
    message: str


def c_echo(dest: Destination, calling_aet: str, timeout: int = 10,
           tls_context: Optional[ssl.SSLContext] = None) -> SendResult:
    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(Verification)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout
    try:
        assoc = ae.associate(dest.host, dest.port, ae_title=dest.aet, tls_args=_tls_args(dest, tls_context))
    except (ssl.SSLError, OSError) as exc:
        return SendResult(False, f"TLS/connection error: {exc}")
    if not assoc.is_established:
        scheme = "TLS " if dest.tls else ""
        return SendResult(False, f"{scheme}association rejected/aborted to {dest.host}:{dest.port}")
    try:
        status = assoc.send_c_echo()
        if status and status.Status == 0x0000:
            return SendResult(True, "verification OK")
        code = f"0x{status.Status:04X}" if status else "no response"
        return SendResult(False, f"C-ECHO failed ({code})")
    finally:
        assoc.release()


def c_store(dest: Destination, filepath: str, calling_aet: str, timeout: int = 30,
            tls_context: Optional[ssl.SSLContext] = None) -> SendResult:
    try:
        ds = dcmread(filepath)
    except Exception as exc:
        return SendResult(False, f"unreadable ({exc})")

    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None) or ImplicitVRLittleEndian
    sop_class = getattr(ds, "SOPClassUID", None)
    if not sop_class:
        return SendResult(False, "no SOPClassUID")

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(sop_class, ts)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout
    try:
        assoc = ae.associate(dest.host, dest.port, ae_title=dest.aet, tls_args=_tls_args(dest, tls_context))
    except (ssl.SSLError, OSError) as exc:
        return SendResult(False, f"TLS/connection error: {exc}")
    if not assoc.is_established:
        scheme = "TLS " if dest.tls else ""
        return SendResult(False, f"{scheme}association rejected/aborted to {dest.host}:{dest.port}")
    try:
        status = assoc.send_c_store(ds)
        if not status:
            return SendResult(False, "no C-STORE response (timeout/abort)")
        code = status.Status
        if code == 0x0000 or code in _WARNING_STATUSES:
            return SendResult(True, "stored" if code == 0x0000 else f"stored (warning 0x{code:04X})")
        return SendResult(False, f"C-STORE failed (0x{code:04X})")
    finally:
        assoc.release()
