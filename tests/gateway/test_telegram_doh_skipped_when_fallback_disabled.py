"""Regression tests — Telegram boot-time DNS-over-HTTPS noise.

Background
----------
``HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1`` (the documented remedy for
restricted networks / NordVPN) correctly stops the adapter *using* the
fallback-IP transport, but it did **not** stop the adapter *discovering*
the IPs: ``connect()`` read the flag, then ran DoH auto-discovery
unconditionally, and only consulted the flag afterwards when choosing a
transport.  Every gateway boot therefore paid a DNS-over-HTTPS round trip
and emitted::

    WARNING [Telegram] Discovering Telegram API fallback IPs via DNS-over-HTTPS…
    INFO    [Telegram] Auto-discovered Telegram fallback IPs: 149.154.166.110
    INFO    [Telegram] Telegram fallback-IP transport disabled via env

...only to throw the result away.  Operators read the WARNING as a
connectivity fault and re-applied an env remedy that was already in place.

Contract asserted here (mutation-survivable)
--------------------------------------------
1. Flag set  → ``discover_fallback_ips`` is never awaited and no
   "Discovering ..." record is emitted.
2. Flag unset → discovery still runs (guards against over-correcting into
   "never discover", which would break restricted-network users).
3. The *first* connect attempt is logged below WARNING, so a healthy boot
   produces no Telegram warnings at all.
"""

import asyncio
import logging
import sys
from unittest.mock import MagicMock

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

DISCOVER_MSG = "Discovering Telegram API fallback IPs"
CONNECTING_MSG = "Connecting to Telegram (attempt"


class _StopConnect(Exception):
    """Sentinel raised to abort connect() once the point of interest is passed.

    Deliberately NOT network-shaped, so the adapter's retry ladder
    re-raises it immediately instead of sleeping through 8 attempts.
    """


class _RecordingHTTPXRequest:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _drive_connect(monkeypatch, *, disabled, stop_at):
    """Run ``connect()`` up to ``stop_at`` ('build' or 'initialize').

    Returns a dict with the discovery spy's call count.
    """
    if disabled:
        monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "1")
    else:
        monkeypatch.delenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", raising=False)

    calls = {"discover": 0}

    async def _spy_discover():
        calls["discover"] += 1
        return ["149.154.167.220"]

    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", _spy_discover)
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _RecordingHTTPXRequest)

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])
    monkeypatch.setattr(
        adapter, "_instrument_polling_request", lambda req: req, raising=False
    )

    chainable = MagicMock()
    for meth in (
        "token", "base_url", "base_file_url", "local_mode",
        "request", "get_updates_request",
    ):
        getattr(chainable, meth).return_value = chainable

    if stop_at == "build":
        chainable.build.side_effect = _StopConnect
    else:
        built = MagicMock()
        # Non-network failure → re-raised on the first pass through the
        # retry ladder, after the "Connecting …" line is emitted.
        built.initialize = MagicMock(side_effect=_StopConnect)
        chainable.build.return_value = built

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)

    try:
        asyncio.run(adapter.connect())
    except _StopConnect:
        pass
    except Exception:
        pass

    return calls


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_doh_discovery_skipped_when_fallback_disabled(monkeypatch, caplog):
    """Flag set → no DoH round trip and no "Discovering …" warning."""
    with caplog.at_level(logging.DEBUG):
        calls = _drive_connect(monkeypatch, disabled=True, stop_at="build")

    assert calls["discover"] == 0, (
        "discover_fallback_ips() must NOT be awaited when "
        "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS is set — the result is "
        "discarded, so the DoH round trip is pure boot latency."
    )
    assert not [m for m in _messages(caplog) if DISCOVER_MSG in m], (
        "The 'Discovering Telegram API fallback IPs' record must not be "
        "emitted when the fallback transport is disabled; operators read it "
        "as a connectivity fault."
    )


def test_doh_discovery_still_runs_when_not_disabled(monkeypatch, caplog):
    """Flag unset → discovery must still happen (no over-correction)."""
    with caplog.at_level(logging.DEBUG):
        calls = _drive_connect(monkeypatch, disabled=False, stop_at="build")

    assert calls["discover"] == 1, (
        "With the flag unset the adapter must still auto-discover fallback "
        "IPs — restricted-network users depend on it."
    )
    assert [m for m in _messages(caplog) if DISCOVER_MSG in m]


def test_first_connect_attempt_is_not_logged_as_warning(monkeypatch, caplog):
    """A healthy first attempt is routine progress, not a warning."""
    with caplog.at_level(logging.DEBUG):
        _drive_connect(monkeypatch, disabled=True, stop_at="initialize")

    attempts = [r for r in caplog.records if CONNECTING_MSG in r.getMessage()]
    assert attempts, "connect() never reached the retry ladder — setup is wrong"
    assert attempts[0].levelno < logging.WARNING, (
        "The first connect attempt must be logged below WARNING. It succeeds "
        "on essentially every boot, and logging it at WARNING makes a healthy "
        "gateway look like it is failing. Retries 2+ stay at WARNING."
    )
