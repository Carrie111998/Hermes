"""Canonical request identity used by short-task Kanban control.

The trusted tool path reads request-scoped ``HERMES_SESSION_*`` ContextVars,
so the gateway must bind canonical platform IDs and the exact inbound delivery
ID for one turn, then explicitly clear every value before the next turn.
"""
from __future__ import annotations

from types import SimpleNamespace
import json

import pytest

import gateway.run as gateway_run
import gateway.session_context as session_context
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    _SESSION_ASYNC_DELIVERY,
    _UNSET,
    _VAR_MAP,
    get_session_env,
)


_CONTROL_ENV_NAMES = (
    "HERMES_SESSION_CHAT_ID_ALT",
    "HERMES_SESSION_SCOPE_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_USER_ID_ALT",
    "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_PROFILE",
    "HERMES_SESSION_INTERNAL",
)


@pytest.fixture(autouse=True)
def _isolate_session_context():
    saved_values = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_async = _SESSION_ASYNC_DELIVERY.get()
    saved_engaged = session_context._session_context_engaged
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        yield
    finally:
        for name, value in saved_values.items():
            _VAR_MAP[name].set(value)
        _SESSION_ASYNC_DELIVERY.set(saved_async)
        session_context._session_context_engaged = saved_engaged


def _feishu_source(*, profile: str | None = "source-profile") -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_transport_chat",
        chat_id_alt="oc_canonical_chat",
        scope_id="tenant_scope",
        chat_type="group",
        user_id="ou_transport_user",
        user_id_alt="union_canonical_user",
        user_name="operator",
        thread_id="thread-1",
        message_id="source-message",
        profile=profile,
    )


def test_new_control_fields_preserve_legacy_positional_session_signature():
    """Appending control fields must not reinterpret existing positional calls."""
    tokens = session_context.set_session_vars(
        "feishu",
        "tool",
        "legacy-chat",
        "Legacy Chat",
        "legacy-thread",
        "legacy-user",
        "Legacy User",
        "legacy-session-key",
        "legacy-session-id",
        "legacy-message",
        "legacy-profile",
        "/legacy/cwd",
        False,
        "legacy-ui-session",
    )
    try:
        assert get_session_env("HERMES_SESSION_CHAT_ID") == "legacy-chat"
        assert get_session_env("HERMES_SESSION_CHAT_NAME") == "Legacy Chat"
        assert get_session_env("HERMES_SESSION_THREAD_ID") == "legacy-thread"
        assert get_session_env("HERMES_SESSION_USER_ID") == "legacy-user"
        assert get_session_env("HERMES_SESSION_USER_NAME") == "Legacy User"
        assert get_session_env("HERMES_SESSION_KEY") == "legacy-session-key"
        assert get_session_env("HERMES_SESSION_ID") == "legacy-session-id"
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "legacy-message"
        assert get_session_env("HERMES_SESSION_PROFILE") == "legacy-profile"
        assert get_session_env("HERMES_UI_SESSION_ID") == "legacy-ui-session"
        assert session_context.async_delivery_supported() is False
        assert get_session_env("HERMES_SESSION_CHAT_ID_ALT") == ""
        assert get_session_env("HERMES_SESSION_INTERNAL") == ""
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize(
    ("multiplex", "source_profile", "expected_profile"),
    [
        (True, "coder", "coder"),
        (False, "must-be-ignored", "resolved-profile"),
    ],
)
def test_canonical_control_identity_propagates_and_clears(
    monkeypatch,
    multiplex,
    source_profile,
    expected_profile,
):
    """Canonical alt IDs and the resolved profile last exactly one turn."""
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=multiplex)
    runner._active_profile_name = lambda: "resolved-profile"
    source = _feishu_source(profile=source_profile)
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:coder:feishu:group:oc_canonical_chat:union_canonical_user",
    )
    context._inbound_message_id = "event-message"

    # A completed sibling turn may have left process-global compatibility
    # values. ContextVars must override them while bound and suppress them after
    # explicit clear rather than falling back to the stale identity.
    for name in _CONTROL_ENV_NAMES:
        monkeypatch.setenv(name, f"stale:{name}")
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    tokens = runner._set_session_env(context)
    try:
        assert {
            name: get_session_env(name) for name in _CONTROL_ENV_NAMES
        } == {
            "HERMES_SESSION_CHAT_ID_ALT": "oc_canonical_chat",
            "HERMES_SESSION_SCOPE_ID": "tenant_scope",
            "HERMES_SESSION_CHAT_TYPE": "group",
            "HERMES_SESSION_USER_ID_ALT": "union_canonical_user",
            "HERMES_SESSION_MESSAGE_ID": "event-message",
            "HERMES_SESSION_PROFILE": expected_profile,
            "HERMES_SESSION_INTERNAL": "",
        }

        from tools.kanban_tools import _trusted_gateway_control_identity

        assert _trusted_gateway_control_identity() == {
            "platform": "feishu",
            "scope_id": "tenant_scope",
            "chat_type": "group",
            "chat_id": "oc_canonical_chat",
            "thread_id": "thread-1",
            "user_id": "union_canonical_user",
            "notifier_profile": expected_profile,
            "session_key": context.session_key,
            "message_id": "event-message",
        }
    finally:
        runner._clear_session_env(tokens)

    assert {name: get_session_env(name) for name in _CONTROL_ENV_NAMES} == {
        name: "" for name in _CONTROL_ENV_NAMES
    }
    assert _trusted_gateway_control_identity() is None


class _CapturedContext(Exception):
    pass


class _SessionStoreStub:
    def __init__(self, store):
        self._store = store

    async def get_or_create_session(self, _source):
        return SimpleNamespace(
            session_key="agent:default:feishu:group:oc_canonical_chat",
            session_id="session-1",
            created_at=1,
            updated_at=2,
            was_auto_reset=False,
            is_fresh_reset=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "event_message_id",
        "source_message_id",
        "update_id",
        "event_internal",
        "expected",
    ),
    [
        ("event-message", "source-message", 73, False, "event-message"),
        (None, "source-message", 73, False, "source-message"),
        (None, None, 73, False, "update:73"),
        ("internal-message", "source-message", 74, True, "internal-message"),
    ],
)
async def test_inbound_message_id_precedence_reaches_session_context(
    monkeypatch,
    event_message_id,
    source_message_id,
    update_id,
    event_internal,
    expected,
):
    """Exercise the real gateway funnel through its session-bind boundary."""
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner.session_store = object()
    runner._async_session_store = _SessionStoreStub(runner.session_store)
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda *_args: None
    runner._is_telegram_topic_lane = lambda _source: False

    source = _feishu_source(profile=None)
    source.message_id = source_message_id
    event = MessageEvent(
        text="continue",
        source=source,
        message_id=event_message_id,
        platform_update_id=update_id,
        internal=event_internal,
    )
    captured = {}

    def build_context(bound_source, _config, session_entry):
        return SessionContext(
            source=bound_source,
            connected_platforms=[],
            home_channels={},
            session_key=session_entry.session_key,
            session_id=session_entry.session_id,
        )

    def capture_session_bind(context):
        captured["message_id"] = context._inbound_message_id
        captured["internal"] = context._inbound_internal
        raise _CapturedContext

    monkeypatch.setattr(gateway_run, "build_session_context", build_context)
    runner._set_session_env = capture_session_bind

    with pytest.raises(_CapturedContext):
        await runner._handle_message_with_agent(event, source, "quick-key", 1)

    assert captured["message_id"] == expected
    assert captured["internal"] is event_internal


def _scope_config(tmp_path):
    workspace = tmp_path / "short-task-control-workspace"
    workspace.mkdir(exist_ok=True)
    return {
        "agent": {"max_turns": 90},
        "terminal": {"backend": "local"},
        "toolsets": ["kanban"],
        "kanban": {
            "failure_limit": 2,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_workspace_roots": [str(workspace.resolve())],
                "allowed_origins": [
                    {
                        "platform": "feishu",
                        "chat_type": "group",
                        "chat_id": "oc_canonical_chat",
                        "user_id": "union_canonical_user",
                    }
                ],
            },
        },
    }


def test_model_control_surface_is_visible_only_in_exact_allowed_session(
    monkeypatch,
    tmp_path,
):
    from agent import kanban_handoff_scope as handoff_scope
    from tools import kanban_tools as kanban_tools

    config = _scope_config(tmp_path)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_tools, "load_config", lambda: config)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )

    allowed_tokens = session_context.set_session_vars(
        platform="feishu",
        scope_id="tenant_scope",
        chat_type="group",
        chat_id_alt="oc_canonical_chat",
        thread_id="thread-1",
        user_id_alt="union_canonical_user",
        session_key="agent:default:feishu:group:oc_canonical_chat:user",
        message_id="message-1",
        profile="default",
    )
    try:
        assert kanban_tools._kanban_control_mode_state() == "enabled"
    finally:
        session_context.clear_session_vars(allowed_tokens)

    denied_tokens = session_context.set_session_vars(
        platform="feishu",
        scope_id="tenant_scope",
        chat_type="group",
        chat_id_alt="oc_canonical_chat",
        thread_id="thread-1",
        user_id_alt="another-user",
        session_key="agent:default:feishu:group:oc_canonical_chat:other",
        message_id="message-2",
        profile="default",
    )
    try:
        assert kanban_tools._kanban_control_mode_state() == "disabled"
    finally:
        session_context.clear_session_vars(denied_tokens)


def test_real_tool_schema_never_reuses_control_visibility_across_sources(
    monkeypatch,
    tmp_path,
):
    """Exercise both cache layers without clearing between source changes."""
    from agent import kanban_handoff_scope as handoff_scope
    from model_tools import _clear_tool_defs_cache, _tool_defs_cache, get_tool_definitions
    from tools import kanban_tools
    from tools.registry import invalidate_check_fn_cache

    config = _scope_config(tmp_path)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_tools, "load_config", lambda: config)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    def visible(*, user_id: str, session_key: str, message_id: str) -> bool:
        tokens = session_context.set_session_vars(
            platform="feishu",
            scope_id="tenant_scope",
            chat_type="group",
            chat_id_alt="oc_canonical_chat",
            thread_id="thread-1",
            user_id_alt=user_id,
            session_key=session_key,
            message_id=message_id,
            profile="default",
        )
        try:
            definitions = get_tool_definitions(
                enabled_toolsets=["kanban"],
                quiet_mode=True,
            )
            names = {item["function"]["name"] for item in definitions}
            return "kanban_control" in names
        finally:
            session_context.clear_session_vars(tokens)

    # denied -> allowed, then allowed -> denied, followed by a second exact
    # authorized session. No cache is cleared between calls.
    assert visible(
        user_id="another-user",
        session_key="denied-session-1",
        message_id="message-1",
    ) is False
    assert visible(
        user_id="union_canonical_user",
        session_key="allowed-session-1",
        message_id="message-2",
    ) is True
    assert visible(
        user_id="another-user",
        session_key="denied-session-2",
        message_id="message-3",
    ) is False
    assert visible(
        user_id="union_canonical_user",
        session_key="allowed-session-2",
        message_id="message-4",
    ) is True

    # This schema class is intentionally not stored in the process-wide defs
    # cache. Ordinary tool schemas and ordinary external check_fn probes retain
    # their established caches.
    assert _tool_defs_cache == {}

    # Clearing ContextVars must suppress stale process-level compatibility
    # values, not resurrect authority from os.environ.
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "feishu")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID_ALT", "oc_canonical_chat")
    monkeypatch.setenv("HERMES_SESSION_USER_ID_ALT", "union_canonical_user")
    monkeypatch.setenv("HERMES_SESSION_KEY", "stale-authorized-session")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "stale-message")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "default")
    cleared = get_tool_definitions(enabled_toolsets=["kanban"], quiet_mode=True)
    assert "kanban_control" not in {
        item["function"]["name"] for item in cleared
    }

    # Even if a stale caller retained the schema from the preceding authorized
    # request, the registered handler performs the same current-source check
    # again before opening or mutating any board.
    entry = kanban_tools.registry.get_entry("kanban_control")
    assert entry is not None
    refused = json.loads(
        entry.handler(
            {"task_id": "t_not_reached", "kind": "stop", "message": "stop"}
        )
    )
    assert "error" in refused
    assert "authenticated gateway session" in refused["error"]


def _prepare_model_create_scope(monkeypatch, config):
    from agent import kanban_handoff_scope as handoff_scope
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_tools, "load_config", lambda: config)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kanban_tools, kb


def _model_create_db_snapshot(kb):
    tables = (
        "tasks",
        "task_events",
        "task_runs",
        "kanban_notify_subs",
        "kanban_control_bindings",
    )
    with kb.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


@pytest.mark.parametrize(
    "missing_field",
    ["session_key", "message_id", "notifier_profile", "thread_id"],
)
def test_model_create_from_intended_source_with_incomplete_proof_is_zero_write(
    monkeypatch,
    tmp_path,
    missing_field,
):
    """The model handler cannot downgrade an intended controlled launch."""
    config = _scope_config(tmp_path)
    if missing_field == "thread_id":
        config["kanban"]["short_task_handoff"]["allowed_origins"][0][
            "thread_id"
        ] = "thread-1"
    kanban_tools, kb = _prepare_model_create_scope(monkeypatch, config)
    values = {
        "platform": "feishu",
        "scope_id": "tenant_scope",
        "chat_type": "group",
        "chat_id_alt": "oc_canonical_chat",
        "thread_id": "thread-1",
        "user_id_alt": "union_canonical_user",
        "session_key": "allowed-session",
        "message_id": "create-message",
        "profile": "default",
    }
    values["profile" if missing_field == "notifier_profile" else missing_field] = ""
    tokens = session_context.set_session_vars(**values)
    try:
        before = _model_create_db_snapshot(kb)

        result = json.loads(
            kanban_tools._handle_create(
                {
                    "title": "must not be downgraded",
                    "assignee": "default",
                    "_hermes_creation_slot": "tool:0",
                }
            )
        )
        after = _model_create_db_snapshot(kb)
    finally:
        session_context.clear_session_vars(tokens)

    assert "error" in result
    assert "no task was created" in result["error"]
    assert after == before


def test_model_create_from_internal_allowed_source_is_zero_write(
    monkeypatch,
    tmp_path,
):
    config = _scope_config(tmp_path)
    kanban_tools, kb = _prepare_model_create_scope(monkeypatch, config)
    tokens = session_context.set_session_vars(
        platform="feishu",
        scope_id="tenant_scope",
        chat_type="group",
        chat_id_alt="oc_canonical_chat",
        thread_id="thread-1",
        user_id_alt="union_canonical_user",
        session_key="allowed-session",
        message_id="copied-message",
        profile="default",
        internal=True,
    )
    try:
        before = _model_create_db_snapshot(kb)
        result = json.loads(
            kanban_tools._handle_create(
                {
                    "title": "synthetic event must not create",
                    "assignee": "default",
                    "_hermes_creation_slot": "tool:0",
                }
            )
        )
        after = _model_create_db_snapshot(kb)
    finally:
        session_context.clear_session_vars(tokens)

    assert "error" in result
    assert "no task was created" in result["error"]
    assert after == before


def test_model_create_from_true_nonallowlisted_source_stays_ordinary(
    monkeypatch,
    tmp_path,
):
    config = _scope_config(tmp_path)
    kanban_tools, kb = _prepare_model_create_scope(monkeypatch, config)
    tokens = session_context.set_session_vars(
        platform="feishu",
        scope_id="tenant_scope",
        chat_type="group",
        chat_id_alt="oc_canonical_chat",
        thread_id="thread-1",
        user_id_alt="different-user",
        session_key="ordinary-session",
        message_id="ordinary-message",
        profile="default",
    )
    try:
        result = json.loads(
            kanban_tools._handle_create(
                {"title": "ordinary task", "assignee": "default"}
            )
        )
    finally:
        session_context.clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["controllable"] is False
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_model_create_from_exact_allowed_source_is_control_bound(
    monkeypatch,
    tmp_path,
):
    config = _scope_config(tmp_path)
    kanban_tools, kb = _prepare_model_create_scope(monkeypatch, config)
    tokens = session_context.set_session_vars(
        platform="feishu",
        scope_id="tenant_scope",
        chat_type="group",
        chat_id_alt="oc_canonical_chat",
        thread_id="thread-1",
        user_id_alt="union_canonical_user",
        session_key="allowed-session",
        message_id="allowed-message",
        profile="default",
    )
    try:
        result = json.loads(
            kanban_tools._handle_create(
                {
                    "title": "controlled task",
                    "assignee": "default",
                    "workspace_kind": "dir",
                    "workspace_path": config["kanban"]["short_task_handoff"][
                        "allowed_workspace_roots"
                    ][0],
                    "_hermes_creation_slot": "tool:0",
                }
            )
        )
    finally:
        session_context.clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["controllable"] is True
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_control_bindings WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_overrides", "expect_authorized"),
    [
        ({}, True),
        ({"platform": "weixin"}, False),
        ({"chat_id": "another-group"}, False),
        ({"user_id": "another-user"}, False),
    ],
)
async def test_gateway_create_freezes_policy_only_for_allowed_origin(
    monkeypatch,
    tmp_path,
    identity_overrides,
    expect_authorized,
):
    from agent import kanban_auto_handoff
    from agent import kanban_handoff_scope as handoff_scope
    from hermes_cli import kanban as kanban_cli

    config = _scope_config(tmp_path)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    captured = {}

    def fake_run_slash(
        text,
        *,
        control_origin=None,
        mutation_identity=None,
    ):
        captured["text"] = text
        captured["control_origin"] = control_origin
        captured["mutation_identity"] = mutation_identity
        return "created for test"

    monkeypatch.setattr(kanban_cli, "run_slash", fake_run_slash)

    runner = object.__new__(GatewayRunner)
    runner._kanban_handoff_policy_for_source = lambda _source: (
        kanban_auto_handoff.build_dispatcher_policy_snapshot(config)
    )
    identity = {
        "platform": "feishu",
        "scope_id": "tenant_scope",
        "chat_type": "group",
        "chat_id": "oc_canonical_chat",
        "thread_id": "thread-1",
        "user_id": "union_canonical_user",
        "notifier_profile": "default",
        "session_key": "agent:default:feishu:group:oc_canonical_chat:user",
        "message_id": "create-message",
    }
    identity.update(identity_overrides)

    async def trusted_identity(_event):
        return dict(identity)

    runner._trusted_kanban_control_identity = trusted_identity
    event = MessageEvent(
        text='/kanban create "synthetic" --assignee default',
        source=_feishu_source(),
        message_id="create-message",
    )

    await GatewayRunner._handle_kanban_command(runner, event)

    if expect_authorized:
        control_origin = captured["control_origin"]
        assert control_origin["operation_slot"] == "slash"
        frozen = json.loads(control_origin["short_handoff_policy"])
        assert frozen["origin"]["chat_id"] == "oc_canonical_chat"
        assert frozen["origin"]["user_id"] == "union_canonical_user"
    else:
        assert captured["control_origin"] is None
    assert captured["mutation_identity"] == identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["session_key", "message_id", "notifier_profile"],
)
async def test_allowed_source_with_incomplete_delivery_identity_creates_nothing(
    monkeypatch,
    tmp_path,
    missing_field,
):
    from agent import kanban_auto_handoff
    from agent import kanban_handoff_scope as handoff_scope
    from hermes_cli import kanban as kanban_cli

    config = _scope_config(tmp_path)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    calls = []
    monkeypatch.setattr(
        kanban_cli,
        "run_slash",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "unexpected",
    )
    runner = object.__new__(GatewayRunner)
    runner._kanban_handoff_policy_for_source = lambda _source: (
        kanban_auto_handoff.build_dispatcher_policy_snapshot(config)
    )
    identity = {
        "platform": "feishu",
        "scope_id": "tenant_scope",
        "chat_type": "group",
        "chat_id": "oc_canonical_chat",
        "thread_id": "thread-1",
        "user_id": "union_canonical_user",
        "notifier_profile": "default",
        "session_key": "allowed-session",
        "message_id": "create-message",
    }
    identity[missing_field] = ""

    async def partial_identity(_event):
        return dict(identity)

    runner._trusted_kanban_control_identity = partial_identity
    event = MessageEvent(
        text='/kanban create "synthetic" --assignee default',
        source=_feishu_source(),
        message_id="create-message",
    )

    reply = await GatewayRunner._handle_kanban_command(runner, event)

    assert calls == []
    assert "没有开始任务" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["scope_id", "thread_id"])
async def test_allowed_source_missing_optional_allowlist_proof_creates_nothing(
    monkeypatch,
    tmp_path,
    missing_field,
):
    """An intended source cannot downgrade to ordinary creation on missing proof."""
    from agent import kanban_auto_handoff
    from agent import kanban_handoff_scope as handoff_scope
    from hermes_cli import kanban as kanban_cli

    config = _scope_config(tmp_path)
    config["kanban"]["short_task_handoff"]["allowed_origins"][0][missing_field] = (
        "tenant_scope" if missing_field == "scope_id" else "thread-1"
    )
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    calls = []
    monkeypatch.setattr(
        kanban_cli,
        "run_slash",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "unexpected",
    )
    runner = object.__new__(GatewayRunner)
    runner._kanban_handoff_policy_for_source = lambda _source: (
        kanban_auto_handoff.build_dispatcher_policy_snapshot(config)
    )
    identity = {
        "platform": "feishu",
        "scope_id": "tenant_scope",
        "chat_type": "group",
        "chat_id": "oc_canonical_chat",
        "thread_id": "thread-1",
        "user_id": "union_canonical_user",
        "notifier_profile": "default",
        "session_key": "allowed-session",
        "message_id": "create-message",
    }
    identity[missing_field] = ""

    async def incomplete_optional_identity(_event):
        return dict(identity)

    runner._trusted_kanban_control_identity = incomplete_optional_identity
    event = MessageEvent(
        text='/kanban create "synthetic" --assignee default',
        source=_feishu_source(),
        message_id="create-message",
    )

    reply = await GatewayRunner._handle_kanban_command(runner, event)

    assert calls == []
    assert "没有开始任务" in reply


@pytest.mark.asyncio
async def test_internal_allowed_source_cannot_launch_control_bound_task(
    monkeypatch,
    tmp_path,
):
    from agent import kanban_auto_handoff
    from agent import kanban_handoff_scope as handoff_scope
    from hermes_cli import kanban as kanban_cli

    config = _scope_config(tmp_path)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    calls = []
    monkeypatch.setattr(
        kanban_cli,
        "run_slash",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "unexpected",
    )
    runner = object.__new__(GatewayRunner)
    runner._kanban_handoff_policy_for_source = lambda _source: (
        kanban_auto_handoff.build_dispatcher_policy_snapshot(config)
    )

    async def copied_identity(_event):
        return {
            "platform": "feishu",
            "scope_id": "tenant_scope",
            "chat_type": "group",
            "chat_id": "oc_canonical_chat",
            "thread_id": "thread-1",
            "user_id": "union_canonical_user",
            "notifier_profile": "default",
            "session_key": "allowed-session",
            "message_id": "copied-user-message",
        }

    runner._trusted_kanban_control_identity = copied_identity
    event = MessageEvent(
        text='/kanban create "synthetic" --assignee default',
        source=_feishu_source(),
        message_id="copied-user-message",
        internal=True,
    )

    reply = await GatewayRunner._handle_kanban_command(runner, event)

    assert calls == []
    assert "没有开始任务" in reply
