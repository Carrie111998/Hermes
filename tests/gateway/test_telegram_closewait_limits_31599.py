"""Regression test for #31599 — Telegram general-pool CLOSE_WAIT fd leak.

Background
----------
PTB's ``telegram.request.HTTPXRequest`` builds the underlying
``httpx.AsyncClient`` with ``limits = httpx.Limits(max_connections=...)``
and *no* keepalive tuning, so httpx's default ``keepalive_expiry=5.0``
applies.  Behind an HTTP proxy (Cloudflare Warp etc.) a peer-initiated
FIN can sit in ``CLOSE_WAIT`` longer than that, leaking fds in the
general request pool (``_request[1]`` — the pool that routes
``bot.send_message`` / ``set_my_commands``), which
``_drain_polling_connections`` never resets.

The fix wires the shared ``gateway.platforms._http_client_limits``
``platform_httpx_limits()`` helper into *every* HTTPXRequest the adapter
builds — the fallback-transport branch, the proxy branch, and the plain
branch — so idle keepalive sockets drain aggressively.

Contracts asserted here (mutation-survivable)
----------------------------------------------
Proxy and direct-DNS ``HTTPXRequest`` instances must receive
``httpx_kwargs["limits"]`` with a ``keepalive_expiry`` strictly below
httpx's 5.0 default.  The fallback-IP instances must pass equivalent
limits into both inner ``AsyncHTTPTransport`` pools because httpx ignores
client-level limits when a custom transport is supplied.
"""

import asyncio
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


class _StopConnect(Exception):
    """Sentinel raised to abort connect() once requests are built."""


class _RecordingHTTPXRequest:
    """Stand-in for PTB's HTTPXRequest that records constructor kwargs."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _RecordingHTTPXRequest.instances.append(self)


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))


def _drive_connect(monkeypatch, *, proxy_url, fallback_ips=None):
    """Run connect() far enough to build the HTTPXRequests, then abort.

    Returns the list of recorded _RecordingHTTPXRequest instances.
    """
    _RecordingHTTPXRequest.instances = []

    # No DoH auto-discovery → exercise the proxy / plain branches, not fallback.
    async def _no_fallback():
        return list(fallback_ips or [])

    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", _no_fallback)
    monkeypatch.setattr(
        tg_adapter, "resolve_proxy_url", lambda *a, **k: proxy_url
    )
    # Replace the real HTTPXRequest with our recorder.
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _RecordingHTTPXRequest)

    adapter = _make_adapter()
    # Skip the cross-process token lock.
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    # Ensure the adapter reports no statically-configured fallback IPs.
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    if fallback_ips is not None:
        monkeypatch.setattr(adapter, "_fallback_ips", lambda: list(fallback_ips))

    # builder.request(...).get_updates_request(...).build() must be harmless;
    # make build() raise our sentinel so connect() stops right after the
    # HTTPXRequests are constructed (before any real network/init).
    fake_built_app = MagicMock()
    fake_built_app.initialize = MagicMock(side_effect=_StopConnect)

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.base_url.return_value = chainable
    chainable.base_file_url.return_value = chainable
    chainable.local_mode.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.side_effect = _StopConnect

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)

    try:
        asyncio.run(adapter.connect())
    except _StopConnect:
        pass
    except Exception:
        # connect() wraps work in a try; if it swallows the sentinel and
        # continues to real init, the recorded instances are still valid.
        pass

    return list(_RecordingHTTPXRequest.instances)


def _limits_of(inst):
    limits = inst.kwargs.get("httpx_kwargs", {}).get("limits")
    assert isinstance(limits, httpx.Limits), (
        "HTTPXRequest must receive httpx_kwargs['limits'] = httpx.Limits "
        "wired from platform_httpx_limits() (#31599). Missing → PTB falls "
        "back to default keepalive_expiry=5.0 and leaks CLOSE_WAIT fds."
    )
    # Holds for every pool: keepalive must be tighter than httpx's 5.0 default.
    assert limits.keepalive_expiry is not None
    assert limits.keepalive_expiry < 5.0, (
        "keepalive_expiry must be < httpx default 5.0 so idle/CLOSE_WAIT "
        "sockets drain promptly behind a proxy (#31599)."
    )
    # PTB's connection_pool_size (max_connections) must be preserved.
    assert limits.max_connections is not None and limits.max_connections > 0
    return limits


def _assert_keepalive_tight(instances):
    """Contract for the *general* request pool.

    Ordinary Bot API calls are short and sporadic, so reusing a connection is
    a win — the pool just must not sit on idle sockets (#31599).
    """
    assert instances, "connect() built no HTTPXRequest — test setup is wrong"
    limits = _limits_of(instances[0])
    assert limits.max_keepalive_connections is not None
    assert 1 <= limits.max_keepalive_connections <= 50


def _assert_updates_pool_never_reuses(instances):
    """Contract for the getUpdates pool: no connection reuse at all.

    api.telegram.org closes a pooled connection ~39s after it is opened. The
    long poll runs back-to-back (PTB's poll_interval defaults to 0), so the
    socket is never idle and keepalive_expiry — which measures *idle* time —
    never fires. The next getUpdates then goes out over a socket the server
    already closed and httpx raises a bare ReadError, which the adapter reads
    as a network fault and answers with a 5s reconnect: a reconnect every
    ~44s, forever, each costing a 5s window in which no updates arrive.

    Measured against the live endpoint: the connection died at 38.7s and
    38.9s, matching the adapter's observed 39.3s reconnect period. With
    max_keepalive_connections=0 the same probe ran 100s with zero errors.
    """
    assert len(instances) >= 2, "connect() must build a separate getUpdates pool"
    limits = _limits_of(instances[1])
    assert limits.max_keepalive_connections == 0, (
        "the getUpdates pool must not reuse connections — Telegram closes "
        "them server-side at ~39s and httpx then writes to a dead socket."
    )


def test_proxy_branch_general_pool_has_tight_keepalive(monkeypatch):
    """The proxy path the #31599 reporter hit must wire tuned limits."""
    instances = _drive_connect(monkeypatch, proxy_url="http://127.0.0.1:9/")
    # Both the general request pool and the get_updates pool are built here.
    assert len(instances) >= 2
    _assert_keepalive_tight(instances)
    _assert_updates_pool_never_reuses(instances)
    # Sanity: the proxy was actually threaded through (we're on the proxy branch).
    assert any(inst.kwargs.get("proxy") == "http://127.0.0.1:9/" for inst in instances)


def test_fallback_branch_forwards_tuned_limits_to_inner_transports(monkeypatch):
    monkeypatch.delenv("HERMES_TELEGRAM_HTTP_POOL_SIZE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_HTTPX_KEEPALIVE_EXPIRY", raising=False)

    instances = _drive_connect(
        monkeypatch,
        proxy_url=None,
        fallback_ips=["149.154.167.220"],
    )

    assert len(instances) >= 2
    for instance in instances:
        transport = instance.kwargs["httpx_kwargs"]["transport"]
        assert isinstance(transport, tg_adapter.TelegramFallbackTransport)
        limits = transport._transport_kwargs["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.keepalive_expiry is not None
        assert limits.keepalive_expiry < 5.0
        assert limits.max_connections == 512

    # On this branch the limits live on the transport, not the client, so the
    # per-pool contracts are asserted here rather than through
    # _assert_keepalive_tight / _assert_updates_pool_never_reuses (both read
    # client-level httpx_kwargs["limits"], which this branch deliberately does
    # not set — httpx would discard it alongside a custom transport).
    general_limits = instances[0].kwargs["httpx_kwargs"]["transport"]._transport_kwargs["limits"]
    assert 1 <= general_limits.max_keepalive_connections <= 50, (
        "ordinary Bot API calls should still reuse connections — only the "
        "getUpdates pool opts out."
    )
    updates_limits = instances[1].kwargs["httpx_kwargs"]["transport"]._transport_kwargs["limits"]
    assert updates_limits.max_keepalive_connections == 0, (
        "the getUpdates pool must not reuse connections — Telegram closes "
        "them server-side at ~39s and httpx then writes to a dead socket."
    )

    for instance in instances:
        asyncio.run(instance.kwargs["httpx_kwargs"]["transport"].aclose())
