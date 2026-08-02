"""Tests for gateway /fast support and Priority Processing routing."""

import asyncio
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
            "persist_user_timestamp": persist_user_timestamp,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_discord_auto_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="user-1",
        thread_id="999",
        parent_chat_id="100",
        auto_thread_created=True,
        auto_thread_initial_name="raw user prompt",
    )


def _make_matrix_source() -> SessionSource:
    return SessionSource(
        platform=Platform.MATRIX,
        chat_id="!room:matrix.org",
        chat_type="dm",
        user_id="@user:matrix.org",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


class _TitleAwareAdapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def on_session_title_changed(self, source, title):
        return None

    async def on_session_semantic_base_changed(self, source, base):
        return None


class _LegacyTitleAwareAdapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def on_session_title_changed(self, source, title):
        return None


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_handle_fast_command_global_flag_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await runner._handle_fast_command(_make_event("/fast fast --global"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"
    # Global write supersedes the session override.
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_session_fast_override_beats_config_default(monkeypatch, tmp_path):
    """A session /fast normal wins over agent.service_tier: fast in config."""
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    event = _make_event("/fast normal")
    session_key = runner._session_key_for_source(event.source)

    response = await runner._handle_fast_command(event)

    assert "NORMAL" in response
    # Override stores explicit None (normal) and wins over config "fast".
    assert session_key in runner._session_service_tier_overrides
    assert runner._resolve_session_service_tier(session_key=session_key) is None
    # A different session still gets the config default.
    assert runner._resolve_session_service_tier(session_key="other-session") == "priority"


@pytest.mark.asyncio
async def test_run_agent_passes_priority_processing_to_gateway_agent(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    # ``_load_service_tier`` was refactored to call ``_load_gateway_runtime_config``
    # (which wraps ``_load_gateway_config`` plus env-expansion).  Since the test
    # stubs ``_load_gateway_config`` to ``{}``, also stub the runtime wrapper
    # directly so the priority routing assertions still exercise the live tier.
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None
    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["service_tier"] == "priority"
    assert _CapturingAgent.last_init["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_run_agent_passes_discord_auto_thread_title_callback(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="raw user prompt",
            context_prompt="",
            history=[],
            source=_make_discord_auto_thread_source(),
            session_id="session-1",
            session_key="agent:main:discord:thread:999",
        )

    mock_title.assert_called_once()
    callback = mock_title.call_args.kwargs["title_callback"]
    with patch.object(runner, "_schedule_discord_semantic_thread_rename") as mock_schedule:
        callback("Semantic Session Title")
    mock_schedule.assert_called_once()
    assert mock_schedule.call_args.args[1] == "session-1"
    assert mock_schedule.call_args.args[2] == "Semantic Session Title"


@pytest.mark.asyncio
async def test_run_agent_passes_optional_adapter_title_callback(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]

    adapter = _TitleAwareAdapter(PlatformConfig(), Platform.MATRIX)
    runner.adapters = {Platform.MATRIX: adapter}  # type: ignore[dict-item]

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"core"},
    )

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="raw user prompt",
            context_prompt="",
            history=[],
            source=_make_matrix_source(),
            session_id="session-1",
            session_key="agent:main:matrix:dm:!room:matrix.org",
        )

    mock_title.assert_called_once()
    callback = mock_title.call_args.kwargs["title_callback"]
    with patch.object(runner, "_schedule_adapter_semantic_base_refresh") as mock_schedule:
        callback("Semantic Session Title")
    mock_schedule.assert_called_once_with(
        _make_matrix_source(),
        "session-1",
    )


@pytest.mark.asyncio
async def test_run_agent_retains_legacy_title_callback_without_semantic_hook(
    monkeypatch, tmp_path
):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]
    runner.adapters = {
        Platform.TELEGRAM: _LegacyTitleAwareAdapter(
            PlatformConfig(), Platform.TELEGRAM
        )
    }

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(
        tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"}
    )

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="raw user prompt",
            context_prompt="",
            history=[],
            source=_make_source(),
            session_id="session-1",
            session_key="agent:main:telegram:dm:12345",
        )

    callback = mock_title.call_args.kwargs["title_callback"]
    with patch.object(runner, "_schedule_adapter_semantic_base_refresh") as semantic, patch.object(
        runner, "_schedule_adapter_session_title_propagation"
    ) as legacy:
        callback("Semantic Session Title")
    semantic.assert_not_called()
    legacy.assert_called_once_with(
        _make_source(), "session-1", "Semantic Session Title"
    )


@pytest.mark.asyncio
async def test_post_turn_goal_completion_refreshes_semantic_base_immediately():
    runner = _make_runner()
    runner.config = SimpleNamespace(goals=SimpleNamespace(max_turns=5))
    runner._schedule_adapter_semantic_base_refresh = MagicMock()
    manager = MagicMock()
    manager.is_active.side_effect = [True, False]
    manager.evaluate_after_turn.return_value = {
        "should_continue": False,
        "message": "",
    }

    with patch("hermes_cli.goals.GoalManager", return_value=manager):
        await runner._post_turn_goal_continuation(
            session_entry=SimpleNamespace(session_id="session-1"),
            source=_make_source(),
            final_response="finished",
        )

    manager.evaluate_after_turn.assert_called_once()
    runner._schedule_adapter_semantic_base_refresh.assert_called_once_with(
        _make_source(), "session-1"
    )


@pytest.mark.asyncio
async def test_adapter_title_propagation_invokes_optional_hook():
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1")
        ),
    )
    runner._session_db = SimpleNamespace(
        get_session_title=AsyncMock(return_value="Current Session Title")
    )

    adapter = SimpleNamespace(on_session_title_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]

    await runner._propagate_session_title_to_adapter(
        _make_source(),
        "session-1",
        "Current Session Title",
    )

    adapter.on_session_title_changed.assert_awaited_once_with(
        _make_source(),
        "Current Session Title",
    )


@pytest.mark.asyncio
async def test_adapter_title_propagation_schedules_foreign_callback_on_gateway_loop():
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1")
        ),
    )
    runner._session_db = SimpleNamespace(
        get_session_title=AsyncMock(return_value="Current Session Title")
    )
    callback_loops = []

    async def on_session_title_changed(_source, _title):
        callback_loops.append(asyncio.get_running_loop())

    adapter = SimpleNamespace(on_session_title_changed=on_session_title_changed)
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]

    gateway_loop = asyncio.new_event_loop()
    gateway_ready = threading.Event()

    def run_gateway_loop():
        asyncio.set_event_loop(gateway_loop)
        gateway_ready.set()
        gateway_loop.run_forever()

    gateway_thread = threading.Thread(target=run_gateway_loop)
    gateway_thread.start()
    assert gateway_ready.wait(timeout=2)
    runner._gateway_loop = gateway_loop
    foreign_loop = asyncio.get_running_loop()

    try:
        runner._schedule_adapter_session_title_propagation(
            _make_source(), "session-1", "Current Session Title"
        )
        for _ in range(200):
            if callback_loops and not runner._adapter_title_apply_locks:
                break
            await asyncio.sleep(0.01)
    finally:
        gateway_loop.call_soon_threadsafe(gateway_loop.stop)
        gateway_thread.join(timeout=2)
        gateway_loop.close()

    assert callback_loops == [gateway_loop]
    assert callback_loops[0] is not foreign_loop
    assert runner._adapter_title_apply_locks == {}
    assert runner._adapter_title_generations == {}
    assert runner._adapter_title_pending == {}


@pytest.mark.asyncio
async def test_adapter_title_propagation_does_not_overwrite_newer_manual_title(tmp_path):
    from hermes_state import AsyncSessionDB, SessionDB

    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1")
        ),
    )
    session_db = SessionDB(db_path=tmp_path / "state.db")
    session_db.create_session("session-1", "matrix")
    session_db.set_session_title("session-1", "New Manual Title")
    runner._session_db = AsyncSessionDB(session_db)
    adapter = SimpleNamespace(on_session_title_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]

    await runner._propagate_session_title_to_adapter(
        _make_source(),
        "session-1",
        "Old Generated Title",
    )

    adapter.on_session_title_changed.assert_not_awaited()
    session_db.close()


@pytest.mark.asyncio
async def test_adapter_title_propagation_stale_validated_write_cannot_overwrite_newer():
    """A delayed older callback cannot apply after a newer scheduled title."""
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1")
        ),
    )
    old_title_validating = asyncio.Event()
    release_old_validation = asyncio.Event()
    persisted_title = "Old Title"

    async def get_session_title(_session_id):
        if persisted_title == "Old Title":
            old_title_validating.set()
            await release_old_validation.wait()
            return "Old Title"
        return persisted_title

    runner._session_db = SimpleNamespace(get_session_title=get_session_title)
    applied = []
    adapter = SimpleNamespace(
        on_session_title_changed=AsyncMock(
            side_effect=lambda _source, title: applied.append(title)
        )
    )
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]

    old_generation = runner._reserve_adapter_title_generation("session-1")
    old_task = asyncio.create_task(
        runner._propagate_session_title_to_adapter(
            _make_source(), "session-1", "Old Title", old_generation
        )
    )
    await old_title_validating.wait()

    persisted_title = "New Title"
    new_generation = runner._reserve_adapter_title_generation("session-1")
    try:
        await runner._propagate_session_title_to_adapter(
            _make_source(), "session-1", "New Title", new_generation
        )
        release_old_validation.set()
        await old_task
    finally:
        release_old_validation.set()
        await old_task
        runner._release_adapter_title_generation("session-1")
        runner._release_adapter_title_generation("session-1")

    assert applied == ["New Title"]
    assert runner._adapter_title_generations == {}
    assert runner._adapter_title_pending == {}
    assert runner._adapter_title_apply_locks == {}


@pytest.mark.asyncio
async def test_adapter_title_propagation_ignores_stale_session():
    runner = _make_runner()

    adapter = SimpleNamespace(on_session_title_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]
    runner.session_store = SimpleNamespace(  # type: ignore[assignment]
        get_or_create_session=lambda source: SimpleNamespace(session_id="new-session")
    )
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="new-session")
        ),
    )

    await runner._propagate_session_title_to_adapter(
        _make_source(),
        "old-session",
        "Old Session Title",
    )

    adapter.on_session_title_changed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_status", ["paused", "done"])
async def test_semantic_base_refresh_uses_title_when_goal_not_active(goal_status):
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="session-1"))
    )
    runner._session_db = SimpleNamespace(
        get_session_title=AsyncMock(return_value="Persisted session title")
    )
    applied = []
    adapter = SimpleNamespace(
        on_session_semantic_base_changed=AsyncMock(
            side_effect=lambda _source, base: applied.append(base)
        )
    )
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]
    fake_manager = MagicMock()
    fake_manager.is_active.return_value = False
    fake_manager.state = SimpleNamespace(goal="Old epic", status=goal_status)
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        with patch("hermes_cli.goals.GoalManager", return_value=fake_manager):
            await runner._refresh_adapter_semantic_base(
                _make_source(), "session-1", generation
            )
    finally:
        runner._release_adapter_title_generation("session-1")
    assert applied == ["Persisted session title"]


@pytest.mark.asyncio
async def test_semantic_base_refresh_active_goal_precedes_persisted_title():
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="session-1"))
    )
    runner._session_db = SimpleNamespace(
        get_session_title=AsyncMock(return_value="Delayed auto title")
    )
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]
    fake_manager = MagicMock()
    fake_manager.is_active.return_value = True
    fake_manager.state = SimpleNamespace(goal="Authoritative epic")
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        with patch("hermes_cli.goals.GoalManager", return_value=fake_manager):
            await runner._refresh_adapter_semantic_base(
                _make_source(), "session-1", generation
            )
    finally:
        runner._release_adapter_title_generation("session-1")
    adapter.on_session_semantic_base_changed.assert_awaited_once_with(
        _make_source(), "Authoritative epic"
    )
    runner._session_db.get_session_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_base_refresh_no_persisted_source_delegates_recovery():
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="session-1"))
    )
    runner._session_db = SimpleNamespace(get_session_title=AsyncMock(return_value=None))
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]
    fake_manager = MagicMock()
    fake_manager.is_active.return_value = False
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        with patch("hermes_cli.goals.GoalManager", return_value=fake_manager):
            await runner._refresh_adapter_semantic_base(
                _make_source(), "session-1", generation
            )
    finally:
        runner._release_adapter_title_generation("session-1")
    adapter.on_session_semantic_base_changed.assert_awaited_once_with(
        _make_source(), None
    )


@pytest.mark.asyncio
async def test_delayed_auto_title_refresh_cannot_overwrite_newer_active_epic():
    runner = _make_runner()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1")
        ),
    )
    title_read_started = asyncio.Event()
    release_title_read = asyncio.Event()

    async def delayed_title(_session_id):
        title_read_started.set()
        await release_title_read.wait()
        return "Stale auto title"

    runner._session_db = SimpleNamespace(get_session_title=delayed_title)
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[dict-item]
    goal_active = False

    def manager_factory(*_args, **_kwargs):
        manager = MagicMock()
        manager.is_active.return_value = goal_active
        manager.state = SimpleNamespace(goal="New active epic")
        return manager

    old_generation = runner._reserve_adapter_title_generation("session-1")
    with patch("hermes_cli.goals.GoalManager", side_effect=manager_factory):
        old_task = asyncio.create_task(
            runner._refresh_adapter_semantic_base(
                _make_source(), "session-1", old_generation
            )
        )
        await title_read_started.wait()
        goal_active = True
        new_generation = runner._reserve_adapter_title_generation("session-1")
        new_task = asyncio.create_task(
            runner._refresh_adapter_semantic_base(
                _make_source(), "session-1", new_generation
            )
        )
        release_title_read.set()
        await asyncio.gather(old_task, new_task)
    runner._release_adapter_title_generation("session-1")
    runner._release_adapter_title_generation("session-1")

    adapter.on_session_semantic_base_changed.assert_awaited_once_with(
        _make_source(), "New active epic"
    )
