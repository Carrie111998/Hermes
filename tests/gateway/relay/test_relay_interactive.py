"""Relay Phase 3 interactive tests — prompt op egress, prompt_response
consumption, and the react ack lifecycle.

Covers:
  - send_exec_approval / send_slash_confirm / send_clarify render through ONE
    `prompt` op with the right option sets, honoring op gating (legacy
    connectors get the base/text behaviour or a structured failure that
    triggers run.py's text fallback);
  - the pending-prompt registry: mint → consume-once → expiry;
  - _consume_prompt_response routes answers to the approval / slash-confirm /
    clarify resolvers and CONSUMES the event; unknown/expired ids fall
    through to normal dispatch;
  - the Discord type-3 hp1 decode (structured prompt_response replacing the
    bare-custom_id stub; foreign custom_ids keep the legacy text shape);
  - on_processing_start/complete drive react ops (👀 → ✅/❌), op-gated and
    best-effort.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.relay.ws_transport import _event_from_wire
from gateway.session import SessionSource, build_session_key

from tests.gateway.relay.stub_connector import StubConnector

FULL_OPS = (
    "send",
    "edit",
    "typing",
    "get_chat_info",
    "send_media",
    "prompt",
    "react",
)


def make_desc(**kw) -> CapabilityDescriptor:
    base = dict(
        contract_version=CONTRACT_VERSION,
        platform="telegram",
        label="Telegram",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="markdown_v2",
        len_unit="utf16",
        supported_ops=FULL_OPS,
    )
    base.update(kw)
    return CapabilityDescriptor(**base)


def _adapter(**desc_kw) -> tuple[RelayAdapter, StubConnector]:
    stub = StubConnector(make_desc(**desc_kw))
    adapter = RelayAdapter(PlatformConfig(), make_desc(**desc_kw), transport=stub)
    return adapter, stub


def _event(
    prompt_response: Optional[Dict[str, Any]] = None,
    text: str = "/once",
    chat_id: str = "c1",
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform="telegram", chat_id=chat_id, chat_type="dm", user_id="u1"
        ),
        prompt_response=prompt_response,
    )


# ── egress: the three prompt surfaces ────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_approval_renders_full_option_set():
    adapter, stub = _adapter()
    result = await adapter.send_exec_approval(
        "c1", "rm -rf /tmp/x", "sess:1", description="deletes files"
    )
    assert result.success is True
    assert result.message_id == "pm1"
    action = stub.sent[-1]
    assert action["op"] == "prompt"
    assert action["prompt_kind"] == "approval"
    ids = [o["id"] for o in action["options"]]
    assert ids == ["once", "session", "always", "deny"]
    assert "rm -rf /tmp/x" in action["content"]
    assert "deletes files" in action["content"]
    # The registry holds the pending prompt keyed by the wire's prompt_id.
    assert action["prompt_id"] in adapter._pending_prompts
    state = adapter._pending_prompts[action["prompt_id"]]
    assert state["kind"] == "exec_approval"
    assert state["session_key"] == "sess:1"


@pytest.mark.asyncio
async def test_exec_approval_smart_denied_and_flag_gating():
    adapter, stub = _adapter()
    await adapter.send_exec_approval(
        "c1", "cmd", "s", smart_denied=True, allow_permanent=True, allow_session=True
    )
    ids = [o["id"] for o in stub.sent[-1]["options"]]
    assert ids == ["once", "deny"]  # smart-deny: no session/always
    await adapter.send_exec_approval(
        "c1", "cmd", "s", allow_session=True, allow_permanent=False
    )
    ids = [o["id"] for o in stub.sent[-1]["options"]]
    assert ids == ["once", "session", "deny"]


@pytest.mark.asyncio
async def test_exec_approval_without_prompt_op_fails_for_text_fallback():
    adapter, stub = _adapter(supported_ops=("send", "edit", "typing"))
    result = await adapter.send_exec_approval("c1", "cmd", "s")
    # success=False → gateway/run.py falls back to the text approval prompt
    # (same contract as a failed native button send).
    assert result.success is False
    assert all(a["op"] != "prompt" for a in stub.sent)
    assert adapter._pending_prompts == {}  # nothing left pending


@pytest.mark.asyncio
async def test_slash_confirm_renders_three_options():
    adapter, stub = _adapter()
    result = await adapter.send_slash_confirm(
        "c1", "Reload MCP", "This invalidates the prompt cache.", "sess:1", "cf-9"
    )
    assert result.success is True
    action = stub.sent[-1]
    ids = [o["id"] for o in action["options"]]
    assert ids == ["once", "always", "cancel"]
    assert "Reload MCP" in action["content"]
    state = adapter._pending_prompts[action["prompt_id"]]
    assert state == {
        **state,
        "kind": "slash_confirm",
        "confirm_id": "cf-9",
        "session_key": "sess:1",
    }


@pytest.mark.asyncio
async def test_clarify_renders_choices_plus_other_with_positional_ids():
    adapter, stub = _adapter()
    result = await adapter.send_clarify(
        "c1",
        "Which environment?",
        ["staging — the safe one", "production"],
        "cl-1",
        "sess:1",
    )
    assert result.success is True
    action = stub.sent[-1]
    assert action["prompt_kind"] == "clarify"
    ids = [o["id"] for o in action["options"]]
    # Positional ids (choice text is arbitrary UTF-8; ids must be callback-safe).
    assert ids == ["c0", "c1", "other"]
    labels = [o["label"] for o in action["options"]]
    assert labels[0].startswith("staging")
    state = adapter._pending_prompts[action["prompt_id"]]
    assert state["choices"] == ["staging — the safe one", "production"]


@pytest.mark.asyncio
async def test_clarify_open_ended_uses_base_text_path(monkeypatch):
    adapter, stub = _adapter()
    # No choices → base class question-only text send (no prompt op).
    result = await adapter.send_clarify("c1", "What now?", None, "cl-2", "sess:1")
    assert result.success is True
    assert all(a["op"] != "prompt" for a in stub.sent)


@pytest.mark.asyncio
async def test_prompt_decline_degrades_clarify_to_numbered_text(monkeypatch):
    adapter, stub = _adapter()
    stub.next_prompt_result = {"success": False, "error": "nope"}
    marked: list[str] = []
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text", lambda cid: marked.append(cid)
    )
    result = await adapter.send_clarify(
        "c1", "Which?", ["a", "b"], "cl-3", "sess:1"
    )
    # Falls back to the base numbered-text clarify (a plain send).
    assert result.success is True
    assert stub.sent[-1]["op"] == "send"
    assert "1. a" in stub.sent[-1]["content"]
    assert marked == ["cl-3"]
    assert adapter._pending_prompts == {}


# ── the pending-prompt registry ──────────────────────────────────────────


def test_registry_mint_consume_once_and_expiry():
    adapter, _stub = _adapter()
    pid = adapter._mint_prompt("exec_approval", {"session_key": "s"}, timeout_s=3600)
    assert adapter._pop_prompt(pid) is not None
    assert adapter._pop_prompt(pid) is None  # one answer wins
    stale = adapter._mint_prompt("exec_approval", {"session_key": "s"}, timeout_s=-1)
    assert adapter._pop_prompt(stale) is None  # expired misses


# ── inbound consumption ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_response_resolves_exec_approval(monkeypatch):
    adapter, stub = _adapter()
    await adapter.send_exec_approval("c1", "cmd", "sess:9")
    prompt_id = stub.sent[-1]["prompt_id"]

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda sk, choice, **kw: calls.append((sk, choice)) or 1,
    )
    event = _event({"prompt_id": prompt_id, "option_id": "session"})
    consumed = await adapter._consume_prompt_response(event)
    assert consumed is True
    assert calls == [("sess:9", "session")]
    # Consumed prompts leave the registry; the ack landed as a plain send.
    assert prompt_id not in adapter._pending_prompts
    assert stub.sent[-1]["op"] == "send"
    assert "session" in stub.sent[-1]["content"].lower()


@pytest.mark.asyncio
async def test_prompt_response_resolves_slash_confirm(monkeypatch):
    adapter, stub = _adapter()
    await adapter.send_slash_confirm("c1", "T", "msg", "sess:9", "cf-1")
    prompt_id = stub.sent[-1]["prompt_id"]

    resolved: list[tuple] = []

    async def fake_resolve(session_key, confirm_id, choice, **kw):
        resolved.append((session_key, confirm_id, choice))
        return "done!"

    monkeypatch.setattr("tools.slash_confirm.resolve", fake_resolve)
    event = _event({"prompt_id": prompt_id, "option_id": "always"})
    assert await adapter._consume_prompt_response(event) is True
    assert resolved == [("sess:9", "cf-1", "always")]
    # The handler's result text went out as a follow-up send.
    sends = [a for a in stub.sent if a["op"] == "send"]
    assert any("done!" in a["content"] for a in sends)


@pytest.mark.asyncio
async def test_prompt_response_resolves_clarify_choice_and_other(monkeypatch):
    adapter, stub = _adapter()
    await adapter.send_clarify("c1", "Which?", ["alpha", "beta"], "cl-9", "s")
    prompt_id = stub.sent[-1]["prompt_id"]

    resolved: list[tuple] = []
    marked: list[str] = []
    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify",
        lambda cid, resp: resolved.append((cid, resp)) or True,
    )
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text", lambda cid: marked.append(cid)
    )
    # Positional id maps back to the REAL choice text.
    event = _event({"prompt_id": prompt_id, "option_id": "c1"})
    assert await adapter._consume_prompt_response(event) is True
    assert resolved == [("cl-9", "beta")]

    # "Other" flips to text capture.
    await adapter.send_clarify("c1", "Which?", ["a"], "cl-10", "s")
    prompt_id2 = stub.sent[-1]["prompt_id"]
    event2 = _event({"prompt_id": prompt_id2, "option_id": "other"})
    assert await adapter._consume_prompt_response(event2) is True
    assert marked == ["cl-10"]


@pytest.mark.asyncio
async def test_unknown_or_expired_prompt_falls_through():
    adapter, _stub = _adapter()
    event = _event({"prompt_id": "deadbeef", "option_id": "once"})
    assert await adapter._consume_prompt_response(event) is False
    assert await adapter._consume_prompt_response(_event(None)) is False
    # Malformed shapes never consume.
    assert await adapter._consume_prompt_response(_event({"prompt_id": ""})) is False


# ── Discord type-3 hp1 decode ────────────────────────────────────────────


def test_discord_component_interaction_decodes_prompt_token():
    adapter, _stub = _adapter()

    class Forward:
        platform = "discord"
        method = "POST"
        path = "/interactions/bot1"
        body = (
            b'{"type": 3, "id": "i1", "channel_id": "ch1", "guild_id": "g1",'
            b' "message": {"id": "pm55"},'
            b' "member": {"user": {"id": "u1", "username": "ben"}},'
            b' "data": {"custom_id": "hp1:a1b2c3d4:deny"}}'
        )

    event = adapter._discord_interaction_to_event(Forward())
    assert event is not None
    assert event.prompt_response == {
        "prompt_id": "a1b2c3d4",
        "option_id": "deny",
        "prompt_message_id": "pm55",
    }
    assert event.text == "/deny"
    assert event.message_type == MessageType.COMMAND


def test_discord_foreign_custom_id_keeps_legacy_text_shape():
    adapter, _stub = _adapter()

    class Forward:
        platform = "discord"
        method = "POST"
        path = "/interactions/bot1"
        body = (
            b'{"type": 3, "id": "i1", "channel_id": "ch1", "guild_id": "g1",'
            b' "data": {"custom_id": "someones_button"}}'
        )

    event = adapter._discord_interaction_to_event(Forward())
    assert event is not None
    assert event.prompt_response is None
    assert event.text == "someones_button"
    assert event.message_type == MessageType.TEXT


def test_decode_prompt_token_matches_connector_codec():
    adapter, _stub = _adapter()
    assert adapter._decode_prompt_token("hp1:p1:deny") == ("p1", "deny")
    assert adapter._decode_prompt_token("ea:once:3") is None
    assert adapter._decode_prompt_token("hp1:p1") is None
    assert adapter._decode_prompt_token("hp1:bad id:x") is None
    assert adapter._decode_prompt_token("") is None


# ── react ack lifecycle ──────────────────────────────────────────────────


def _reactable_event() -> MessageEvent:
    return MessageEvent(
        text="do something",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform="discord",
            chat_id="ch1",
            chat_type="channel",
            user_id="u1",
            message_id="m42",
        ),
        message_id="m42",
    )


@pytest.mark.asyncio
async def test_processing_lifecycle_reacts_eyes_then_check():
    adapter, stub = _adapter()
    event = _reactable_event()
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    reacts = [a for a in stub.sent if a["op"] == "react"]
    assert [(r["emoji"], r.get("remove", False)) for r in reacts] == [
        ("👀", False),
        ("👀", True),
        ("✅", False),
    ]
    assert all(r["message_id"] == "m42" and r["chat_id"] == "ch1" for r in reacts)


@pytest.mark.asyncio
async def test_processing_failure_reacts_cross():
    adapter, stub = _adapter()
    event = _reactable_event()
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    emojis = [a["emoji"] for a in stub.sent if a["op"] == "react"]
    assert emojis[-1] == "❌"


@pytest.mark.asyncio
async def test_react_is_op_gated_and_best_effort():
    adapter, stub = _adapter(supported_ops=("send", "edit", "typing"))
    event = _reactable_event()
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert all(a["op"] != "react" for a in stub.sent)  # never hit the wire
    # And a connector decline never raises.
    adapter2, stub2 = _adapter()
    stub2.next_react_result = {"success": False, "error": "nope"}
    await adapter2.on_processing_start(event)  # must not raise


@pytest.mark.asyncio
async def test_cancelled_outcome_removes_eyes_without_verdict():
    adapter, stub = _adapter()
    event = _reactable_event()
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
    reacts = [(a["emoji"], a.get("remove", False)) for a in stub.sent if a["op"] == "react"]
    assert reacts == [("👀", True)]  # eyes removed, no ✅/❌


# ── owner-scoped prompt authorization (CWE-639) ──────────────────────────


def _inbound_channel_event(user_id, *, platform="discord", chat_id="chan42", **src_kw):
    """A normal inbound channel message, built through the real wire decoder.

    ``_event_from_wire`` keeps the UNDERLYING platform (``discord``), which is
    what makes the session key differ from the one a Discord button press
    normalizes to — the split this authorization has to survive.
    """
    src = {
        "platform": platform,
        "chat_id": chat_id,
        "chat_type": "channel",
        "scope_id": "guild9",
        "user_id": user_id,
    }
    src.update(src_kw)
    return _event_from_wire({"text": "hi", "message_type": "text", "source": src})


def _prompt_response_event(base_event, prompt_id, option_id):
    """The same conversation's source answering a prompt on the inbound lane."""
    return MessageEvent(
        text=f"/{option_id}",
        message_type=MessageType.COMMAND,
        source=base_event.source,
        prompt_response={"prompt_id": prompt_id, "option_id": option_id},
    )


def _discord_button_forward(prompt_id, option_id, user_id, channel_id="chan42"):
    """A real Discord component press as the passthrough plane delivers it."""

    class Forward:
        platform = "discord"
        method = "POST"
        path = "/interactions/bot1"
        body = (
            '{"type": 3, "id": "i1", "channel_id": "%s", "guild_id": "guild9",'
            ' "message": {"id": "pm55"},'
            ' "member": {"user": {"id": "%s", "username": "%s"}},'
            ' "data": {"custom_id": "hp1:%s:%s"}}'
            % (channel_id, user_id, user_id, prompt_id, option_id)
        ).encode()

    return Forward()


def _own_a_session(adapter, event) -> str:
    """Run ``event`` through the inbound capture and return its session key."""
    adapter._capture_scope(event)
    return build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )


@pytest.fixture
def approval_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda sk, choice, **kw: calls.append((sk, choice)) or 1,
    )
    return calls


@pytest.mark.asyncio
async def test_prompt_response_rejects_non_owner_in_per_user_channel(approval_calls):
    """A co-member must not resolve another user's approval in a per-user
    channel session — the relay analog of the native adapters' interaction
    owner check. Resolution runs before handle_message, so without this the
    normal auth gate never fires for the click."""
    adapter, _stub = _adapter()
    adapter.config.extra["group_sessions_per_user"] = True
    owner_key = _own_a_session(adapter, _inbound_channel_event("owner"))
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": owner_key, "chat_id": "chan42"}
    )

    attacker = _inbound_channel_event("attacker")
    consumed = await adapter._consume_prompt_response(
        _prompt_response_event(attacker, pid, "always")
    )
    assert consumed is True  # dropped, not re-dispatched as the attacker's chat
    assert approval_calls == []  # the victim's approval was NOT resolved
    assert pid in adapter._pending_prompts  # left intact for its real owner


@pytest.mark.asyncio
async def test_prompt_response_allows_owner_in_per_user_channel(approval_calls):
    adapter, _stub = _adapter()
    adapter.config.extra["group_sessions_per_user"] = True
    owner = _inbound_channel_event("owner")
    owner_key = _own_a_session(adapter, owner)
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": owner_key, "chat_id": "chan42"}
    )

    consumed = await adapter._consume_prompt_response(
        _prompt_response_event(owner, pid, "always")
    )
    assert consumed is True
    assert approval_calls == [(owner_key, "always")]
    assert pid not in adapter._pending_prompts


@pytest.mark.asyncio
async def test_prompt_response_shared_channel_allows_any_member(approval_calls):
    """A shared session (group_sessions_per_user=False) has no participant id in
    its key, so every co-member is a legitimate responder — the fix must not
    regress that."""
    adapter, _stub = _adapter()
    adapter.config.extra["group_sessions_per_user"] = False
    shared_key = _own_a_session(adapter, _inbound_channel_event("owner"))
    assert adapter._session_owner_by_key == {}  # a shared key is never owned
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": shared_key, "chat_id": "chan42"}
    )

    other = _inbound_channel_event("someone_else")
    consumed = await adapter._consume_prompt_response(
        _prompt_response_event(other, pid, "always")
    )
    assert consumed is True
    assert approval_calls == [(shared_key, "always")]


@pytest.mark.asyncio
async def test_prompt_response_unowned_session_still_resolves(approval_calls):
    """A session this adapter never saw inbound (cron-/API-started turn) has no
    recorded owner and must keep resolving — the gate adds a check, it must not
    add a dead end."""
    adapter, _stub = _adapter()
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": "agent:main:discord:channel:elsewhere", "chat_id": "chan42"}
    )
    assert await adapter._consume_prompt_response(
        _prompt_response_event(_inbound_channel_event("whoever"), pid, "once")
    ) is True
    assert approval_calls == [("agent:main:discord:channel:elsewhere", "once")]


# ── the same guard across the two relay lanes ────────────────────────────
#
# A Discord conversation arrives over the connector inbound lane, which KEEPS
# the underlying platform (agent:main:discord:…), but its buttons come back on
# the passthrough plane, where _discord_interaction_to_event normalizes to
# Platform.RELAY and drops thread_id (agent:main:relay:channel:…). Ownership
# must therefore be decided on the participant id, not on a re-derived key.


@pytest.mark.asyncio
async def test_discord_button_owner_resolves_across_relay_lanes(approval_calls):
    adapter, _stub = _adapter(platform="discord")
    adapter.config.extra["group_sessions_per_user"] = True
    owner = _inbound_channel_event("owner")
    owner_key = _own_a_session(adapter, owner)
    assert owner_key.startswith("agent:main:discord:")  # the inbound lane's key
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": owner_key, "chat_id": "chan42"}
    )

    await adapter._on_passthrough(_discord_button_forward(pid, "always", "owner"))
    assert approval_calls == [(owner_key, "always")]
    assert pid not in adapter._pending_prompts


@pytest.mark.asyncio
async def test_discord_button_non_owner_rejected_across_relay_lanes(approval_calls):
    adapter, _stub = _adapter(platform="discord")
    adapter.config.extra["group_sessions_per_user"] = True
    owner_key = _own_a_session(adapter, _inbound_channel_event("owner"))
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": owner_key, "chat_id": "chan42"}
    )

    await adapter._on_passthrough(_discord_button_forward(pid, "always", "attacker"))
    assert approval_calls == []
    assert pid in adapter._pending_prompts


@pytest.mark.asyncio
async def test_discord_thread_button_owner_resolves_across_relay_lanes(approval_calls):
    """The lanes disagree about threads too: the connector sends a Discord
    thread as chat_type=thread + thread_id, while the interaction body only has
    the thread's channel_id. Per-user threads (thread_sessions_per_user) are the
    case where that key would diverge on two segments, not just the platform."""
    adapter, _stub = _adapter(platform="discord")
    adapter.config.extra["group_sessions_per_user"] = True
    adapter.config.extra["thread_sessions_per_user"] = True
    owner = _inbound_channel_event(
        "owner", chat_id="th9", chat_type="thread", thread_id="th9"
    )
    owner_key = _own_a_session(adapter, owner)
    assert owner_key == "agent:main:discord:thread:th9:th9:owner"
    pid = adapter._mint_prompt(
        "exec_approval", {"session_key": owner_key, "chat_id": "th9"}
    )

    await adapter._on_passthrough(
        _discord_button_forward(pid, "once", "owner", channel_id="th9")
    )
    assert approval_calls == [(owner_key, "once")]
