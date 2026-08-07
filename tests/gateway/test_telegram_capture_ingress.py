"""Tests for capture ingress — capture-aware asyncio.Queue for Telegram."""
import asyncio
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from plugins.platforms.telegram.capture_ingress import (
    CaptureIngressQueue,
    atomic_insert_capture,
    build_event_id,
    canonicalize_payload,
    payload_hash,
)


# --- Task 1: event_id construction ---

def test_event_id_format():
    """event_id must match ingress-ledger.schema.json: telegram:{profile}:{account_id}:{update_id}"""
    event_id = build_event_id(profile="default", account_id=123456789, update_id=987654321)
    assert event_id == "telegram:default:123456789:987654321"


def test_event_id_rejects_invalid_profile():
    """Profile must match ^[a-z][a-z0-9_-]{0,63}$"""
    with pytest.raises(ValueError):
        build_event_id(profile="INVALID UPPERCASE", account_id=1, update_id=1)


# --- Task 2: canonical payload serialization ---

def test_canonicalize_payload_is_deterministic():
    """Same dict must produce identical bytes regardless of key order."""
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert canonicalize_payload(d1) == canonicalize_payload(d2)


def test_canonicalize_payload_is_utf8():
    """Output must be UTF-8 bytes."""
    result = canonicalize_payload({"text": "café"})
    assert isinstance(result, bytes)
    assert result == json.dumps(
        {"text": "café"}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# --- Fixtures ---

@pytest.fixture
def temp_capture_db():
    """Create a temporary SQLite database with capture tables and row_factory."""
    db_path = Path(tempfile.mkdtemp()) / "capture_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingress_ledger (
            event_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            update_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            route_mode TEXT NOT NULL,
            sink TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            received_at TEXT NOT NULL,
            message_thread_id INTEGER,
            media_kind TEXT,
            command_text TEXT,
            content_preview TEXT,
            lifecycle TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capture_payload (
            event_id TEXT PRIMARY KEY REFERENCES ingress_ledger(event_id),
            payload BLOB NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    yield conn
    conn.close()


def make_text_update(update_id, chat_id, text, user_id=98765, thread_id=None):
    """Build a minimal PTB-like Update dict for a text message."""
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "date": 1722900000,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }
    if thread_id is not None:
        update["message"]["message_thread_id"] = thread_id
        update["message"]["is_topic_message"] = True
    return update


# --- Task 3: atomic ledger + payload storage ---

def test_atomic_insert_creates_both_rows(temp_capture_db):
    """Ledger row and payload must be inserted in one transaction."""
    conn = temp_capture_db
    event_id = build_event_id("default", 123456789, 1001)
    update_dict = {
        "update_id": 1001,
        "message": {
            "message_id": 55,
            "chat": {"id": -1001111222},
            "from": {"id": 98765, "is_bot": False},
            "text": "hello world",
        },
    }
    payload_bytes = canonicalize_payload(update_dict)
    p_hash = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    assert p_hash == payload_hash(update_dict)

    atomic_insert_capture(
        conn,
        event_id=event_id,
        profile="default",
        account_id=123456789,
        update_id=1001,
        chat_id=-1001111222,
        message_id=55,
        sender_id=98765,
        event_type="text",
        route_mode="capture_only",
        sink="inbox",
        payload=payload_bytes,
        payload_hash_str=p_hash,
    )

    row = conn.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    assert row["payload_hash"] == p_hash
    assert row["route_mode"] == "capture_only"

    prow = conn.execute(
        "SELECT * FROM capture_payload WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert prow is not None
    assert prow["payload"] == payload_bytes


# --- Task 4: idempotency, conflict, first-capture preservation ---

def test_atomic_insert_idempotent(temp_capture_db):
    """Second insert with same event_id must not create duplicate row."""
    conn = temp_capture_db
    event_id = build_event_id("default", 1, 2001)
    p = b'{"test":true}'

    for _ in range(2):
        atomic_insert_capture(
            conn, event_id=event_id, profile="default", account_id=1,
            update_id=2001, chat_id=1, message_id=1, sender_id=1,
            event_type="text", route_mode="capture_only", sink="inbox",
            payload=p, payload_hash_str="sha256:abc",
        )

    count = conn.execute(
        "SELECT COUNT(*) FROM ingress_ledger WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    assert count == 1


def test_atomic_insert_rejects_conflicting_payload(temp_capture_db):
    """Same event_id + different payload_hash must raise IntegrityError."""
    conn = temp_capture_db
    event_id = build_event_id("default", 1, 3001)

    atomic_insert_capture(
        conn, event_id=event_id, profile="default", account_id=1,
        update_id=3001, chat_id=1, message_id=1, sender_id=1,
        event_type="text", route_mode="capture_only", sink="inbox",
        payload=b'{"v":1}', payload_hash_str="sha256:aaa",
    )

    with pytest.raises(sqlite3.IntegrityError):
        atomic_insert_capture(
            conn, event_id=event_id, profile="default", account_id=1,
            update_id=3001, chat_id=1, message_id=1, sender_id=1,
            event_type="text", route_mode="capture_only", sink="inbox",
            payload=b'{"v":2}', payload_hash_str="sha256:bbb",
        )

    # No overwrite: original hash and payload survive
    row = conn.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row["payload_hash"] == "sha256:aaa"
    prow = conn.execute(
        "SELECT * FROM capture_payload WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert prow["payload"] == b'{"v":1}'


def test_atomic_insert_preserves_first_capture_timestamps(temp_capture_db):
    """A repeat insert must not rewrite received_at/recorded_at."""
    conn = temp_capture_db
    event_id = build_event_id("default", 1, 4001)
    kwargs = dict(
        event_id=event_id, profile="default", account_id=1,
        update_id=4001, chat_id=1, message_id=1, sender_id=1,
        event_type="text", route_mode="capture_only", sink="inbox",
        payload=b'{"v":1}', payload_hash_str="sha256:aaa",
    )

    atomic_insert_capture(conn, **kwargs)
    first = conn.execute(
        "SELECT received_at, recorded_at FROM ingress_ledger WHERE event_id = ?",
        (event_id,),
    ).fetchone()

    atomic_insert_capture(conn, **kwargs)
    second = conn.execute(
        "SELECT received_at, recorded_at FROM ingress_ledger WHERE event_id = ?",
        (event_id,),
    ).fetchone()

    assert (first["received_at"], first["recorded_at"]) == (
        second["received_at"],
        second["recorded_at"],
    )


# --- Task 5: CaptureIngressQueue basic interception ---

@pytest.mark.asyncio
async def test_capture_queue_intercepts_text_and_denies_delegate(temp_capture_db):
    """Text on capture-only route: persisted, not delegated to PTB dispatcher."""
    inner_queue = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}

    queue = CaptureIngressQueue(
        inner_queue=inner_queue,
        db_connection=temp_capture_db,
        profile="default",
        account_id=123456789,
        route_map=route_map,
    )

    update = make_text_update(1, -1001111222, "hello capture")
    await queue.put(update)

    # CaptureIngressQueue IS the queue PTB reads from — must be empty for capture_only
    assert queue.empty()

    event_id = build_event_id("default", 123456789, 1)
    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "text"
    assert row["content_preview"] == "hello capture"

    # Companion payload written in the same transaction
    prow = temp_capture_db.execute(
        "SELECT * FROM capture_payload WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert prow["payload"] == canonicalize_payload(update)


# --- Task 6: command interception ---

@pytest.mark.asyncio
async def test_capture_queue_intercepts_command_as_inert_text(temp_capture_db):
    """/command on capture-only route: captured as event_type=command, never dispatched."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = make_text_update(2, -1001111222, "/start")
    await queue.put(update)

    assert queue.empty()
    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?",
        (build_event_id("default", 123456789, 2),),
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "command"
    assert row["command_text"] == "/start"


# --- Task 7: media and location ---

@pytest.mark.asyncio
async def test_capture_queue_media_no_dispatch(temp_capture_db):
    """Photo on capture-only route: event_type=media, no media download, no dispatch."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = {
        "update_id": 3,
        "message": {
            "message_id": 103,
            "date": 1722900000,
            "chat": {"id": -1001111222, "type": "supergroup"},
            "from": {"id": 98765, "is_bot": False},
            "photo": [
                {"file_id": "abc123", "file_unique_id": "uniq1", "width": 100, "height": 100}
            ],
            "caption": "check this out",
        },
    }
    await queue.put(update)
    assert queue.empty()

    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?",
        (build_event_id("default", 123456789, 3),),
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "media"
    assert row["command_text"] is None


@pytest.mark.asyncio
async def test_capture_queue_location_no_dispatch(temp_capture_db):
    """Location on capture-only route: event_type=location, no dispatch."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = {
        "update_id": 4,
        "message": {
            "message_id": 104,
            "date": 1722900000,
            "chat": {"id": -1001111222, "type": "supergroup"},
            "from": {"id": 98765, "is_bot": False},
            "location": {"latitude": 51.5074, "longitude": -0.1278},
        },
    }
    await queue.put(update)
    assert queue.empty()

    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?",
        (build_event_id("default", 123456789, 4),),
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "location"


# --- Task 8: pass-through, agent delegation, drop route ---

@pytest.mark.asyncio
async def test_non_message_like_update_passes_through(temp_capture_db):
    """Callback query passes through to PTB queue unchanged, no capture."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = {
        "update_id": 5,
        "callback_query": {"id": "cb1", "from": {"id": 98765}, "data": "ping"},
    }
    await queue.put(update)
    # Pass-through via super().put() — item is in the CaptureIngressQueue
    assert not queue.empty()
    passed = queue.get_nowait()
    assert passed == update

    rows = temp_capture_db.execute("SELECT COUNT(*) FROM ingress_ledger").fetchone()[0]
    assert rows == 0


@pytest.mark.asyncio
async def test_agent_route_persists_then_delegates(temp_capture_db):
    """Agent route: persist first, then delegate to PTB queue via super().put()."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "agent", "sink": "general"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = make_text_update(6, -1001111222, "hello agent")
    await queue.put(update)

    assert not queue.empty()
    delegated = queue.get_nowait()
    assert delegated == update

    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?",
        (build_event_id("default", 123456789, 6),),
    ).fetchone()
    assert row is not None
    assert row["route_mode"] == "agent"


@pytest.mark.asyncio
async def test_drop_route_no_capture_unchanged_dispatch(temp_capture_db):
    """Drop route: no ledger row, pass-through to PTB queue unchanged."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "drop", "sink": ""}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = make_text_update(7, -1001111222, "should drop")
    await queue.put(update)

    assert not queue.empty()
    got = queue.get_nowait()
    assert got == update
    rows = temp_capture_db.execute("SELECT COUNT(*) FROM ingress_ledger").fetchone()[0]
    assert rows == 0


# --- Task 9: duplicate, conflict, route matching, sentinels ---

@pytest.mark.asyncio
async def test_identical_duplicate_no_second_row(temp_capture_db):
    """Same (profile, account_id, update_id): no second row, first timestamps preserved."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = make_text_update(8, -1001111222, "first capture")
    await queue.put(update)
    first = temp_capture_db.execute(
        "SELECT received_at FROM ingress_ledger WHERE update_id = 8"
    ).fetchone()["received_at"]

    await queue.put(update)  # Duplicate

    count = temp_capture_db.execute(
        "SELECT COUNT(*) FROM ingress_ledger WHERE update_id = 8 AND account_id = 123456789"
    ).fetchone()[0]
    assert count == 1
    still = temp_capture_db.execute(
        "SELECT received_at FROM ingress_ledger WHERE update_id = 8"
    ).fetchone()["received_at"]
    assert still == first
    assert queue.empty()


@pytest.mark.asyncio
async def test_same_identity_different_payload_fails_closed(temp_capture_db):
    """Same event_id, different payload: no overwrite, no delegation."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 0): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update1 = make_text_update(9, -1001111222, "original")
    await queue.put(update1)

    update2 = make_text_update(9, -1001111222, "tampered")  # Same update_id!
    await queue.put(update2)

    event_id = build_event_id("default", 123456789, 9)
    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    # Must still hold the original content, not the conflicting replay
    assert row["payload_hash"] == payload_hash(update1)
    prow = temp_capture_db.execute(
        "SELECT payload FROM capture_payload WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert prow["payload"] == canonicalize_payload(update1)
    # Queue must NOT have the conflicting update
    assert queue.empty()


@pytest.mark.asyncio
async def test_exact_route_matching_requires_chat_and_thread(temp_capture_db):
    """Same thread_id in a different chat does not match."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 10): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    update = make_text_update(10, -1009999999, "wrong chat", thread_id=10)
    await queue.put(update)

    # No matching route → must pass through
    assert not queue.empty()
    got = queue.get_nowait()
    assert got == update
    rows = temp_capture_db.execute("SELECT COUNT(*) FROM ingress_ledger").fetchone()[0]
    assert rows == 0


@pytest.mark.asyncio
async def test_thread_scoped_route_captures_matching_thread(temp_capture_db):
    """A thread-scoped route matches only its own (chat_id, thread_id) pair."""
    inner = asyncio.Queue()
    route_map = {(-1001111222, 10): {"mode": "capture_only", "sink": "inbox"}}
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, route_map)

    await queue.put(make_text_update(11, -1001111222, "right thread", thread_id=10))
    assert queue.empty()

    row = temp_capture_db.execute(
        "SELECT * FROM ingress_ledger WHERE event_id = ?",
        (build_event_id("default", 123456789, 11),),
    ).fetchone()
    assert row is not None
    assert row["message_thread_id"] == 10

    # Same chat, different thread → no route, passes through
    await queue.put(make_text_update(12, -1001111222, "other thread", thread_id=99))
    assert not queue.empty()
    got = queue.get_nowait()
    assert got["message"]["message_thread_id"] == 99
    rows = temp_capture_db.execute("SELECT COUNT(*) FROM ingress_ledger").fetchone()[0]
    assert rows == 1


@pytest.mark.asyncio
async def test_queue_sentinel_passes_through(temp_capture_db):
    """Non-dict sentinel objects pass through unchanged."""
    inner = asyncio.Queue()
    queue = CaptureIngressQueue(inner, temp_capture_db, "default", 123456789, {})

    sentinel = object()
    await queue.put(sentinel)
    assert not queue.empty()
    assert queue.get_nowait() is sentinel
