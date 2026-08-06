"""Slice 1.1R-B: capture-aware pre-ack queue seam.

Real PTB ``Update``/``Message`` fixtures, a real temporary SQLite database
per test (no mocked persistence) -- proving the durable-before-ack guarantee
at the one seam early enough for both polling and webhook: ``Queue.put()``.
"""
import asyncio
import datetime
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# tests/gateway/conftest.py unconditionally installs a MagicMock stand-in for
# the ``telegram`` package at collection time (see its ``_ensure_telegram_mock``
# docstring) so unrelated adapter tests don't need python-telegram-bot
# installed. This slice's TDD sequence explicitly requires real PTB fixtures
# and a real temporary SQLite database, no mocked persistence -- so force the
# genuine installed package back in before importing anything that touches it.
for _mod_name in [n for n in list(sys.modules) if n == "telegram" or n.startswith("telegram.")]:
    if not hasattr(sys.modules[_mod_name], "__file__"):
        del sys.modules[_mod_name]
import telegram as _real_telegram  # noqa: E402

assert hasattr(_real_telegram, "__file__"), (
    "expected the real python-telegram-bot package, not tests/gateway/conftest.py's mock"
)

from telegram import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from plugins.platforms.telegram.capture_ingress import (
    CaptureAwareQueue,
    CaptureIngressStore,
    CapturePersistenceError,
    DUPLICATE_SAME,
    INSERTED,
    RouteConflict,
    RoutePolicyTable,
    canonicalize_update,
    classify_event_type,
    compute_event_id,
    compute_payload_hash,
    normalize_thread_id,
)

ACCOUNT_ID = 777
PROFILE = "default"
CAPTURE_CHAT_ID = -1001
CAPTURE_THREAD_ID = 271


def _chat(chat_id=CAPTURE_CHAT_ID, is_forum=True, chat_type="supergroup"):
    return Chat(id=chat_id, type=chat_type, is_forum=is_forum)


def _user(user_id=555, is_bot=False):
    return User(id=user_id, first_name="Alice", is_bot=is_bot)


def _message(
    *,
    message_id=42,
    chat=None,
    from_user=None,
    text="hello",
    thread_id=CAPTURE_THREAD_ID,
    **extra,
):
    return Message(
        message_id=message_id,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=chat or _chat(),
        from_user=from_user if from_user is not None else _user(),
        text=text,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        **extra,
    )


def _update(update_id=1000, message=None, **kwargs):
    return Update(update_id=update_id, message=message if message is not None else _message(**kwargs))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "capture_ingress.db"


@pytest.fixture()
def store(db_path):
    s = CaptureIngressStore(db_path)
    yield s
    s.close()


def _capture_only_routes():
    return [
        {
            "chat_id": CAPTURE_CHAT_ID,
            "thread_id": CAPTURE_THREAD_ID,
            "mode": "capture_only",
            "sink": "braindump",
            "policy_version": "1.0.0",
        }
    ]


def _agent_routes():
    return [
        {
            "chat_id": CAPTURE_CHAT_ID,
            "thread_id": CAPTURE_THREAD_ID,
            "mode": "agent",
            "sink": "agent-dispatch",
            "policy_version": "1.0.0",
        }
    ]


def _drop_routes():
    return [
        {
            "chat_id": CAPTURE_CHAT_ID,
            "thread_id": CAPTURE_THREAD_ID,
            "mode": "drop",
            "sink": "n-a",
            "policy_version": "1.0.0",
        }
    ]


def _make_queue(store, routes, *, is_own=False, is_authorized=True, alert=None):
    return CaptureAwareQueue(
        store=store,
        route_table_provider=lambda: RoutePolicyTable(routes),
        account_id_provider=lambda: ACCOUNT_ID,
        profile_provider=lambda: PROFILE,
        thread_id_resolver=lambda message: (
            str(message.message_thread_id) if message.message_thread_id is not None else None
        ),
        is_own_message=lambda message: is_own,
        is_authorized_sender=lambda message: is_authorized,
        alert_failure=alert,
    )


def _ledger_row(store, event_id):
    cur = store._conn.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?", (event_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _payload_row(store, event_id):
    cur = store._conn.execute(
        "SELECT * FROM ingress_payload WHERE event_id = ?", (event_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


class TestPureHelpers:
    def test_canonicalize_update_is_deterministic_sorted_json(self):
        upd = _update()
        a = canonicalize_update(upd)
        b = canonicalize_update(upd)
        assert a == b
        assert a == a.strip()  # no incidental whitespace padding
        assert b'": ' not in a  # compact separators, not the default json.dumps spacing

    def test_event_id_format(self):
        assert compute_event_id("default", 777, 1000) == "telegram:default:777:1000"

    def test_payload_hash_format(self):
        h = compute_payload_hash(b"abc")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_general_topic_normalizes_to_null(self):
        assert normalize_thread_id("1") is None
        assert normalize_thread_id(None) is None
        assert normalize_thread_id("271") == 271

    def test_classify_event_type_command(self):
        assert classify_event_type(_message(text="/start")) == "command"

    def test_classify_event_type_text(self):
        assert classify_event_type(_message(text="hello")) == "text"

    def test_classify_event_type_other_when_no_recognized_content(self):
        assert classify_event_type(_message(text=None)) == "other"


class TestRoutePolicyTable:
    def test_exact_match_requires_both_chat_and_thread(self):
        table = RoutePolicyTable(_capture_only_routes())
        assert table.lookup(CAPTURE_CHAT_ID, CAPTURE_THREAD_ID) is not None
        # Same thread number, different chat: must not match.
        assert table.lookup(-999, CAPTURE_THREAD_ID) is None
        # Same chat, different thread: must not match.
        assert table.lookup(CAPTURE_CHAT_ID, 999) is None

    def test_general_topic_route_matches_null_thread(self):
        routes = [
            {"chat_id": CAPTURE_CHAT_ID, "thread_id": None, "mode": "capture_only", "sink": "s", "policy_version": "1.0.0"}
        ]
        table = RoutePolicyTable(routes)
        assert table.lookup(CAPTURE_CHAT_ID, None) is not None

    def test_no_configured_route_is_none(self):
        table = RoutePolicyTable([])
        assert table.lookup(CAPTURE_CHAT_ID, CAPTURE_THREAD_ID) is None

    def test_unrecognized_mode_fails_closed_not_registered(self):
        """A typo'd/unrecognized mode (e.g. wrong case) must not silently
        behave like 'agent' dispatch just because it fails the exact
        string comparisons in CaptureAwareQueue.put(). Fail closed: the
        entry is dropped, so the route is treated as unconfigured (today's
        safe pass-through default), not as a hidden 'deliver' policy.
        """
        routes = [
            {"chat_id": CAPTURE_CHAT_ID, "thread_id": CAPTURE_THREAD_ID, "mode": "Capture_Only", "sink": "s", "policy_version": "1.0.0"}
        ]
        table = RoutePolicyTable(routes)
        assert table.lookup(CAPTURE_CHAT_ID, CAPTURE_THREAD_ID) is None


class TestCaptureIngressStore:
    def _kwargs(self, **override):
        base = dict(
            event_id="telegram:default:777:1000",
            platform="telegram",
            account_id=ACCOUNT_ID,
            profile=PROFILE,
            update_id=1000,
            chat_id=CAPTURE_CHAT_ID,
            thread_id=CAPTURE_THREAD_ID,
            message_id=42,
            sender_id=555,
            event_type="text",
            received_at="2026-08-06T20:00:00Z",
            payload_hash="sha256:" + "a" * 64,
            route_mode="capture_only",
            sink="braindump",
            payload_json="{}",
        )
        base.update(override)
        return base

    def test_first_insert_returns_inserted_and_persists_row_and_payload(self, store):
        result = store.commit_capture(**self._kwargs())
        assert result == INSERTED
        row = _ledger_row(store, "telegram:default:777:1000")
        assert row["status"] == "pending"
        assert row["attempts"] == 0
        assert row["lease_expires_at"] is None
        assert row["last_error"] is None
        assert row["completed_at"] is None
        payload = _payload_row(store, "telegram:default:777:1000")
        assert payload["payload_json"] == "{}"

    def test_identical_duplicate_is_a_noop_first_fields_preserved(self, store):
        store.commit_capture(**self._kwargs(received_at="2026-08-06T20:00:00Z"))
        result = store.commit_capture(**self._kwargs(received_at="2026-08-06T20:05:00Z"))
        assert result == DUPLICATE_SAME
        row = _ledger_row(store, "telegram:default:777:1000")
        assert row["received_at"] == "2026-08-06T20:00:00Z"  # first write wins, not overwritten

    def test_conflicting_duplicate_payload_raises_and_does_not_overwrite(self, store):
        store.commit_capture(**self._kwargs(payload_hash="sha256:" + "a" * 64))
        with pytest.raises(RouteConflict):
            store.commit_capture(**self._kwargs(payload_hash="sha256:" + "b" * 64))
        row = _ledger_row(store, "telegram:default:777:1000")
        assert row["payload_hash"] == "sha256:" + "a" * 64  # unchanged

    def test_integrity_error_unrelated_to_a_duplicate_race_is_persistence_error_not_conflict(self, store):
        """A NOT-NULL violation (e.g. a caller passing account_id=None,
        which can happen if commit_capture is ever reached before the bot's
        own id is known) is a genuine storage/programming-adjacent failure,
        not a lost race against a concurrent insert of the same event_id --
        raising RouteConflict here would misdiagnose it and mislead
        debugging/alerting, even though both exception types fail closed the
        same way (no delegation, no ack).
        """
        with pytest.raises(CapturePersistenceError):
            store.commit_capture(**self._kwargs(account_id=None))
        assert _ledger_row(store, "telegram:default:777:1000") is None

    def test_injected_storage_failure_raises_capture_persistence_error(self, store):
        store._conn.close()  # simulate a hard storage failure (closed handle)
        with pytest.raises(CapturePersistenceError):
            store.commit_capture(**self._kwargs())


class TestCaptureAwareQueueDispatchGating:
    @pytest.mark.asyncio
    async def test_capture_only_route_commits_then_terminal_deny(self, store):
        queue = _make_queue(store, _capture_only_routes())
        upd = _update(update_id=1, text="hello")

        await queue.put(upd)

        assert queue.qsize() == 0  # never delegated to the underlying queue
        eid = compute_event_id(PROFILE, ACCOUNT_ID, 1)
        row = _ledger_row(store, eid)
        assert row is not None
        assert row["route_mode"] == "capture_only"

    @pytest.mark.asyncio
    async def test_sender_identity_less_message_on_capture_only_route_still_denied(self, store):
        """A message-like update with no from_user (e.g. a channel post,
        whose identity lives in sender_chat instead) cannot be keyed into a
        ledger row -- the ingress-ledger contract requires a non-null human
        sender_id -- but a capture-only route's terminal deny is an
        unconditional owner directive ("Capture must never start an agent
        turn... for the capture-only topic"), not conditioned on whether a
        ledger row could be produced. It must still be denied, not silently
        delegated to dispatch just because it couldn't be captured.
        """
        queue = _make_queue(store, _capture_only_routes())
        msg = _message(text="channel announcement", from_user=None)
        msg = Message(
            message_id=msg.message_id,
            date=msg.date,
            chat=msg.chat,
            from_user=None,
            sender_chat=_chat(chat_id=-500, chat_type="channel"),
            text=msg.text,
            message_thread_id=msg.message_thread_id,
            is_topic_message=msg.is_topic_message,
        )
        upd = _update(update_id=16, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 16)
        assert _ledger_row(store, eid) is None  # cannot be captured (no human sender_id)
        assert queue.qsize() == 0  # but must NOT be delegated to dispatch either

    @pytest.mark.asyncio
    async def test_sender_identity_less_message_on_agent_route_passes_through(self, store):
        """Same identity-less shape, but on an 'agent' route: existing
        (non-capture) dispatch behavior is unaffected -- this envelope only
        adds a new hard deny for capture_only, never a new block for agent.
        """
        queue = _make_queue(store, _agent_routes())
        msg = _message(text="channel announcement", from_user=None)
        msg = Message(
            message_id=msg.message_id,
            date=msg.date,
            chat=msg.chat,
            from_user=None,
            sender_chat=_chat(chat_id=-500, chat_type="channel"),
            text=msg.text,
            message_thread_id=msg.message_thread_id,
            is_topic_message=msg.is_topic_message,
        )
        upd = _update(update_id=17, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 17)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1  # unchanged existing behavior

    @pytest.mark.asyncio
    async def test_command_on_capture_only_route_captured_as_inert_text_not_dispatched(self, store):
        queue = _make_queue(store, _capture_only_routes())
        upd = _update(update_id=2, text="/deploy prod")

        await queue.put(upd)

        assert queue.qsize() == 0
        eid = compute_event_id(PROFILE, ACCOUNT_ID, 2)
        row = _ledger_row(store, eid)
        assert row["event_type"] == "command"

    @pytest.mark.asyncio
    async def test_media_on_capture_only_route(self, store):
        from telegram import PhotoSize

        queue = _make_queue(store, _capture_only_routes())
        msg = _message(text=None, photo=[PhotoSize(file_id="f1", file_unique_id="u1", width=10, height=10)])
        upd = _update(update_id=3, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 3)
        row = _ledger_row(store, eid)
        assert row["event_type"] == "media"
        assert queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_location_on_capture_only_route(self, store):
        from telegram import Location

        queue = _make_queue(store, _capture_only_routes())
        msg = _message(text=None, location=Location(longitude=1.0, latitude=2.0))
        upd = _update(update_id=4, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 4)
        row = _ledger_row(store, eid)
        assert row["event_type"] == "location"
        assert queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_other_message_like_content_kind(self, store):
        queue = _make_queue(store, _capture_only_routes())
        # message_id/from_user present, no text/media/location: e.g. a
        # message the ledger contract still requires a row for.
        msg = _message(text=None)
        upd = _update(update_id=5, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 5)
        row = _ledger_row(store, eid)
        assert row["event_type"] == "other"

    @pytest.mark.asyncio
    async def test_non_message_like_update_passes_through_unchanged(self, store):
        queue = _make_queue(store, _capture_only_routes())
        cq = CallbackQuery(id="cbq1", from_user=_user(), chat_instance="ci1", data="x")
        upd = Update(update_id=6, callback_query=cq)

        await queue.put(upd)

        assert queue.qsize() == 1  # delegated unchanged, not captured
        assert queue.get_nowait() is upd

    @pytest.mark.asyncio
    async def test_agent_route_commits_then_delegates_exactly_once(self, store):
        queue = _make_queue(store, _agent_routes())
        upd = _update(update_id=7)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 7)
        row = _ledger_row(store, eid)
        assert row["route_mode"] == "agent"
        assert queue.qsize() == 1
        assert queue.get_nowait() is upd

    @pytest.mark.asyncio
    async def test_drop_route_no_ledger_row_passthrough(self, store):
        queue = _make_queue(store, _drop_routes())
        upd = _update(update_id=8)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 8)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_no_configured_route_passthrough(self, store):
        queue = _make_queue(store, [])  # nothing configured for this chat/topic
        upd = _update(update_id=9)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 9)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_exact_route_matching_same_thread_number_other_chat_no_match(self, store):
        queue = _make_queue(store, _capture_only_routes())
        msg = _message(chat=_chat(chat_id=-2002), thread_id=CAPTURE_THREAD_ID)
        upd = _update(update_id=10, message=msg)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 10)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1  # unmatched: passes through like an unrouted chat

    @pytest.mark.asyncio
    async def test_identical_duplicate_no_second_row_capture_only_still_denied(self, store):
        queue = _make_queue(store, _capture_only_routes())
        upd = _update(update_id=11, text="hello")

        await queue.put(upd)
        await queue.put(upd)  # simulated redelivery of the same update

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 11)
        cur = store._conn.execute(
            "SELECT COUNT(*) FROM ingress_ledger WHERE event_id = ?", (eid,)
        )
        assert cur.fetchone()[0] == 1
        assert queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_conflicting_duplicate_fails_closed_no_delegation(self, store):
        queue = _make_queue(store, _agent_routes())
        msg_a = _message(message_id=42, text="hello")
        upd_a = Update(update_id=12, message=msg_a)
        await queue.put(upd_a)
        assert queue.qsize() == 1
        queue.get_nowait()

        # Same update_id, different canonical payload (different text).
        msg_b = _message(message_id=42, text="different content")
        upd_b = Update(update_id=12, message=msg_b)
        with pytest.raises(RouteConflict):
            await queue.put(upd_b)
        assert queue.qsize() == 0  # no delegation on conflict

    @pytest.mark.asyncio
    async def test_injected_storage_failure_no_delegation(self, store):
        alerts = []
        queue = _make_queue(store, _capture_only_routes(), alert=alerts.append)
        store.close()  # force every commit to fail
        upd = _update(update_id=13)

        with pytest.raises(CapturePersistenceError):
            await queue.put(upd)

        assert queue.qsize() == 0
        assert alerts  # deterministic failure was alerted

    @pytest.mark.asyncio
    async def test_unauthorized_sender_no_capture_existing_behavior_preserved(self, store):
        queue = _make_queue(store, _capture_only_routes(), is_authorized=False)
        upd = _update(update_id=14)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 14)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1  # existing (downstream) authorization behavior unchanged

    @pytest.mark.asyncio
    async def test_bot_authored_no_capture(self, store):
        queue = _make_queue(store, _capture_only_routes(), is_own=True)
        upd = _update(update_id=15)

        await queue.put(upd)

        eid = compute_event_id(PROFILE, ACCOUNT_ID, 15)
        assert _ledger_row(store, eid) is None
        assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_queue_sentinel_passes_through_unchanged(self, store):
        queue = _make_queue(store, _capture_only_routes())
        sentinel = object()

        await queue.put(sentinel)

        assert queue.qsize() == 1
        assert queue.get_nowait() is sentinel

    @pytest.mark.asyncio
    async def test_polling_batch_partial_failure_then_retry_collapses_first_inserts_second_once(self, store):
        queue = _make_queue(store, _capture_only_routes())
        upd_n = _update(update_id=20, message_id=100, text="first")
        upd_n1 = _update(update_id=21, message_id=101, text="second")

        await queue.put(upd_n)  # N commits

        store.close()  # simulate N+1 failing to persist
        with pytest.raises(CapturePersistenceError):
            await queue.put(upd_n1)

        # Reopen (as a fresh retry attempt would) and replay both.
        reopened = CaptureIngressStore(store._db_path)
        try:
            retry_queue = _make_queue(reopened, _capture_only_routes())
            await retry_queue.put(upd_n)  # redelivered N: collapses, no second row
            await retry_queue.put(upd_n1)  # N+1 inserts once now that storage recovered

            eid_n = compute_event_id(PROFILE, ACCOUNT_ID, 20)
            eid_n1 = compute_event_id(PROFILE, ACCOUNT_ID, 21)
            assert reopened._conn.execute(
                "SELECT COUNT(*) FROM ingress_ledger WHERE event_id = ?", (eid_n,)
            ).fetchone()[0] == 1
            assert reopened._conn.execute(
                "SELECT COUNT(*) FROM ingress_ledger WHERE event_id = ?", (eid_n1,)
            ).fetchone()[0] == 1
        finally:
            reopened.close()
