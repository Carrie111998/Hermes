"""Unit tests for acp_adapter.keepalive.TurnKeepalive."""
import time
from unittest.mock import MagicMock

import pytest

from acp_adapter.keepalive import (
    TurnKeepalive,
    get_keepalive_interval,
    make_turn_keepalive,
)


class DummyConn:
    def __init__(self):
        self.calls = []

    def session_update(self, session_id, update):
        self.calls.append((session_id, update))


class DummyLoop:
    pass


def _payload():
    return "payload"


def test_fires_after_interval():
    mock = MagicMock()
    k = TurnKeepalive(DummyConn(), "s1", DummyLoop(), interval_s=0.1, payload_factory=_payload)
    # Replace network-facing _run entirely: use the real loop but stub send.
    k_orig_send = "acp_adapter.keepalive._send_update"
    # Just count fires by monkeypatching _send_update via the module.
    import acp_adapter.keepalive as mod
    orig = mod._send_update
    mod._send_update = lambda *a, **kw: mock()
    try:
        k.start()
        time.sleep(0.35)
        assert mock.call_count >= 3, f"expected >=3 calls, got {mock.call_count}"
    finally:
        k.stop()
        mod._send_update = orig


def test_mark_activity_resets_timer():
    import acp_adapter.keepalive as mod
    calls = []
    orig = mod._send_update
    mod._send_update = lambda *a, **kw: calls.append(time.time())
    k = TurnKeepalive(DummyConn(), "s1", DummyLoop(), interval_s=0.1, payload_factory=_payload)
    try:
        k.start()
        # Repeatedly extend the deadline before it can fire.
        for _ in range(4):
            time.sleep(0.05)
            k.mark_activity()
        assert not calls, "should not have fired while activity kept resetting"
        time.sleep(0.20)
        assert calls, "should have fired after resets stopped"
    finally:
        k.stop()
        mod._send_update = orig


def test_stop_prevents_further_fires():
    import acp_adapter.keepalive as mod
    calls = []
    orig = mod._send_update
    mod._send_update = lambda *a, **kw: calls.append(time.time())
    k = TurnKeepalive(DummyConn(), "s1", DummyLoop(), interval_s=0.1, payload_factory=_payload)
    try:
        k.start()
        time.sleep(0.15)
        k.stop()
        c1 = len(calls)
        time.sleep(0.3)
        assert len(calls) == c1, "no further fires after stop"
    finally:
        k.stop()
        mod._send_update = orig


def test_stop_is_idempotent():
    k = TurnKeepalive(DummyConn(), "sess", DummyLoop(), interval_s=0.1)
    k.start()
    k.stop()
    k.stop()  # must not raise


def test_start_is_idempotent():
    import acp_adapter.keepalive as mod
    calls = []
    orig = mod._send_update
    mod._send_update = lambda *a, **kw: calls.append(1)
    k = TurnKeepalive(DummyConn(), "s", DummyLoop(), interval_s=0.1, payload_factory=_payload)
    try:
        k.start()
        k.start()  # second start must be a no-op — single worker thread
        assert k._thread is not None
        thread_id = k._thread.ident
        k.start()
        assert k._thread.ident == thread_id, "start() must not spawn extra threads"
        time.sleep(0.25)
        assert 1 <= len(calls) <= 3, "multiple uncoordinated timers present?"
    finally:
        k.stop()
        mod._send_update = orig


def test_env_disable(monkeypatch):
    monkeypatch.setenv("HERMES_ACP_KEEPALIVE_INTERVAL_S", "0")
    assert make_turn_keepalive(DummyConn(), "sess", DummyLoop()) is None


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("HERMES_ACP_KEEPALIVE_INTERVAL_S", "12.5")
    assert get_keepalive_interval() == pytest.approx(12.5)


def test_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_ACP_KEEPALIVE_INTERVAL_S", "not-a-number")
    # No config value expected in test env → falls to default 45.
    assert get_keepalive_interval(default=45.0) == 45.0


def test_default_payload_is_valid_agent_message_chunk():
    from acp.schema import AgentMessageChunk, TextContentBlock

    payload = TurnKeepalive._default_payload_factory()
    assert isinstance(payload, AgentMessageChunk)
    assert payload.session_update == "agent_message_chunk"
    assert isinstance(payload.content, TextContentBlock)
    assert payload.content.text == ""
