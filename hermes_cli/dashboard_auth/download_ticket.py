"""Short-lived signed download tickets for external viewers (WPS Office etc.).

The gated dashboard authenticates with session cookies. An external viewer
like WPS Office opens a plain GET URL and can attach neither cookies nor
headers, so the cookie gate would 401 every direct link. These tickets let a
first-party caller (agent, App, or the /api/files/download-ticket endpoint)
mint a time-limited, path-scoped signed URL for ``/api/files/download`` that
bypasses the cookie gate for exactly one file for a few minutes.

Security model:

* HMAC-SHA256 over ``f"{path}|{expiry}"`` with an in-process random secret,
  so the signature cannot be forged without process access and dies with the
  dashboard process.
* Tickets are short-lived (default 300s) and single-file: even a leaked URL
  is bounded in both time and scope.
* Bypassing the gate only skips *authentication* — every file-level guard in
  ``download_managed_file`` (managed-root resolution, sensitive-path
  denylist, size cap) still runs unchanged.

Mobile/gateway host is unchanged; this is a dashboard-side capability.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse

from fastapi import Request

_TICKET_TTL_SECONDS = 300  # 5 minutes
_SECRET = secrets.token_bytes(32)


def _sign(payload: str) -> str:
    return (
        base64.urlsafe_b64encode(
            hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )


def build_download_url(
    base_url: str,
    path: str,
    ttl_seconds: int = _TICKET_TTL_SECONDS,
) -> str:
    """Mint a signed download URL for ``path`` valid for ``ttl_seconds``.

    ``path`` is the absolute host path (or ``~``-expanded); the signed
    payload uses the raw path so verification is exact.
    """
    exp = int(time.time()) + ttl_seconds
    sig = _sign(f"{path}|{exp}")
    enc_path = urllib.parse.quote(path)
    return f"{base_url.rstrip('/')}/api/files/download?path={enc_path}&exp={exp}&sig={sig}"


def verify_download_ticket(request: Request) -> bool:
    """True iff the request carries a valid, unexpired ticket for its path.

    Only acts on ``/api/files/download``; every other path returns False so
    this helper can never widen the auth surface of a different route.
    """
    if request.url.path != "/api/files/download":
        return False
    path = request.query_params.get("path", "")
    exp_raw = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")
    if not path or not exp_raw or not sig:
        return False
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = _sign(f"{path}|{exp}")
    return hmac.compare_digest(sig, expected)
