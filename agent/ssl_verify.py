"""TLS verify resolution for httpx/OpenAI provider clients."""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _build_truststore_context() -> ssl.SSLContext | None:
    """Return an ``ssl.SSLContext`` that delegates to the OS trust store.

    Only activated when the operator opts in via
    ``network.trust_store: true`` in ``config.yaml``.  The SSLContext is
    intended for the ``verify=`` parameter of owned HTTP clients
    (httpx.Client / httpx.AsyncClient, OpenAI SDK, requests sessions), so
    truststore's CA set is applied ONLY to connections Hermes owns — not to
    every ssl consumer in the process.

    Returns None when:
      - ``truststore`` is not installed (the optional ``[truststore]`` extra
        is absent).  Default installs are certifi-only.
      - ``sys.platform`` is Linux (distros already bridge the OS store into
        ``/etc/ssl/certs/ca-certificates.crt`` + certifi; truststore is a
        no-op there).
      - Any import/injection failure (OS store inaccessible, etc.).
        Fall back to the certifi default; ``ssl_guard.py`` surfaces a clear
        error if the bundle is broken.

    This approach avoids ``truststore.inject_into_ssl()`` at import time,
    which mutates a process-global default and violates Truststore's own
    guidance: libraries and packages should not call global injection as a
    side effect of import because they cannot guarantee process-wide ordering.
    The opt-in is a ``config.yaml`` key (per ``AGENTS.md``: ``.env`` is for
    secrets only; behavioral settings go in config).
    """
    # Linux distros already bridge the OS trust store into certifi /
    # /etc/ssl/certs/ca-certificates.crt; truststore is a no-op there —
    # skip the import cost.
    if sys.platform not in ("win32", "darwin"):
        return None
    try:
        import truststore
    except Exception:
        return None
    try:
        return truststore.SSLContext()
    except Exception:
        return None


def _coerce_insecure(ssl_verify: Any) -> bool:
    if ssl_verify is False:
        return True
    if isinstance(ssl_verify, str) and ssl_verify.strip().lower() in {"false", "0", "no", "off"}:
        return True
    return False


def resolve_httpx_verify(
    *,
    ca_bundle: Optional[str] = None,
    ssl_verify: Any = None,
    base_url: str = "",
) -> bool | ssl.SSLContext:
    """Resolve httpx ``verify`` for provider HTTP clients.

    Priority:
    1. ``ssl_verify: false`` — disable verification (local dev only)
    2. explicit ``ca_bundle`` (per-provider ``ssl_ca_cert`` config field)
    3. ``HERMES_CA_BUNDLE``, ``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE``,
       ``CURL_CA_BUNDLE`` env vars
    4. ``True`` (httpx/certifi default)

    ``base_url`` is used only for the insecure-mode warning message.
    """
    if _coerce_insecure(ssl_verify):
        logger.warning(
            "TLS certificate verification DISABLED (ssl_verify: false) for %s — "
            "this is intended for local development only and is unsafe on any "
            "network you do not fully control.",
            base_url or "a custom provider endpoint",
        )
        return False

    effective_ca = (
        (ca_bundle or "").strip()
        or os.getenv("HERMES_CA_BUNDLE", "").strip()
        or os.getenv("SSL_CERT_FILE", "").strip()
        or os.getenv("REQUESTS_CA_BUNDLE", "").strip()
        or os.getenv("CURL_CA_BUNDLE", "").strip()
    )
    if effective_ca:
        ca_path = str(Path(effective_ca).expanduser())
        if os.path.isfile(ca_path):
            return ssl.create_default_context(cafile=ca_path)
        logger.warning(
            "CA bundle path does not exist: %s — falling back to default certificates",
            effective_ca,
        )
    return True


def resolve_httpx_verify_with_truststore(
    *,
    ca_bundle: Optional[str] = None,
    ssl_verify: Any = None,
    base_url: str = "",
    trust_store: bool = False,
) -> bool | ssl.SSLContext:
    """Like :func:`resolve_httpx_verify` but honours ``network.trust_store``.

    When ``trust_store=`` is True, replaces the ``True`` (certifi-default)
    fallback with a ``truststore.SSLContext`` that consults the OS trust
    store in addition to certifi.  This is the entry point for the concrete
    callers that can read the config key and pass it through.
    """
    result = resolve_httpx_verify(
        ca_bundle=ca_bundle, ssl_verify=ssl_verify, base_url=base_url,
    )
    if result is not True or not trust_store:
        return result
    ctx = _build_truststore_context()
    if ctx is not None:
        return ctx
    return True