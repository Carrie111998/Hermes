"""Process-level bootstrap helpers for ``run_agent``.

Three concerns, all tied to ``AIAgent`` boot-time / runtime IO setup:

1. **Lazy OpenAI SDK import** — ``_load_openai_cls`` + ``_OpenAIProxy``
   defer the 240ms-ish ``from openai import OpenAI`` cost until first use,
   while preserving ``isinstance(client, OpenAI)`` checks and
   ``patch("run_agent.OpenAI", ...)`` test patterns.

2. **Crash-resistant stdio** — ``_SafeWriter`` wraps stdout/stderr so
   ``OSError: Input/output error`` from broken pipes (systemd, Docker,
   thread teardown races) cannot crash the agent.  ``_install_safe_stdio``
   applies the wrapper.

3. **HTTP proxy resolution** — ``_get_proxy_from_env`` reads
   ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``;
   ``_get_proxy_for_base_url`` respects ``NO_PROXY`` for the given base URL.

``run_agent`` re-exports every name so existing
``from run_agent import _get_proxy_from_env`` imports keep working
unchanged.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from typing import Any, Optional

from utils import base_url_hostname, normalize_proxy_url


# Inject the OS trust store into Python's default SSLContext on platforms
# where certifi does not see corporate / MDM-deployed root CAs (Windows,
# macOS). On corporate networks with TLS-inspecting proxies, IT deploys the
# proxy's root CA to the OS trust store via Group Policy / MDM; Python's
# certifi bundle is statically bundled and never sees it, so every outbound
# HTTPS call fails with a certificate-verification error. truststore makes
# Python's ssl module consult the OS trust store — the same trust decision
# the operator's admin already made — closing the gap.
#
# This is opt-in via the optional `truststore` extra (`uv pip install -e
# ".[truststore]"`) and silently no-ops when the package is not installed,
# so default installs are untouched. Never raises: a failure here leaves the
# certifi default in place and lets ssl_guard.py surface a clear error.
#
# Gates:
#   - opt-out env var HERMES_DISABLE_TRUSTSTORE=1 (escape hatch)
#   - platform check: only Windows / macOS where the OS trust store is the
#     primary trust authority; Linux distros already wire the OS store via
#     ca-certificates.crt and certifi, so injecting truststore is a no-op
#     there and we skip the extra import cost.
_TRUSTSTORE_INJECTED = False


def _maybe_inject_truststore() -> None:
    """Inject the OS trust store into Python's default SSLContext.

    Idempotent. No-op on Linux, when the env escape hatch is set, or when the
    `truststore` package is not installed. Never raises.
    """
    global _TRUSTSTORE_INJECTED
    if _TRUSTSTORE_INJECTED:
        return
    if os.getenv("HERMES_DISABLE_TRUSTSTORE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    # Linux distros already bridge the OS trust store into certifi /
    # /etc/ssl/certs/ca-certificates.crt; truststore is a no-op there.
    if sys.platform not in ("win32", "darwin"):
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        _TRUSTSTORE_INJECTED = True
    except Exception:
        # truststore is an optional extra. If it's not installed or the
        # injection fails for any reason, fall back to the certifi default.
        # ssl_guard.py will surface a clear error if the bundle is broken.
        pass


# Apply on import so the injection is in place before any httpx / openai /
# requests client is constructed downstream.  Entry points import this
# module early via run_agent.
_maybe_inject_truststore()


# Cached at module level so we only pay the OpenAI SDK import cost once
# per process (after the first lazy load).
_OPENAI_CLS_CACHE = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _cls
        _OPENAI_CLS_CACHE = _cls
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like ``openai.OpenAI`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


class _SafeWriter:
    """Transparent stdio wrapper that catches OSError/ValueError from broken pipes.

    When hermes-agent runs as a systemd service, Docker container, or headless
    daemon, the stdout/stderr pipe can become unavailable (idle timeout, buffer
    exhaustion, socket reset). Any print() call then raises
    ``OSError: [Errno 5] Input/output error``, which can crash agent setup or
    run_conversation() — especially via double-fault when an except handler
    also tries to print.

    Additionally, when subagents run in ThreadPoolExecutor threads, the shared
    stdout handle can close between thread teardown and cleanup, raising
    ``ValueError: I/O operation on closed file`` instead of OSError.

    This wrapper delegates all writes to the underlying stream and silently
    catches both OSError and ValueError. It is transparent when the wrapped
    stream is healthy.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def write(self, data):
        try:
            return self._inner.write(data)
        except (OSError, ValueError):
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def fileno(self):
        return self._inner.fileno()

    def isatty(self):
        try:
            return self._inner.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _get_proxy_from_env() -> Optional[str]:
    """Read proxy URL from environment variables.

    Checks HTTPS_PROXY, HTTP_PROXY, ALL_PROXY (and lowercase variants) in order.
    Returns the first valid proxy URL found, or None if no proxy is configured.
    """
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return normalize_proxy_url(value)
    return None


def _get_proxy_for_base_url(base_url: Optional[str]) -> Optional[str]:
    """Return an env-configured proxy unless NO_PROXY excludes this base URL."""
    proxy = _get_proxy_from_env()
    if not proxy or not base_url:
        return proxy

    host = base_url_hostname(base_url)
    if not host:
        return proxy

    try:
        if urllib.request.proxy_bypass_environment(host):
            return None
    except Exception:
        pass

    return proxy


def build_keepalive_http_client(
    base_url: str = "",
    *,
    async_mode: bool = False,
    verify: Any = True,
) -> Optional[Any]:
    """Build an httpx client for OpenAI SDK calls with env-only proxy policy.

    Uses explicit ``HTTPS_PROXY`` / ``NO_PROXY`` env vars via
    ``_get_proxy_for_base_url``. Plain no-proxy mounts disable httpx's default
    ``trust_env`` proxy path, so macOS system proxy settings from
    ``urllib.request.getproxies()`` (which omit the ExceptionsList) are not
    applied. Mirrors ``AIAgent._build_keepalive_http_client``.

    Connection lifecycle is managed at the HTTP pool layer
    (``keepalive_expiry=20.0`` reaps idle connections before reverse proxies'
    typical 30-60 s timeouts) instead of the former custom
    ``socket_options`` transport, which broke streaming behind reverse
    proxies (#54049, #12952) and stalled TLS handshakes by stripping
    ``TCP_NODELAY``.

    ``verify`` is forwarded to httpx so auxiliary-client calls (compression,
    vision, web_extract, title generation, etc.) honor the same per-provider
    ``ssl_ca_cert`` / ``ssl_verify`` and ``HERMES_CA_BUNDLE`` settings the main
    client uses. It is passed on the client AND on the plain no-proxy mounts
    (a mounted transport owns the SSL context for its scheme).
    """
    try:
        import httpx

        proxy = _get_proxy_for_base_url(base_url)

        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=20.0,
        )
        # Generous read=None for SSE streaming endpoints.
        timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=10.0)

        transport_cls = httpx.AsyncHTTPTransport if async_mode else httpx.HTTPTransport
        client_cls = httpx.AsyncClient if async_mode else httpx.Client
        mounts = {}
        if proxy is None:
            mounts = {
                "http://": transport_cls(verify=verify),
                "https://": transport_cls(verify=verify),
            }
        return client_cls(
            limits=limits,
            timeout=timeout,
            proxy=proxy,
            mounts=mounts or None,
            verify=verify,
        )
    except Exception:
        return None


def _install_safe_stdio() -> None:
    """Wrap stdout/stderr so best-effort console output cannot crash the agent."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            setattr(sys, stream_name, _SafeWriter(stream))


# Module-level proxy instance — drops in for ``openai.OpenAI``.  Imported as
# ``from agent.process_bootstrap import OpenAI`` (or re-exported via
# ``run_agent`` for legacy tests).
OpenAI = _OpenAIProxy()


__all__ = [
    "OpenAI",
    "_OpenAIProxy",
    "_load_openai_cls",
    "_SafeWriter",
    "_install_safe_stdio",
    "_get_proxy_from_env",
    "_get_proxy_for_base_url",
    "build_keepalive_http_client",
    "_maybe_inject_truststore",
]
