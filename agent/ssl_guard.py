"""Preventive SSL CA certificate checks for Hermes Agent.

This module catches broken CA bundle paths before OpenAI/httpx turns them into
opaque ``FileNotFoundError: [Errno 2] No such file or directory`` failures.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

from agent.errors import SSLConfigurationError

logger = logging.getLogger(__name__)

_CA_BUNDLE_ENV_VARS = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

_SKIP_VALUES = {"1", "true", "yes", "on"}


def _skip_ssl_guard_enabled() -> bool:
    return os.getenv("HERMES_SKIP_SSL_GUARD", "").strip().lower() in _SKIP_VALUES


def _repair_hint() -> str:
    return (
        "Repair: python -m pip install --force-reinstall certifi openai httpx\n"
        "If you configured a custom corporate CA bundle, fix or unset the "
        "broken CA bundle environment variable."
    )


def _ssl_err(message: str) -> SSLConfigurationError:
    """Create a consistent, user-actionable SSL configuration error."""
    return SSLConfigurationError(f"{message}\n{_repair_hint()}")


# Bundles already parsed successfully in this process, keyed by file identity
# (resolved path + size + mtime). Loading a CA bundle means asking OpenSSL to
# parse ~150 PEM certificates and then decoding every one of them into a Python
# dict for the emptiness check — measured at 3.8-9.4s per call on a Windows dev
# box. That was paid on EVERY AIAgent construction, because agent_init calls
# this guard unconditionally: a test file building a handful of agents spent
# minutes here, and under the nightly gate's per-test --timeout individual
# tests were killed mid-construction.
#
# This is a preventive startup check whose answer is a property of a file, so
# re-deriving it per agent buys nothing. Keying on (path, size, mtime_ns) rather
# than caching a bare "already ran" flag keeps it honest: a different bundle, or
# the same path rewritten, is a different key and gets revalidated — which is
# what the guard's own tests rely on as they point certifi at successive
# tmp_path files. Only successes are cached; failures are rare and re-checked.
_VALIDATED_BUNDLES: set[tuple[str, int, int]] = set()


def _validate_bundle_path(label: str, value: str, *, require_substantial: bool = False) -> None:
    path = Path(value).expanduser()
    if not path.exists():
        raise _ssl_err(f"{label} points to a missing CA bundle: {value}")
    if not path.is_file():
        raise _ssl_err(f"{label} does not point to a CA bundle file: {value}")
    stat = path.stat()
    if require_substantial and stat.st_size < 1024:
        raise _ssl_err(f"{label} at {value} appears corrupted (too small)")

    cache_key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if cache_key in _VALIDATED_BUNDLES:
        return

    try:
        ctx = ssl.create_default_context(cafile=str(path))
    except Exception as exc:
        raise _ssl_err(f"{label} CA bundle at {value} cannot be loaded: {exc}") from exc
    if not ctx.get_ca_certs():
        raise _ssl_err(f"{label} CA bundle at {value} did not load any certificates")
    _VALIDATED_BUNDLES.add(cache_key)


def verify_ca_bundle() -> None:
    """Verify configured and bundled CA certificates are present and loadable.

    Raises:
        SSLConfigurationError: If an explicit CA-bundle environment variable
            points at a bad path, or if certifi's bundled ``cacert.pem`` is
            missing/corrupt.
    """
    if _skip_ssl_guard_enabled():
        logger.debug("SSL CA bundle guard skipped via HERMES_SKIP_SSL_GUARD")
        return

    for env_var in _CA_BUNDLE_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            _validate_bundle_path(env_var, value)

    try:
        import certifi
    except Exception as exc:
        raise _ssl_err(f"certifi is not importable: {exc}") from exc

    ca_bundle = str(certifi.where())
    _validate_bundle_path("certifi", ca_bundle, require_substantial=True)


def verify_ca_bundle_with_fallback() -> None:
    """Backward-compatible wrapper for older call sites.

    The old PR name mentioned a platform fallback, but allowing startup with a
    broken certifi bundle still leaves httpx/OpenAI and requests call sites
    failing later. Keep the wrapper name but enforce the same check.
    """
    verify_ca_bundle()
