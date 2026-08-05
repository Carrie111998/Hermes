"""Regression tests for the wave-1 (w1a, shard s3) extraction of
``gateway/platforms/base.py``.

Covers the five unanimous move clusters lifted verbatim into mixin modules:

- c5:  ``render_message_event`` / ``format_tool_event`` / ``format_tool_preview``
       -> ``StreamRenderingMixin`` (stream_rendering_mixin.py)
- c12: ``_get_ephemeral_system_ttl_default`` / ``_schedule_ephemeral_delete``
       -> ``EphemeralMixin`` (ephemeral_mixin.py)
- c13: ``_truncate_preview`` / ``_ea_escape`` / ``_format_exec_approval`` /
       ``_format_choice_page`` -> ``PromptFormattingMixin`` (prompt_formatting_mixin.py)
- c14: ``send_slash_confirm`` / ``send_clarify`` / ``send_private_notice``
       -> ``InteractiveSendsMixin`` (interactive_sends_mixin.py)
- c16: ``_should_auto_tts_for_chat`` / ``send_voice`` / ``prepare_tts_text`` /
       ``play_tts`` -> ``VoiceTtsMixin`` (voice_tts_mixin.py)

API-survival checks: ``BasePlatformAdapter`` must still expose every moved
method via MRO.  Bare instances are built with ``object.__new__`` per the
documented test pattern (see tests/gateway/test_interactive_prompt_base.py);
the values asserted are the historical outputs of the pre-extraction code.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.ephemeral_mixin import EphemeralMixin
from gateway.platforms.interactive_sends_mixin import InteractiveSendsMixin
from gateway.platforms.prompt_formatting_mixin import PromptFormattingMixin
from gateway.platforms.stream_rendering_mixin import StreamRenderingMixin
from gateway.platforms.voice_tts_mixin import VoiceTtsMixin


def _bare(cls):
    """Bare instance without running __init__ (documented test pattern)."""
    return object.__new__(cls)


class _BareAdapter(BasePlatformAdapter):
    """Concrete subclass using only base-class state (bare-instance pattern)."""

    name = "stub"

    async def connect(self, *, is_reconnect=False):  # pragma: no cover
        return True

    async def disconnect(self):  # pragma: no cover
        return None

    async def get_chat_info(self, chat_id):  # pragma: no cover
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):  # pragma: no cover
        raise NotImplementedError

    async def send_typing(self, chat_id, metadata=None):  # pragma: no cover
        return None

    async def stop_typing(self, chat_id, metadata=None):  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Wiring: BasePlatformAdapter inherits every mixin, methods reachable via MRO
# ---------------------------------------------------------------------------


def test_api_survival_mixins_wired_into_base():
    assert issubclass(BasePlatformAdapter, StreamRenderingMixin)
    assert issubclass(BasePlatformAdapter, EphemeralMixin)
    assert issubclass(BasePlatformAdapter, PromptFormattingMixin)
    assert issubclass(BasePlatformAdapter, InteractiveSendsMixin)
    assert issubclass(BasePlatformAdapter, VoiceTtsMixin)
    for name in (
        "render_message_event", "format_tool_event", "format_tool_preview",
        "_get_ephemeral_system_ttl_default", "_schedule_ephemeral_delete",
        "_truncate_preview", "_ea_escape", "_format_exec_approval",
        "_format_choice_page", "send_slash_confirm", "send_clarify",
        "send_private_notice", "_should_auto_tts_for_chat", "send_voice",
        "prepare_tts_text", "play_tts",
    ):
        assert callable(getattr(BasePlatformAdapter, name)), name


# ---------------------------------------------------------------------------
# c13: prompt-formatting cores
# ---------------------------------------------------------------------------


def test_truncate_preview_short_exact_long():
    assert PromptFormattingMixin._truncate_preview("abc", 10) == "abc"
    assert PromptFormattingMixin._truncate_preview("x" * 10, 10) == "x" * 10
    assert PromptFormattingMixin._truncate_preview("x" * 11, 10) == "x" * 10 + "..."
    assert PromptFormattingMixin._truncate_preview(None, 5) == ""
    assert PromptFormattingMixin._truncate_preview("abc", 2, suffix=" [cut]") == "ab [cut]"


def test_ea_escape_default_passthrough():
    assert _bare(PromptFormattingMixin)._ea_escape("<raw & text>") == "<raw & text>"


def test_format_exec_approval_default_template_and_smart_deny():
    ad = _bare(_BareAdapter)
    text = ad._format_exec_approval("rm -rf /", "scary")
    assert text == (
        "⚠️ Command Approval Required\n\n"
        "```\nrm -rf /\n```\n"
        "Reason: scary"
    )
    denied = ad._format_exec_approval("rm -rf /", "scary", smart_denied=True)
    assert denied.endswith(
        "\n\nSmart DENY: owner override applies to this one operation only."
    )


def test_format_exec_approval_truncates_long_command_to_budget():
    ad = _bare(_BareAdapter)
    text = ad._format_exec_approval("x" * 4000, "why")
    assert "x" * 3000 + "..." in text
    assert "x" * 4000 not in text


def test_format_choice_page_pagination_contract():
    opts, meta = PromptFormattingMixin._format_choice_page([1, 2, 3], 0, 10)
    assert opts == [1, 2, 3]
    assert meta == {
        "page": 0, "total_pages": 1, "start": 0, "end": 3, "total": 3,
        "page_info": "",
    }
    opts, meta = PromptFormattingMixin._format_choice_page(list(range(25)), 99, 10)
    assert meta["page"] == 2 and meta["total_pages"] == 3
    assert opts == list(range(20, 25))
    assert meta["page_info"] == " (21–25 of 25)"


# ---------------------------------------------------------------------------
# c16: voice / TTS
# ---------------------------------------------------------------------------


def test_should_auto_tts_for_chat_decision_layers():
    v = _bare(VoiceTtsMixin)
    v._auto_tts_enabled_chats = {"c1"}
    v._auto_tts_disabled_chats = {"c2"}
    v._auto_tts_default = False
    assert v._should_auto_tts_for_chat("c1") is True
    assert v._should_auto_tts_for_chat("c2") is False
    assert v._should_auto_tts_for_chat("c3") is False
    v._auto_tts_default = True
    assert v._should_auto_tts_for_chat("c3") is True


class _RecordingSend(_BareAdapter):
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content,
                          "reply_to": reply_to, "metadata": metadata})
        return SendResult(success=True, message_id="m1")


def test_send_voice_fallback_never_leaks_audio_path():
    ad = _RecordingSend()
    result = asyncio.run(ad.send_voice("chat-1", "C:\\secret\\voice.ogg", caption="here it is"))
    assert result.success
    assert len(ad.sent) == 1
    sent = ad.sent[0]
    assert sent["chat_id"] == "chat-1"
    # friendly notice + caption, host path NEVER echoed into chat
    assert "⚠️ Couldn't deliver the audio attachment." in sent["content"]
    assert sent["content"].startswith("here it is\n")
    assert "secret" not in sent["content"]
    assert "voice.ogg" not in sent["content"]


def test_play_tts_delegates_to_send_voice():
    calls = {}

    class _Voice(_BareAdapter):
        def __init__(self):  # bare-instance pattern: skip BasePlatformAdapter.__init__
            pass

        async def send_voice(self, **kwargs):
            calls.update(kwargs)
            return SendResult(success=True, message_id="m2")

    result = asyncio.run(_Voice().play_tts("chat-1", "/tmp/a.ogg", extra=1))
    assert result.success
    assert calls == {"chat_id": "chat-1", "audio_path": "/tmp/a.ogg", "extra": 1}


def test_prepare_tts_text_fallback_strips_think_and_markdown(monkeypatch):
    # Force the best-effort fallback path: the normalizer import fails.
    monkeypatch.setitem(sys.modules, "tools.tts_text_normalize", None)
    v = _bare(VoiceTtsMixin)
    out = v.prepare_tts_text(
        "Hello <think>hidden reasoning</think> **world** with `code` [x](y)"
    )
    assert "<think>" not in out and "hidden reasoning" not in out
    assert "**" not in out and "`" not in out
    assert out.startswith("Hello")


# ---------------------------------------------------------------------------
# c12: ephemeral deletes
# ---------------------------------------------------------------------------


def test_ephemeral_ttl_default_zero_when_config_unreadable(monkeypatch):
    class _BoomConfig:
        @staticmethod
        def load_config_readonly():
            raise RuntimeError("config exploded")

    monkeypatch.setitem(sys.modules, "hermes_cli.config", _BoomConfig)
    assert _bare(EphemeralMixin)._get_ephemeral_system_ttl_default() == 0


def test_ephemeral_ttl_default_reads_display_section(monkeypatch):
    class _Cfg:
        @staticmethod
        def load_config_readonly():
            return {"display": {"ephemeral_system_ttl": "120"}}

    monkeypatch.setitem(sys.modules, "hermes_cli.config", _Cfg)
    assert _bare(EphemeralMixin)._get_ephemeral_system_ttl_default() == 120


def test_schedule_ephemeral_delete_deletes_after_ttl():
    deleted = []

    class _Ephemeral(_BareAdapter):
        def __init__(self):  # bare-instance pattern: skip BasePlatformAdapter.__init__
            pass

        async def delete_message(self, chat_id, message_id):
            deleted.append((chat_id, message_id))
            return SendResult(success=True, message_id=message_id)

    async def run():
        e = _Ephemeral()
        e._schedule_ephemeral_delete("chat-9", "msg-9", ttl_seconds=0)
        # ttl<=0 still schedules (max(1, ttl)); give the task a beat
        await asyncio.sleep(1.2)

    asyncio.run(run())
    assert deleted == [("chat-9", "msg-9")]


# ---------------------------------------------------------------------------
# c14: interactive sends
# ---------------------------------------------------------------------------


def test_send_slash_confirm_default_not_supported():
    ad = _RecordingSend()
    result = asyncio.run(ad.send_slash_confirm(
        "c", "title", "msg", session_key="s", confirm_id="k"
    ))
    assert result.success is False
    assert result.error == "Not supported"
    assert ad.sent == []


def test_send_clarify_open_ended_uses_question_only():
    ad = _RecordingSend()
    result = asyncio.run(ad.send_clarify("c", "What next?", None, "k1", "s1"))
    assert result.success
    assert ad.sent[0]["content"] == "❓ What next?"
    assert ad.sent[0]["metadata"] is None


def test_send_clarify_choices_numbered_list(monkeypatch):
    calls = []

    class _Cg:
        _lock = SimpleNamespace(__enter__=lambda s: None, __exit__=lambda *a: None)
        _entries = {"k2": SimpleNamespace(multi_select=False)}

        @staticmethod
        def mark_awaiting_text(clarify_id):
            calls.append(clarify_id)

    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", _Cg)
    ad = _RecordingSend()
    result = asyncio.run(ad.send_clarify("c", "Pick", ["a", "b"], "k2", "s1"))
    assert result.success
    content = ad.sent[0]["content"]
    assert content.startswith("❓ Pick")
    assert "  1. a" in content and "  2. b" in content
    assert "Reply with the number, the option text, or your own answer." in content
    assert calls == ["k2"]


def test_send_private_notice_forwards_to_send():
    ad = _RecordingSend()
    result = asyncio.run(ad.send_private_notice(
        "c", user_id="u1", content="psst", reply_to="r1", metadata={"m": 1}
    ))
    assert result.success
    assert ad.sent[0] == {"chat_id": "c", "content": "psst",
                          "reply_to": "r1", "metadata": {"m": 1}}


# ---------------------------------------------------------------------------
# c5: stream-event rendering
# ---------------------------------------------------------------------------


def test_format_tool_event_eats_non_tool_events():
    ad = _bare(_BareAdapter)
    assert ad.format_tool_event(SimpleNamespace(text="x")) is None


def test_format_tool_preview_returns_text():
    ad = _bare(_BareAdapter)
    assert ad.format_tool_preview(SimpleNamespace(text="preview text")) == "preview text"


def test_render_message_event_delegates_to_sink():
    from gateway.stream_events import Commentary, MessageChunk, MessageStop

    events = []

    class _Sink:
        def on_delta(self, text):
            events.append(("delta", text))

        def on_segment_break(self):
            events.append(("break", None))

        def on_commentary(self, text):
            events.append(("commentary", text))

    ad = _bare(_BareAdapter)
    sink = _Sink()
    ad.render_message_event(MessageChunk(text="hi"), sink)
    ad.render_message_event(MessageStop(final=False), sink)
    ad.render_message_event(MessageStop(final=True), sink)  # terminal stop: no break
    ad.render_message_event(Commentary(text="note"), sink)
    assert events == [
        ("delta", "hi"),
        ("break", None),
        ("commentary", "note"),
    ]
