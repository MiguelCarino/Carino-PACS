"""Build ssl.SSLContext objects for DICOM-over-TLS.

The receiver (SCP) uses a *server* context: its own certificate + private key,
and — if a CA is supplied — it also requires and verifies client certificates
(mutual TLS). The sender (SCU) uses a *client* context: verify the remote's
certificate against a CA (or the system trust store), optionally present our
own certificate for mutual TLS, or skip verification entirely for self-signed
/ test setups.
"""

from __future__ import annotations

import ssl


def server_context(certfile: str, keyfile: str, ca: str = "") -> ssl.SSLContext:
    if not certfile or not keyfile:
        raise ValueError("TLS receiver needs both a certificate and a private key")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    if ca:
        # A CA on the receiver side means: require + verify client certs (mTLS).
        ctx.load_verify_locations(cafile=ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_context(verify: bool = True, ca: str = "", certfile: str = "", keyfile: str = "") -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if verify:
        if ca:
            ctx.load_verify_locations(cafile=ca)
        else:
            ctx.load_default_certs()  # system trust store
    else:
        # Encrypted but unauthenticated — fine for self-signed / testing.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if certfile:
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile or None)
    return ctx
