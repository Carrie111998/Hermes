"""TLS verify resolution for httpx/OpenAI provider clients."""

from __future__ import annotations

import ipaddress
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


# Operator-controlled address space (RFC1918, link-local, RFC6598 shared CGNAT).
# Classified against explicit networks rather than ``IPv4Address.is_private`` /
# ``is_shared`` because those properties are version-divergent (``is_shared``
# only landed in Python 3.11's typeshed) and ``is_private`` is over-broad for
# IPv6 — CPython reports the *documentation* range 2001:db8::/32 as private,
# yet that prefix is not a network the operator controls.
PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("100.64.0.0/10"),  # RFC6598 shared CGNAT
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)


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

    Classification parses literal addresses with :mod:`ipaddress` and checks
    them against the explicit private/link-local/shared networks above.  DNS
    names that merely *look* like a private address (``127.attacker.example``,
    ``172.16.attacker.example``) are never treated as local — only actual
    literal IP addresses and single-label (LAN/container) hostnames are.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    # Exact localhost names and single-label hostnames (``ollama``, ``vllm``,
    # ``llama-cpp``) are LAN or container names — the operator controls that
    # endpoint.  Dotted names could resolve publicly, so they require
    # ``ssl_ca_cert``.
    if host in {"localhost", "0.0.0.0", "::1"} or ("." not in host and ":" not in host):
        return True
    # Strip IPv6 zone id (``fe80::1%eth0``) before parsing.
    if ":" in host and "%" in host:
        host = host.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP — a DNS name (even one that *looks* like a private
        # address, e.g. ``127.attacker.example``) is not local.
        return False
    # IPv4-mapped IPv6 addresses (``::ffff:192.168.0.1``) classify by their
    # embedded IPv4 address.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_unspecified:
        return True
    return any(addr in network for network in PRIVATE_NETWORKS)


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
