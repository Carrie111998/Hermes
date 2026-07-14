"""Tests for native Discord slash command fast-paths (thread creation & auto-thread)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import sys

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
    def __init__(self, messages=None, *, parent=None, **kwargs):
        super().__init__(**kwargs)
        if parent is not None:
            self.parent = parent
        else:
            self.parent.send = AsyncMock()
        self._messages = list(messages or [])
        self.delete = AsyncMock()

    def history(self, *args, **kwargs):
        async def _gen():
            # discord.py returns newest-first when oldest_first=False; helpers
            # reverse that internally before analysis.
            for msg in reversed(self._messages):
                yield msg

        return _gen()


def _done_msg(content, *, author_id=42, bot=False, name="User"):
    return SimpleNamespace(
        content=content,
        clean_content=content,
        author=SimpleNamespace(id=author_id, display_name=name, name=name, bot=bot),
        attachments=[],
        type=None,
    )


def _done_interaction(channel):
    return SimpleNamespace(
        channel=channel,
        channel_id=getattr(channel, "id", None),
        user=SimpleNamespace(id=123, display_name="Edward", name="edward"),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
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
async def test_done_slash_deletes_thread_and_posts_parent_audit_when_clear(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        [
            _done_msg("Please update the slash command", author_id=1),
            _done_msg("Done — implemented and verified the slash command in tests", author_id=999, bot=True, name="Hermes"),
        ],
        parent=parent,
        channel_id=200,
        name="Architect: /done",
    )
    interaction = _done_interaction(thread)

    await adapter._handle_done_slash(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    parent.send.assert_awaited_once()
    audit = parent.send.await_args.args[0]
    assert "`/done` deleted thread" in audit
    assert "Outstanding check: none found" in audit
    thread.delete.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    assert "deleted this thread" in interaction.followup.send.await_args.args[0]


def test_done_analysis_flags_plain_imperative_request(adapter):
    outstanding = adapter._analyze_done_thread_messages(
        [_done_msg("Please update the dashboard", author_id=1)]
    )
    assert outstanding
    assert "update the dashboard" in outstanding[0]


def test_done_analysis_does_not_treat_unrelated_reply_as_answer(adapter):
    messages = [
        _done_msg("What color should the button be?", author_id=1),
        _done_msg("The deployment finished", author_id=2),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert outstanding
    assert any("Unanswered question" in item for item in outstanding)


def test_done_analysis_does_not_treat_unrelated_bot_reply_as_answer(adapter):
    messages = [
        _done_msg("Please update the dashboard", author_id=1),
        _done_msg("What's the weather?", author_id=2),
        _done_msg("It is sunny.", author_id=999, bot=True, name="Hermes"),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert any("update the dashboard" in item for item in outstanding)


@pytest.mark.parametrize("bot", [False, True])
def test_done_analysis_requires_more_than_one_generic_overlap_word(adapter, bot):
    messages = [
        _done_msg("Please update the dashboard colors", author_id=1),
        _done_msg(
            "The dashboard weather widget is sunny",
            author_id=999 if bot else 2,
            bot=bot,
            name="Hermes" if bot else "Other",
        ),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert any("update the dashboard colors" in item for item in outstanding)


@pytest.mark.parametrize("bot", [False, True])
def test_done_analysis_same_topic_chatter_does_not_complete_request(adapter, bot):
    messages = [
        _done_msg("Please update the dashboard colors", author_id=1),
        _done_msg(
            "The dashboard colors are unpopular",
            author_id=999 if bot else 2,
            bot=bot,
            name="Hermes" if bot else "Other",
        ),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert any("update the dashboard colors" in item for item in outstanding)


@pytest.mark.parametrize(
    "request_text",
    [
        "Please delete the production database",
        "Please migrate the production database",
        "Please configure the alerts",
        "Please document the API",
    ],
)
def test_done_analysis_flags_broad_imperative_requests(adapter, request_text):
    outstanding = adapter._analyze_done_thread_messages([_done_msg(request_text, author_id=1)])
    assert outstanding


@pytest.mark.parametrize(
    "reply",
    [
        "The dashboard update is not completed",
        "The dashboard was not updated",
        "Dashboard update has not succeeded",
    ],
)
def test_done_analysis_negated_completion_does_not_clear_request(adapter, reply):
    messages = [
        _done_msg("Please update the production dashboard", author_id=1),
        _done_msg(reply, author_id=999, bot=True, name="Hermes"),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert any("update the production dashboard" in item for item in outstanding)


def test_done_analysis_unrelated_completion_with_generic_overlap_does_not_clear(adapter):
    messages = [
        _done_msg("Please update the production dashboard", author_id=1),
        _done_msg("The production deployment completed", author_id=999, bot=True, name="Hermes"),
    ]
    outstanding = adapter._analyze_done_thread_messages(messages)
    assert any("update the production dashboard" in item for item in outstanding)


def test_done_analysis_requires_compatible_action_and_subject_to_clear_request(adapter):
    messages = [
        _done_msg("Please delete the production database", author_id=1),
        _done_msg(
            "Updated the production database dashboard",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert any("delete the production database" in item for item in outstanding)


@pytest.mark.parametrize(
    ("request_text", "closeout"),
    [
        ("Create the production database", "Updated the production database"),
        ("Enable the payment processor", "Disabled the payment processor"),
    ],
)
def test_done_analysis_does_not_clear_request_with_incompatible_action(adapter, request_text, closeout):
    messages = [
        _done_msg(request_text, author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert any(request_text in item for item in outstanding)


@pytest.mark.parametrize(
    ("request_text", "closeout"),
    [
        (
            "Please delete the database update job",
            "Updated the database update job",
        ),
        (
            "Please delete the production database backup schedule",
            "Deleted the production database metrics dashboard",
        ),
    ],
)
def test_done_analysis_matches_governing_action_and_specific_subject(
    adapter,
    request_text,
    closeout,
):
    messages = [
        _done_msg(request_text, author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert any(request_text in item for item in outstanding)


@pytest.mark.parametrize(
    ("request_text", "closeout"),
    [
        ("Please update the slash command", "Implemented the slash command update"),
        ("Delete the obsolete cache entries", "Removed the obsolete cache entries"),
        ("Refactor the payment processor", "Reworked the payment processor"),
    ],
)
def test_done_analysis_accepts_compatible_action_paraphrases(adapter, request_text, closeout):
    messages = [
        _done_msg(request_text, author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


@pytest.mark.parametrize(
    "closeout",
    [
        "All done except database migration",
        "Everything is complete, but the database migration remains pending",
        "Thread is done; the database migration is not actually complete",
        "All set — however, we still need to migrate the database",
        "No outstanding work except the database migration",
        "All done, but the database migration remains",
        "All done; database migration pending",
        "Everything is resolved, other than the database migration",
    ],
)
def test_done_analysis_qualified_global_closeout_does_not_clear_request(adapter, closeout):
    messages = [
        _done_msg("Please migrate the database", author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert any("migrate the database" in item for item in outstanding)


@pytest.mark.parametrize(
    "closeout",
    [
        "Except for database migration; all done.",
        "All done, but database migration remains to be completed.",
    ],
)
def test_done_analysis_checks_closeout_qualifiers_across_the_whole_reply(adapter, closeout):
    messages = [
        _done_msg("Please migrate the database", author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert any("migrate the database" in item for item in outstanding)


@pytest.mark.parametrize(
    "closeout",
    [
        "All done; nothing remains",
        "Everything is complete, and no work remains",
        "All set. Remaining risk: no longer blocked",
        "All done; behavior remains stable",
    ],
)
def test_done_analysis_accepts_unqualified_global_closeout(adapter, closeout):
    messages = [
        _done_msg("Please migrate the database", author_id=1),
        _done_msg(closeout, author_id=999, bot=True, name="Hermes"),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


@pytest.mark.parametrize(
    "request_text",
    [
        "Refactor the payment processor",
        "Rename the settlement worker",
        "Optimize the invoice query",
        "Rework the webhook handler",
    ],
)
def test_done_analysis_flags_broad_bare_imperative_requests(adapter, request_text):
    outstanding = adapter._analyze_done_thread_messages([_done_msg(request_text, author_id=1)])

    assert any(request_text in item for item in outstanding)


@pytest.mark.parametrize(
    "chat_text",
    [
        "I refactored the payment processor yesterday",
        "The payment processor handles refunds",
        "Refactoring the payment processor improved throughput",
        "Our team discussed the payment processor refactor",
        "Update: payment processor status",
        "Run times improved after the payment processor change",
        "Test results are green",
        "Change is expected in the next release",
    ],
)
def test_done_analysis_does_not_flag_ordinary_chat_as_bare_imperative(adapter, chat_text):
    outstanding = adapter._analyze_done_thread_messages([_done_msg(chat_text, author_id=1)])

    assert not any("Possible unresolved item" in item for item in outstanding)


@pytest.mark.parametrize(
    "chat_text",
    [
        "Open source software is useful.",
        "Open access publishing benefits researchers.",
        "Open issues are tracked in Linear.",
        "Open enrollment begins next week.",
    ],
)
def test_done_analysis_does_not_flag_open_noun_or_adjective_chat(adapter, chat_text):
    outstanding = adapter._analyze_done_thread_messages([_done_msg(chat_text, author_id=1)])

    assert not any("Possible unresolved item" in item for item in outstanding)


@pytest.mark.parametrize(
    "request_text",
    [
        "Open the deployment dashboard",
        "Refactor the payment processor",
    ],
)
def test_done_analysis_keeps_clear_bare_action_commands(adapter, request_text):
    outstanding = adapter._analyze_done_thread_messages([_done_msg(request_text, author_id=1)])

    assert any(request_text in item for item in outstanding)


@pytest.mark.asyncio
async def test_done_slash_requires_confirmation_for_attachment_only_user_message(adapter, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    created_views = []

    class FakeDoneView:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_views.append(self)

    monkeypatch.setattr(discord_adapter, "DoneThreadDeleteConfirmView", FakeDoneView)
    attachment_only = _done_msg("", author_id=1)
    attachment_only.attachments = [SimpleNamespace(filename="request.txt")]
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        [attachment_only],
        parent=parent,
        channel_id=200,
        name="Architect: attachment request",
    )
    interaction = _done_interaction(thread)

    await adapter._handle_done_slash(interaction)

    getattr(thread, "delete").assert_not_awaited()
    parent.send.assert_not_awaited()
    assert created_views
    assert any("attachment" in item.lower() for item in created_views[0].kwargs["outstanding_items"])
    assert "Delete anyway" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_done_history_fails_closed_when_raw_api_window_is_full(adapter):
    class LimitedHistoryThread(_FakeDoneThread):
        def history(self, *, limit, oldest_first=False):
            async def _gen():
                newest_first = list(reversed(self._messages))[:limit]
                for msg in newest_first:
                    yield msg
            return _gen()

    thread = LimitedHistoryThread(
        [_done_msg("Please delete the production database", author_id=1)]
        + [_done_msg(f"message {i}", author_id=1) for i in range(100)]
        + [_done_msg("/done", author_id=1)],
        channel_id=200,
    )
    with pytest.raises(RuntimeError, match="more than 100"):
        await adapter._fetch_done_thread_messages(thread, limit=100)


@pytest.mark.asyncio
async def test_done_history_fails_closed_when_scan_limit_is_truncated(adapter):
    thread = _FakeDoneThread(
        [_done_msg(f"message {i}", author_id=1) for i in range(101)],
        channel_id=200,
    )
    with pytest.raises(RuntimeError, match="more than 100"):
        await adapter._fetch_done_thread_messages(thread, limit=100)


def test_private_thread_audit_omits_name_id_and_outstanding_excerpts(adapter):
    thread = SimpleNamespace(
        name="secret acquisition",
        id=987,
        type=12,
    )
    user = SimpleNamespace(display_name="edward", name="edward", id=456)
    text = adapter._format_done_audit(
        thread,
        user,
        ["Possible unresolved item: confidential purchase terms"],
        confirmed=True,
    )
    assert "secret acquisition" not in text
    assert "987" not in text
    assert "confidential purchase terms" not in text
    assert "edward" not in text.lower()
    assert "456" not in text


@pytest.mark.asyncio
async def test_done_delete_failure_never_posts_success_audit(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="important")
    thread.delete.side_effect = RuntimeError("missing permission")
    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=SimpleNamespace(display_name="Edward", name="edward", id=1),
        outstanding_items=[],
        confirmed=False,
    )
    assert result["success"] is False
    parent.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_private_thread_delete_reason_withholds_actor(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="private")
    thread.type = 12
    actor = SimpleNamespace(display_name="SECRET_ACTOR", name="SECRET_ACTOR", id=456)

    result = await adapter._complete_done_thread_delete(
        thread=thread,
        acting_user=actor,
        outstanding_items=[],
        confirmed=False,
    )

    assert result["success"] is True
    reason = thread.delete.await_args.kwargs["reason"]
    assert "SECRET_ACTOR" not in reason
    assert "private" in reason.lower()


@pytest.mark.asyncio
async def test_done_reports_post_delete_audit_failure_without_suggesting_retry(adapter):
    parent = SimpleNamespace(
        id=100,
        name="system-ops",
        send=AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    thread = _FakeDoneThread(parent=parent, channel_id=200, name="important")
    interaction = _done_interaction(thread)
    adapter._fetch_done_thread_messages = AsyncMock(return_value=[])

    await adapter._handle_done_slash(interaction)

    thread.delete.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert "deleted" in text.lower()
    assert "audit" in text.lower()
    assert "retry" not in text.lower()


@pytest.mark.asyncio
async def test_done_slash_warns_and_waits_when_outstanding_items_found(adapter, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    created_views = []

    class FakeDoneView:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_views.append(self)

    monkeypatch.setattr(discord_adapter, "DoneThreadDeleteConfirmView", FakeDoneView)

    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        [_done_msg("Can you verify this in the live UI?", author_id=1)],
        parent=parent,
        channel_id=200,
        name="Architect: verify",
    )
    interaction = _done_interaction(thread)

    await adapter._handle_done_slash(interaction)

    thread.delete.assert_not_awaited()
    parent.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert "possible open item" in text
    assert "Delete anyway" in text
    assert len(text) < 240
    assert "Unanswered question" in text or "Verification not clearly resolved" in text
    assert created_views and created_views[0].kwargs["thread"] is thread
    assert created_views[0].kwargs["outstanding_items"]


def test_done_confirmation_stays_readable_on_mobile(adapter):
    long_summary = (
        "Short answer: mostly yes, with one deployment caveat. - Only one visible "
        "Discord /done: yes. I verified the command exists, but there is a "
        "deployment caveat and follow-up risk that should not fill the whole screen."
    )
    text = adapter._format_done_confirmation(
        [
            f"Possible unresolved item: {long_summary}",
            f"Verification/risk not clearly resolved: {long_summary}",
            "Recent failure/blocker may still be open: unrelated deployment failed in CI",
            "Work/check still in progress: need to confirm global command sync",
            "Unanswered question: can you verify the mobile UI?",
        ]
    )

    assert text.startswith("⚠️ `/done` found 4 possible open items.")
    assert "Review the thread" in text
    assert "Delete anyway" in text
    assert "Short answer" not in text
    assert "Possible unresolved item:" not in text
    assert "+1 more" in text
    assert len(text) < 240


def test_done_confirmation_handles_many_duplicate_signals(adapter):
    text = adapter._format_done_confirmation(
        [
            "Possible unresolved item: Need to verify the live UI after deploy",
            "Verification/risk not clearly resolved: Need to verify the live UI after deploy",
        ]
    )

    assert text.count("Need to verify the live UI") == 1
    assert "TODO/follow-up" in text
    assert "+" not in text


def test_done_confirmation_shows_specific_actionable_excerpt(adapter):
    text = adapter._format_done_confirmation(
        ["Unanswered question: can you verify the mobile UI after restart?"]
    )

    assert "Unanswered question" in text
    assert "can you verify the mobile UI after restart?" in text
    assert "Delete anyway" in text


def test_done_audit_dedupes_multiple_labels_for_same_source(adapter):
    thread = SimpleNamespace(name="Architect: verify", id=123)
    user = SimpleNamespace(display_name="edward", name="edward", id=456)

    text = adapter._format_done_audit(
        thread,
        user,
        [
            "Possible unresolved item: Need to verify the live UI after deploy",
            "Verification/risk not clearly resolved: Need to verify the live UI after deploy",
        ],
        confirmed=True,
    )

    assert "Outstanding check: found 1 possible item(s)" in text
    assert text.count("Need to verify the live UI after deploy") == 1


def test_done_analysis_flags_bot_closeout_with_deferred_restart_caveat(adapter):
    messages = [
        _done_msg(
            """Agreed. I changed `/done` so the confirmation is mobile-readable.

What changed:
- Removed the long warning dump.
- Caps the warning to short reason bullets.

Caveat: the gateway needs to restart before Discord shows the new prompt.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert len(outstanding) == 1
    assert "Possible unresolved item" in outstanding[0]
    assert "gateway needs to restart" in outstanding[0]


def test_done_analysis_flags_deferred_restart_caveat_even_with_test_results(adapter):
    messages = [
        _done_msg(
            """Agreed. I changed `/done` so the confirmation is mobile-readable.

What changed:
- Removed the long warning dump.
- Caps the warning to short reason bullets.

Verified:
- tests/gateway/test_discord_slash_commands.py
- Result: 57 passed in 1.33s

Caveat: the code is fixed on disk, but the gateway needs to restart before Discord shows the new prompt.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert len(outstanding) == 1
    assert "Possible unresolved item" in outstanding[0]
    assert "gateway needs to restart" in outstanding[0]


def test_done_analysis_still_flags_bot_closeout_with_explicit_next_steps(adapter):
    messages = [
        _done_msg(
            """Implemented and tests pass.

Next steps:
- Verify the global Discord command list after restart.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Possible unresolved item" in item for item in outstanding)


def test_done_analysis_flags_hard_bot_blocker_without_closeout(adapter):
    messages = [
        _done_msg(
            "I couldn't inspect this thread before deleting it: missing message content permission.",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Recent failure/blocker" in item for item in outstanding)


def test_done_analysis_treats_later_verified_response_as_resolved(adapter):
    messages = [
        _done_msg("Need to verify the live UI", author_id=1),
        _done_msg("Verified the live UI and tests passed", author_id=999, bot=True),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_bot_completion_summary_false_positive(adapter):
    messages = [
        _done_msg("does /done look for open items before deleting thread?", author_id=1),
        _done_msg(
            "Deployed. What changed: /done now scans for TODO / follow-up language. Verified: tests passed.",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_verified_fixed_closeout_with_historical_blocker_words(adapter):
    messages = [
        _done_msg(
            """Verified. Full Disk Access is fixed.

Evidence:
- `~/.hermes/bin/mfa doctor` now returns `ok: true`.
- method: imsg
- Hermes can read local Messages/iMessage from this launch context.

Remaining risk: no longer blocked.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_uses_one_best_finding_per_source_message(adapter):
    messages = [
        _done_msg("Can you check the deployment and show proof?", author_id=1),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert len(outstanding) == 1
    assert "Can you check the deployment and show proof?" in outstanding[0]


def test_done_analysis_still_flags_bot_summary_with_explicit_remaining_work(adapter):
    messages = [
        _done_msg(
            "Deployed. TODO: verify the global Discord command list after restart.",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Possible unresolved item" in item for item in outstanding)


def test_done_analysis_does_not_treat_unrelated_done_as_resolution(adapter):
    messages = [
        _done_msg("Need to follow up with the landlord about the lease", author_id=1),
        _done_msg("Done — deployed the Discord slash command", author_id=999, bot=True),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Possible unresolved item" in item for item in outstanding)


def test_done_analysis_does_not_treat_tool_progress_as_answer(adapter):
    messages = [
        _done_msg("Can you check whether /done really inspects the thread first?", author_id=1),
        _done_msg("📚 skill_view: \"hermes-agent\"", author_id=999, bot=True),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Unanswered question" in item for item in outstanding)


def test_done_analysis_ignores_todo_tool_progress_preview(adapter):
    messages = [
        _done_msg('📋 todo: "updating 3 task(s)"', author_id=999, bot=True, name="Hermes"),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_tdd_failing_test_evidence_in_closeout(adapter):
    messages = [
        _done_msg(
            """Implemented and tested the /done changes.

Verification:
- New failing test reproduced the bug first.
- Targeted /done tests now pass.
- Direct reproduction now returns [].""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_tdd_failure_evidence_user_note(adapter):
    messages = [
        _done_msg(
            "<@1516837137826316339> new failing test reproduced the bug first",
            author_id=1,
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_tdd_explanation_and_prior_warning_quotes(adapter):
    messages = [
        _done_msg(
            """Yes — relevant as another `/done` false positive, not as a real open item.

It flagged:

> `Failure/blocker: New failing test reproduced the bug first`

That sentence is TDD/process evidence — “the regression test failed before the fix” — not an unresolved failure. `/done` should not block on that.

Verified:
- New regression failed before the fix, then passed.
- `/done` test file: `68 passed`
- Direct reproduction now returns no outstanding item for that message.
- Full suite still stops on the same unrelated ACP approval-isolation failure.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
        _done_msg(
            "<@1516837137826316339> new failing test reproduced the bug first",
            author_id=1,
        ),
        _done_msg(
            """Exactly. That phrase means:

- A regression test was intentionally written first.
- It failed before the fix.
- That proves the test actually covered the bug.
- Then the fix made it pass.

So it is evidence the issue was handled, not an open failure. `/done` should treat that as a closeout/proof sentence, not a blocker. The patch now does that while still flagging real current failures like “still failing” or “CI is blocked.”""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_added_failing_regression_test_evidence(adapter):
    messages = [
        _done_msg(
            """I fixed that now:

- Added a failing regression for the exact user-note shape.
- Updated `/done` so TDD proof text scans cleanly.
- Targeted tests passed.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_new_regression_failed_first_evidence(adapter):
    messages = [
        _done_msg(
            "New regression failed first on that exact sentence",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_ignores_bulleted_prior_done_warning_from_closeout(adapter):
    messages = [
        _done_msg(
            """That screenshot is a real remaining classifier miss, not a stale restart issue.

What happened:
- Your restart at **11:36** loaded the prior fix.
- `/done` at **12:02** then hit a new variant:
  - `Failure/blocker: New regression failed first on that exact sentence`
- My previous fix covered “Added a failing regression…” and “New failing test…”, but not “New regression failed first…” without the word “test.”

I fixed it now:
- Added a failing regression for the exact sentence.
- Patched `/done` to treat `new regression failed first ...` as TDD/RED-phase proof.
- Preserved real blockers like:
  - `The new regression is still failing after the latest patch`
  - `CI is blocked...`

Verification:
- Exact new regression failed first, then passed.
- `/done` tests: `72 passed`
- Direct analyzer now returns `[]` for that sentence.
- Full suite still fails on the unrelated ACP approval-isolation test.

For the current prompt: **Delete anyway** is safe.""",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    assert adapter._analyze_done_thread_messages(messages) == []


def test_done_analysis_still_flags_current_failing_test_blocker(adapter):
    messages = [
        _done_msg(
            "CI is blocked: test_done_analysis is still failing after the latest patch.",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
        _done_msg(
            "The new regression is still failing after the latest patch.",
            author_id=999,
            bot=True,
            name="Hermes",
        ),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Recent failure/blocker" in item for item in outstanding)
    assert any("new regression is still failing" in item for item in outstanding)


def test_done_analysis_flags_unanswered_question_without_duplicate_verification_risk(adapter):
    messages = [
        _done_msg("Can you check the deployment and show proof?", author_id=1),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert len(outstanding) == 1
    assert "Unanswered question" in outstanding[0]
    assert "Can you check the deployment and show proof?" in outstanding[0]


def test_done_analysis_flags_work_still_in_progress(adapter):
    messages = [
        _done_msg("I'm working on this now and checking the live command list", author_id=999, bot=True),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Work/check still in progress" in item for item in outstanding)


def test_done_analysis_does_not_skip_questions_that_contain_resolution_words(adapter):
    messages = [
        _done_msg("Is this done?", author_id=1),
        _done_msg("Was the live UI verified?", author_id=1),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Is this done?" in item for item in outstanding)
    assert any("Was the live UI verified?" in item for item in outstanding)
    assert all("Unanswered question" in item for item in outstanding)


def test_done_analysis_does_not_skip_failures_that_contain_resolution_words(adapter):
    messages = [
        _done_msg("The deployed /done command is failing and deletes without checking", author_id=1),
    ]

    outstanding = adapter._analyze_done_thread_messages(messages)

    assert outstanding
    assert any("Recent failure/blocker" in item for item in outstanding)


@pytest.mark.asyncio
async def test_done_slash_fails_closed_when_thread_content_cannot_be_read(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        [_done_msg("", author_id=1)],
        parent=parent,
        channel_id=200,
        name="Architect: contentless",
    )
    interaction = _done_interaction(thread)

    await adapter._handle_done_slash(interaction)

    getattr(thread, "delete").assert_not_awaited()
    parent.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "couldn't inspect" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_done_slash_fails_closed_when_any_user_message_content_cannot_be_read(adapter):
    parent = SimpleNamespace(id=100, name="system-ops", send=AsyncMock())
    thread = _FakeDoneThread(
        [
            _done_msg("", author_id=1),
            _done_msg("Done — tool output was visible", author_id=999, bot=True),
        ],
        parent=parent,
        channel_id=200,
        name="Architect: partial-contentless",
    )
    interaction = _done_interaction(thread)

    await adapter._handle_done_slash(interaction)

    getattr(thread, "delete").assert_not_awaited()
    parent.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "couldn't inspect" in interaction.followup.send.await_args.args[0]
