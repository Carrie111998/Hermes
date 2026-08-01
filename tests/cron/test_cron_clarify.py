"""Tests for the cron.allow_clarify human-in-the-loop opt-in.

Default posture (4494c0b0): cron agents run unattended, so the clarify
toolset is hard-disabled and the cron platform hint demands fully autonomous
execution. ``cron.allow_clarify: true`` opts a deployment into clarify
prompts that render through the job's live delivery adapter (gateway-fired
runs only) and block until the user answers or agent.clarify_timeout fires.

Text-reply binding (review round 1): pending entries register under the
DELIVERY CHAT's gateway session key — the same key the gateway's inbound
text intercept resolves typed answers against — so open-ended clarifies,
text fallback, and the native-button "Other" path work from the delivery
chat. Fan-out group targets (no predictable replying user) fall back to the
cron session key; button clicks resolve by clarify_id either way.
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


def _patch_delivery(monkeypatch, adapter, *, targets=None, is_relay=False):
    """Route the job's delivery-target resolution at a fake live transport."""
    if targets is None:
        targets = [{"platform": "discord", "chat_id": "12345"}]
    monkeypatch.setattr(s, "_resolve_delivery_targets", lambda job: targets)
    monkeypatch.setattr(gw_config, "load_gateway_config", lambda: object())
    monkeypatch.setattr(
        gw_delivery, "resolve_delivery_transport",
        lambda platform, config, adapters: types.SimpleNamespace(
            adapter=adapter, config=None, is_relay=is_relay,
        ),
    )


def _run_callback(cb, *args):
    """Invoke a clarify callback on a worker thread; returns (result dict, thread)."""
    result = {}
    caller = threading.Thread(target=lambda: result.setdefault("r", cb(*args)))
    caller.start()
    return result, caller


def _wait_sent(adapter):
    for _ in range(100):
        if adapter.sent:
            return adapter.sent[0]
        time.sleep(0.05)
    raise AssertionError("clarify prompt was never sent")


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
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)
    assert cb is None


def test_callback_none_when_loop_not_running(monkeypatch):
    _patch_delivery(monkeypatch, _FakeAdapter())
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, asyncio.new_event_loop())
    assert cb is None


def test_callback_none_without_capable_adapter(monkeypatch, running_loop):
    _patch_delivery(monkeypatch, object())  # no send_clarify attribute
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)
    assert cb is None


# -- callback behavior ------------------------------------------------------


def test_callback_delivers_prompt_and_returns_response(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Deploy to prod?", ["yes", "no"])
    sent = _wait_sent(adapter)
    assert sent["chat_id"] == "12345"
    assert sent["question"] == "Deploy to prod?"
    # Origin-less job (e.g. CLI-created) delivering to a group channel: no
    # predictable replying user, so registration falls back to the cron key.
    assert sent["session_key"] == "cron:j1"
    assert clarify_gateway.resolve_gateway_clarify(sent["clarify_id"], "yes")
    caller.join(timeout=10)
    assert result["r"] == "yes"


def test_callback_send_failure_returns_sentinel(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=False)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)

    assert cb("Pick one", ["a", "b"]) == "[clarify prompt could not be delivered]"


def test_callback_timeout_returns_sentinel(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 1)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)

    response = cb("Anyone there?", None)
    assert response.startswith("[user did not respond within")


# -- text-reply binding (review round 1) -------------------------------------


def test_open_ended_text_reply_resolves_via_delivery_chat_key(monkeypatch, running_loop):
    """Open-ended clarify to a DM target: the entry registers under the
    delivery chat's gateway session key, so the gateway's inbound text
    intercept resolves a typed answer (previously the entry was indexed by
    the cron session id and typed answers could never match)."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    job = {
        "id": "j-dm",
        "origin": {"platform": "discord", "chat_id": "12345", "chat_type": "dm"},
    }
    cb = s._build_cron_clarify_callback(job, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "What should I do?", None)
    sent = _wait_sent(adapter)
    assert sent["session_key"] == "agent:main:discord:dm:12345"
    assert clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:dm:12345", "ship it"
    )
    caller.join(timeout=10)
    assert result["r"] == "ship it"


def test_other_path_text_reply_resolves_via_group_origin_user_key(monkeypatch, running_loop):
    """Group/channel target that IS the job's origin: the entry binds to the
    origin member's per-user session key (mirrors _seed_cron_channel_session
    key rules), so a typed custom answer after picking "Other" resolves —
    and only from that member's key."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    job = {
        "id": "j-grp",
        "origin": {"platform": "discord", "chat_id": "12345", "user_id": "u-owner"},
    }
    cb = s._build_cron_clarify_callback(job, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Pick one", ["a", "b"])
    sent = _wait_sent(adapter)
    assert sent["session_key"] == "agent:main:discord:group:12345:u-owner"
    # The user picked "Other" — the adapter flips the entry to text-capture.
    assert clarify_gateway.mark_awaiting_text(sent["clarify_id"])
    # A different member's reply keys to a different session and cannot resolve.
    assert not clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:group:12345:u-other", "custom answer"
    )
    # The origin member's typed answer resolves.
    assert clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:group:12345:u-owner", "custom answer"
    )
    caller.join(timeout=10)
    assert result["r"] == "custom answer"


def test_fanout_group_target_falls_back_to_cron_session_key(monkeypatch, running_loop):
    """A group/channel target with no origin (or a fan-out target) cannot
    predict the replying user: registration falls back to the cron session
    key — typed answers don't resolve, but button clicks (key-independent)
    still do."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j-fanout"}, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Q?", ["x", "y"])
    sent = _wait_sent(adapter)
    assert sent["session_key"] == "cron:j-fanout"
    assert clarify_gateway.resolve_gateway_clarify(sent["clarify_id"], "x")
    caller.join(timeout=10)
    assert result["r"] == "x"


def test_thread_target_binds_participant_shared_key(monkeypatch, running_loop):
    """Thread targets key participant-shared (thread_id present), so typed
    replies from any participant resolve."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(
        monkeypatch, adapter,
        targets=[{"platform": "discord", "chat_id": "12345", "thread_id": "T9"}],
    )
    cb = s._build_cron_clarify_callback({"id": "j-thread"}, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Q?", None)
    sent = _wait_sent(adapter)
    assert sent["session_key"] == "agent:main:discord:thread:12345:T9"
    assert sent["metadata"] == {"thread_id": "T9"}
    assert clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:thread:12345:T9", "thread answer"
    )
    caller.join(timeout=10)
    assert result["r"] == "thread answer"


# -- relay transport (review round 1) ----------------------------------------


def test_relay_target_stamps_logical_platform(monkeypatch, running_loop):
    """Relay-fronted delivery: the clarify send carries the job's logical
    platform via the _relay_logical_platform escape hatch — a scheduled send
    has no inbound event to populate the relay's _platform_by_chat map."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(
        monkeypatch, adapter,
        targets=[{"platform": "slack", "chat_id": "C1", "thread_id": "T1"}],
        is_relay=True,
    )
    cb = s._build_cron_clarify_callback({"id": "j-relay"}, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Q?", ["x"])
    sent = _wait_sent(adapter)
    assert sent["metadata"]["_relay_logical_platform"] == "slack"
    assert sent["metadata"]["thread_id"] == "T1"
    # Thread targets still bind participant-shared reply keys over relay.
    assert sent["session_key"] == "agent:main:slack:thread:C1:T1"
    assert clarify_gateway.resolve_gateway_clarify(sent["clarify_id"], "x")
    caller.join(timeout=10)
    assert result["r"] == "x"


def test_native_adapter_send_omits_relay_platform_key(monkeypatch, running_loop):
    """A native (non-relay) adapter gets no _relay_logical_platform stamp."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter, is_relay=False)
    cb = s._build_cron_clarify_callback({"id": "j-native"}, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Q?", ["x"])
    sent = _wait_sent(adapter)
    assert sent["metadata"] is None
    assert clarify_gateway.resolve_gateway_clarify(sent["clarify_id"], "x")
    caller.join(timeout=10)
    assert result["r"] == "x"
