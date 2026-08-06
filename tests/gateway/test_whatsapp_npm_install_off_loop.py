"""Regression test: the WhatsApp bridge npm install must not block the loop.

``WhatsAppAdapter.connect()`` reinstalls the bridge's node_modules whenever
``package.json`` changed since the last install — which is exactly what
``hermes update`` does when it bumps the Baileys pin. That install ran through
a synchronous ``subprocess.run`` inside the ``async def``, so it blocked the
gateway's event loop for the whole install: every other platform adapter, every
in-flight turn, the session watchdogs and the SIGTERM handler stopped for up to
``WHATSAPP_NPM_INSTALL_TIMEOUT`` seconds (default 300).

It also defeated ``_connect_adapter_with_timeout``, whose docstring promises to
"connect an adapter without allowing one platform to block others" — that
guarantee is an ``asyncio.wait(..., timeout=...)``, which cannot fire while the
loop itself is blocked.

The probe here is deliberately built on ``loop.call_later`` rather than
``asyncio.sleep``: the shared connect harness patches ``asyncio.sleep``, and a
patched sleep would make any coroutine-based probe look responsive whether or
not the loop was actually free.
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.gateway.test_whatsapp_connect import (
    _connect_patches,
    _make_adapter,
    _mock_aiohttp,
)


# How long the faked npm install blocks its thread, and how often the probe
# fires. A free loop gets ~30 ticks in that window; a blocked one gets ~0.
_INSTALL_SECONDS = 0.3
_TICK_SECONDS = 0.01
_MIN_TICKS = 5


class _LoopProbe:
    """Counts how many times the event loop got to run a scheduled callback."""

    def __init__(self):
        self.ticks = 0
        self._handle = None

    def start(self):
        self._schedule()
        return self

    def _schedule(self):
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(_TICK_SECONDS, self._fire)

    def _fire(self):
        self.ticks += 1
        self._schedule()

    def stop(self):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


@pytest.mark.asyncio
async def test_npm_install_does_not_block_the_event_loop():
    """A slow bridge install must leave the loop free to run other work."""
    adapter = _make_adapter()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_fh = MagicMock()
    mock_client_cls = _mock_aiohttp(status=200, json_data={"status": "connecting"})

    install_calls = []

    def _slow_npm_install(*args, **kwargs):
        # Synchronous, like the real npm install.
        install_calls.append(args[0] if args else None)
        time.sleep(_INSTALL_SECONDS)
        return MagicMock(returncode=0, stderr="")

    patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

    probe = _LoopProbe().start()
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], \
             patch.object(type(adapter), "_poll_messages", return_value=MagicMock()), \
             patch("subprocess.run", side_effect=_slow_npm_install):
            try:
                await adapter.connect()
            except Exception:
                # connect() may still fail further down (no real bridge); the
                # install is the only part under test.
                pass
    finally:
        probe.stop()

    assert install_calls, "the npm install path was never reached"
    assert probe.ticks >= _MIN_TICKS, (
        f"event loop was blocked during the bridge install "
        f"({probe.ticks} ticks in {_INSTALL_SECONDS}s, expected >= {_MIN_TICKS})"
    )


@pytest.mark.asyncio
async def test_install_failure_still_aborts_connect():
    """A non-zero install exit still fails the connect, as before."""
    adapter = _make_adapter()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_fh = MagicMock()
    mock_client_cls = _mock_aiohttp(status=200, json_data={"status": "connecting"})

    patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], \
         patch.object(type(adapter), "_poll_messages", return_value=MagicMock()), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
        result = await adapter.connect()

    assert result is False


@pytest.mark.asyncio
async def test_install_timeout_still_aborts_connect():
    """``subprocess.TimeoutExpired`` still propagates out of the worker thread."""
    adapter = _make_adapter()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_fh = MagicMock()
    mock_client_cls = _mock_aiohttp(status=200, json_data={"status": "connecting"})

    import subprocess as _sp

    patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], \
         patch.object(type(adapter), "_poll_messages", return_value=MagicMock()), \
         patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="npm", timeout=1)):
        result = await adapter.connect()

    assert result is False
