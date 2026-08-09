"""Tests for Telegram message reactions tied to processing lifecycle hooks."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


def _make_adapter(**extra):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter._bot = AsyncMock()
    adapter._bot.set_message_reaction = AsyncMock()
    adapter._intentional_reaction_targets = set()
    return adapter


def _make_event(chat_id: str = "123", message_id: str = "456") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="private",
            user_id="42",
            user_name="TestUser",
        ),
        message_id=message_id,
    )


# ── _reactions_enabled ───────────────────────────────────────────────


def test_reactions_disabled_by_default(monkeypatch):
    """Telegram reactions should be disabled by default."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


def test_reactions_enabled_when_set_true(monkeypatch):
    """Setting TELEGRAM_REACTIONS=true enables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is True


# ── _set_reaction ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_reaction_calls_bot_api(monkeypatch):
    """_set_reaction should call bot.set_message_reaction with correct args."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()

    result = await adapter._set_reaction("123", "456", "\U0001f440")

    assert result is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )


# ── on_processing_start ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_start_handles_missing_ids(monkeypatch):
    """Should handle events without chat_id or message_id gracefully."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(chat_id=None),
        message_id=None,
    )

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()


# ── on_processing_complete ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_clears_reaction(monkeypatch):
    """Cancelled processing should clear the in-progress reaction.

    Without this clear, the 👀 reaction lingers on the user's message
    indefinitely (until another agent run swaps it for 👍/👎). On a
    ``/stop`` that ends a session, that reaction never gets cleaned up.
    """
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    # set_message_reaction with reaction=None clears all reactions on the
    # message (Bot API documented semantics; equivalent to Bot API 10.0's
    # deleteMessageReaction but works on PTB 22.6 already).
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction=None,
    )


@pytest.mark.asyncio
async def test_clear_reactions_handles_api_error_gracefully(monkeypatch):
    """API errors during clear should not propagate."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no perms"))

    result = await adapter._clear_reactions("123", "456")
    assert result is False


# ── config.py bridging ───────────────────────────────────────────────


def test_config_bridges_telegram_reactions(monkeypatch, tmp_path):
    """gateway/config.py bridges telegram.reactions to TELEGRAM_REACTIONS env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reactions": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use setenv (not delenv) so monkeypatch registers cleanup even when
    # the var doesn't exist yet — load_gateway_config will overwrite it.
    monkeypatch.setenv("TELEGRAM_REACTIONS", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    assert os.getenv("TELEGRAM_REACTIONS") == "true"


def _patch_index(monkeypatch, tmp_path):
    from gateway import rich_sent_store
    monkeypatch.setattr(rich_sent_store, "_store_path",
                        lambda: str(tmp_path / "state" / "rich_sent_index.json"))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: tmp_path / "base")
    return rich_sent_store


def _rx_adapter(
    monkeypatch, tmp_path, *, authorized=True, thread_id="77", sender_id: str | None = "111",
    extra=None,
):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    store = _patch_index(monkeypatch, tmp_path)
    store.record("-100", "900", "A bot-authored answer", thread_id=thread_id, sender_id=sender_id)
    runner = SimpleNamespace(_is_user_authorized=Mock(return_value=authorized), _profile_adapters={})
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra or {})
    adapter.gateway_runner = runner
    adapter._bot = SimpleNamespace(id=111)
    adapter.handle_message = AsyncMock()
    adapter._authorization_check = lambda user_id, chat_type=None, chat_id=None: authorized
    runner._authorization_adapter = lambda platform, profile=None: adapter
    return adapter, runner


def _rx_update(*, old, new, user_id="42", is_bot=False, chat_id="-100", update_id=123):
    return SimpleNamespace(
        update_id=update_id,
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="supergroup", is_forum=True),
            message_id=900,
            user=SimpleNamespace(
                id=user_id, username="authorized-user", full_name="Authorized User", is_bot=is_bot
            ),
            old_reaction=[SimpleNamespace(emoji=v) for v in old],
            new_reaction=[SimpleNamespace(emoji=v) for v in new],
        ),
    )


def _live_adapter(extra):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    return TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra=extra))


@pytest.mark.asyncio
async def test_lifecycle_off_intentional_survives_overwrite(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    adapter.gateway_runner = SimpleNamespace(_session_key_for_source=lambda source: "session")
    event = _make_event()
    await adapter.on_processing_start(event)
    adapter._bot.set_message_reaction.assert_not_awaited()
    assert adapter._current_reaction_targets == {"session": ("123", "456")}
    assert not hasattr(adapter, "add_reaction")
    assert await adapter.add_current_reaction("session", "❤️") is True
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert adapter._bot.set_message_reaction.await_args.kwargs["reaction"] == "❤"
    assert adapter._bot.set_message_reaction.await_count == 1
    assert adapter._current_reaction_targets == {}
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter.gateway_runner = SimpleNamespace(_session_key_for_source=lambda source: "session")
    event = _make_event()
    await adapter.on_processing_start(event)
    assert await adapter.add_current_reaction("session", "❤️") is True
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert [c.kwargs["reaction"] for c in adapter._bot.set_message_reaction.await_args_list] == ["👀", "❤"]
    assert adapter._intentional_reaction_targets == set()


@pytest.mark.asyncio
async def test_intentional_reaction_never_uses_stale_session_origin(monkeypatch):
    """A reset session's old origin id must never become the reaction target."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    adapter.gateway_runner = SimpleNamespace(_session_key_for_source=lambda source: "random")
    event = _make_event(message_id="30999")

    await adapter.on_processing_start(event)
    assert await adapter.add_current_reaction("random", "👍") is True

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=30999, reaction="👍"
    )


@pytest.mark.parametrize(("enabled", "expected"), [(False, False), (True, True), ("yes", True), ("off", False)])
def test_inbound_opt_in_and_old_ptb(monkeypatch, caplog, enabled, expected):
    from plugins.platforms.telegram import adapter as mod

    class FakeRH:
        MESSAGE_REACTION_UPDATED = "updated"
        def __init__(self, callback, **kwargs):
            self.callback, self.kwargs = callback, kwargs

    monkeypatch.setattr(mod, "MessageReactionHandler", FakeRH)
    handlers = []
    ad = _make_adapter(inbound_reactions=enabled)
    ad._register_reaction_handler(SimpleNamespace(add_handler=handlers.append))
    found = [h for h in handlers if isinstance(h, FakeRH)]
    assert bool(found) is expected
    if expected:
        assert found[0].callback == ad._handle_message_reaction
        assert found[0].kwargs == {"message_reaction_types": "updated"}
    if enabled is True:  # once: old/missing PTB keeps connect path alive
        monkeypatch.setattr(mod, "MessageReactionHandler", None)
        with caplog.at_level("WARNING"):
            _make_adapter(inbound_reactions=True)._register_reaction_handler(
                SimpleNamespace(add_handler=lambda *_: None)
            )
        assert "reaction updates unsupported" in caplog.text


def test_provenance_contracts(monkeypatch, tmp_path):
    store = _patch_index(monkeypatch, tmp_path)
    store.record("123", "456", "first", thread_id="77")
    store.record("123", "456", "edited")
    assert store.lookup_entry("123", "456")["thread_id"] == "77"
    ad = _make_adapter(inbound_reactions=True)
    ad._bot = SimpleNamespace(id=12345)
    real_record = store.record
    rec = Mock()
    monkeypatch.setattr(store, "record", rec)
    ad._record_sent_message("123", "456", "answer")
    assert rec.call_args.kwargs["sender_id"] == 12345
    off = _make_adapter()
    off._rich_messages_enabled = False
    rec2 = Mock()
    monkeypatch.setattr(store, "record", rec2)
    off._record_sent_message("123", "456", "answer")
    rec2.assert_not_called()
    monkeypatch.setattr(store, "record", real_record)
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "restart")}
    subprocess.run([sys.executable, "-c",
        "from gateway.rich_sent_store import record; record('c','m','text',thread_id='1',sender_id='7')"],
        check=True, env=env)
    out = subprocess.run([sys.executable, "-c",
        "from gateway.rich_sent_store import lookup_entry; print(lookup_entry('c','m'))"],
        check=True, capture_output=True, text=True, env=env).stdout
    assert "'thread_id': '1'" in out and "'sender_id': '7'" in out
    path = tmp_path / "state" / "rich_sent_index.json"
    monkeypatch.setattr(store, "_MAX_ENTRIES", 20)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bad:x": {"t": "old", "ts": None}}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: store.record("c", i, str(i)), range(20)))
    data = json.loads(path.read_text())
    assert len(data) == 20 and "bad:x" not in data
    cur = tmp_path / "current" / "state" / "rich_sent_index.json"
    sec = tmp_path / "base" / "profiles" / "secondary" / "state" / "rich_sent_index.json"
    monkeypatch.setattr(store, "_store_path", lambda: str(cur))
    cur.parent.mkdir(parents=True); sec.parent.mkdir(parents=True)
    now = store.time.time()
    cur.write_text(json.dumps({"-100:900": {"t": "one", "ts": now, "thread_id": "77"}}))
    sec.write_text(json.dumps({"-100:900": {"t": "two", "ts": now, "thread_id": "88"}}))
    assert store.lookup_entry("-100", "900", all_profiles=True) is None
    from plugins.platforms.telegram.adapter import TelegramAdapter
    ad2 = object.__new__(TelegramAdapter)
    ad2._record_sent_message = Mock()
    ad2._record_sent_result("-100", SimpleNamespace(
        message_id=900, message_thread_id=123, is_topic_message=False,
        chat=SimpleNamespace(is_forum=False), text="answer", caption=None,
    ), effective_thread_id=None)
    assert ad2._record_sent_message.call_args.kwargs["effective_thread_id"] is None


def test_age_expiry_enforced_on_lookup(monkeypatch, tmp_path):
    store = _patch_index(monkeypatch, tmp_path)
    path = tmp_path / "state" / "rich_sent_index.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"-100:900": {"t": "ancient", "ts": 1, "thread_id": "77", "sender_id": "111"}}))
    assert store.lookup_entry("-100", "900") is None
    path.write_text(json.dumps({"-100:900": {"t": "future", "ts": store.time.time() + 3600}}))
    assert store.lookup_entry("-100", "900") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_thread", "is_forum", "wire", "logical", "expected", "retry"),
    [(88, True, 77, "77", 88, False), (None, False, 77, "77", None, True),
     (None, False, None, "1", "1", False)],
)
async def test_thread_fallback_indexes_effective_thread(
    returned_thread, is_forum, wire, logical, expected, retry
):
    adapter = _make_adapter()
    adapter._is_bad_request_error = adapter._is_thread_not_found_error = lambda e: True
    adapter._prune_stale_dm_topic_binding = Mock()
    adapter._record_sent_message = Mock()
    returned = SimpleNamespace(
        message_id=999, message_thread_id=returned_thread, is_topic_message=is_forum,
        chat=SimpleNamespace(is_forum=is_forum), text="fallback message",
    )
    adapter._bot.send_message = AsyncMock(
        side_effect=([RuntimeError("Message thread not found"), returned] if retry else [returned])
    )
    kwargs = {"chat_id": "123", "text": "fallback message"}
    if wire is not None:
        kwargs["message_thread_id"] = wire
    assert await adapter._send_message_with_thread_fallback(_logical_thread_id=logical, **kwargs) is returned
    assert adapter._record_sent_message.call_args.kwargs["effective_thread_id"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new", "added", "removed"),
    [([], ["❤️"], "added ❤️", None), (["👍"], ["❤️"], "added ❤️", "removed 👍"),
     (["❤️"], [], None, "removed ❤️")],
)
async def test_reaction_delta_routes_deferred_turn(monkeypatch, tmp_path, old, new, added, removed):
    adapter, runner = _rx_adapter(monkeypatch, tmp_path)
    await adapter._handle_message_reaction(_rx_update(old=old, new=new))
    runner._is_user_authorized.assert_called_once()
    ev = adapter.handle_message.await_args.args[0]
    assert ev.source is runner._is_user_authorized.call_args.args[0]
    assert ev.source.thread_id == "77" and ev.source.chat_type == "group"
    assert ev.metadata["deferred_followup_event"] is True
    assert ev.metadata["suppress_typing"] is True
    assert ev.internal is False and ev.reply_to_is_own_message is True
    assert ev.message_id == ev.reply_to_message_id == "900"
    if added:
        assert added in ev.text
    if removed:
        assert removed in ev.text
    assert '"A bot-authored answer"' in ev.text
    assert "NO_REPLY" in ev.channel_prompt and "consequential or risky action" in ev.channel_prompt
    assert "answers a question" in ev.channel_prompt
    assert "do not call telegram_react" in ev.channel_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "old", "new", "expected"),
    [
        ({}, [], ["👍"], False),
        ({"proposal_approval_reactions": ["👍"]}, [], ["👍"], True),
        ({"proposal_approval_reactions": ["👍"]}, ["👍"], [], False),
        ({"proposal_approval_reactions": ["🆒"]}, [], ["👍"], False),
    ],
)
async def test_proposal_approval_reactions_are_opt_in_added_only(
    monkeypatch, tmp_path, extra, old, new, expected
):
    adapter, _ = _rx_adapter(monkeypatch, tmp_path, extra=extra)
    await adapter._handle_message_reaction(_rx_update(old=old, new=new))
    guidance = getattr(adapter.handle_message, "await_args").args[0].channel_prompt
    assert ("configured as approval of the exact proposal" in guidance) is expected
    assert ("execute exactly that proposal in this turn" in guidance) is expected


def test_proposal_approval_reactions_accept_only_standard_telegram_emoji(monkeypatch):
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.canonical_standard_emoji",
        lambda value: value if value in {"👍", "🆒"} else None,
    )
    adapter = _make_adapter(proposal_approval_reactions=["👍", "🆒", "✅", "👍"])
    assert adapter._proposal_approval_reactions() == {"👍", "🆒"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["unauthorized", "bot_actor", "unknown_chat", "missing_thread", "unknown_message",
     "wrong_bot", "missing_sender", "no_delta"],
)
async def test_invalid_reaction_updates_fail_closed(monkeypatch, tmp_path, case):
    adapter, runner = _rx_adapter(
        monkeypatch, tmp_path, authorized=case != "unauthorized",
        thread_id=None if case == "missing_thread" else "77",
        sender_id=None if case == "missing_sender" else "111",
    )
    update = _rx_update(
        old=["👍"] if case == "no_delta" else [], new=["👍"],
        is_bot=case == "bot_actor", chat_id="-200" if case == "unknown_chat" else "-100",
    )
    if case == "unknown_message":
        update.message_reaction.message_id = 901
    elif case == "wrong_bot":
        adapter._bot = SimpleNamespace(id=222)

    await adapter._handle_message_reaction(update)
    assert getattr(adapter.handle_message, "await_count", 0) == 0
    if case != "unauthorized":
        runner._is_user_authorized.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_multiplex_reply_anchor_and_general_sends(monkeypatch, tmp_path):
    from gateway.platforms.base import _reply_anchor_for_event

    adapter, runner = _rx_adapter(monkeypatch, tmp_path)
    update = _rx_update(old=[], new=["👍"], update_id=456)
    await adapter._handle_message_reaction(update)
    await adapter._handle_message_reaction(update)
    assert adapter.handle_message.await_count == 1 and runner._is_user_authorized.call_count == 2

    (tmp_path / "state" / "rich_sent_index.json").unlink()
    sec = tmp_path / "base" / "profiles" / "secondary" / "state" / "rich_sent_index.json"
    sec.parent.mkdir(parents=True)
    sec.write_text(json.dumps({"-100:900": {"t": "answer", "ts": time.time(), "sender_id": "111"}}))
    adapter.handle_message.reset_mock()
    update = _rx_update(old=[], new=["👍"])
    update.message_reaction.chat.is_forum = False
    update.message_reaction.chat.type = SimpleNamespace(value="private")
    await adapter._handle_message_reaction(update)
    ev = adapter.handle_message.await_args.args[0]
    assert ev.source.profile == "secondary" and ev.source.chat_type == "dm"

    for thread_id, expected in [(None, "900"), ("77", None)]:
        assert _reply_anchor_for_event(MessageEvent(
            text="[Telegram reaction: added 👍]",
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="-100", chat_type="group",
                                 user_id="42", thread_id=thread_id),
            message_id="900", reply_to_message_id="900",
            metadata={"deferred_followup_event": True},
        )) == expected

    store = _patch_index(monkeypatch, tmp_path)
    plain = _live_adapter({"rich_messages": False, "inbound_reactions": True})
    bot = MagicMock(id=111, send_chat_action=AsyncMock())
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=700, message_thread_id=None))
    plain._bot = bot
    assert (await plain.send("-100", "The final answer.", metadata={"thread_id": "1", "notify": True})).success
    assert bot.send_message.await_args.kwargs["message_thread_id"] is None
    entry = store.lookup_entry("-100", "700")
    assert entry["thread_id"] == "1" and entry["sender_id"] == "111"
    plain.gateway_runner = SimpleNamespace(_is_user_authorized=Mock(return_value=True), _profile_adapters={})
    plain.handle_message = AsyncMock()
    plain.gateway_runner._authorization_adapter = lambda platform, profile=None: plain
    upd = _rx_update(old=[], new=["👍"]); upd.message_reaction.message_id = 700
    await plain._handle_message_reaction(upd)
    routed = plain.handle_message.await_args.args[0]
    assert routed.source.thread_id == "1" and routed.metadata["deferred_followup_event"] is True

    rich = _live_adapter({"rich_messages": True, "inbound_reactions": True})
    rich._bot = MagicMock(id=111, send_chat_action=AsyncMock(),
        do_api_request=AsyncMock(return_value=SimpleNamespace(message_id=701, message_thread_id=None)))
    content = "## Results\n\n| Case | Status |\n|---|---|\n| rich | ok |"
    assert (await rich.send("-100", content, metadata={"thread_id": "1", "notify": True})).success
    assert "message_thread_id" not in rich._bot.do_api_request.call_args.kwargs["api_kwargs"]
    assert store.lookup_entry("-100", "701")["thread_id"] == "1"

    overflow = _live_adapter({"rich_messages": False, "inbound_reactions": True})
    overflow._bot = MagicMock(
        id=111, edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=500)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=501, message_thread_id=None)),
    )
    assert (await overflow._edit_overflow_split(
        "-100", "500", "word " * 1200, finalize=True, metadata={"thread_id": "1", "notify": True},
    )).success
    assert store.lookup_entry("-100", "500")["thread_id"] == "1"
    assert store.lookup_entry("-100", "501")["thread_id"] == "1"
