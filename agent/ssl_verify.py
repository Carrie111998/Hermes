"""TLS verify resolution for httpx/OpenAI provider clients."""

from __future__ import annotations

import logging
import os
import ssl
import urllib.parse
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _coerce_insecure(ssl_verify: Any) -> bool:
    if ssl_verify is False:
        return True
    if isinstance(ssl_verify, str) and ssl_verify.strip().lower() in {"false", "0", "no", "off"}:
        return True
    return False


_IPV4_LOCAL_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "100.64.")


def _is_local_host(host: str) -> bool:
    """True when ``host`` is loopback or an operator-controlled private address.

    ``ssl_verify: false`` is only a legitimate operator choice for endpoints
    whose network path the operator controls: local LLM gateways (Ollama,
    llama.cpp, vLLM), LAN hosts, containers, and single-label hostnames. For
    anything publicly routable the correct mitigation for a private CA is
    ``ssl_ca_cert``, never disabling verification.

    This predicate is shared by every TLS resolution path (httpx chat client,
    urllib and requests ``/models`` probes) so the whole bug class is guarded
    in one place.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost", "::1", "0.0.0.0"} or host.startswith(_IPV4_LOCAL_PREFIXES):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".", 2)[1])
        except (IndexError, ValueError):
            second = -1
        if 16 <= second <= 31:  # 172.16.0.0/12
            return True
    if ":" in host:
        # IPv6 loopback / unique-local (fc00::/7) / link-local (fe80::/10)
        if host == "::1" or host.startswith(("fc", "fd", "fe8")):
            return True
    # Single-label hostnames (``ollama``, ``vllm``, ``llama-cpp``) are LAN or
    # container names, not public DNS — the operator controls that endpoint.
    # Dotted names could resolve publicly, so they require ``ssl_ca_cert``.
    if "." not in host and ":" not in host:
        return True
    return False


def _host_of(base_url: str) -> str:
    try:
        return urllib.parse.urlparse(base_url).hostname or ""
    except ValueError:
        return ""


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
        # ``ssl_verify: false`` is a local-development escape hatch. Refuse it
        # for publicly routable endpoints: an entry with a public HTTPS URL
        # plus disabled verification is a config mistake (the legitimate fix
        # for a private CA there is ``ssl_ca_cert``), and a misparse that
        # silently turns into a MitM is exactly what this option must not do.
        # Callers without a known base_url keep the legacy behavior (warn and
        # proceed) so the option still works for genuinely local setups whose
        # URL is resolved later.
        host = _host_of(base_url)
        if base_url and not _is_local_host(host):
            raise ValueError(
                f"ssl_verify: false is not allowed for public endpoint {base_url!r} "
                f"(host {host!r}). Disabling TLS verification is only supported "
                "for local/private hosts (localhost, LAN addresses, container "
                "names). For a private CA on a public host, set ssl_ca_cert "
                "instead."
            )
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
