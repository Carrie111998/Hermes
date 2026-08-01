"""Tests for the cron.allow_clarify human-in-the-loop opt-in.

Default posture (4494c0b0): cron agents run unattended, so the clarify
toolset is hard-disabled and the cron platform hint demands fully autonomous
execution. ``cron.allow_clarify: true`` opts a deployment into clarify
prompts that render through the job's live delivery adapter (gateway-fired
runs only) and block until the user answers or agent.clarify_timeout fires.

Text-reply binding: pending entries register under the DELIVERY CHAT's
gateway session key — the same key the gateway's inbound text intercept
resolves typed answers against. Chat-type resolution is platform-aware
(live-adapter get_chat_info hint → origin stamp → id heuristics), Slack
scope_id is carried, Discord parent:thread targets remap to the thread id,
and fan-out group targets fall back to the cron session key (buttons resolve
by clarify_id either way).
"""
import asyncio
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as s
import gateway.config as gw_config
import gateway.delivery as gw_delivery
import gateway.session_context as session_context
import tools.cronjob_tools as cronjob_tools
from gateway.config import Platform
from gateway.session import SessionSource
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


class _ChatInfoAdapter(_FakeAdapter):
    """Fake adapter that also answers get_chat_info (the fire-time DM hint)."""

    def __init__(self, chat_type, success=True):
        super().__init__(success=success)
        self._chat_type = chat_type

    async def get_chat_info(self, chat_id):
        return {"name": "chat", "type": self._chat_type}


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
    """Invoke a clarify callback on a daemon worker thread."""
    result = {}
    caller = threading.Thread(
        target=lambda: result.setdefault("r", cb(*args)), daemon=True,
    )
    caller.start()
    return result, caller


def _wait_sent(adapter):
    for _ in range(100):
        if adapter.sent:
            return adapter.sent[0]
        time.sleep(0.05)
    raise AssertionError("clarify prompt was never sent")


def _origin_via_env(monkeypatch, *, platform, chat_id, user_id=None,
                    thread_id=None, chat_name=None, source=None):
    """Build a job origin through the REAL ``_origin_from_env`` path, so
    tests see the exact shape production stamps (including the round-2
    chat_type/scope_id fields from the bound session source)."""
    env = {
        "HERMES_SESSION_PLATFORM": platform,
        "HERMES_SESSION_CHAT_ID": chat_id,
        "HERMES_SESSION_CHAT_NAME": chat_name or "",
        "HERMES_SESSION_THREAD_ID": thread_id or "",
        "HERMES_SESSION_USER_ID": user_id or "",
        "HERMES_SESSION_SOURCE": source,
    }
    monkeypatch.setattr(
        session_context, "get_session_env",
        lambda name, default="": env.get(name) or default,
    )
    return cronjob_tools._origin_from_env()


class _GwCfg:
    """Gateway-config stand-in with the session-key flags build_session_key reads."""

    def __init__(self, *, group_sessions_per_user=True, thread_sessions_per_user=False):
        self.group_sessions_per_user = group_sessions_per_user
        self.thread_sessions_per_user = thread_sessions_per_user


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


# -- reply-key matrix (direct helper tests) ----------------------------------


def test_key_discord_dm_via_origin_chat_type_stamp(monkeypatch):
    """Chat-created Discord DM job: origin carries chat_type from the bound
    session source (stamped by _origin_from_env), keying `…:dm:<chat>`."""
    origin = _origin_via_env(
        monkeypatch, platform="discord", chat_id="8801234567",
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="8801234567", chat_type="dm",
        ),
    )
    assert origin["chat_type"] == "dm"
    job = {"id": "j", "origin": origin}
    target = {"platform": "discord", "chat_id": "8801234567"}
    key = s._cron_clarify_reply_session_key(job, target, Platform.DISCORD, _GwCfg())
    assert key == "agent:main:discord:dm:8801234567"


def test_key_discord_dm_numeric_id_via_adapter_hint():
    """Old jobs carry no origin chat_type and Discord snowflake ids don't
    encode DM-vs-channel — the live adapter's get_chat_info hint decides."""
    job = {"id": "j", "origin": {"platform": "discord", "chat_id": "8801234567"}}
    target = {"platform": "discord", "chat_id": "8801234567"}
    key = s._cron_clarify_reply_session_key(
        job, target, Platform.DISCORD, _GwCfg(), chat_type_hint="dm",
    )
    assert key == "agent:main:discord:dm:8801234567"
    # A guild channel hint binds the group key shape instead (no user → None).
    key_group = s._cron_clarify_reply_session_key(
        job, target, Platform.DISCORD, _GwCfg(), chat_type_hint="group",
    )
    assert key_group is None


def test_key_slack_scope_id_carried_from_origin():
    job = {
        "id": "j",
        "origin": {
            "platform": "slack", "chat_id": "D123", "chat_type": "dm",
            "scope_id": "T123", "user_id": "U1",
        },
    }
    target = {"platform": "slack", "chat_id": "D123"}
    key = s._cron_clarify_reply_session_key(job, target, Platform.SLACK, _GwCfg())
    assert key == "agent:main:slack:dm:T123:D123"


def test_key_telegram_dm_and_group_from_id_shape():
    dm_job = {"id": "j", "origin": {"platform": "telegram", "chat_id": "421337"}}
    dm_target = {"platform": "telegram", "chat_id": "421337"}
    assert s._cron_clarify_reply_session_key(
        dm_job, dm_target, Platform.TELEGRAM, _GwCfg()
    ) == "agent:main:telegram:dm:421337"
    # Negative ids are groups — per-user sessions make the key unpredictable.
    grp_job = {"id": "j", "origin": {"platform": "telegram", "chat_id": "-1009"}}
    grp_target = {"platform": "telegram", "chat_id": "-1009"}
    assert s._cron_clarify_reply_session_key(
        grp_job, grp_target, Platform.TELEGRAM, _GwCfg()
    ) is None


def test_key_discord_parent_thread_target_remaps_to_thread_id():
    """Explicit parent:thread target: inbound thread messages stamp
    chat_id = thread_id, so the key must be thread:<T>:<T>."""
    job = {"id": "j"}
    target = {"platform": "discord", "chat_id": "P1", "thread_id": "T9"}
    key = s._cron_clarify_reply_session_key(job, target, Platform.DISCORD, _GwCfg())
    assert key == "agent:main:discord:thread:T9:T9"


def test_key_telegram_forum_topic():
    job = {"id": "j"}
    target = {"platform": "telegram", "chat_id": "-1009", "thread_id": "55"}
    key = s._cron_clarify_reply_session_key(job, target, Platform.TELEGRAM, _GwCfg())
    assert key == "agent:main:telegram:forum:-1009:55"


def test_key_slack_thread_keys_as_container_plus_thread_id():
    job = {
        "id": "j",
        "origin": {"platform": "slack", "chat_id": "D123", "scope_id": "T123"},
    }
    target = {"platform": "slack", "chat_id": "D123", "thread_id": "1700.01"}
    key = s._cron_clarify_reply_session_key(job, target, Platform.SLACK, _GwCfg())
    assert key == "agent:main:slack:dm:T123:D123:1700.01"


def test_key_none_when_thread_sessions_per_user_isolates():
    """thread_sessions_per_user: true makes thread replies per-user — the
    replying user is unpredictable for a scheduled prompt."""
    job = {"id": "j"}
    target = {"platform": "discord", "chat_id": "P1", "thread_id": "T9"}
    key = s._cron_clarify_reply_session_key(
        job, target, Platform.DISCORD,
        _GwCfg(thread_sessions_per_user=True),
    )
    assert key is None


# -- callback construction --------------------------------------------------


def test_callback_none_without_delivery_targets(monkeypatch, running_loop):
    monkeypatch.setattr(s, "_resolve_delivery_targets", lambda job: [])
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)
    assert cb is None


def test_callback_none_when_loop_not_running(monkeypatch):
    _patch_delivery(monkeypatch, _FakeAdapter())
    loop = asyncio.new_event_loop()
    try:
        cb = s._build_cron_clarify_callback({"id": "j1"}, {}, loop)
        assert cb is None
    finally:
        loop.close()


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


def test_callback_send_failure_returns_sentinel_and_discards_only_own_entry(
    monkeypatch, running_loop
):
    """Send failure drops ONLY the cron entry — a concurrent interactive
    clarify registered under the same session key must survive (round-2:
    was clear_session, which cancelled everything in the chat)."""
    adapter = _FakeAdapter(success=False)
    _patch_delivery(monkeypatch, adapter)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)

    # An unrelated interactive prompt pending in the same fallback session.
    clarify_gateway.register(
        clarify_id="interactive1", session_key="cron:j1",
        question="unrelated", choices=None,
    )
    assert cb("Pick one", ["a", "b"]) == "[clarify prompt could not be delivered]"
    # The cron entry is gone; the interactive one is untouched.
    assert clarify_gateway.get_pending_for_session("cron:j1") is not None
    assert clarify_gateway.discard("interactive1")


def test_callback_timeout_returns_sentinel_with_seconds(monkeypatch, running_loop):
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 1)
    cb = s._build_cron_clarify_callback({"id": "j1"}, {}, running_loop)

    response = cb("Anyone there?", None)
    # Sub-minute timeouts render seconds, never "0m".
    assert response == "[user did not respond within 1s]"


# -- text-reply binding ------------------------------------------------------


def test_open_ended_text_reply_resolves_via_delivery_chat_key(monkeypatch, running_loop):
    """Open-ended clarify to a DM target: the entry registers under the
    delivery chat's gateway session key, so the gateway's inbound text
    intercept resolves a typed answer. Origin built through the real
    _origin_from_env path (production shape)."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    origin = _origin_via_env(
        monkeypatch, platform="discord", chat_id="12345",
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="12345", chat_type="dm",
        ),
    )
    job = {"id": "j-dm", "origin": origin}
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


def test_discord_dm_numeric_id_binds_via_live_adapter_get_chat_info(
    monkeypatch, running_loop
):
    """End-to-end DM detection for old jobs (no origin chat_type) on
    Discord: the builder's fire-time get_chat_info call classifies the
    numeric target as a DM and typed answers bind `…:dm:<chat>`."""
    adapter = _ChatInfoAdapter("dm", success=True)
    _patch_delivery(
        monkeypatch, adapter,
        targets=[{"platform": "discord", "chat_id": "8801234567"}],
    )
    job = {"id": "j-ddm", "origin": {"platform": "discord", "chat_id": "8801234567"}}
    cb = s._build_cron_clarify_callback(job, {}, running_loop)
    assert cb is not None

    result, caller = _run_callback(cb, "Q?", None)
    sent = _wait_sent(adapter)
    assert sent["session_key"] == "agent:main:discord:dm:8801234567"
    assert clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:dm:8801234567", "blue"
    )
    caller.join(timeout=10)
    assert result["r"] == "blue"


def test_other_path_text_reply_resolves_via_group_origin_user_key(monkeypatch, running_loop):
    """Group/channel target that IS the job's origin: the entry binds to the
    origin member's per-user session key — a typed custom answer after
    "Other" resolves only from that member's key."""
    adapter = _FakeAdapter(success=True)
    _patch_delivery(monkeypatch, adapter)
    origin = _origin_via_env(
        monkeypatch, platform="discord", chat_id="12345", user_id="u-owner",
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="12345", chat_type="group",
        ),
    )
    job = {"id": "j-grp", "origin": origin}
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
    assert sent["session_key"] == "agent:main:discord:thread:T9:T9"
    assert sent["metadata"] == {"thread_id": "T9"}
    assert clarify_gateway.resolve_text_response_for_session(
        "agent:main:discord:thread:T9:T9", "thread answer"
    )
    caller.join(timeout=10)
    assert result["r"] == "thread answer"


# -- relay transport ---------------------------------------------------------


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


# -- run_job wiring (gate + attach + platform-hint swap) ----------------------


def _run_job_for_wiring(tmp_path, *, allow_clarify, preset_hints=None, spy=None):
    """Drive run_job with the standard test patch bundle plus the clarify
    delivery seams; return the constructed agent mock."""
    fake_db = MagicMock()
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "ok"}
    if preset_hints is not None:
        mock_agent._platform_hint_overrides = preset_hints
    adapter = _FakeAdapter(success=True)
    loop = MagicMock()
    loop.is_running.return_value = True
    job = {"id": "j-wire", "name": "t", "prompt": "hello"}
    builder_patch = (
        patch.object(s, "_build_cron_clarify_callback", spy) if spy is not None
        else patch.object(gw_config, "load_gateway_config", lambda: object())
    )
    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", return_value=mock_agent),
        patch.object(s, "load_config",
                     return_value={"cron": {"allow_clarify": allow_clarify}}),
        patch.object(s, "_resolve_delivery_targets",
                     return_value=[{"platform": "discord", "chat_id": "12345"}]),
        patch.object(
            gw_delivery, "resolve_delivery_transport",
            lambda platform, config, adapters: types.SimpleNamespace(
                adapter=adapter, config=None, is_relay=False,
            ),
        ),
        builder_patch,
    ):
        s.run_job(job, adapters={"live": adapter}, loop=loop)
    return mock_agent


def test_run_job_attaches_clarify_callback_and_swaps_hint(tmp_path):
    """The wiring block runs for gated gateway-fired jobs: callback attached
    AND the autonomous cron platform hint swapped for the HITL variant."""
    agent = _run_job_for_wiring(tmp_path, allow_clarify=True)
    assert callable(agent.clarify_callback)
    assert agent._platform_hint_overrides == {
        "cron": {"replace": s._CRON_CLARIFY_PLATFORM_HINT}
    }


def test_run_job_operator_hint_override_wins(tmp_path):
    """An explicit agent.platform_hints.cron override is never replaced."""
    agent = _run_job_for_wiring(
        tmp_path, allow_clarify=True,
        preset_hints={"cron": {"replace": "custom-cron-hint"}},
    )
    assert callable(agent.clarify_callback)
    assert agent._platform_hint_overrides == {"cron": {"replace": "custom-cron-hint"}}


def test_run_job_gate_off_attaches_nothing(tmp_path):
    """Default posture: no callback, no hint swap."""
    spy = MagicMock()
    agent = _run_job_for_wiring(tmp_path, allow_clarify=False, spy=spy)
    spy.assert_not_called()
    assert not isinstance(agent._platform_hint_overrides, dict)
