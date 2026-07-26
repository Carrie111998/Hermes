"""Security contract for the explicit gateway owner-command boundary."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import owner_commands
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.owner_commands import (
    _canonical_youtube_url,
    _handle_owner_command,
    _load_owner_config,
    _stamp_direct_human_event,
)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


VIDEO_ID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHORT_URL = f"https://youtu.be/{VIDEO_ID}"


def _telegram_event(
    text: str = f"/youtube-probe {WATCH_URL}",
    *,
    user_id: str = "owner-1",
    chat_type: str = "dm",
    reply: bool = False,
    forwarded: bool = False,
    bot: bool = False,
) -> MessageEvent:
    author = SimpleNamespace(id=user_id, is_bot=bot)
    raw = SimpleNamespace(
        from_user=author,
        chat=SimpleNamespace(id=user_id, type=SimpleNamespace(name="private")),
        message_id="message-1",
        forward_origin=object() if forwarded else None,
        forward_date=None,
        sender_chat=None,
        via_bot=None,
        is_automatic_forward=False,
        reply_to_message=object() if reply else None,
    )
    event = MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=user_id,
            chat_type=chat_type,
            user_id=user_id,
            is_bot=bot,
        ),
        raw_message=raw,
        message_id="message-1",
        reply_to_message_id="prior" if reply else None,
    )
    _stamp_direct_human_event(event)
    return event


def _decision(event, config=None, callback=lambda request: "accepted", **kwargs):
    return _handle_owner_command(
        event,
        config or _load_owner_config("telegram:owner-1"),
        callback,
        session_key="opaque-input-session",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("url", "canonical"),
    [(WATCH_URL, WATCH_URL), (SHORT_URL, SHORT_URL)],
)
def test_exact_canonical_url_grammar_accepts_only_supported_forms(url, canonical):
    assert _canonical_youtube_url(url) == canonical


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=abc",
        f"https://www.youtube.com/watch?v={VIDEO_ID}#fragment",
        "https://www.youtube.com/watch?",
        "https://www.youtube.com/watch#",
        f"https://www.youtube.com/watch?v={VIDEO_ID}?",
        f"https://www.youtube.com/watch?v={VIDEO_ID}#",
        f"https://WWW.YOUTUBE.COM/watch?v={VIDEO_ID}",
        "https://www.youtube.com/watch?v=%64Qw4w9WgXcQ",
        f"https://youtu.be/{VIDEO_ID}?feature=share",
        f"https://youtu.be/{VIDEO_ID}?",
        f"https://youtu.be/{VIDEO_ID}#",
        "https://youtu.be/not-eleven",
        "https://example.invalid/watch?v=dQw4w9WgXcQ",
    ],
)
def test_exact_canonical_url_grammar_rejects_variants(url):
    assert _canonical_youtube_url(url) is None


@pytest.mark.parametrize(
    "text",
    [
        "/youtube-probe",
        f"/youtube-probe  {WATCH_URL}",
        f"/youtube-probe {WATCH_URL} extra",
        f"/youtube-probe\t{WATCH_URL}",
        f"/youtube-probe\n{WATCH_URL}",
    ],
)
def test_malformed_command_is_consumed_without_callback(text):
    callback = MagicMock(return_value="accepted")
    handled, response = _decision(_telegram_event(text), callback=callback)
    assert handled is True
    assert response.startswith("Usage:")
    callback.assert_not_called()


def test_command_prefix_collision_is_not_consumed():
    callback = MagicMock(return_value="accepted")
    handled, response = _decision(
        _telegram_event(f"/youtube-probe-extra {WATCH_URL}"), callback=callback
    )
    assert handled is False
    assert response is None
    callback.assert_not_called()


def test_owner_config_is_bounded_disabled_by_default_and_not_serialized():
    assert _load_owner_config(None).enabled is False
    assert _load_owner_config("telegram:*").enabled is False
    oversized = ",".join(f"telegram:user-{i}" for i in range(17))
    assert _load_owner_config(oversized).enabled is False

    cfg = GatewayConfig(owner_config=_load_owner_config("telegram:private-owner"))
    rendered = cfg.to_dict()
    assert "owner_config" not in rendered
    assert "private-owner" not in repr(cfg)
    assert "private-owner" not in repr(cfg.owner_config)


@pytest.mark.parametrize(
    "event,config,rewritten",
    [
        (
            _telegram_event(user_id="ordinary"),
            _load_owner_config("telegram:owner-1"),
            False,
        ),
        (
            _telegram_event(chat_type="group"),
            _load_owner_config("telegram:owner-1"),
            False,
        ),
        (_telegram_event(reply=True), _load_owner_config("telegram:owner-1"), False),
        (
            _telegram_event(forwarded=True),
            _load_owner_config("telegram:owner-1"),
            False,
        ),
        (_telegram_event(bot=True), _load_owner_config("telegram:owner-1"), False),
        (_telegram_event(), _load_owner_config("telegram:owner-1"), True),
    ],
)
def test_auth_and_provenance_matrix_fails_closed(event, config, rewritten):
    callback = MagicMock(return_value="accepted")
    handled, response = _decision(
        event, config=config, callback=callback, rewritten=rewritten
    )
    assert handled is True
    assert response == "Owner command unavailable."
    callback.assert_not_called()


def test_api_bearer_relay_pairing_role_and_allow_all_cannot_imply_owner():
    for platform in (Platform.API_SERVER, Platform.WEBHOOK, Platform.RELAY):
        event = MessageEvent(
            text=f"/youtube-probe {WATCH_URL}",
            source=SessionSource(
                platform=platform,
                chat_id="private",
                chat_type="dm",
                user_id="owner-1",
                role_authorized=True,
                delivered_via_upstream_relay=platform == Platform.RELAY,
            ),
            raw_message=SimpleNamespace(),
        )
        callback = MagicMock(return_value="accepted")
        handled, response = _decision(event, callback=callback)
        assert handled is True
        assert response == "Owner command unavailable."
        callback.assert_not_called()


def test_direct_discord_human_can_be_proven_but_forward_snapshot_cannot():
    def make_event(snapshots):
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="private-discord-chat",
            chat_type="dm",
            user_id="discord-owner",
        )
        raw = SimpleNamespace(
            author=SimpleNamespace(id="discord-owner", bot=False),
            id="discord-message",
            channel=SimpleNamespace(
                id="private-discord-chat",
                type=SimpleNamespace(name="private"),
                guild=None,
            ),
            guild=None,
            webhook_id=None,
            reference=None,
            message_snapshots=snapshots,
            type=SimpleNamespace(name="default"),
        )
        event = MessageEvent(
            text=f"/youtube-probe {WATCH_URL}",
            source=source,
            raw_message=raw,
            message_id="discord-message",
        )
        _stamp_direct_human_event(event)
        return event

    config = _load_owner_config("discord:discord-owner")
    assert _decision(make_event([]), config=config) == (
        True,
        "YouTube probe accepted.",
    )
    callback = MagicMock(return_value="accepted")
    assert _decision(make_event([object()]), config=config, callback=callback) == (
        True,
        "Owner command unavailable.",
    )
    callback.assert_not_called()


def test_unstamped_direct_local_and_public_constructor_fail_closed():
    event = MessageEvent(
        text=f"/youtube-probe {WATCH_URL}",
        source=SessionSource(
            platform=Platform.LOCAL,
            chat_id="local",
            chat_type="dm",
            user_id="owner-1",
        ),
    )
    handled, response = _decision(event, config=_load_owner_config("local:owner-1"))
    assert handled is True
    assert response == "Owner command unavailable."


def test_callback_request_is_immutable_opaque_and_repr_redacted():
    observed = {}

    def callback(request):
        observed["request"] = request
        return "accepted"

    handled, response = _decision(_telegram_event(), callback=callback)
    assert (handled, response) == (True, "YouTube probe accepted.")
    principal = observed["request"].principal
    assert observed["request"].request_binding == principal.request_binding
    assert len(observed["request"].request_binding) == 32
    assert len(observed["request"].session_binding) == 32
    assert "owner-1" not in repr(observed["request"])
    assert WATCH_URL not in repr(observed["request"])
    assert observed["request"].request_binding not in repr(observed["request"])
    assert observed["request"].session_binding not in repr(observed["request"])
    assert observed["request"].request_binding not in repr(principal)
    assert observed["request"].session_binding not in repr(principal)
    with pytest.raises((AttributeError, TypeError)):
        principal.platform = "forged"


@pytest.mark.asyncio
async def test_child_task_created_inside_callback_has_no_ambient_owner_authority():
    tasks = []

    async def inspect_ambient_state():
        return hasattr(owner_commands, "_current_owner_principal")

    def callback(request):
        tasks.append(asyncio.create_task(inspect_ambient_state()))
        return "accepted"

    assert _decision(_telegram_event(), callback=callback) == (
        True,
        "YouTube probe accepted.",
    )
    assert await tasks[0] is False


@pytest.mark.parametrize("field", ["text", "source", "message_id", "raw"])
def test_in_place_event_and_source_mutation_invalidates_provenance(field):
    event = _telegram_event()
    if field == "text":
        event.text = f"/youtube-probe {SHORT_URL}"
    elif field == "source":
        event.source.user_id = "owner-2"
    elif field == "message_id":
        event.message_id = "message-2"
    else:
        event.raw_message.forward_origin = object()

    callback = MagicMock(return_value="accepted")
    config = _load_owner_config("telegram:owner-1,telegram:owner-2")
    assert _decision(event, config=config, callback=callback) == (
        True,
        "Owner command unavailable.",
    )
    callback.assert_not_called()


def test_mutated_event_cannot_be_restamped_over_existing_provenance():
    event = _telegram_event()
    original_provenance = event._owner_provenance
    event.text = f"/youtube-probe {SHORT_URL}"
    _stamp_direct_human_event(event)
    assert event._owner_provenance is original_provenance
    assert _decision(event) == (True, "Owner command unavailable.")


@pytest.mark.parametrize(
    ("callback", "expected"),
    [
        (None, "Owner command service unavailable."),
        (
            lambda request: (_ for _ in ()).throw(RuntimeError("private failure")),
            "Owner command service unavailable.",
        ),
        (lambda request: "unexpected", "Owner command service unavailable."),
        (lambda request: "duplicate", "YouTube probe already accepted."),
    ],
)
def test_callback_absent_failure_duplicate_are_consumed(callback, expected):
    handled, response = _decision(_telegram_event(), callback=callback)
    assert handled is True
    assert response == expected


@pytest.mark.asyncio
async def test_async_callback_is_rejected_not_fire_and_forget():
    called = False

    async def callback(request):
        nonlocal called
        called = True
        return "accepted"

    handled, response = _decision(_telegram_event(), callback=callback)
    await asyncio.sleep(0)
    assert handled is True
    assert response == "Owner command service unavailable."
    assert called is False


def test_no_owner_ids_or_command_text_are_logged(caplog):
    caplog.set_level(logging.DEBUG)
    secret_id = "private-owner-id"
    secret_url = f"https://youtu.be/{VIDEO_ID}"
    event = _telegram_event(f"/youtube-probe {secret_url}", user_id=secret_id)
    handled, _ = _decision(
        event,
        config=_load_owner_config(f"telegram:{secret_id}"),
        callback=lambda request: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert handled is True
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_id not in rendered
    assert secret_url not in rendered

    captured = {}
    event = _telegram_event(user_id=secret_id)
    _decision(
        event,
        config=_load_owner_config(f"telegram:{secret_id}"),
        callback=lambda request: captured.setdefault("request", request) and "accepted",
    )
    request_repr = repr(captured["request"])
    assert secret_id not in request_repr
    assert WATCH_URL not in request_repr
    assert captured["request"].request_binding not in request_repr
    provenance_repr = repr(event._owner_provenance)
    assert secret_id not in provenance_repr
    assert WATCH_URL not in provenance_repr
    assert event._owner_provenance._digest not in provenance_repr


def test_normal_message_regression_is_not_consumed():
    callback = MagicMock(return_value="accepted")
    handled, response = _decision(
        _telegram_event("please inspect this video"), callback=callback
    )
    assert (handled, response) == (False, None)
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_canonical_gateway_runs_owner_hook_after_authorization(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner-1")
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
        owner_config=_load_owner_config("telegram:owner-1"),
    )
    runner.adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._owner_command_callback = MagicMock(return_value="accepted")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])

    response = await runner._handle_message(_telegram_event())

    assert response == "YouTube probe accepted."
    runner._owner_command_callback.assert_called_once()


@pytest.mark.asyncio
async def test_pairing_only_authorization_does_not_imply_owner(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
        owner_config=_load_owner_config("telegram:different-owner"),
    )
    runner.adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._owner_command_callback = MagicMock(return_value="accepted")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])

    response = await runner._handle_message(_telegram_event(user_id="paired-user"))

    assert response == "Owner command unavailable."
    runner._owner_command_callback.assert_not_called()


@pytest.mark.asyncio
async def test_global_allow_all_authorization_does_not_imply_owner(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
        owner_config=_load_owner_config("telegram:different-owner"),
    )
    runner.adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._owner_command_callback = MagicMock(return_value="accepted")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])

    response = await runner._handle_message(_telegram_event(user_id="allow-all-user"))

    assert response == "Owner command unavailable."
    runner._owner_command_callback.assert_not_called()
