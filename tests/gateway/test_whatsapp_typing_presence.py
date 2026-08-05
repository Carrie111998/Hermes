"""Tests for the WhatsApp typing indicator lifecycle.

Regression tests for a stuck "typing…" indicator.

WhatsApp presence is **sticky**: a ``composing`` presence update stays up until
an explicit ``paused`` arrives.  Telegram and Discord expire their indicator a
few seconds after the last refresh, so an adapter that only ever sends
"start typing" is harmless there — but on WhatsApp it leaves the contact
showing "typing…" indefinitely once the agent's turn ends.

``WhatsAppAdapter`` implemented ``send_typing()`` and inherited the base class's
no-op ``stop_typing()``, and ``bridge.js``'s ``/typing`` endpoint could only
send ``composing``.  So ``_keep_typing`` refreshed ``composing`` every ~2s for
the life of a turn and nothing ever took it down.

These tests assert the behaviour contract rather than the wire format:

1. ``send_typing`` posts a ``composing`` presence.
2. ``stop_typing`` posts a ``paused`` presence.
3. ``stop_typing`` is reachable through the base class's real cleanup
   chokepoint, ``_stop_typing_with_metadata`` — the path
   ``gateway/platforms/base.py`` actually uses.  A ``stop_typing`` that exists
   but is never called would still leave the indicator stuck.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter():
    """Create a WhatsAppAdapter with the attributes the typing path touches."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = 19876
    adapter._running = True
    adapter._http_session = MagicMock()
    adapter._http_session.post = MagicMock(return_value=_AsyncCM(MagicMock()))
    return adapter


def _posted_payloads(adapter):
    """Return the JSON bodies POSTed to the bridge."""
    return [c.kwargs["json"] for c in adapter._http_session.post.call_args_list]


def _posted_urls(adapter):
    return [c.args[0] for c in adapter._http_session.post.call_args_list]


@pytest.mark.asyncio
async def test_send_typing_posts_composing():
    adapter = _make_adapter()
    with patch.object(
        adapter, "_check_managed_bridge_exit", AsyncMock(return_value=False)
    ):
        await adapter.send_typing("1234567890@s.whatsapp.net")

    payloads = _posted_payloads(adapter)
    assert len(payloads) == 1
    assert payloads[0]["state"] == "composing"
    assert payloads[0]["chatId"] == "1234567890@s.whatsapp.net"
    assert _posted_urls(adapter)[0].endswith("/typing")


@pytest.mark.asyncio
async def test_stop_typing_posts_paused():
    """The whole point: something has to send 'paused' or the bubble sticks."""
    adapter = _make_adapter()
    with patch.object(
        adapter, "_check_managed_bridge_exit", AsyncMock(return_value=False)
    ):
        await adapter.stop_typing("1234567890@s.whatsapp.net")

    payloads = _posted_payloads(adapter)
    assert len(payloads) == 1
    assert payloads[0]["state"] == "paused"
    assert _posted_urls(adapter)[0].endswith("/typing")


@pytest.mark.asyncio
async def test_stop_typing_reachable_via_base_class_chokepoint():
    """``_stop_typing_with_metadata`` is the path base.py actually calls.

    A ``stop_typing`` that the cleanup path never reaches would leave the
    indicator stuck exactly as before, so assert the wiring, not just the
    method.  The signature also has to tolerate the ``metadata`` kwarg that
    chokepoint passes for platforms that need thread routing.
    """
    adapter = _make_adapter()
    with patch.object(
        adapter, "_check_managed_bridge_exit", AsyncMock(return_value=False)
    ):
        await adapter._stop_typing_with_metadata(
            "1234567890@s.whatsapp.net", metadata={"thread_id": "t1"}
        )

    payloads = _posted_payloads(adapter)
    assert len(payloads) == 1, "base-class cleanup did not reach stop_typing()"
    assert payloads[0]["state"] == "paused"


@pytest.mark.asyncio
async def test_typing_calls_are_noop_when_not_running():
    """No presence traffic once the adapter has shut down."""
    adapter = _make_adapter()
    adapter._running = False
    with patch.object(
        adapter, "_check_managed_bridge_exit", AsyncMock(return_value=False)
    ):
        await adapter.send_typing("1234567890@s.whatsapp.net")
        await adapter.stop_typing("1234567890@s.whatsapp.net")

    assert _posted_payloads(adapter) == []


@pytest.mark.asyncio
async def test_stop_typing_swallows_bridge_errors():
    """A dead bridge must not raise out of the turn's cleanup path."""
    adapter = _make_adapter()
    adapter._http_session.post = MagicMock(side_effect=RuntimeError("bridge down"))
    with patch.object(
        adapter, "_check_managed_bridge_exit", AsyncMock(return_value=False)
    ):
        await adapter.stop_typing("1234567890@s.whatsapp.net")  # must not raise
