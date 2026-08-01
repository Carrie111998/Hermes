"""Tests for the cron.allow_clarify human-in-the-loop opt-in.

Default posture (4494c0b0): cron agents run unattended, so the clarify
toolset is hard-disabled and the cron platform hint demands fully autonomous
execution. ``cron.allow_clarify: true`` opts a deployment into clarify
prompts that render through the job's live delivery adapter (gateway-fired
runs only) and block until the user answers or agent.clarify_timeout fires.
"""
import asyncio
import threading
import time
import types

import pytest

import cron.scheduler as s
import gateway.config as gw_config
import gateway.delivery as gw_delivery
from tools import clarify_gateway


class _SendResult:
    def __init__(self, success):
        self.success = success


class _FakeAdapter:
    """Stand-in for a live platform adapter with a send_clarify coroutine."""

    def __init__(self, success=True):
        self.success = success
        self.sent = []

    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "question": question,
            "choices": choices,
            "clarify_id": clarify_id,
            "session_key": session_key,
            "metadata": metadata,
        })
        return _SendResult(self.success)


@pytest.fixture
def running_loop():
    """A real asyncio loop running on a background thread (the gateway loop
    stand-in that run_coroutine_threadsafe schedules onto)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if loop.is_running():
            break
        time.sleep(0.01)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _patch_delivery(monkeypatch, adapter):
    """Route the job's delivery-target resolution at a fake live adapter."""
    monkeypatch.setattr(
        s, "_resolve_delivery_targets",
        lambda job: [{"platform": "discord", "chat_id": "12345"}],
    )
    monkeypatch.setattr(gw_config, "load_gateway_config", lambda: object())
    monkeypatch.setattr(
        gw_delivery, "resolve_delivery_transport",
        lambda platform, config, adapters: types.SimpleNamespace(adapter=adapter),
    )


# -- toolset gate ----------------------------------------------------------


def test_clarify_hard_disabled_by_default():
    disabled = s._resolve_cron_disabled_toolsets({})
    assert "clarify" in disabled
    assert "cronjob" in disabled
    assert "messaging" in disabled


def test_clarify_hard_disabled_when_allow_clarify_false():
    disabled = s._resolve_cron_disabled_toolsets({"cron": {"allow_clarify": False}})
    assert "clarify" in disabled


def test_clarify_allowed_when_allow_clarify_true():
    disabled = s._resolve_cron_disabled_toolsets({"cron": {"allow_clarify": True}})
    assert "clarify" not in disabled
    # The other protected toolsets stay disabled.
    assert "cronjob" in disabled
    assert "messaging" in disabled


def test_user_disabled_toolsets_still_layer_on_top():
    cfg = {
        "cron": {"allow_clarify": True},
        "agent": {"disabled_toolsets": ["clarify", "browser"]},
    }
    disabled = s._resolve_cron_disabled_toolsets(cfg)
    # An explicit operator denylist entry still wins over the opt-in.
    assert "clarify" in disabled
    assert "browser" in disabled


# -- callback construction --------------------------------------------------


def test_callback_none_without_delivery_targets(monkeypatch, running_loop):
    monkeypatch.setattr(s, "_resolve_delivery_targets", lambda job: [])
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop, "sess")
    assert cb is None


def test_callback_none_when_loop_not_running(monkeypatch):
    _patch_delivery(monkeypatch, _FakeAdapter())
    cb = s._build_cron_clarify_callback(
        {"id": "j1"}, {}, asyncio.new_event_loop(), "sess",
    )
    assert cb is None


def test_callback_none_without_capable_adapter(monkeypatch, running_loop):
    _patch_delivery(monkeypatch, object())  # no send_clarify attribute
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop, "sess")
    assert cb is None


# -- callback behavior ------------------------------------------------------


def test_callback_delivers_prompt_and_returns_response(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop, "sess-j1")
    assert cb is not None

    result = {}

    def _call():
        result["response"] = cb("Deploy to prod?", ["yes", "no"])

    caller = threading.Thread(target=_call)
    caller.start()
    # Wait for the prompt to go out, then resolve it as the user would.
    for _ in range(100):
        if adapter.sent:
            break
        time.sleep(0.05)
    assert adapter.sent, "clarify prompt was never sent"
    sent = adapter.sent[0]
    assert sent["chat_id"] == "12345"
    assert sent["question"] == "Deploy to prod?"
    assert sent["session_key"] == "sess-j1"
    assert clarify_gateway.resolve_gateway_clarify(sent["clarify_id"], "yes")
    caller.join(timeout=10)
    assert result["response"] == "yes"


def test_callback_send_failure_returns_sentinel(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=False)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop, "sess-j2")

    assert cb("Pick one", ["a", "b"]) == "[clarify prompt could not be delivered]"


def test_callback_timeout_returns_sentinel(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 1)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop, "sess-j3")

    response = cb("Anyone there?", None)
    assert response.startswith("[user did not respond within")
