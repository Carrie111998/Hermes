"""mTLS helpers — control-plane orchestrator rejects peers without a CA-signed cert."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


def server_ssl_context(*, server_pem: PathLike, ca_crt: PathLike) -> ssl.SSLContext:
    """TLS server that **requires** a client certificate signed by ``ca_crt``.

    Connections without a valid client cert fail the handshake (dropped).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(server_pem))
    ctx.load_verify_locations(cafile=str(ca_crt))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False  # clients are agents, not DNS names
    return ctx


def client_ssl_context(
    *,
    ca_crt: PathLike,
    client_pem: PathLike,
    check_hostname: bool = False,
) -> ssl.SSLContext:
    """TLS client presenting ``client_pem`` and trusting ``ca_crt``."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(ca_crt))
    ctx.load_cert_chain(certfile=str(client_pem))
    ctx.check_hostname = check_hostname
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def peer_cert_cn(cert: Optional[dict]) -> Optional[str]:
    """Extract CN from an SSL peer certificate dict (as returned by getpeercert)."""
    if not cert:
        return None
    subject = cert.get("subject") or ()
    for rdn in subject:
        for key, value in rdn:
            if key == "commonName":
                return str(value)
    return None
