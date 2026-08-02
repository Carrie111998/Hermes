"""Tests for Discord channel_prompts resolution and injection."""

import sys
import threading
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore, initial_context_model_config


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
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


def _make_adapter():
    _ensure_discord_mock()
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter.config = MagicMock()
    adapter.config.extra = {}
    return adapter


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = "Global prompt"
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
        platform=Platform.DISCORD,
        chat_id="12345",
        chat_type="thread",
        user_id="user-1",
    )


class TestResolveChannelPrompts:
    def test_no_prompt_returns_none(self):
        adapter = _make_adapter()
        assert adapter._resolve_channel_prompt("123") is None

    def test_match_by_channel_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"

    def test_numeric_yaml_keys_normalized_at_config_load(self):
        """Numeric YAML keys are normalized to strings by config bridging.

        The resolver itself expects string keys (config.py handles normalization),
        so raw numeric keys will not match — this is intentional.
        """
        adapter = _make_adapter()
        # Simulates post-bridging state: keys are already strings
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"
        # Pre-bridging numeric key would not match (bridging is responsible)
        adapter.config.extra = {"channel_prompts": {100: "Research mode"}}
        assert adapter._resolve_channel_prompt("100") is None

    def test_match_by_parent_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"200": "Forum prompt"}}
        assert adapter._resolve_channel_prompt("999", parent_id="200") == "Forum prompt"

    def test_context_file_match_by_parent_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_context_files": {"200": "/tmp/channel-state.md"}}
        assert adapter._resolve_channel_context_file("999", parent_id="200") == "/tmp/channel-state.md"

    def test_exact_channel_overrides_parent(self):
        adapter = _make_adapter()
        adapter.config.extra = {
            "channel_prompts": {
                "999": "Thread override",
                "200": "Forum prompt",
            }
        }
        assert adapter._resolve_channel_prompt("999", parent_id="200") == "Thread override"

    def test_build_message_event_sets_channel_prompt(self):
        adapter = _make_adapter()
        adapter.config.extra = {
            "channel_prompts": {"321": "Command prompt"},
            "channel_context_files": {"321": "/tmp/context.md"},
        }
        adapter.build_source = MagicMock(return_value=SimpleNamespace())

        interaction = SimpleNamespace(
            channel_id=321,
            channel=SimpleNamespace(name="general", guild=None, parent_id=None),
            user=SimpleNamespace(id=1, display_name="Brenner"),
        )
        adapter._get_effective_topic = MagicMock(return_value=None)

        event = adapter._build_slash_event(interaction, "/retry")

        assert event.channel_prompt == "Command prompt"
        assert event.channel_context_file == "/tmp/context.md"

    @pytest.mark.asyncio
    async def test_dispatch_thread_session_inherits_parent_channel_prompt(self):
        adapter = _make_adapter()
        adapter.config.extra = {
            "channel_prompts": {"200": "Parent prompt"},
            "channel_context_files": {"200": "/tmp/parent-context.md"},
        }
        adapter.build_source = MagicMock(return_value=SimpleNamespace())
        adapter._get_effective_topic = MagicMock(return_value=None)
        adapter.handle_message = AsyncMock()

        interaction = SimpleNamespace(
            guild=SimpleNamespace(name="Wetlands"),
            channel=SimpleNamespace(id=200, parent=None),
            user=SimpleNamespace(id=1, display_name="Brenner"),
        )

        await adapter._dispatch_thread_session(interaction, "999", "new-thread", "hello")

        dispatched_event = adapter.handle_message.await_args.args[0]
        assert dispatched_event.channel_prompt == "Parent prompt"
        assert dispatched_event.channel_context_file == "/tmp/parent-context.md"

    @pytest.mark.asyncio
    async def test_regular_thread_message_sets_parent_channel_context_file(self):
        _ensure_discord_mock()
        discord_mod = sys.modules["discord"]
        adapter = _make_adapter()
        adapter.config.extra = {
            "channel_prompts": {"200": "Parent prompt"},
            "channel_context_files": {"200": "/tmp/parent-context.md"},
        }
        adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))

        class _ThreadTracker:
            def __contains__(self, item):
                return False

            def mark(self, item):
                self.marked = item

        adapter._threads = _ThreadTracker()
        adapter._voice_text_channels = {}
        adapter._text_batch_delay_seconds = 0
        adapter._discord_require_mention = MagicMock(return_value=False)
        adapter._discord_free_response_channels = MagicMock(return_value=set())
        adapter._discord_history_backfill = MagicMock(return_value=False)
        adapter._discord_allow_any_attachment = MagicMock(return_value=False)
        adapter._get_parent_channel_id = MagicMock(return_value="200")
        adapter._format_thread_chat_name = MagicMock(return_value="guild / #parent / thread")
        adapter._get_effective_topic = MagicMock(return_value=None)
        adapter.build_source = MagicMock(return_value=SimpleNamespace())
        adapter.handle_message = AsyncMock()

        channel = discord_mod.Thread()
        channel.id = 999
        channel.parent_id = 200
        author = SimpleNamespace(id=1, display_name="Brenner", bot=False)
        message = SimpleNamespace(
            id=123,
            channel=channel,
            content="hello",
            mentions=[],
            attachments=[],
            author=author,
            guild=SimpleNamespace(id=42, name="Wetlands"),
            created_at=datetime.now(),
            reference=None,
        )

        await adapter._handle_message(message)

        dispatched_event = adapter.handle_message.await_args.args[0]
        assert dispatched_event.channel_prompt == "Parent prompt"
        assert dispatched_event.channel_context_file == "/tmp/parent-context.md"

    def test_blank_prompts_are_ignored(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"100": "   "}}
        assert adapter._resolve_channel_prompt("100") is None

@pytest.mark.asyncio
async def test_retry_preserves_channel_prompt(monkeypatch):
    runner = _make_runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1", last_prompt_tokens=10),
        load_transcript=lambda session_id: [
            {"role": "user", "content": "original message"},
            {"role": "assistant", "content": "old reply"},
        ],
        rewrite_transcript=MagicMock(),
    )
    runner._handle_message = AsyncMock(return_value="ok")

    event = MessageEvent(
        text="/retry",
        message_type=gateway_run.MessageType.COMMAND,
        source=_make_source(),
        raw_message=SimpleNamespace(),
        channel_prompt="Channel prompt",
        channel_context_file="/tmp/channel-state.md",
    )

    result = await runner._handle_retry_command(event)

    assert result == "ok"
    retried_event = runner._handle_message.await_args.args[0]
    assert retried_event.channel_prompt == "Channel prompt"
    assert retried_event.channel_context_file == "/tmp/channel-state.md"


@pytest.mark.asyncio
async def test_run_agent_appends_channel_prompt_to_ephemeral_system_prompt(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
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
    event = MessageEvent(
        text="hi",
        source=_make_source(),
        message_id="m1",
        channel_prompt="Channel prompt",
    )
    result = await runner._run_agent(
        message="hi",
        context_prompt="Context prompt",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:discord:thread:12345",
        channel_prompt=event.channel_prompt,
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["ephemeral_system_prompt"] == (
        "Context prompt\n\nChannel prompt\n\nGlobal prompt"
    )


def test_channel_context_file_snapshot_reads_file(monkeypatch, tmp_path):
    context_file = tmp_path / "channel-state.md"
    context_file.write_text("# State\n\n- Keep this context", encoding="utf-8")

    snapshot = gateway_run._load_channel_context_file_snapshot(str(context_file))

    assert snapshot is not None
    assert "Initial channel context loaded from" in snapshot
    assert "# State" in snapshot
    assert "Keep this context" in snapshot


def test_channel_context_file_snapshot_truncates_bounded_read(tmp_path):
    context_file = tmp_path / "large-context.md"
    context_file.write_text(
        "x" * (gateway_run._CHANNEL_CONTEXT_FILE_MAX_CHARS + 100),
        encoding="utf-8",
    )

    snapshot = gateway_run._load_channel_context_file_snapshot(str(context_file))

    assert snapshot is not None
    assert f"truncated to {gateway_run._CHANNEL_CONTEXT_FILE_MAX_CHARS} characters" in snapshot
    assert snapshot.endswith("x" * gateway_run._CHANNEL_CONTEXT_FILE_MAX_CHARS)


def test_channel_context_file_snapshot_resolves_relative_to_hermes_home(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "context.md").write_text("relative context", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    snapshot = gateway_run._load_channel_context_file_snapshot("context.md")

    assert snapshot is not None
    assert "relative context" in snapshot


def _make_snapshot_runner(monkeypatch, tmp_path, store):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = store
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    return runner


def test_channel_context_snapshot_compare_and_set_guards_session_id(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    source = _make_source()
    entry = store.get_or_create_session(source)
    session_key = store._generate_session_key(source)

    assert store.initialize_context_prompt(session_key, "stale-session", "wrong") == (
        False,
        None,
    )
    assert entry.initial_context_prompt is None
    assert store.initialize_context_prompt(
        session_key, entry.session_id, "version one"
    ) == (True, "version one")
    assert store.initialize_context_prompt(
        session_key, entry.session_id, "version two"
    ) == (True, "version one")


def test_channel_context_snapshot_stays_stable_when_routing_save_fails(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    source = _make_source()
    entry = store.get_or_create_session(source)
    session_key = store._generate_session_key(source)
    store._save = MagicMock(side_effect=OSError("disk unavailable"))

    assert store.initialize_context_prompt(
        session_key, entry.session_id, "stable context"
    ) == (True, "stable context")
    assert entry.initial_context_prompt == "stable context"
    assert entry.initial_context_initialized is True
    assert store.initialize_context_prompt(
        session_key, entry.session_id, "different context"
    ) == (True, "stable context")


def test_channel_context_snapshot_hydrates_by_session_id_on_resume(tmp_path):
    class _FakeSessionDB:
        def __init__(self):
            self.rows = {}

        def get_session(self, session_id):
            return self.rows.get(session_id)

        def update_session_meta(self, session_id, model_config, model=None):
            self.rows.setdefault(session_id, {})["model_config"] = model_config
            self.rows[session_id]["model"] = model

        def end_session(self, session_id, _reason):
            return None

        def reopen_session(self, session_id):
            return None

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    source = _make_source()
    original = store.get_or_create_session(source)
    session_key = store._generate_session_key(source)
    db = _FakeSessionDB()
    db.rows[original.session_id] = {"model_config": "{}", "model": None}
    db.rows["other-session"] = {"model_config": "{}", "model": None}
    store._db = db  # type: ignore[assignment]

    assert store.initialize_context_prompt(
        session_key, original.session_id, "resumable context"
    ) == (True, "resumable context")
    assert store.switch_session(session_key, "other-session") is not None
    resumed = store.switch_session(session_key, original.session_id)

    assert resumed is not None
    assert resumed.initial_context_initialized is True
    assert resumed.initial_context_prompt == "resumable context"


def test_branch_model_config_inherits_initialized_context_snapshot():
    assert initial_context_model_config(True, "branch context") == {
        "gateway_initial_context_initialized": True,
        "gateway_initial_context_prompt": "branch context",
    }
    assert initial_context_model_config(False, "not ready") == {}


@pytest.mark.parametrize(
    ("model_config", "expected_prompt"),
    [
        ("{}", None),
        (
            '{"gateway_initial_context_initialized": true, '
            '"gateway_initial_context_prompt": "recovered context"}',
            "recovered context",
        ),
    ],
)
def test_recovered_existing_session_never_reinitializes_context(
    tmp_path, model_config, expected_prompt
):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None

    recovered = store._create_entry_from_recovered_row(
        row={
            "id": "recovered-session",
            "started_at": datetime.now().timestamp(),
            "model_config": model_config,
        },
        session_key="agent:main:discord:thread:12345",
        source=_make_source(),
        now=datetime.now(),
    )

    assert recovered.initial_context_initialized is True
    assert recovered.initial_context_prompt == expected_prompt


@pytest.mark.asyncio
async def test_channel_context_snapshot_persists_without_drift(monkeypatch, tmp_path):
    sessions_dir = tmp_path / "sessions"
    context_file = tmp_path / "channel-state.md"
    context_file.write_text("version one", encoding="utf-8")
    source = _make_source()
    session_key = "agent:main:discord:thread:12345"

    first_store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    first_store._db = None
    first_runner = _make_snapshot_runner(monkeypatch, tmp_path, first_store)
    first_event = MessageEvent(
        text="hello",
        source=source,
        message_id="m1",
        channel_context_file=str(context_file),
    )

    await first_runner._handle_message_with_agent(first_event, source, session_key, 1)
    first_prompt = first_runner._run_agent.await_args.kwargs["context_prompt"]
    assert "version one" in first_prompt

    context_file.write_text("version two", encoding="utf-8")
    reloaded_store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    reloaded_store._db = None
    second_runner = _make_snapshot_runner(monkeypatch, tmp_path, reloaded_store)
    second_event = MessageEvent(
        text="hello again",
        source=source,
        message_id="m2",
        channel_context_file=str(context_file),
    )

    await second_runner._handle_message_with_agent(second_event, source, session_key, 1)
    second_prompt = second_runner._run_agent.await_args.kwargs["context_prompt"]
    assert "version one" in second_prompt
    assert "version two" not in second_prompt


@pytest.mark.asyncio
async def test_synthetic_first_turn_resolves_channel_context_centrally(
    monkeypatch, tmp_path
):
    context_file = tmp_path / "channel-state.md"
    context_file.write_text("synthetic version one", encoding="utf-8")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="system:wake",
        thread_id="999",
        parent_chat_id="200",
    )
    session_key = "agent:main:discord:thread:999"
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    runner = _make_snapshot_runner(monkeypatch, tmp_path, store)
    resolver = MagicMock(return_value=str(context_file))
    runner._resolve_event_channel_context_file = resolver

    synthetic_event = MessageEvent(text="wake", source=source)
    await runner._handle_message_with_agent(synthetic_event, source, session_key, 1)

    first_prompt = runner._run_agent.await_args.kwargs["context_prompt"]  # type: ignore[attr-defined]
    assert "synthetic version one" in first_prompt
    resolver.assert_called_once_with(synthetic_event, source)

    context_file.write_text("synthetic version two", encoding="utf-8")
    persisted_entry = store.get_or_create_session(source)
    assert "synthetic version one" in (persisted_entry.initial_context_prompt or "")
    assert "synthetic version two" not in (persisted_entry.initial_context_prompt or "")


def test_central_context_resolver_uses_thread_and_parent_ids():
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter_resolver = MagicMock(return_value="/tmp/central-context.md")
    setattr(
        runner,
        "adapters",
        {
            Platform.DISCORD: SimpleNamespace(
                _resolve_channel_context_file=adapter_resolver,
            )
        },
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        thread_id="999",
        parent_chat_id="200",
    )

    result = runner._resolve_event_channel_context_file(
        MessageEvent(text="wake", source=source),
        source,
    )

    assert result == "/tmp/central-context.md"
    adapter_resolver.assert_called_once_with("999", "200")


@pytest.mark.asyncio
async def test_voice_channel_input_preserves_channel_context_file():
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = SimpleNamespace(
        _voice_text_channels={42: 321},
        _voice_sources={},
        _client=SimpleNamespace(get_channel=lambda _channel_id: None),
        _resolve_channel_prompt=MagicMock(return_value="Voice prompt"),
        _resolve_channel_context_file=MagicMock(return_value="/tmp/voice-context.md"),
        handle_message=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}  # type: ignore[dict-item]
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._is_duplicate_voice_transcript = MagicMock(return_value=False)

    await runner._handle_voice_channel_input(42, 7, "hello from voice")

    dispatched_event = adapter.handle_message.await_args.args[0]
    assert dispatched_event.channel_prompt == "Voice prompt"
    assert dispatched_event.channel_context_file == "/tmp/voice-context.md"
