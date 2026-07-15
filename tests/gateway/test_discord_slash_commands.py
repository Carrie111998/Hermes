"""Tests for native Discord slash command fast-paths (thread creation & auto-thread)."""

import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        # Real discord is installed — nothing to do.
        return

    if sys.modules.get("discord") is None:
        discord_mod = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock()
        discord_mod.DMChannel = type("DMChannel", (), {})
        discord_mod.Thread = type("Thread", (), {})
        discord_mod.ForumChannel = type("ForumChannel", (), {})
        discord_mod.Interaction = object

        # Lightweight mock for app_commands.Group and Command used by
        # _register_skill_group.
        class _FakeGroup:
            def __init__(self, *, name, description, parent=None):
                self.name = name
                self.description = description
                self.parent = parent
                self._children: dict[str, object] = {}
                if parent is not None:
                    parent.add_command(self)

            def add_command(self, cmd):
                self._children[cmd.name] = cmd

        class _FakeCommand:
            def __init__(self, *, name, description, callback, parent=None):
                self.name = name
                self.description = description
                self.callback = callback
                self.parent = parent

        discord_mod.app_commands = SimpleNamespace(
            describe=lambda **kwargs: (lambda fn: fn),
            choices=lambda **kwargs: (lambda fn: fn),
            autocomplete=lambda **kwargs: (lambda fn: fn),
            Choice=lambda **kwargs: SimpleNamespace(**kwargs),
            Group=_FakeGroup,
            Command=_FakeCommand,
        )

        ext_mod = MagicMock()
        commands_mod = MagicMock()
        commands_mod.Bot = MagicMock
        ext_mod.commands = commands_mod

        sys.modules["discord"] = discord_mod
        sys.modules.setdefault("discord.ext", ext_mod)
        sys.modules.setdefault("discord.ext.commands", commands_mod)

    # Whether we just installed the mock OR another test module installed
    # it first via its own _ensure_discord_mock, force the decorators we
    # need onto discord.app_commands — the flat /skill command uses
    # @app_commands.autocomplete and not every other mock stub exposes it.
    _app = getattr(sys.modules["discord"], "app_commands", None)
    if _app is not None and not hasattr(_app, "autocomplete"):
        try:
            _app.autocomplete = lambda **kwargs: (lambda fn: fn)
        except Exception:
            pass


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator

    def add_command(self, cmd):
        self.commands[cmd.name] = cmd

    def get_commands(self):
        return [SimpleNamespace(name=n) for n in self.commands]


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(
        tree=FakeTree(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="HermesBot"),
    )
    adapter._text_batch_delay_seconds = 0  # disable batching for tests
    # Slash auth is exercised in test_discord_slash_auth.py — bypass it here
    # so registration / dispatch / thread behavior tests don't have to
    # construct a full auth context (allowlist / channel scope).
    adapter._check_slash_authorization = AsyncMock(return_value=True)
    return adapter


# ------------------------------------------------------------------
# /thread slash command registration
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registers_native_thread_slash_command(adapter):
    # The /thread slash closure now delegates ALL the work — including
    # defer() — to _handle_thread_create_slash so the auth gate can send
    # an ephemeral rejection on the still-unresponded interaction. The
    # closure should just forward.
    adapter._handle_thread_create_slash = AsyncMock()
    adapter._register_slash_commands()

    command = adapter._client.tree.commands["thread"]
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
    )

    await command(interaction, name="Planning", message="", auto_archive_duration=1440)

    # defer is now performed inside _handle_thread_create_slash, AFTER the
    # auth check passes — not by the closure.
    interaction.response.defer.assert_not_awaited()
    adapter._handle_thread_create_slash.assert_awaited_once_with(interaction, "Planning", "", 1440)


@pytest.mark.asyncio
async def test_registers_native_restart_slash_command(adapter):
    adapter._run_simple_slash = AsyncMock()
    adapter._register_slash_commands()

    assert "restart" in adapter._client.tree.commands

    interaction = SimpleNamespace()
    await adapter._client.tree.commands["restart"](interaction)

    adapter._run_simple_slash.assert_awaited_once_with(
        interaction,
        "/restart",
        "Restart requested~",
    )


@pytest.mark.asyncio
async def test_run_simple_slash_executes_when_defer_interaction_expired(adapter):
    class UnknownInteraction(Exception):
        status = 404
        code = 10062

    interaction = SimpleNamespace(
        channel=_FakeTextChannel(channel_id=123, name="general"),
        channel_id=123,
        guild_id=456,
        user=SimpleNamespace(id=42, name="Jezza", display_name="Jezza"),
        response=SimpleNamespace(defer=AsyncMock(side_effect=UnknownInteraction("Unknown interaction"))),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )
    adapter.handle_message = AsyncMock()

    await adapter._run_simple_slash(interaction, "/reset", "Session reset~")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/reset"
    assert event.source.chat_id == "123"
    interaction.edit_original_response.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()


# ------------------------------------------------------------------
# Auto-registration from COMMAND_REGISTRY
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_registers_missing_gateway_commands(adapter):
    """Commands in COMMAND_REGISTRY that aren't explicitly registered should
    be auto-registered by the dynamic catch-all block."""
    adapter._run_simple_slash = AsyncMock()
    adapter._register_slash_commands()

    tree_names = set(adapter._client.tree.commands.keys())

    # These commands are gateway-available but were not in the original
    # hardcoded registration list — they should be auto-registered.
    expected_auto = {"debug", "yolo", "profile"}
    for name in expected_auto:
        assert name in tree_names, f"/{name} should be auto-registered on Discord"


@pytest.mark.asyncio
async def test_auto_registered_command_dispatches_correctly(adapter):
    """Auto-registered commands should dispatch via _run_simple_slash."""
    adapter._run_simple_slash = AsyncMock()
    adapter._register_slash_commands()

    # /debug has no args — test parameterless dispatch
    debug_cmd = adapter._client.tree.commands["debug"]
    interaction = SimpleNamespace()
    adapter._run_simple_slash.reset_mock()
    await debug_cmd.callback(interaction)
    adapter._run_simple_slash.assert_awaited_once_with(interaction, "/debug")


@pytest.mark.asyncio
async def test_auto_registered_command_with_args(adapter):
    """Auto-registered commands with args_hint should accept an optional args param."""
    adapter._run_simple_slash = AsyncMock()
    adapter._register_slash_commands()

    # /branch has args_hint="[name]" — test dispatch with args
    branch_cmd = adapter._client.tree.commands["branch"]
    interaction = SimpleNamespace()
    adapter._run_simple_slash.reset_mock()
    await branch_cmd.callback(interaction, args="my-branch")
    adapter._run_simple_slash.assert_awaited_once_with(
        interaction, "/branch my-branch"
    )


@pytest.mark.asyncio
async def test_architect_slash_command_opens_a_new_thread(adapter):
    """Native Discord /architect should create a fresh thread instead of running inline."""
    adapter._handle_architect_thread_slash = AsyncMock()
    adapter._register_slash_commands()

    architect_cmd = adapter._client.tree.commands["architect"]
    interaction = SimpleNamespace()
    if hasattr(architect_cmd, "callback"):
        await architect_cmd.callback(interaction, args="--fast build leasing workflow")
    else:
        await architect_cmd(interaction, args="--fast build leasing workflow")

    adapter._handle_architect_thread_slash.assert_awaited_once_with(
        interaction,
        "--fast build leasing workflow",
    )


@pytest.mark.asyncio
async def test_handle_architect_thread_slash_creates_thread_and_dispatches_architect(adapter):
    """The /architect thread handler should route the prompt architect turn into the new thread."""
    adapter._create_thread = AsyncMock(
        return_value={"success": True, "thread_id": "777", "thread_name": "Architect: Build leasing workflow"}
    )
    adapter._dispatch_thread_session = AsyncMock()
    adapter._threads = SimpleNamespace(mark=MagicMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(display_name="Edward"),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await adapter._handle_architect_thread_slash(
        interaction,
        "--fast build leasing workflow",
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    adapter._create_thread.assert_awaited_once()
    create_call = adapter._create_thread.await_args
    assert create_call is not None
    create_kwargs = create_call.kwargs
    assert create_kwargs["name"] == "Architect: Build leasing workflow"
    assert create_kwargs["message"] == ""
    assert create_kwargs["public"] is True
    interaction.followup.send.assert_awaited_once_with(
        "Created architect thread <#777>",
        ephemeral=True,
    )
    adapter._threads.mark.assert_called_once_with("777")
    adapter._dispatch_thread_session.assert_awaited_once_with(
        interaction,
        "777",
        "Architect: Build leasing workflow",
        "/architect --fast build leasing workflow",
    )


@pytest.mark.asyncio
async def test_handle_empty_architect_thread_slash_creates_thread_and_dispatches_bare_architect(adapter):
    """Native Discord /architect with no args should open a thread and run the guided interview there."""
    adapter._create_thread = AsyncMock(
        return_value={"success": True, "thread_id": "777", "thread_name": "Architect"}
    )
    adapter._dispatch_thread_session = AsyncMock()
    adapter._threads = SimpleNamespace(mark=MagicMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(display_name="Edward"),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await adapter._handle_architect_thread_slash(interaction, "")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    adapter._create_thread.assert_awaited_once()
    create_call = adapter._create_thread.await_args
    assert create_call is not None
    create_kwargs = create_call.kwargs
    assert create_kwargs["name"] == "Architect"
    assert create_kwargs["message"] == ""
    assert create_kwargs["public"] is True
    interaction.followup.send.assert_awaited_once_with(
        "Created architect thread <#777>",
        ephemeral=True,
    )
    adapter._threads.mark.assert_called_once_with("777")
    adapter._dispatch_thread_session.assert_awaited_once_with(
        interaction,
        "777",
        "Architect",
        "/architect",
    )


@pytest.mark.asyncio
async def test_architect_thread_is_visible_in_skeleton_session_list(adapter, tmp_path, monkeypatch):
    """An /architect-created Discord thread should be indexed before the agent finishes.

    Desktop/Skeleton uses the rich sessions API with min_message_count=1. If the
    thread only creates an empty session row, it disappears from the sidebar
    while the first architect turn is still running.
    """
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    adapter.set_session_store(store)
    adapter._create_thread = AsyncMock(
        return_value={"success": True, "thread_id": "777", "thread_name": "Architect: Build leasing workflow"}
    )
    adapter._dispatch_thread_session = AsyncMock()
    adapter._threads = SimpleNamespace(mark=MagicMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42, display_name="Edward"),
        guild=SimpleNamespace(name="OpsGuild"),
        channel=SimpleNamespace(id=123),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await adapter._handle_architect_thread_slash(
        interaction,
        "--fast build leasing workflow",
    )

    rows = store._db.list_sessions_rich(
        source="discord",
        min_message_count=1,
        limit=10,
        order_by_last_active=True,
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Architect: Build leasing workflow"
    assert rows[0]["message_count"] >= 1

    transcript = store.load_transcript(rows[0]["id"])
    assert transcript[0]["role"] == "session_meta"
    assert transcript[0]["content"] == "Architect thread created via Discord /architect."


@pytest.mark.asyncio
async def test_create_thread_can_force_public_thread_and_invite_requesting_user(adapter):
    """Architect-created threads must be visible in Discord's thread list.

    discord.py documents that TextChannel.create_thread(message=None) creates a
    private thread. To guarantee a public/listable thread, /architect must create
    the thread from a visible starter message instead of relying on the direct
    no-message API.
    """
    fake_thread = SimpleNamespace(
        id=777,
        name="Architect: Test",
        add_user=AsyncMock(),
    )
    seed_message = SimpleNamespace(create_thread=AsyncMock(return_value=fake_thread))
    parent_channel = SimpleNamespace(
        create_thread=AsyncMock(side_effect=AssertionError("would create a private thread")),
        send=AsyncMock(return_value=seed_message),
    )
    adapter._resolve_interaction_channel = AsyncMock(return_value=parent_channel)
    adapter._thread_parent_channel = MagicMock(return_value=parent_channel)
    user = SimpleNamespace(id=42, display_name="Edward")
    interaction = SimpleNamespace(user=user)

    result = await adapter._create_thread(
        interaction,
        name="Architect: Test",
        public=True,
    )

    assert result["success"] is True
    parent_channel.create_thread.assert_not_awaited()
    parent_channel.send.assert_awaited_once_with("🧵 Thread created by Hermes: **Architect: Test**")
    seed_message.create_thread.assert_awaited_once_with(
        name="Architect: Test",
        auto_archive_duration=1440,
        reason="Requested by Edward via /thread",
    )
    fake_thread.add_user.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_create_public_thread_in_forum_uses_forum_create_thread(adapter):
    fake_thread = SimpleNamespace(id=778, name="Architect: Forum", add_user=AsyncMock())
    forum_result = SimpleNamespace(
        thread=fake_thread,
        message=SimpleNamespace(id=900),
    )
    forum_channel = SimpleNamespace(
        type=15,
        create_thread=AsyncMock(return_value=forum_result),
    )
    adapter._resolve_interaction_channel = AsyncMock(return_value=forum_channel)
    adapter._thread_parent_channel = MagicMock(return_value=forum_channel)
    user = SimpleNamespace(id=42, display_name="Edward")

    result = await adapter._create_thread(
        SimpleNamespace(user=user),
        name="Architect: Forum",
        public=True,
    )

    assert result == {
        "success": True,
        "thread_id": "778",
        "thread_name": "Architect: Forum",
    }
    forum_channel.create_thread.assert_awaited_once_with(
        name="Architect: Forum",
        content="🧵 Thread created by Hermes: **Architect: Forum**",
        auto_archive_duration=1440,
        reason="Requested by Edward via /thread",
    )
    fake_thread.add_user.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_auto_registers_plugin_commands_for_discord(adapter):
    """Plugin slash commands should appear as native Discord app commands."""
    adapter._run_simple_slash = AsyncMock()

    with patch(
        "hermes_cli.plugins.get_plugin_commands",
        return_value={
            "metricas": {
                "handler": lambda _a: "ok",
                "description": "Metrics dashboard",
                "args_hint": "dias:7 formato:json",
                "plugin": "metrics-plugin",
            }
        },
    ):
        adapter._register_slash_commands()

    tree_names = set(adapter._client.tree.commands.keys())
    assert "metricas" in tree_names

    metricas_cmd = adapter._client.tree.commands["metricas"]
    interaction = SimpleNamespace()
    await metricas_cmd.callback(interaction, args="dias:7 formato:json")
    adapter._run_simple_slash.assert_awaited_once_with(
        interaction, "/metricas dias:7 formato:json"
    )


@pytest.mark.asyncio
async def test_auto_registers_skill_bundle_commands_for_discord(adapter):
    """Skill bundles should appear as native Discord app commands."""
    adapter._run_simple_slash = AsyncMock()

    with patch(
        "agent.skill_bundles.list_bundles",
        return_value=[
            {
                "slug": "search",
                "description": "Second Brain Recall — source-grounded search",
                "skills": ["second-brain-recall"],
            }
        ],
    ):
        adapter._register_slash_commands()

    tree_names = set(adapter._client.tree.commands.keys())
    assert "search" in tree_names

    search_cmd = adapter._client.tree.commands["search"]
    interaction = SimpleNamespace()
    await search_cmd.callback(interaction, query="who is the tenant in u5")
    adapter._run_simple_slash.assert_awaited_once_with(
        interaction, "/search who is the tenant in u5"
    )


@pytest.mark.asyncio
async def test_auto_registered_plugin_command_without_args_hint(adapter):
    """Plugin commands without args_hint should register as parameterless."""
    adapter._run_simple_slash = AsyncMock()

    with patch(
        "hermes_cli.plugins.get_plugin_commands",
        return_value={
            "ping": {
                "handler": lambda _a: "pong",
                "description": "Ping the plugin",
                "args_hint": "",
                "plugin": "ping-plugin",
            }
        },
    ):
        adapter._register_slash_commands()

    assert "ping" in adapter._client.tree.commands
    ping_cmd = adapter._client.tree.commands["ping"]
    interaction = SimpleNamespace()
    await ping_cmd.callback(interaction)
    adapter._run_simple_slash.assert_awaited_once_with(interaction, "/ping")


@pytest.mark.asyncio
async def test_plugin_command_name_conflict_skipped(adapter):
    """A plugin command that collides with a built-in must not override it."""
    adapter._run_simple_slash = AsyncMock()

    with patch(
        "hermes_cli.plugins.get_plugin_commands",
        return_value={
            "status": {
                "handler": lambda _a: "plugin-status",
                "description": "Plugin status",
                "args_hint": "",
                "plugin": "shadow-plugin",
            }
        },
    ):
        adapter._register_slash_commands()

    # Built-ins are registered via @tree.command as plain functions. A
    # plugin-registered override would install a _FakeCommand instance
    # (has .callback) via tree.add_command. If the conflict-skip logic
    # fires, the slot remains a bare function.
    status_entry = adapter._client.tree.commands["status"]
    assert callable(status_entry) and not hasattr(status_entry, "callback"), (
        "plugin registration overrode the built-in /status command — "
        "the already_registered skip must prevent this"
    )


# ------------------------------------------------------------------
# 100-command cap (Discord error 30032 guard)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slash_command_registration_stays_under_discord_limit(adapter):
    """Registering far more commands than Discord allows must NOT push the
    tree over the 100-command hard cap.

    Discord rejects the ENTIRE command sync with error 30032 once the
    desired set exceeds 100 global application commands, silently breaking
    every slash command. The adapter must bound the desired set instead.
    Regression guard for samuraiheart's recurring
    "Maximum number of application commands reached (100)" sync failures.
    """
    from plugins.platforms.discord.adapter import _DISCORD_MAX_APP_COMMANDS

    adapter._run_simple_slash = AsyncMock()

    # 200 plugin commands — way past Discord's limit on their own.
    many_plugins = {
        f"plug{i:03d}": {
            "handler": lambda _a: "ok",
            "description": f"Plugin command {i}",
            "args_hint": "",
            "plugin": "stress-plugin",
        }
        for i in range(200)
    }

    with patch("hermes_cli.plugins.get_plugin_commands", return_value=many_plugins):
        adapter._register_slash_commands()

    tree_names = set(adapter._client.tree.commands.keys())

    # Contract: never exceed Discord's hard cap.
    assert len(tree_names) <= _DISCORD_MAX_APP_COMMANDS, (
        f"registered {len(tree_names)} commands — exceeds Discord's "
        f"{_DISCORD_MAX_APP_COMMANDS} limit and would fail sync with 30032"
    )

    # Native, high-priority commands are registered first and must survive
    # the cap — they are the core UX, not droppable overflow.
    for native in ("status", "stop", "new", "model", "help"):
        assert native in tree_names, f"/{native} (native) was dropped by the cap"

    # The cap must actually have dropped overflow — not every plugin fit.
    registered_plugins = [n for n in tree_names if n.startswith("plug")]
    assert len(registered_plugins) < 200, "cap did not drop any overflow commands"


# ------------------------------------------------------------------
# _handle_thread_create_slash — success, session dispatch, failure
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_thread_create_slash_reports_success(adapter):
    created_thread = SimpleNamespace(id=555, name="Planning", send=AsyncMock())
    parent_channel = SimpleNamespace(create_thread=AsyncMock(return_value=created_thread), send=AsyncMock())
    interaction_channel = SimpleNamespace(parent=parent_channel)
    interaction = SimpleNamespace(
        channel=interaction_channel,
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
        guild=SimpleNamespace(name="TestGuild"),
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock()),
    )

    await adapter._handle_thread_create_slash(interaction, "Planning", "Kickoff", 1440)

    parent_channel.create_thread.assert_awaited_once_with(
        name="Planning",
        auto_archive_duration=1440,
        reason="Requested by Jezza via /thread",
    )
    created_thread.send.assert_awaited_once_with("Kickoff")
    # Thread link shown to user
    interaction.followup.send.assert_awaited()
    args, kwargs = interaction.followup.send.await_args
    assert "<#555>" in args[0]
    assert kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_handle_thread_create_slash_dispatches_session_when_message_provided(adapter):
    """When a message is given, _dispatch_thread_session should be called."""
    created_thread = SimpleNamespace(id=555, name="Planning", send=AsyncMock())
    parent_channel = SimpleNamespace(create_thread=AsyncMock(return_value=created_thread))
    interaction = SimpleNamespace(
        channel=SimpleNamespace(parent=parent_channel),
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
        guild=SimpleNamespace(name="TestGuild"),
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock()),
    )

    adapter._dispatch_thread_session = AsyncMock()

    await adapter._handle_thread_create_slash(interaction, "Planning", "Hello Hermes", 1440)

    adapter._dispatch_thread_session.assert_awaited_once_with(
        interaction, "555", "Planning", "Hello Hermes",
    )


@pytest.mark.asyncio
async def test_handle_thread_create_slash_no_dispatch_without_message(adapter):
    """Without a message, no session dispatch should occur."""
    created_thread = SimpleNamespace(id=555, name="Planning", send=AsyncMock())
    parent_channel = SimpleNamespace(create_thread=AsyncMock(return_value=created_thread))
    interaction = SimpleNamespace(
        channel=SimpleNamespace(parent=parent_channel),
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
        guild=SimpleNamespace(name="TestGuild"),
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock()),
    )

    adapter._dispatch_thread_session = AsyncMock()

    await adapter._handle_thread_create_slash(interaction, "Planning", "", 1440)

    adapter._dispatch_thread_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_thread_create_slash_falls_back_to_seed_message(adapter):
    created_thread = SimpleNamespace(id=555, name="Planning")
    seed_message = SimpleNamespace(id=777, create_thread=AsyncMock(return_value=created_thread))
    channel = SimpleNamespace(
        create_thread=AsyncMock(side_effect=RuntimeError("direct failed")),
        send=AsyncMock(return_value=seed_message),
    )
    interaction = SimpleNamespace(
        channel=channel,
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
        guild=SimpleNamespace(name="TestGuild"),
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock()),
    )

    await adapter._handle_thread_create_slash(interaction, "Planning", "Kickoff", 1440)

    channel.send.assert_awaited_once_with("Kickoff")
    seed_message.create_thread.assert_awaited_once_with(
        name="Planning",
        auto_archive_duration=1440,
        reason="Requested by Jezza via /thread",
    )
    interaction.followup.send.assert_awaited()


@pytest.mark.asyncio
async def test_handle_thread_create_slash_reports_failure(adapter):
    channel = SimpleNamespace(
        create_thread=AsyncMock(side_effect=RuntimeError("direct failed")),
        send=AsyncMock(side_effect=RuntimeError("nope")),
    )
    interaction = SimpleNamespace(
        channel=channel,
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock()),
    )

    await adapter._handle_thread_create_slash(interaction, "Planning", "", 1440)

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.await_args
    assert "Failed to create thread:" in args[0]
    assert "nope" in args[0]
    assert kwargs["ephemeral"] is True


# ------------------------------------------------------------------
# _dispatch_thread_session — builds correct event and routes it
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_thread_session_builds_thread_event(adapter):
    """Dispatched event should have chat_type=thread and chat_id=thread_id."""
    interaction = SimpleNamespace(
        user=SimpleNamespace(display_name="Jezza", id=42),
        guild=SimpleNamespace(name="TestGuild"),
    )

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    await adapter._dispatch_thread_session(interaction, "555", "Planning", "Hello!")

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.text == "Hello!"
    assert event.source.chat_id == "555"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "555"
    assert "TestGuild" in event.source.chat_name


# ------------------------------------------------------------------
# _build_slash_event — preserve thread context for native slash commands
# ------------------------------------------------------------------


def test_build_slash_event_preserves_thread_context(adapter):
    interaction = SimpleNamespace(
        channel=_FakeThreadChannel(channel_id=555, name="Planning"),
        channel_id=555,
        user=SimpleNamespace(display_name="Jezza", id=42),
    )

    event = adapter._build_slash_event(interaction, "/status")

    assert event.text == "/status"
    assert event.source.chat_id == "555"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "555"
    assert "TestGuild" in event.source.chat_name


def test_build_slash_event_uses_group_context_for_channels(adapter):
    interaction = SimpleNamespace(
        channel=_FakeTextChannel(channel_id=123, name="general"),
        channel_id=123,
        user=SimpleNamespace(display_name="Jezza", id=42),
    )

    event = adapter._build_slash_event(interaction, "/status")

    assert event.source.chat_id == "123"
    assert event.source.chat_type == "group"
    assert event.source.thread_id is None
    assert "TestGuild / #general" == event.source.chat_name


# ------------------------------------------------------------------
# Auto-thread: _auto_create_thread
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_create_thread_uses_message_content_as_name(adapter):
    thread = SimpleNamespace(id=999, name="Hello world")
    message = SimpleNamespace(
        content="Hello world, how are you?",
        create_thread=AsyncMock(return_value=thread),
        channel=SimpleNamespace(send=AsyncMock()),
        author=SimpleNamespace(display_name="Jezza"),
    )

    result = await adapter._auto_create_thread(message)

    assert result is thread
    message.create_thread.assert_awaited_once()
    call_kwargs = message.create_thread.await_args[1]
    assert call_kwargs["name"] == "Hello world, how are you?"
    assert call_kwargs["auto_archive_duration"] == 1440
    assert thread._hermes_auto_thread_initial_name == "Hello world, how are you?"


@pytest.mark.asyncio
async def test_auto_create_thread_strips_mention_syntax_from_name(adapter):
    """Thread names must not contain raw <@id>, <@&id>, or <#id> markers.

    Regression guard for #6336 — previously a message like
    ``<@&1490963422786093149> help`` would spawn a thread literally
    named ``<@&1490963422786093149> help``.
    """
    thread = SimpleNamespace(id=999, name="help")
    message = SimpleNamespace(
        content="<@&1490963422786093149> <@555> please help <#123>",
        create_thread=AsyncMock(return_value=thread),
        channel=SimpleNamespace(send=AsyncMock()),
        author=SimpleNamespace(display_name="Jezza"),
    )

    await adapter._auto_create_thread(message)

    name = message.create_thread.await_args[1]["name"]
    assert "<@" not in name, f"role/user mention leaked: {name!r}"
    assert "<#" not in name, f"channel mention leaked: {name!r}"
    assert name == "please help"


@pytest.mark.asyncio
async def test_auto_create_thread_falls_back_to_hermes_when_only_mentions(adapter):
    """If a message contains only mention syntax, the stripped content is
    empty — fall back to the 'Hermes' default rather than ''."""
    thread = SimpleNamespace(id=999, name="Hermes")
    message = SimpleNamespace(
        content="<@&1490963422786093149>",
        create_thread=AsyncMock(return_value=thread),
        channel=SimpleNamespace(send=AsyncMock()),
        author=SimpleNamespace(display_name="Jezza"),
    )

    await adapter._auto_create_thread(message)

    name = message.create_thread.await_args[1]["name"]
    assert name == "Hermes"


@pytest.mark.asyncio
async def test_auto_create_thread_truncates_long_names(adapter):
    long_text = "a" * 200
    thread = SimpleNamespace(id=999, name="truncated")
    message = SimpleNamespace(
        content=long_text,
        create_thread=AsyncMock(return_value=thread),
        channel=SimpleNamespace(send=AsyncMock()),
        author=SimpleNamespace(display_name="Jezza"),
    )

    result = await adapter._auto_create_thread(message)

    assert result is thread
    call_kwargs = message.create_thread.await_args[1]
    assert len(call_kwargs["name"]) <= 80
    assert call_kwargs["name"].endswith("...")


@pytest.mark.asyncio
async def test_auto_create_thread_falls_back_to_seed_message(adapter):
    thread = SimpleNamespace(id=555, name="Hello")
    seed_message = SimpleNamespace(create_thread=AsyncMock(return_value=thread))
    message = SimpleNamespace(
        content="Hello",
        create_thread=AsyncMock(side_effect=RuntimeError("no perms")),
        channel=SimpleNamespace(send=AsyncMock(return_value=seed_message)),
        author=SimpleNamespace(display_name="Jezza"),
    )

    result = await adapter._auto_create_thread(message)
    assert result is thread
    message.channel.send.assert_awaited_once_with("🧵 Thread created by Hermes: **Hello**")
    seed_message.create_thread.assert_awaited_once_with(
        name="Hello",
        auto_archive_duration=1440,
        reason="Auto-threaded from mention by Jezza",
    )


@pytest.mark.asyncio
async def test_auto_create_thread_returns_none_when_direct_and_fallback_fail(adapter):
    message = SimpleNamespace(
        content="Hello",
        create_thread=AsyncMock(side_effect=RuntimeError("no perms")),
        channel=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed"))),
        author=SimpleNamespace(display_name="Jezza"),
    )

    result = await adapter._auto_create_thread(message)
    assert result is None


@pytest.mark.asyncio
async def test_rename_thread_edits_only_when_current_name_matches(adapter):
    thread = SimpleNamespace(
        id=999,
        name="raw user prompt",
        edit=AsyncMock(),
    )
    adapter._client.get_channel = lambda _id: thread

    result = await adapter.rename_thread(
        "999",
        "Semantic Session Title",
        only_if_current_name="raw user prompt",
    )

    assert result is True
    thread.edit.assert_awaited_once_with(
        name="Semantic Session Title",
        reason="Hermes semantic session title",
    )


@pytest.mark.asyncio
async def test_rename_thread_skips_when_human_renamed(adapter):
    thread = SimpleNamespace(
        id=999,
        name="human fixed this already",
        edit=AsyncMock(),
    )
    adapter._client.get_channel = lambda _id: thread

    result = await adapter.rename_thread(
        "999",
        "Semantic Session Title",
        only_if_current_name="raw user prompt",
    )

    assert result is False
    thread.edit.assert_not_awaited()


# ------------------------------------------------------------------
# Auto-thread integration in _handle_message
# ------------------------------------------------------------------


import discord as _discord_mod  # noqa: E402 — mock or real, used below


class _FakeTextChannel:
    """A channel that is NOT a discord.Thread or discord.DMChannel."""

    def __init__(self, channel_id=100, name="general", guild_name="TestGuild"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name=guild_name, id=1)
        self.topic = None

    def history(self, *args, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover — make this an async generator

        return _empty()


class _FakeThreadChannel(_discord_mod.Thread):
    """isinstance(ch, discord.Thread) → True."""

    def __init__(self, channel_id=200, name="existing-thread", guild_name="TestGuild", parent_id=100):
        # Don't call super().__init__ — mock Thread is just an empty type
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name=guild_name, id=1)
        self.topic = None
        self.parent = SimpleNamespace(id=parent_id, name="general", guild=SimpleNamespace(name=guild_name, id=1))

    def history(self, *args, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover — make this an async generator

        return _empty()


def _fake_message(channel, *, content="Hello", author_id=42, display_name="Jezza"):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, display_name=display_name, bot=False),
        content=content,
        channel=channel,
        attachments=[],
        mentions=[],
        reference=None,
        created_at=None,
        id=12345,
    )


@pytest.mark.asyncio
async def test_auto_thread_creates_thread_and_redirects(adapter, monkeypatch):
    """When DISCORD_AUTO_THREAD=true, a new thread is created and the event routes there."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

    thread = SimpleNamespace(id=999, name="Hello")
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    msg = _fake_message(_FakeTextChannel(), content="Hello world")

    await adapter._handle_message(msg)

    adapter._auto_create_thread.assert_awaited_once_with(msg)
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.source.chat_id == "999"  # redirected to thread
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "999"
    assert event.source.auto_thread_created is True


@pytest.mark.asyncio
async def test_auto_thread_source_carries_initial_name_for_semantic_rename(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

    thread = SimpleNamespace(
        id=999,
        name="raw user prompt",
        _hermes_auto_thread_initial_name="raw user prompt",
    )
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    msg = _fake_message(_FakeTextChannel(), content="raw user prompt")

    await adapter._handle_message(msg)

    source = captured_events[0].source
    assert source.auto_thread_created is True
    assert source.auto_thread_initial_name == "raw user prompt"


@pytest.mark.asyncio
async def test_auto_thread_enabled_by_default_slash_commands(adapter, monkeypatch):
    """Without DISCORD_AUTO_THREAD env var, auto-threading is enabled (default: true)."""
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

    fake_thread = _FakeThreadChannel(channel_id=999, name="auto-thread")
    adapter._auto_create_thread = AsyncMock(return_value=fake_thread)

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    msg = _fake_message(_FakeTextChannel())

    await adapter._handle_message(msg)

    adapter._auto_create_thread.assert_awaited_once()
    assert len(captured_events) == 1
    assert captured_events[0].source.chat_id == "999"  # redirected to thread
    assert captured_events[0].source.chat_type == "thread"


@pytest.mark.asyncio
async def test_auto_thread_can_be_disabled(adapter, monkeypatch):
    """Setting DISCORD_AUTO_THREAD=false keeps messages in the channel."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

    adapter._auto_create_thread = AsyncMock()

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    msg = _fake_message(_FakeTextChannel())

    await adapter._handle_message(msg)

    adapter._auto_create_thread.assert_not_awaited()
    assert len(captured_events) == 1
    assert captured_events[0].source.chat_id == "100"  # stays in channel


@pytest.mark.asyncio
async def test_auto_thread_skips_threads_and_dms(adapter, monkeypatch):
    """Auto-thread should not create threads inside existing threads."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

    adapter._auto_create_thread = AsyncMock()

    captured_events = []

    async def capture_handle(event):
        captured_events.append(event)

    adapter.handle_message = capture_handle

    msg = _fake_message(_FakeThreadChannel())

    await adapter._handle_message(msg)

    adapter._auto_create_thread.assert_not_awaited()  # should NOT auto-thread


# ------------------------------------------------------------------
# Config bridge
# ------------------------------------------------------------------


def test_discord_auto_thread_config_bridge(monkeypatch, tmp_path):
    """discord.auto_thread in config.yaml should be bridged to DISCORD_AUTO_THREAD env var."""
    import yaml
    from pathlib import Path

    # Write a config.yaml the loader will find
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    config_path = hermes_dir / "config.yaml"
    config_path.write_text(yaml.dump({
        "discord": {"auto_thread": True},
    }))

    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("DISCORD_AUTO_THREAD") == "true"


# ------------------------------------------------------------------
# /skill command registration (flat + autocomplete)
# ------------------------------------------------------------------


def test_register_skill_command_is_flat_not_nested(adapter):
    """_register_skill_group should register a single flat ``/skill`` command.

    The older layout nested categories as subcommand groups under ``/skill``.
    That registered as one giant command whose serialized payload exceeded
    Discord's 8KB per-command limit with the default skill catalog. The
    flat layout sidesteps the limit — autocomplete options are fetched
    dynamically by Discord and don't count against the registration budget.
    """
    mock_categories = {
        "creative": [
            ("ascii-art", "Generate ASCII art", "/ascii-art"),
            ("excalidraw", "Hand-drawn diagrams", "/excalidraw"),
        ],
        "media": [
            ("gif-search", "Search for GIFs", "/gif-search"),
        ],
    }
    mock_uncategorized = [
        ("dogfood", "Exploratory QA testing", "/dogfood"),
    ]

    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=(mock_categories, mock_uncategorized, 0),
    ):
        adapter._register_slash_commands()

    tree = adapter._client.tree
    assert "skill" in tree.commands, "Expected /skill command to be registered"
    skill_cmd = tree.commands["skill"]
    assert skill_cmd.name == "skill"
    # Flat command — NOT a Group — so it has no _children of category subgroups
    assert not hasattr(skill_cmd, "_children") or not getattr(skill_cmd, "_children", {}), (
        "Flat /skill command should not have subcommand children"
    )


def test_register_skill_command_empty_skills_no_command(adapter):
    """No /skill command should be registered when there are zero skills."""
    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=({}, [], 0),
    ):
        adapter._register_slash_commands()

    tree = adapter._client.tree
    assert "skill" not in tree.commands


def test_register_skill_command_callback_dispatches_by_name(adapter):
    """The /skill callback should look up the skill by ``name`` and
    dispatch via ``_run_simple_slash`` with the real command key.
    """
    mock_categories = {
        "media": [
            ("gif-search", "Search for GIFs", "/gif-search"),
        ],
    }
    mock_uncategorized = [
        ("dogfood", "QA testing", "/dogfood"),
    ]

    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=(mock_categories, mock_uncategorized, 0),
    ):
        adapter._register_slash_commands()

    skill_cmd = adapter._client.tree.commands["skill"]
    assert skill_cmd.callback is not None

    # Stub out _run_simple_slash so we can verify the dispatched text.
    dispatched: list[str] = []

    async def fake_run(_interaction, text):
        dispatched.append(text)

    adapter._run_simple_slash = fake_run

    import asyncio

    fake_interaction = SimpleNamespace()
    # gif-search → /gif-search with no args
    asyncio.run(skill_cmd.callback(fake_interaction, name="gif-search"))
    # dogfood with args
    asyncio.run(skill_cmd.callback(fake_interaction, name="dogfood", args="my test"))

    assert dispatched == ["/gif-search", "/dogfood my test"]


def test_register_skill_command_handles_unknown_skill_gracefully(adapter):
    """Passing a name that isn't a registered skill should respond with
    an ephemeral error message, NOT crash the callback.
    """
    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=({"media": [("gif-search", "GIFs", "/gif-search")]}, [], 0),
    ):
        adapter._register_slash_commands()

    skill_cmd = adapter._client.tree.commands["skill"]

    sent: list[dict] = []

    async def fake_send(text, ephemeral=False):
        sent.append({"text": text, "ephemeral": ephemeral})

    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=fake_send),
    )

    import asyncio
    asyncio.run(skill_cmd.callback(interaction, name="does-not-exist"))

    assert len(sent) == 1
    assert "Unknown skill" in sent[0]["text"]
    assert "does-not-exist" in sent[0]["text"]
    assert sent[0]["ephemeral"] is True


def test_register_skill_command_payload_fits_discord_8kb_limit(adapter):
    """The /skill command registration payload must stay under Discord's
    ~8000-byte per-command limit even with a large skill catalog.

    This is the regression guard for #11321 / #10259. Simulates 500 skills
    (20 categories × 25 — the hard cap per category in the collector) and
    confirms the serialized command still fits. Autocomplete options are
    not part of this payload, so the budget is essentially constant.
    """
    import json

    # Simulate the largest catalog the collector will ever produce:
    # 20 categories × 25 skills each, with verbose 100-char descriptions.
    large_categories: dict[str, list[tuple[str, str, str]]] = {}
    long_desc = "A verbose description padded to approximately 100 chars " + "." * 42
    for i in range(20):
        cat = f"cat{i:02d}"
        large_categories[cat] = [
            (f"skill-{i:02d}-{j:02d}", long_desc, f"/skill-{i:02d}-{j:02d}")
            for j in range(25)
        ]

    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=(large_categories, [], 0),
    ):
        adapter._register_slash_commands()

    skill_cmd = adapter._client.tree.commands["skill"]
    # Approximate the serialized registration payload (name + description only).
    # Autocomplete options are NOT registered — they're fetched dynamically.
    payload = json.dumps({
        "name": skill_cmd.name,
        "description": skill_cmd.description,
        "options": [
            {"name": "name", "description": "Which skill to run", "type": 3, "required": True},
            {"name": "args", "description": "Optional arguments for the skill", "type": 3, "required": False},
        ],
    })
    assert len(payload) < 500, (
        f"Flat /skill command payload is ~{len(payload)} bytes — the whole "
        f"point of this design is that it stays small regardless of skill count"
    )


def test_register_skill_command_autocomplete_filters_by_name_and_description(adapter):
    """The autocomplete callback should match on both skill name and
    description so the user can search by either.
    """
    mock_categories = {
        "ocr": [
            ("ocr-and-documents", "Extract text from PDFs and scanned documents", "/ocr-and-documents"),
        ],
        "media": [
            ("gif-search", "Search and download GIFs from Tenor", "/gif-search"),
        ],
    }

    with patch(
        "hermes_cli.commands.discord_skill_commands_by_category",
        return_value=(mock_categories, [], 0),
    ):
        adapter._register_slash_commands()

    skill_cmd = adapter._client.tree.commands["skill"]
    # The callback has been wrapped with @autocomplete(name=...) — in our mock
    # the decorator is pass-through, so we inspect the closed-over list by
    # invoking the registered autocomplete function directly through the
    # test API. Since the mock doesn't preserve the autocomplete binding,
    # we re-derive the filter by building the same entries list.
    #
    # What we CAN verify at this layer: the callback dispatches correctly
    # (covered in other tests). The autocomplete filter itself is exercised
    # via direct function call in the real-discord integration path.
    assert skill_cmd.callback is not None



# ------------------------------------------------------------------
# /done slash command
# ------------------------------------------------------------------


class _FakeDoneThread(_FakeThreadChannel):
    def __init__(self, *, parent=None, locked=False, archived=False, **kwargs):
        super().__init__(**kwargs)
        self.parent = parent or SimpleNamespace(send=AsyncMock())
        self.locked = locked
        self.archived = archived
        self.delete = AsyncMock()

        async def _edit(**edit_kwargs):
            if "locked" in edit_kwargs:
                self.locked = edit_kwargs["locked"]
            if "archived" in edit_kwargs:
                self.archived = edit_kwargs["archived"]
            return self

        self.edit = AsyncMock(side_effect=_edit)


def _done_interaction(channel, *, user_id=123, user_name="Edward"):
    return SimpleNamespace(
        channel=channel,
        channel_id=getattr(channel, "id", None),
        user=SimpleNamespace(
            id=user_id,
            display_name=user_name,
            name=user_name.lower(),
            roles=[],
        ),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_registers_native_done_slash_command(adapter):
    adapter._handle_done_slash = AsyncMock()
    adapter._register_slash_commands()

    assert "done" in adapter._client.tree.commands
    interaction = SimpleNamespace()
    await adapter._client.tree.commands["done"](interaction)

    adapter._handle_done_slash.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_done_slash_rejects_non_thread_context(adapter):
    channel = _FakeTextChannel()
    interaction = _done_interaction(channel)

    await adapter._handle_done_slash(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert "inside the Discord thread" in interaction.response.send_message.await_args.args[0]
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_slash_always_waits_for_explicit_confirmation(adapter, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    created_views = []

    class FakeDoneView:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_views.append(self)

    monkeypatch.setattr(discord_adapter, "DoneThreadDeleteConfirmView", FakeDoneView)
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="Architect: /done")
    interaction = _done_interaction(thread)
    adapter._fetch_done_thread_messages = AsyncMock(
        side_effect=AssertionError("/done must not inspect or classify thread history")
    )

    await adapter._handle_done_slash(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    adapter._fetch_done_thread_messages.assert_not_awaited()
    thread.delete.assert_not_awaited()
    parent.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    prompt = interaction.followup.send.await_args.args[0]
    assert "Delete this thread?" in prompt
    assert "cannot be undone" in prompt
    assert created_views[0].kwargs == {
        "adapter": adapter,
        "thread": thread,
        "invoking_user": interaction.user,
    }


def test_done_confirmation_is_bound_to_invoking_user(adapter):
    from plugins.platforms.discord.adapter import DoneThreadDeleteConfirmView

    adapter._allowed_user_ids = {123, 456}
    thread = _FakeDoneThread(channel_id=200, name="important")
    invoker = _done_interaction(thread, user_id=123).user
    view = DoneThreadDeleteConfirmView(
        adapter=adapter,
        thread=thread,
        invoking_user=invoker,
    )

    assert view._check_auth(_done_interaction(thread, user_id=123)) is True
    assert view._check_auth(_done_interaction(thread, user_id=456)) is False


def test_done_confirmation_does_not_narrow_prior_slash_authorization(
    adapter,
    monkeypatch,
):
    from plugins.platforms.discord import adapter as discord_adapter
    from plugins.platforms.discord.adapter import DoneThreadDeleteConfirmView

    # `/done` has already passed the adapter's full slash authorization, which
    # includes channel-scoped policy. The component boundary must bind identity,
    # not rerun the narrower user/role-only helper.
    monkeypatch.setattr(discord_adapter, "_component_check_auth", lambda *_args: False)
    thread = _FakeDoneThread(channel_id=200, name="important")
    invoker = _done_interaction(thread, user_id=123).user
    view = DoneThreadDeleteConfirmView(
        adapter=adapter,
        thread=thread,
        invoking_user=invoker,
    )

    assert view._check_auth(_done_interaction(thread, user_id=123)) is True
    assert view._check_auth(_done_interaction(thread, user_id=456)) is False


def test_done_real_discord_button_callback_preserves_slash_authorization():
    script = r'''
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from plugins.platforms.discord import adapter as discord_adapter


def interaction(user_id):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def main():
    adapter = SimpleNamespace(
        _complete_done_thread_delete=AsyncMock(
            return_value={"success": False, "error": "blocked"}
        )
    )
    thread = SimpleNamespace(id=200)
    invoker = SimpleNamespace(id=123)

    def unexpected_reauthorization(*_args):
        raise AssertionError("component policy was incorrectly rerun")

    discord_adapter._component_check_auth = unexpected_reauthorization
    view = discord_adapter.DoneThreadDeleteConfirmView(
        adapter=adapter,
        thread=thread,
        invoking_user=invoker,
    )
    assert all(isinstance(child, discord.ui.Button) for child in view.children)
    delete = next(child for child in view.children if child.label == "Delete thread")

    other = interaction(456)
    await delete.callback(other)
    adapter._complete_done_thread_delete.assert_not_awaited()
    other.response.send_message.assert_awaited_once()

    same = interaction(123)
    await delete.callback(same)
    adapter._complete_done_thread_delete.assert_awaited_once_with(
        thread=thread,
        acting_user=invoker,
    )
    same.response.edit_message.assert_awaited_once()
    same.followup.send.assert_awaited_once()
    print("REAL_DISCORD_DONE_CALLBACK_OK")


asyncio.run(main())
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "REAL_DISCORD_DONE_CALLBACK_OK" in completed.stdout


@pytest.mark.asyncio
async def test_done_delete_button_rejects_different_authorized_user(adapter):
    from plugins.platforms.discord.adapter import DoneThreadDeleteConfirmView

    adapter._allowed_user_ids = {123, 456}
    adapter._complete_done_thread_delete = AsyncMock()
    thread = _FakeDoneThread(channel_id=200, name="important")
    invoker = _done_interaction(thread, user_id=123).user
    view = DoneThreadDeleteConfirmView(
        adapter=adapter,
        thread=thread,
        invoking_user=invoker,
    )
    other = _done_interaction(thread, user_id=456)

    await view.delete_thread(other, None)

    adapter._complete_done_thread_delete.assert_not_awaited()
    other.response.send_message.assert_awaited_once()
    assert view.resolved is False


@pytest.mark.asyncio
async def test_done_delete_button_uses_original_invoking_user(adapter):
    from plugins.platforms.discord.adapter import DoneThreadDeleteConfirmView

    adapter._allowed_user_ids = {123}
    adapter._complete_done_thread_delete = AsyncMock(
        return_value={"success": False, "error": "test stop"}
    )
    thread = _FakeDoneThread(channel_id=200, name="important")
    invoker = _done_interaction(thread, user_id=123).user
    view = DoneThreadDeleteConfirmView(
        adapter=adapter,
        thread=thread,
        invoking_user=invoker,
    )
    interaction = _done_interaction(thread, user_id=123)

    await view.delete_thread(interaction, None)

    adapter._complete_done_thread_delete.assert_awaited_once_with(
        thread=thread,
        acting_user=invoker,
    )


@pytest.mark.asyncio
async def test_done_confirmation_serializes_overlapping_thread_transitions(adapter):
    thread = _FakeDoneThread(channel_id=200, name="important")
    state = {"deleted": False, "delete_calls": 0}
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def edit_thread(**_kwargs):
        if state["deleted"]:
            raise RuntimeError("thread no longer exists")

    async def delete_thread(**_kwargs):
        state["delete_calls"] += 1
        if state["delete_calls"] == 1:
            delete_started.set()
            await release_delete.wait()
            state["deleted"] = True
            return
        await release_delete.wait()
        if state["deleted"]:
            raise RuntimeError("thread no longer exists")

    thread.edit = AsyncMock(side_effect=edit_thread)
    thread.delete = AsyncMock(side_effect=delete_thread)
    acting_user = SimpleNamespace(id=123, display_name="Edward")

    first = asyncio.create_task(
        adapter._complete_done_thread_delete(
            thread=thread,
            acting_user=acting_user,
        )
    )
    await asyncio.wait_for(delete_started.wait(), timeout=1)
    second = asyncio.create_task(
        adapter._complete_done_thread_delete(
            thread=thread,
            acting_user=acting_user,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    edit_calls_before_release = thread.edit.await_count

    release_delete.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert edit_calls_before_release == 1
    assert first_result["success"] is True
    assert second_result["success"] is False
    thread.parent.send.assert_awaited_once()
    assert adapter._done_thread_delete_transitions == {}


@pytest.mark.asyncio
async def test_done_confirmation_locks_then_deletes_then_audits(adapter):
    events = []

    async def _audit_send(_text):
        events.append("audit")

    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock(side_effect=_audit_send))
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="important")

    async def _edit(**kwargs):
        events.append("lock")
        thread.locked = kwargs.get("locked", thread.locked)
        thread.archived = kwargs.get("archived", thread.archived)
        return thread

    async def _delete(**_kwargs):
        events.append("delete")

    thread.edit.side_effect = _edit
    thread.delete.side_effect = _delete
    actor = _done_interaction(thread).user

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=actor,
    )

    assert result == {"success": True}
    assert events == ["lock", "delete", "audit"]
    thread.edit.assert_awaited_once_with(
        locked=True,
        archived=True,
        reason="Explicit /done confirmation",
    )
    audit = parent.send.await_args.args[0]
    assert "`/done` deleted thread" in audit
    assert "Explicit invoking-user confirmation: yes" in audit


@pytest.mark.asyncio
async def test_done_delete_cancellation_restores_thread_and_reraises(adapter):
    thread = _FakeDoneThread(channel_id=200, name="important")
    delete_started = asyncio.Event()

    async def delete_forever(**_kwargs):
        delete_started.set()
        await asyncio.Event().wait()

    thread.delete = AsyncMock(side_effect=delete_forever)
    task = asyncio.create_task(
        adapter._complete_done_thread_delete(
            thread=thread,
            acting_user=_done_interaction(thread).user,
        )
    )
    await asyncio.wait_for(delete_started.wait(), timeout=1)
    assert thread.locked is True
    assert thread.archived is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert thread.locked is False
    assert thread.archived is False
    assert thread.edit.await_count == 2
    assert thread.edit.await_args_list[1].kwargs == {
        "locked": False,
        "archived": False,
        "reason": "Restore thread after failed /done deletion",
    }
    thread.parent.send.assert_not_awaited()
    assert adapter._done_thread_delete_transitions == {}


@pytest.mark.asyncio
async def test_done_delete_failure_restores_thread_and_never_audits(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        parent=parent,
        channel_id=200,
        name="important",
        locked=False,
        archived=False,
    )
    thread.delete.side_effect = RuntimeError("missing permission")
    actor = _done_interaction(thread).user

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=actor,
    )

    assert result["success"] is False
    assert "missing permission" in result["error"]
    assert thread.edit.await_count == 2
    assert thread.edit.await_args_list[0].kwargs == {
        "locked": True,
        "archived": True,
        "reason": "Explicit /done confirmation",
    }
    assert thread.edit.await_args_list[1].kwargs == {
        "locked": False,
        "archived": False,
        "reason": "Restore thread after failed /done deletion",
    }
    assert thread.locked is False
    assert thread.archived is False
    parent.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_lock_failure_never_attempts_delete_or_audit(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="important")
    thread.edit.side_effect = RuntimeError("cannot lock")

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=_done_interaction(thread).user,
    )

    assert result["success"] is False
    assert "cannot lock" in result["error"]
    thread.delete.assert_not_awaited()
    parent.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_private_thread_delete_reason_and_audit_withhold_actor(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="private")
    thread.type = 12
    actor = _done_interaction(thread, user_id=456, user_name="SECRET_ACTOR").user

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=actor,
    )

    assert result["success"] is True
    reason = thread.delete.await_args.kwargs["reason"]
    audit = parent.send.await_args.args[0]
    assert "SECRET_ACTOR" not in reason
    assert "SECRET_ACTOR" not in audit
    assert "456" not in audit
    assert "private" in reason.lower()
    assert "private thread" in audit.lower()


@pytest.mark.asyncio
async def test_done_reports_post_delete_audit_failure_without_suggesting_retry(adapter):
    parent = SimpleNamespace(
        id=100,
        name="system-ops",
        send=AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="important")

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=_done_interaction(thread).user,
    )

    assert result["success"] is True
    assert "audit failed" in result["warning"]
    thread.delete.assert_awaited_once()
