"""Bounded, non-multiplex profile-turn routing regression coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore


def _source(**overrides) -> SessionSource:
    values = {
        "platform": Platform.TELEGRAM,
        "chat_id": "-100-route",
        "chat_type": "group",
        "user_id": "user-1",
    }
    values.update(overrides)
    return SessionSource(**values)


def _store(tmp_path, config: GatewayConfig) -> SessionStore:
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    store._db = None
    store._loaded = True
    return store


def _telegram_adapter(
    *,
    require_mention=None,
    mention_patterns=None,
    profile_mention_routes=None,
    allowed_chats=None,
    guest_mode=None,
    exclusive_bot_mentions=None,
):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {
        "allowed_topics": [],
        "allowed_chats": [],
        "group_allowed_chats": [],
    }
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if mention_patterns is not None:
        extra["mention_patterns"] = mention_patterns
    if profile_mention_routes is not None:
        extra["profile_mention_routes"] = profile_mention_routes
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if guest_mode is not None:
        extra["guest_mode"] = guest_mode
    if exclusive_bot_mentions is not None:
        extra["exclusive_bot_mentions"] = exclusive_bot_mentions

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    return adapter


def _group_message(
    text="hello",
    *,
    caption=None,
    chat_id=-100,
    thread_id=None,
    is_forum=False,
    photo=None,
):
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=caption,
        entities=[],
        caption_entities=[],
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        chat=SimpleNamespace(
            id=chat_id, type="group", title="Test Group", is_forum=is_forum
        ),
        from_user=SimpleNamespace(
            id=111, full_name="Alice Example", first_name="Alice"
        ),
        reply_to_message=None,
        date=None,
        photo=photo,
        reply_text=AsyncMock(),
    )


def _dm_message(text="hello"):
    return SimpleNamespace(
        message_id=43,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        chat=SimpleNamespace(
            id=111,
            type="private",
            full_name="Alice Example",
            title=None,
            is_forum=False,
        ),
        from_user=SimpleNamespace(
            id=111, full_name="Alice Example", first_name="Alice"
        ),
        reply_to_message=None,
        date=None,
        reply_text=AsyncMock(),
    )


def _profile_turn_runner(*, target_profiles, static_profile=None, allowlist=None):
    return SimpleNamespace(
        config=SimpleNamespace(
            multiplex_profiles=False,
            profile_turn_allowlist=list(
                target_profiles if allowlist is None else allowlist
            ),
        ),
        _profile_name_for_source=lambda _source: static_profile,
        _profile_turn_target_homes=lambda: {
            profile: object() for profile in target_profiles
        },
    )


def test_profile_mention_route_resolver_normalizes_handles_and_fails_closed():
    from plugins.platforms.telegram.adapter import (
        find_profile_mention_alias,
        resolve_profile_mention_route,
    )

    routes = {"@GLM": "Gemini"}
    assert resolve_profile_mention_route(
        "@glm explain this", {}, {"default", "gemini"}
    ) == (None, False)
    assert resolve_profile_mention_route(
        " \t@glm explain this", routes, {"default", "gemini"}
    ) == ("gemini", True)
    assert resolve_profile_mention_route(
        "@glimmer explain this", routes, {"default", "gemini"}
    ) == (None, False)
    assert resolve_profile_mention_route(
        "@glm_extra explain this", routes, {"default", "gemini"}
    ) == (None, False)
    assert resolve_profile_mention_route("say @glm", routes, {"default", "gemini"}) == (
        None,
        False,
    )
    assert resolve_profile_mention_route(
        "@glm explain this",
        {"glm": "gemini", "@GLM": "other"},
        {"default", "gemini", "other"},
    ) == (None, True)
    assert resolve_profile_mention_route(
        "@glm explain this", {"glm": "missing"}, {"default", "gemini"}
    ) == (None, True)
    assert resolve_profile_mention_route(
        "@glm explain this", {"glm": "not/valid"}, {"default", "gemini"}
    ) == (None, True)
    assert resolve_profile_mention_route(
        "@glm explain this", {"glm": "default"}, {"default"}
    ) == (None, True)
    assert find_profile_mention_alias("please use @glm now", routes) == ("glm", False)
    assert find_profile_mention_alias("@glimmer is not glm", routes) == (None, False)


def test_profile_mention_route_wakes_without_patterns_stamps_and_preserves_text():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"@GLM": "Gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        enqueued = []

        def _enqueue(event):
            enqueued.append((event, adapter._text_batch_key(event)))

        adapter._enqueue_text_event = _enqueue
        update = SimpleNamespace(
            update_id=2001,
            message=_group_message("@GLM keep this handle"),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        assert len(enqueued) == 1
        event, batch_key = enqueued[0]
        assert event.source.profile == "gemini"
        assert event.source.profile_turn_routed is True
        assert event.text == "@GLM keep this handle"
        assert batch_key.startswith("agent:gemini:telegram:")

    asyncio.run(_run())


def test_profile_mention_route_respects_auth_and_allowed_chat_gates():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        adapter._enqueue_text_event = Mock()
        unauthorized = _group_message("@glm blocked by auth")
        adapter._is_callback_user_authorized = lambda *_args, **_kwargs: False
        await adapter._handle_text_message(
            SimpleNamespace(
                update_id=2002, message=unauthorized, effective_message=None
            ),
            SimpleNamespace(),
        )
        adapter._enqueue_text_event.assert_not_called()
        unauthorized.reply_text.assert_not_awaited()

        allowed_adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
            allowed_chats=["-999"],
        )
        allowed_adapter.gateway_runner = _profile_turn_runner(
            target_profiles={"gemini"}
        )
        allowed_adapter._enqueue_text_event = Mock()
        outside_allowlist = _group_message("@glm blocked by chat")
        update = SimpleNamespace(
            update_id=2002,
            message=outside_allowlist,
            effective_message=None,
        )

        await allowed_adapter._handle_text_message(update, SimpleNamespace())

        allowed_adapter._enqueue_text_event.assert_not_called()
        outside_allowlist.reply_text.assert_not_awaited()

    asyncio.run(_run())


def test_profile_mention_route_rejects_unserved_target_before_text_enqueue():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "missing"},
        )
        adapter.gateway_runner = _profile_turn_runner(
            target_profiles=set(), allowlist={"missing"}
        )
        captured = {}
        build_event = adapter._build_message_event

        def _capture_event(*args, **kwargs):
            event = build_event(*args, **kwargs)
            captured["event"] = event
            return event

        adapter._build_message_event = _capture_event
        adapter._enqueue_text_event = Mock()
        message = _group_message("@glm do not fall back")
        update = SimpleNamespace(
            update_id=2003,
            message=message,
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._enqueue_text_event.assert_not_called()
        assert captured["event"].source.profile is None
        assert captured["event"].source.profile_route_rejected is True
        message.reply_text.assert_awaited_once_with(
            "That profile route is unavailable right now."
        )

    asyncio.run(_run())


def test_profile_mention_route_leaves_static_dms_and_secondary_adapters_unchanged():
    adapter = _telegram_adapter(profile_mention_routes={"glm": "gemini"})
    adapter.gateway_runner = _profile_turn_runner(
        target_profiles={"gemini", "static"},
        static_profile="static",
    )
    group_event = adapter._build_message_event(
        _group_message("@glm static route wins"), MessageType.TEXT
    )
    assert (
        adapter._apply_profile_mention_route(
            _group_message("@glm static route wins"), group_event
        )
        is True
    )
    assert group_event.source.profile == "static"

    adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
    dm_event = adapter._build_message_event(_dm_message("@glm dm"), MessageType.TEXT)
    assert (
        adapter._apply_profile_mention_route(_dm_message("@glm dm"), dm_event) is True
    )
    assert dm_event.source.profile is None

    adapter._owner_profile = "default"
    default_event = adapter._build_message_event(
        _group_message("@glm default transport"), MessageType.TEXT
    )
    assert (
        adapter._apply_profile_mention_route(
            _group_message("@glm default transport"), default_event
        )
        is True
    )
    assert default_event.source.profile == "gemini"

    secondary_event = adapter._build_message_event(
        _group_message("@glm secondary"), MessageType.TEXT
    )
    adapter._owner_profile = "other"
    assert (
        adapter._apply_profile_mention_route(
            _group_message("@glm secondary"), secondary_event
        )
        is True
    )
    assert secondary_event.source.profile is None


def test_profile_mention_route_fails_closed_when_allowlist_is_off():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(
            target_profiles={"gemini"}, allowlist=[]
        )
        adapter._enqueue_text_event = Mock()
        message = _group_message("@glm must not fall back to default")
        update = SimpleNamespace(
            update_id=2004,
            message=message,
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._enqueue_text_event.assert_not_called()
        message.reply_text.assert_awaited_once_with(
            "That profile route is unavailable right now."
        )

    asyncio.run(_run())


def test_embedded_alias_with_broad_wake_regex_refuses_in_same_topic():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            mention_patterns=[r"(?i)@glm"],
            profile_mention_routes={"glm": "gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        adapter._enqueue_text_event = Mock()
        message = _group_message(
            "please use @glm after this prefix",
            thread_id=17,
            is_forum=True,
        )

        await adapter._handle_text_message(
            SimpleNamespace(update_id=2005, message=message, effective_message=None),
            SimpleNamespace(),
        )

        adapter._enqueue_text_event.assert_not_called()
        message.reply_text.assert_awaited_once_with(
            "Use a leading profile handle, for example: @glm your request.",
            message_thread_id=17,
        )

    asyncio.run(_run())


def test_media_caption_alias_refuses_without_download_or_default_dispatch():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        photo = SimpleNamespace(get_file=AsyncMock())
        message = _group_message(
            None,
            caption="@glm inspect this photo",
            photo=[photo],
        )

        await adapter._handle_media_message(
            SimpleNamespace(update_id=2006, message=message), SimpleNamespace()
        )

        message.reply_text.assert_awaited_once_with(
            "Profile routing is text-only. Send @glm followed by your request as text."
        )
        photo.get_file.assert_not_awaited()
        adapter._message_handler.assert_not_awaited()

    asyncio.run(_run())


def test_command_alias_refuses_without_default_dispatch():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        adapter.handle_message = AsyncMock()
        message = _group_message("/help @glm")

        await adapter._handle_command(
            SimpleNamespace(update_id=2007, message=message, effective_message=None),
            SimpleNamespace(),
        )

        message.reply_text.assert_awaited_once_with(
            "Profile routing is text-only. Send @glm followed by your request as text."
        )
        adapter.handle_message.assert_not_awaited()

    asyncio.run(_run())


def test_no_route_config_preserves_broad_regex_legacy_dispatch():
    async def _run():
        adapter = _telegram_adapter(
            require_mention=True,
            mention_patterns=[r"(?i)@glm"],
        )
        adapter.gateway_runner = _profile_turn_runner(target_profiles=set())
        enqueued = []
        adapter._enqueue_text_event = enqueued.append
        message = _group_message("legacy broad wake @glm remains unchanged")

        await adapter._handle_text_message(
            SimpleNamespace(update_id=2008, message=message, effective_message=None),
            SimpleNamespace(),
        )

        assert len(enqueued) == 1
        assert enqueued[0].source.profile is None
        message.reply_text.assert_not_awaited()

    asyncio.run(_run())


def test_profile_mention_route_keeps_dm_and_secondary_handlers_inert():
    async def _run():
        dm_adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        dm_adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
        dm_events = []
        dm_adapter._enqueue_text_event = dm_events.append
        dm_message = _dm_message("@glm dm remains legacy")
        await dm_adapter._handle_text_message(
            SimpleNamespace(update_id=2009, message=dm_message, effective_message=None),
            SimpleNamespace(),
        )
        assert len(dm_events) == 1
        assert dm_events[0].source.profile is None
        dm_message.reply_text.assert_not_awaited()

        secondary_adapter = _telegram_adapter(
            require_mention=True,
            profile_mention_routes={"glm": "gemini"},
        )
        secondary_adapter._owner_profile = "other"
        secondary_adapter.gateway_runner = _profile_turn_runner(
            target_profiles={"gemini"}
        )
        secondary_adapter._enqueue_text_event = Mock()
        secondary_message = _group_message("@glm secondary remains legacy")
        await secondary_adapter._handle_text_message(
            SimpleNamespace(
                update_id=2010,
                message=secondary_message,
                effective_message=None,
            ),
            SimpleNamespace(),
        )
        secondary_adapter._enqueue_text_event.assert_not_called()
        secondary_message.reply_text.assert_not_awaited()

    asyncio.run(_run())


def test_profile_mention_route_is_inert_on_named_profile_gateway(monkeypatch):
    adapter = _telegram_adapter(profile_mention_routes={"glm": "gemini"})
    adapter.gateway_runner = _profile_turn_runner(target_profiles={"gemini"})
    event = adapter._build_message_event(
        _group_message("@glm named gateway stays unchanged"), MessageType.TEXT
    )
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda: Path("/tmp/default")
    )
    monkeypatch.setattr(
        "hermes_constants.get_process_hermes_home",
        lambda: Path("/tmp/default/profiles/gemini"),
    )

    assert (
        adapter._apply_profile_mention_route(
            _group_message("@glm named gateway stays unchanged"), event
        )
        is True
    )
    assert event.source.profile is None


def test_profile_turn_allowlist_defaults_off_round_trips_and_loads_nested(
    tmp_path, monkeypatch
):
    assert GatewayConfig().profile_turn_allowlist == []
    assert GatewayConfig().multiplex_profiles is False

    config = GatewayConfig.from_dict({
        "gateway": {
            "profile_turn_allowlist": [
                " Gemini ",
                "gemini",
                "worker",
                "Default",
                "bad/name",
                7,
            ]
        }
    })
    assert config.profile_turn_allowlist == ["gemini", "worker"]
    assert GatewayConfig.from_dict(config.to_dict()).profile_turn_allowlist == [
        "gemini",
        "worker",
    ]
    assert (
        GatewayConfig(profile_turn_allowlist=["default"]).profile_turn_allowlist == []
    )

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "gateway:\n  profile_turn_allowlist:\n    - Gemini\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    loaded = load_gateway_config()
    assert loaded.profile_turn_allowlist == ["gemini"]
    assert loaded.multiplex_profiles is False


def test_profile_turn_session_key_is_namespaced_while_default_stays_legacy(tmp_path):
    store = _store(tmp_path, GatewayConfig(profile_turn_allowlist=["gemini"]))

    ordinary = _source()
    routed = _source(profile="gemini", profile_turn_routed=True)
    forged = _source(profile="gemini")

    assert (
        store._generate_session_key(ordinary)
        == "agent:main:telegram:group:-100-route:user-1"
    )
    assert (
        store._generate_session_key(routed)
        == "agent:gemini:telegram:group:-100-route:user-1"
    )
    assert (
        store._generate_session_key(forged)
        == "agent:main:telegram:group:-100-route:user-1"
    )
    assert "profile_turn_routed" not in routed.to_dict()
    serialized_forgery = SessionSource.from_dict({
        **routed.to_dict(),
        "profile_turn_routed": True,
    })
    assert serialized_forgery.profile_turn_routed is False


def test_profile_turn_allowlist_does_not_enable_multiplex_lifecycle(monkeypatch):
    from cron.scheduler_provider import scheduler_for_profile_mode
    from gateway.run import GatewayRunner

    config = GatewayConfig(profile_turn_allowlist=["gemini"])
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner._profile_adapters = {}

    with patch(
        "gateway.run._multiplex_profile_homes",
        side_effect=AssertionError("profile turns must not enumerate multiplex homes"),
    ):
        assert asyncio.run(GatewayRunner._start_secondary_profile_adapters(runner)) == 0

    external_cron_provider = object()
    assert (
        scheduler_for_profile_mode(
            external_cron_provider, multiplex_profiles=config.multiplex_profiles
        )
        is external_cron_provider
    )
    assert runner._profile_adapters == {}


def test_profile_turn_handler_scopes_target_config_and_credentials_without_multiplexer(
    tmp_path, monkeypatch
):
    from agent.secret_scope import get_secret, is_multiplex_active, set_multiplex_active
    from gateway import run as run_mod
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    root = tmp_path / "home"
    target = root / "profiles" / "gemini"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        "model:\n  default: target-model\n  provider: target-provider\n",
        encoding="utf-8",
    )
    (target / ".env").write_text("PROFILE_TURN_TOKEN=target-token\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("PROFILE_TURN_TOKEN", "default-token")
    monkeypatch.setenv("PROFILE_TURN_PROCESS_ONLY", "default-only")
    monkeypatch.setattr(run_mod, "get_hermes_home", lambda: root)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(profile_turn_allowlist=["gemini"])
    seen = []

    async def capture_scope(_event):
        seen.append((
            Path(get_hermes_home()),
            load_config()["model"]["default"],
            get_secret("PROFILE_TURN_TOKEN"),
            get_secret("PROFILE_TURN_PROCESS_ONLY"),
            is_multiplex_active(),
        ))

    runner._handle_message = capture_scope
    source = _source(profile="Gemini", profile_turn_routed=True)
    event = MessageEvent(text="@glm use this profile", source=source)

    previous_multiplex = is_multiplex_active()
    set_multiplex_active(False)
    try:
        with patch(
            "gateway.run._multiplex_profile_homes",
            side_effect=AssertionError(
                "profile turns must not enumerate multiplex homes"
            ),
        ):
            asyncio.run(runner._primary_message_handler()(event))
    finally:
        set_multiplex_active(previous_multiplex)

    assert seen == [(target, "target-model", "target-token", None, False)]
    assert source.profile == "gemini"
    assert Path(get_hermes_home()) == root
    assert get_secret("PROFILE_TURN_PROCESS_ONLY") == "default-only"


def test_profile_turn_rejects_forged_nonallowlisted_and_missing_sources(
    tmp_path, monkeypatch
):
    from gateway.run import GatewayRunner, _profile_runtime_scope

    root = tmp_path / "home"
    (root / "profiles" / "gemini").mkdir(parents=True)
    (root / "profiles" / "other").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(profile_turn_allowlist=["gemini", "missing"])

    sources = [
        _source(profile="gemini"),
        _source(profile="other", profile_turn_routed=True),
        _source(profile="missing", profile_turn_routed=True),
    ]
    for source in sources:
        result = asyncio.run(
            GatewayRunner._handle_message(
                runner,
                MessageEvent(
                    text="must not select a profile runtime",
                    message_type=MessageType.TEXT,
                    source=source,
                ),
            )
        )
        assert result is None
        assert source.profile_route_rejected is True

    named_gateway_source = _source(profile="gemini", profile_turn_routed=True)
    monkeypatch.setattr(
        "hermes_constants.get_process_hermes_home",
        lambda: root / "profiles" / "named-gateway",
    )
    assert (
        asyncio.run(
            GatewayRunner._handle_message(
                runner,
                MessageEvent(
                    text="named gateway must not select a profile turn",
                    message_type=MessageType.TEXT,
                    source=named_gateway_source,
                ),
            )
        )
        is None
    )
    assert named_gateway_source.profile_route_rejected is True

    named_gateway_ordinary = _source()
    assert (
        GatewayRunner._profile_turn_home_for_event(
            runner,
            MessageEvent(
                text="ordinary named gateway message",
                message_type=MessageType.TEXT,
                source=named_gateway_ordinary,
            ),
        )
        is None
    )
    assert named_gateway_ordinary.profile_route_rejected is False

    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: root)
    scoped_source = _source(profile="gemini", profile_turn_routed=True)
    with _profile_runtime_scope(root / "profiles" / "gemini", strict_secrets=True):
        assert (
            GatewayRunner._profile_turn_home_for_event(
                runner,
                MessageEvent(
                    text="target scope must retain the valid route",
                    message_type=MessageType.TEXT,
                    source=scoped_source,
                ),
            )
            == root / "profiles" / "gemini"
        )
    assert scoped_source.profile_route_rejected is False
