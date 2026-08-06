"""Unit tests for the standalone telegram_outbox module (2026-07-22).

See tests/tools/test_send_message_telegram_outbox.py for the end-to-end
wiring tests (via send_message_tool's Telegram send path). These tests
exercise telegram_outbox.py in isolation — no gateway config, no mocked
Telegram bot, just the append/mark_sent/drain bookkeeping on disk.
"""

from __future__ import annotations

import json

import pytest

from tools import telegram_outbox as ob


@pytest.fixture(autouse=True)
def _isolated_outbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


def test_append_and_pending():
    eid = ob.outbox_append("12345", "hello world")
    assert eid is not None, "append should return an id"
    pending = ob.outbox_pending_entries()
    assert len(pending) == 1
    assert pending[0]["message"] == "hello world"


def test_mark_sent_removes_from_pending():
    eid = ob.outbox_append("12345", "will be sent")
    ob.outbox_mark_sent(eid)
    assert ob.outbox_pending_entries() == []


def test_drain_delivers_pending_and_compacts():
    ob.outbox_append("111", "msg one")
    ob.outbox_append("222", "msg two")
    delivered = []

    def fake_send(chat_id, message, thread_id):
        delivered.append((chat_id, message))
        return True  # pretend Telegram accepted it

    summary = ob.outbox_drain(send_fn=fake_send, grace_seconds=0)
    assert summary == {"attempted": 2, "sent": 2, "dropped_stale": 0,
                       "still_pending": 0, "ambiguous_replayed": 0}
    assert len(delivered) == 2
    assert ob.outbox_pending_entries() == []


def test_drain_leaves_failed_sends_pending():
    ob.outbox_append("333", "will fail to send")

    def fake_send_fail(chat_id, message, thread_id):
        return False  # simulate Telegram API down

    summary = ob.outbox_drain(send_fn=fake_send_fail, grace_seconds=0)
    assert summary["attempted"] == 1
    assert summary["sent"] == 0
    assert summary["still_pending"] == 1
    pending = ob.outbox_pending_entries()
    assert len(pending) == 1
    assert pending[0]["message"] == "will fail to send"


def test_drain_drops_stale_entries_without_attempting_resend():
    ob.outbox_append("444", "ancient message")
    # Backdate created_at to simulate an entry far older than max_age_seconds.
    path = ob._outbox_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["created_at"] = 0  # epoch — infinitely old
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    called = []

    def fake_send(chat_id, message, thread_id):
        called.append(1)
        return True

    summary = ob.outbox_drain(send_fn=fake_send, max_age_seconds=3600, grace_seconds=0)
    assert summary["dropped_stale"] == 1
    assert summary["attempted"] == 0
    assert called == [], "stale entry must not be attempted"


def test_pending_entries_skip_corrupt_lines():
    path = ob._outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json at all\n"
        '{"id": "abc", "status": "pending", "chat_id": "1", "message": "ok", "created_at": 0}\n'
        "\n",
        encoding="utf-8",
    )
    pending = ob.outbox_pending_entries()
    assert len(pending) == 1
    assert pending[0]["id"] == "abc"


def test_append_never_raises_even_if_state_dir_uncreatable(tmp_path, monkeypatch):
    # Point HERMES_HOME at a dir where "state" already exists as a FILE, so
    # state_dir.mkdir() inside _outbox_path() raises FileExistsError.
    blocker = tmp_path / "state"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    eid = ob.outbox_append("1", "test")
    assert eid is None, "append should fail gracefully (return None), not raise"


def test_thread_id_round_trips_through_pending_entries():
    eid = ob.outbox_append("999", "topic message", thread_id="42")
    pending = ob.outbox_pending_entries()
    assert pending[0]["thread_id"] == "42"
    assert pending[0]["id"] == eid


def test_ambiguous_entry_is_replayed_with_marker(tmp_path, monkeypatch):
    """Crash between Bot-API acceptance and tombstone must NOT silently duplicate.

    Review #74085: current main labels that state (delivery_ledger's
    RECOVERED_MARKER) rather than resending blind. This asserts the outbox
    honours the same contract — and that a *clean* pending entry is not
    labeled (a marker on every replay would be crying wolf).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import telegram_outbox as tox

    clean_id = tox.outbox_append(111, "clean pending")
    amb_id = tox.outbox_append(222, "ambiguous send")
    tox.outbox_mark_attempting(amb_id)          # crashed right after this

    seen = {}

    def fake_send(chat_id, message, thread_id, ambiguous=False):
        seen[chat_id] = ambiguous
        return True

    summary = tox.outbox_drain(send_fn=fake_send, grace_seconds=0)
    assert summary["attempted"] == 2
    assert summary["ambiguous_replayed"] == 1, summary
    assert seen[222] is True, "attempting-without-tombstone must be labeled"
    assert seen[111] is False, "clean pending must NOT be labeled"


def test_legacy_send_fn_without_ambiguous_still_works(tmp_path, monkeypatch):
    """Signature detection happens once; a 3-arg send_fn must not be called twice.

    Probing with TypeError at call time double-sent whenever send_fn itself
    raised TypeError internally.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import telegram_outbox as tox

    tox.outbox_append(333, "legacy path")
    calls = []

    def legacy_send(chat_id, message, thread_id):
        calls.append(chat_id)
        return True

    summary = tox.outbox_drain(send_fn=legacy_send, grace_seconds=0)
    assert calls == [333], f"sent {len(calls)}x, expected exactly once"
    assert summary["sent"] == 1


def test_ambiguity_survives_compaction(tmp_path, monkeypatch):
    """An unresolved 'attempting' entry must stay labeled after a compaction pass.

    Otherwise the next drain sees a clean pending and silently duplicates.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import telegram_outbox as tox

    amb_id = tox.outbox_append(444, "stays ambiguous")
    tox.outbox_mark_attempting(amb_id)

    # First drain: send fails, so the entry survives compaction still-pending.
    tox.outbox_drain(send_fn=lambda c, m, t, ambiguous=False: False, grace_seconds=0)

    seen = {}

    def fake_send(chat_id, message, thread_id, ambiguous=False):
        seen[chat_id] = ambiguous
        return True

    summary = tox.outbox_drain(send_fn=fake_send, grace_seconds=0)
    assert seen.get(444) is True, "ambiguity was lost across compaction"
    assert summary["ambiguous_replayed"] == 1
