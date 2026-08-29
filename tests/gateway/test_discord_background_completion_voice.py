"""Background-process completions must speak in a bound LIVE Discord voice session.

Field incident (gateway.log, 2026-08-29 01:25-01:28): the operator was in a
live voice call. Voice-origin finals at 01:18 logged
``[Discord] Playing TTS in voice channel``. The 464-char final produced by a
background process completion at 01:28 logged only ``Sending response`` — the
reply arrived as an audio ATTACHMENT in the text channel and nothing played in
the connected voice channel.

Root cause: ``GatewayRunner._send_voice_reply`` resolved the guild through
``_get_guild_id``, which reads ``event.raw_message``. Watch-pattern /
background-completion events are synthesised in ``_inject_watch_notification``
WITHOUT a ``raw_message``, so the guild came back ``None``,
``in_voice_channel`` was False, and delivery fell through to ``send_voice``.

These tests drive the REAL ``DiscordAdapter`` voice binding state and the REAL
``GatewayRunner`` delivery path. Only the terminal audio egress
(``play_in_voice_channel`` / ``send_voice``) and the TTS synthesiser are
stubbed.
"""

import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


GUILD_ID = 900000000000000001
TEXT_CH = "900000000000000002"
OTHER_CH = "1499999999999999999"


# ---------------------------------------------------------------------------
# discord.py mock shim (mirrors tests/gateway/test_discord_document_handling.py)
# ---------------------------------------------------------------------------

def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object, button=lambda *a, **k: (lambda fn: fn), Button=object
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_adapter(*, bound_channel=TEXT_CH, connected=True, has_voice_client=True):
    """A real DiscordAdapter with real voice-binding state.

    ``_voice_text_channels`` and ``is_in_voice_channel`` are the actual gates
    under test, so they stay real. Only the audio egress is stubbed.
    """
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    if bound_channel is not None:
        adapter._voice_text_channels = {GUILD_ID: int(bound_channel)}
    else:
        adapter._voice_text_channels = {}
    if has_voice_client:
        adapter._voice_clients = {
            GUILD_ID: SimpleNamespace(is_connected=lambda: connected)
        }
    else:
        adapter._voice_clients = {}
    adapter.play_in_voice_channel = AsyncMock(return_value=True)
    adapter.send_voice = AsyncMock(return_value=SimpleNamespace(success=True))
    return adapter


def _make_runner(adapter, *, voice_mode="all", chat_id=TEXT_CH):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = None
    runner._voice_mode = {}
    if voice_mode is not None:
        runner._voice_mode[runner._voice_key(Platform.DISCORD, chat_id)] = voice_mode
    return runner


def _source(chat_id=TEXT_CH):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="900000000000000003",
        user_name="tester",
        chat_type="channel",
    )


def _completion_evt(chat_id=TEXT_CH):
    """The queued background-process completion event shape."""
    return {
        "type": "completion",
        "platform": "discord",
        "chat_type": "channel",
        "chat_id": chat_id,
        "user_id": "900000000000000003",
        "session_id": "proc_754b6c5dc9bf",
        "started_at": 1756430743.0,
    }


async def _synthetic_completion_event(runner, adapter, chat_id=TEXT_CH):
    """Build the event through the REAL watch-notification injection path.

    This is the seam that produced the field bug: whatever shape
    ``_inject_watch_notification`` hands to ``handle_message`` is exactly what
    the delivery path later has to work with.
    """
    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter.handle_message = _capture
    accepted = await runner._inject_watch_notification(
        "[IMPORTANT: Background process proc_754b6c5dc9bf completed normally "
        "(exit code 0)]",
        _completion_evt(chat_id),
    )
    assert accepted is True, "watch notification was not accepted by the adapter"
    event = captured["event"]
    # Guard the premise of this whole file: the synthetic event genuinely has
    # no raw platform message, so _get_guild_id cannot resolve a guild.
    assert getattr(event, "raw_message", None) is None
    assert GatewayRunner._get_guild_id(event) is None
    assert event.message_type == MessageType.TEXT
    return event


def _fake_tts(monkeypatch, *, success=True, audio_bytes=b"\x00" * 32, raises=False):
    def _tool(*, text, output_path, **_kw):
        if raises:
            raise RuntimeError("tts backend unavailable")
        if success:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as fh:
                fh.write(audio_bytes)
            return json.dumps({"success": True, "file_path": output_path})
        return json.dumps({"success": False, "error": "synthesis failed"})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", _tool)
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda t: t)


@pytest.fixture(autouse=True)
def _tmp_audio_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


REPLY = "The background job finished. Two files changed and the tests pass."


# ---------------------------------------------------------------------------
# 1. Voice-origin final (the path that already worked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_origin_final_plays_in_voice_channel(tmp_path):
    """DiscordAdapter.play_tts routes a voice-origin final into the live VC."""
    adapter = _make_adapter()
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"\x00" * 32)

    result = await adapter.play_tts(chat_id=TEXT_CH, audio_path=str(audio))

    assert result.success is True
    adapter.play_in_voice_channel.assert_awaited_once()
    assert adapter.play_in_voice_channel.await_args.args[0] == GUILD_ID
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_origin_final_for_unbound_chat_sends_attachment(tmp_path):
    """play_tts must not hijack a chat that is not the bound voice text channel."""
    adapter = _make_adapter(bound_channel=OTHER_CH)
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"\x00" * 32)

    await adapter.play_tts(chat_id=TEXT_CH, audio_path=str(audio))

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Typed final in a bound active voice session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_typed_final_in_bound_active_voice_session_plays_in_vc(monkeypatch):
    """A typed message carries raw_message, so the guild already resolved."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = MessageEvent(
        text="status?",
        message_type=MessageType.TEXT,
        source=_source(),
        raw_message=SimpleNamespace(guild_id=GUILD_ID, guild=None),
        message_id="m-typed",
    )

    assert runner._should_send_voice_reply(event, REPLY, []) is True
    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_awaited_once()
    assert adapter.play_in_voice_channel.await_args.args[0] == GUILD_ID
    adapter.send_voice.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. THE REGRESSION: background completion final in a bound active session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_completion_final_plays_in_bound_active_voice_session(
    monkeypatch,
):
    """A watch-pattern/background completion final must reach the live mixer.

    Fails before the fix: guild is None, so delivery falls through to
    ``send_voice`` and the operator gets a text-channel audio attachment while
    the voice channel stays silent.
    """
    _fake_tts(monkeypatch)
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._should_send_voice_reply(event, REPLY, []) is True
    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_awaited_once()
    assert adapter.play_in_voice_channel.await_args.args[0] == GUILD_ID
    # Exactly once, and never ALSO as an attachment.
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_completion_resolver_requires_matching_chat(monkeypatch):
    """The resolver keys off the binding, not merely 'some guild is live'."""
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._resolve_voice_playback_guild(event, adapter) == GUILD_ID

    # Same live guild, but bound to a DIFFERENT text channel.
    adapter._voice_text_channels = {GUILD_ID: int(OTHER_CH)}
    assert runner._resolve_voice_playback_guild(event, adapter) is None


# ---------------------------------------------------------------------------
# 4. Unbound background completion — must NOT speak
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unbound_background_completion_does_not_speak(monkeypatch):
    """No voice binding for this chat: keep the attachment delivery policy."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter(bound_channel=OTHER_CH)
    runner = _make_runner(adapter, chat_id=TEXT_CH)
    event = await _synthetic_completion_event(runner, adapter)

    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_completion_with_no_voice_binding_at_all(monkeypatch):
    """Bot is not in voice anywhere: nothing to hijack."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter(bound_channel=None, has_voice_client=False)
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._resolve_voice_playback_guild(event, adapter) is None
    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_discord_background_completion_is_untouched(monkeypatch):
    """The fallback is Discord-only; other platforms keep their exact behaviour."""
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    telegram_event = MessageEvent(
        text="done",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=TEXT_CH,
            user_id="1",
            chat_type="dm",
        ),
        message_id="m-tg",
    )
    assert runner._resolve_voice_playback_guild(telegram_event, adapter) is None


# ---------------------------------------------------------------------------
# 5. Disconnected / missing mixer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnected_voice_client_falls_back_to_attachment(monkeypatch):
    """Binding survives a drop; the connection check must fail closed."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter(connected=False)
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    assert adapter.is_in_voice_channel(GUILD_ID) is False
    assert runner._resolve_voice_playback_guild(event, adapter) is None
    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_binding_without_voice_client_falls_back_to_attachment(
    monkeypatch,
):
    """A binding pointing at a guild with no voice client must not play."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter(has_voice_client=False)
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._resolve_voice_playback_guild(event, adapter) is None
    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_mode_off_never_speaks_a_background_completion(monkeypatch):
    """The voice-mode gate stays upstream of the routing fix."""
    adapter = _make_adapter()
    runner = _make_runner(adapter, voice_mode="off")
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._should_send_voice_reply(event, REPLY, []) is False


@pytest.mark.asyncio
async def test_voice_only_mode_does_not_speak_a_background_completion(monkeypatch):
    """``voice_only`` means voice INPUT only — a background final is not that."""
    adapter = _make_adapter()
    runner = _make_runner(adapter, voice_mode="voice_only")
    event = await _synthetic_completion_event(runner, adapter)

    assert runner._should_send_voice_reply(event, REPLY, []) is False


# ---------------------------------------------------------------------------
# 6. Duplicate / replay delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replayed_delivery_plays_once_per_delivery_and_never_doubles(
    monkeypatch,
):
    """Each delivery speaks exactly once and never also attaches.

    Durable completion rows are at-least-once, so a replay can produce a second
    turn. The routing fix must not add a SECOND delivery channel per turn.
    """
    _fake_tts(monkeypatch)
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    await runner._send_voice_reply(event, REPLY)
    assert adapter.play_in_voice_channel.await_count == 1
    assert adapter.send_voice.await_count == 0

    await runner._send_voice_reply(event, REPLY)
    assert adapter.play_in_voice_channel.await_count == 2
    assert adapter.send_voice.await_count == 0


def test_replayed_completion_has_a_stable_producer_identity():
    """Upstream replay suppression keys off a stable identity for this evt."""
    evt = _completion_evt()
    first = GatewayRunner._completion_delivery_identity(evt)
    second = GatewayRunner._completion_delivery_identity(dict(evt))
    assert first is not None
    assert first == second


# ---------------------------------------------------------------------------
# 7. Attachment / TTS generation failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_synthesis_failure_speaks_nothing_and_does_not_raise(monkeypatch):
    _fake_tts(monkeypatch, success=False)
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_backend_exception_is_contained(monkeypatch):
    _fake_tts(monkeypatch, raises=True)
    adapter = _make_adapter()
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_playback_failure_does_not_also_send_an_attachment(monkeypatch):
    """Playback returning False must not fan out into a second delivery."""
    _fake_tts(monkeypatch)
    adapter = _make_adapter()
    adapter.play_in_voice_channel = AsyncMock(return_value=False)
    runner = _make_runner(adapter)
    event = await _synthetic_completion_event(runner, adapter)

    await runner._send_voice_reply(event, REPLY)

    adapter.play_in_voice_channel.assert_awaited_once()
    adapter.send_voice.assert_not_awaited()
