"""Regression tests for runtime spool-on-drop of the pending transcript queue.

When the per-session pending cap (``_MAX_PENDING_PER_SESSION``) forces the
gateway to evict the oldest queued transcript message during live operation,
the message must be spooled to the on-disk pending spool (the same machinery
``flush_pending_to_file`` uses at shutdown) and replayed on the next
successful transcript flush — not silently discarded (#78182, #82616).
"""
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import shutdown_flush
from gateway.session import SessionStore
from hermes_state import CompressionSessionClosedError, SessionDB


def _make_store(db):
    store = object.__new__(SessionStore)
    store._db = db
    store._transcript_retry_lock = threading.Lock()
    store._dirty_transcripts = {}
    store._transcript_append_failures = {}
    store._transcript_reroutes = {}
    store._fts_rebuild_attempted = True
    setattr(store, "_entries", {"route": SimpleNamespace(session_id="root")})
    setattr(store, "_lock", threading.RLock())
    store._save = lambda: None
    return store


class BrokenThenHealedDb:
    """append_message fails while ``broken`` is True, then records rows."""

    def __init__(self):
        self.broken = True
        self.rows = []

    def append_message(self, **kwargs):
        if self.broken:
            raise RuntimeError("db unavailable")
        self.rows.append(kwargs)


class CompressionRerouteDb:
    """First parent write is transient; later parent writes require reroute."""

    def __init__(self, replay_failures=0):
        self.parent_attempts = 0
        self.replay_failures = replay_failures
        self.rows = []

    def append_message(self, **kwargs):
        if kwargs["session_id"] == "root":
            self.parent_attempts += 1
            if self.parent_attempts == 1:
                raise RuntimeError("db unavailable before compression")
            raise CompressionSessionClosedError("root")
        if kwargs.get("content") == "older" and self.replay_failures:
            self.replay_failures -= 1
            raise RuntimeError("spool replay still unavailable")
        self.rows.append(kwargs)

    def find_live_compression_child(self, session_id):
        if session_id == "root":
            return {"id": "child", "parent_session_id": "root"}
        assert session_id == "child"
        return None


class StrictLegacyAppendDb:
    """Pre-lineage ``append_message`` override with no extra keyword slot."""

    def __init__(self):
        self.rows = []

    def append_message(
        self,
        session_id,
        role,
        content=None,
        tool_name=None,
        tool_calls=None,
        tool_call_id=None,
        token_count=None,
        finish_reason=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
        codex_reasoning_items=None,
        codex_message_items=None,
        platform_message_id=None,
        observed=False,
        effect_disposition=None,
        timestamp=None,
        api_content=None,
        display_kind=None,
        display_metadata=None,
        compression_lock_holder=None,
    ):
        self.rows.append({"session_id": session_id, "role": role, "content": content})


@pytest.fixture()
def spool_home(tmp_path, monkeypatch):
    """Point the pending spool at an isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: tmp_path, raising=True
    )
    return tmp_path


def _spool_files(home):
    d = home / "pending_messages"
    return sorted(d.glob("pending-*.json")) if d.exists() else []


class TestSpoolOnDrop:
    def test_non_rerouted_append_preserves_legacy_override_signature(self):
        db = StrictLegacyAppendDb()
        store = _make_store(db)

        store.append_to_transcript(
            "live", {"role": "assistant", "content": "ordinary"}
        )

        assert db.rows == [
            {"session_id": "live", "role": "assistant", "content": "ordinary"}
        ]

    def test_rerouted_append_preserves_nondurable_strict_override(self):
        store = _make_store(SimpleNamespace())
        rows = []

        def append(session_id, message):
            rows.append((session_id, message["content"]))

        store.__dict__["_append_transcript_message"] = append
        store._append_rerouted_transcript_message(
            "child",
            {"role": "assistant", "content": "routed"},
            source_session_id="root",
        )

        assert rows == [("child", "routed")]

    def test_rerouted_append_requires_lineage_support_for_durable_db(self):
        class DurableDb:
            def get_session(self, session_id):
                return {"id": session_id}

        store = _make_store(DurableDb())
        rows = []

        def append(session_id, message):
            rows.append((session_id, message["content"]))

        store.__dict__["_append_transcript_message"] = append
        with pytest.raises(
            RuntimeError,
            match="override lacks compression_lineage_root",
        ):
            store._append_rerouted_transcript_message(
                "child",
                {"role": "assistant", "content": "blocked"},
                source_session_id="root",
            )

        assert rows == []

    def test_drop_spool_drain_roundtrip(self, spool_home, caplog, monkeypatch):
        # Small cap so the test stays fast.
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 5)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        n_extra = 3
        with caplog.at_level(logging.WARNING, logger="gateway.session"):
            for i in range(SessionStore._MAX_PENDING_PER_SESSION + n_extra):
                store.append_to_transcript(
                    "sess-1", {"role": "user", "content": f"msg{i}"}
                )

        # The oldest n_extra messages were evicted — and spooled, not lost.
        files = _spool_files(spool_home)
        assert len(files) == n_extra
        payloads = [json.loads(p.read_text()) for p in files]
        assert all(
            p["reason"] == shutdown_flush.TRANSCRIPT_CAP_DROP_REASON
            for p in payloads
        )
        spooled_contents = sorted(
            p["data"]["message"]["content"] for p in payloads
        )
        assert spooled_contents == ["msg0", "msg1", "msg2"]

        # Drop log escalated to WARNING and includes the spool path.
        drop_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "spooled oldest message" in r.getMessage()
        ]
        assert len(drop_warnings) == n_extra
        assert str(spool_home / "pending_messages") in drop_warnings[0].getMessage()

        # DB heals; the next successful flush drains the backlog AND
        # replays the spooled messages in drop order.
        db.broken = False
        store.append_to_transcript(
            "sess-1", {"role": "assistant", "content": "recovered"}
        )

        contents = [r["content"] for r in db.rows]
        # All surviving in-memory messages plus the recovery trigger...
        for i in range(n_extra, SessionStore._MAX_PENDING_PER_SESSION + n_extra):
            assert f"msg{i}" in contents
        assert "recovered" in contents
        # ...and the previously dropped messages, replayed in drop order.
        replayed = [c for c in contents if c in ("msg0", "msg1", "msg2")]
        assert replayed == ["msg0", "msg1", "msg2"]
        # Spool files consumed after successful replay.
        assert _spool_files(spool_home) == []
        # Nothing pending in memory.
        assert "sess-1" not in store._dirty_transcripts

    def test_drain_only_touches_own_session(self, spool_home, monkeypatch):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 3)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        for i in range(SessionStore._MAX_PENDING_PER_SESSION + 1):
            store.append_to_transcript("sess-a", {"role": "user", "content": f"a{i}"})
            store.append_to_transcript("sess-b", {"role": "user", "content": f"b{i}"})

        assert len(_spool_files(spool_home)) == 2  # one drop per session

        db.broken = False
        store.append_to_transcript("sess-a", {"role": "user", "content": "go-a"})

        # Only sess-a's spooled drop was replayed; sess-b's remains on disk.
        remaining = [
            json.loads(p.read_text()) for p in _spool_files(spool_home)
        ]
        assert len(remaining) == 1
        assert remaining[0]["session_key"] == "sess-b"
        a_rows = [r["content"] for r in db.rows if r["session_id"] == "sess-a"]
        assert "a0" in a_rows

    def test_spool_replays_before_surviving_in_memory_queue(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 3)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        for i in range(5):
            store.append_to_transcript(
                "ordered", {"role": "user", "content": f"m{i}"}
            )
        assert len(_spool_files(spool_home)) == 2

        db.broken = False
        store.append_to_transcript(
            "ordered", {"role": "assistant", "content": "m5"}
        )

        assert [row["content"] for row in db.rows] == [
            "m0",
            "m1",
            "m2",
            "m3",
            "m4",
            "m5",
        ]
        assert _spool_files(spool_home) == []

    def test_spool_failure_degrades_to_plain_drop(
        self, spool_home, caplog, monkeypatch
    ):
        """If the spool cannot be written, behave exactly like the old
        drop-oldest path: cap enforced, WARNING logged, no crash."""
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 4)

        def _boom():
            raise OSError("disk full")

        monkeypatch.setattr(shutdown_flush, "_get_flush_dir", _boom)

        db = BrokenThenHealedDb()
        store = _make_store(db)

        with caplog.at_level(logging.WARNING, logger="gateway.session"):
            for i in range(SessionStore._MAX_PENDING_PER_SESSION + 5):
                store.append_to_transcript(
                    "sess-x", {"role": "user", "content": f"msg{i}"}
                )

        pending = store._dirty_transcripts.get("sess-x", [])
        assert len(pending) <= SessionStore._MAX_PENDING_PER_SESSION
        assert _spool_files(spool_home) == []
        degraded = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "on-disk spool unavailable" in r.getMessage()
        ]
        assert len(degraded) == 5
        # No spool bookkeeping means recovery must not attempt a drain.
        db.broken = False
        store.append_to_transcript("sess-x", {"role": "user", "content": "fin"})
        assert [r["content"] for r in db.rows][-1] == "fin"

    def test_replay_failure_keeps_spool_files(self, spool_home, monkeypatch):
        """A failed replay must preserve the spool files for a later retry."""
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 3)
        db = BrokenThenHealedDb()
        store = _make_store(db)

        for i in range(SessionStore._MAX_PENDING_PER_SESSION + 2):
            store.append_to_transcript("sess-r", {"role": "user", "content": f"m{i}"})
        assert len(_spool_files(spool_home)) == 2

        # DB heals only for live writes; replayed (spooled) rows still fail.
        class FlakyDb(BrokenThenHealedDb):
            def append_message(self, **kwargs):
                if kwargs["content"] in ("m0", "m1", "m2"):
                    raise RuntimeError("still broken for replays")
                self.rows.append(kwargs)

        flaky = FlakyDb()
        flaky.broken = False
        store._db = flaky
        # This append pushes pending over the cap again (dropping/spooling
        # m2) before the successful flush triggers the drain.
        store.append_to_transcript("sess-r", {"role": "user", "content": "go"})

        # Spool files survive the failed replay for the next attempt.
        assert len(_spool_files(spool_home)) == 3
        assert "sess-r" in getattr(store, "_spooled_drop_sessions", set())
        assert flaky.rows == []


class TestCompressionRerouteSpool:
    def test_runtime_merges_all_source_spools_by_global_creation_order(
        self, spool_home
    ):
        db = BrokenThenHealedDb()
        db.broken = False
        store = _make_store(db)
        shutdown_flush.spool_dropped_transcript_message(
            "child", {"role": "assistant", "content": "older-child"}
        )
        shutdown_flush.spool_dropped_transcript_message(
            "root", {"role": "assistant", "content": "newer-root"}
        )
        store._spooled_drop_sessions = {"root", "child"}
        store._transcript_reroutes = {"root": "child", "child": "tip"}

        assert store._drain_spooled_drops_for_target("tip") is True
        assert [row["content"] for row in db.rows] == [
            "older-child",
            "newer-root",
        ]
        assert _spool_files(spool_home) == []

    def test_runtime_discovers_older_ancestor_outside_local_reroute_map(
        self, spool_home
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="parent",
                source="telegram",
                messages=[{"role": "user", "content": "first summary"}],
                require_compression_lease=False,
            )
            db.publish_compression_child(
                parent_session_id="parent",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "second summary"}],
                require_compression_lease=False,
            )
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "older-root"}
            )
            shutdown_flush.spool_dropped_transcript_message(
                "parent", {"role": "assistant", "content": "newer-parent"}
            )

            store = _make_store(db)
            # Model another process having published the root spool. This
            # process only observed the immediate parent -> tip reroute.
            store._spooled_drop_sessions = {"parent"}
            store._transcript_reroutes = {"parent": "tip"}

            assert store._drain_spooled_drops_for_target("tip") is True
            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["second summary", "older-root", "newer-parent"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_runtime_empty_local_hints_drains_stale_root_before_younger_reroute(
        self, spool_home
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "older"}
            )
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "summary"}],
                require_compression_lease=False,
            )

            store = _make_store(db)
            store._spooled_drop_sessions = set()
            store._transcript_reroutes = {}
            store.append_to_transcript(
                "root", {"role": "assistant", "content": "younger"}
            )

            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["summary", "older", "younger"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_runtime_revalidates_target_when_tip_rotates_during_spool_scan(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "older"}
            )
            real_drain = shutdown_flush.drain_transcript_spools
            rotated = False

            def rotate_then_drain(*args, **kwargs):
                nonlocal rotated
                if not rotated:
                    rotated = True
                    db.publish_compression_child(
                        parent_session_id="root",
                        child_session_id="tip",
                        source="telegram",
                        messages=[{"role": "user", "content": "summary"}],
                        require_compression_lease=False,
                    )
                return real_drain(*args, **kwargs)

            monkeypatch.setattr(
                shutdown_flush,
                "drain_transcript_spools",
                rotate_then_drain,
            )
            store = _make_store(db)
            store._spooled_drop_sessions = set()
            store._transcript_reroutes = {}
            store.append_to_transcript(
                "root", {"role": "assistant", "content": "younger"}
            )

            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["summary", "older", "younger"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_real_db_runtime_reroute_replays_root_spool_into_tip(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 1)
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            store = _make_store(db)
            real_append = store._append_transcript_message
            fail_once = True

            def _transient_then_real(session_id, message, **kwargs):
                nonlocal fail_once
                if fail_once:
                    fail_once = False
                    raise RuntimeError("transient writer outage")
                return real_append(session_id, message, **kwargs)

            store._append_transcript_message = _transient_then_real
            store.append_to_transcript(
                "root", {"role": "user", "content": "older"}
            )
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="child",
                source="telegram",
                messages=[{"role": "user", "content": "first summary"}],
                require_compression_lease=False,
            )
            db.publish_compression_child(
                parent_session_id="child",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "second summary"}],
                require_compression_lease=False,
            )

            store.append_to_transcript(
                "root", {"role": "assistant", "content": "newer"}
            )

            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["second summary", "older", "newer"]
            assert _spool_files(spool_home) == []
            assert store._entries["route"].session_id == "tip"
        finally:
            db.close()

    def test_runtime_reroute_replays_parent_spool_into_child(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 1)
        db = CompressionRerouteDb()
        store = _make_store(db)

        store.append_to_transcript(
            "root", {"role": "user", "content": "older"}
        )
        store.append_to_transcript(
            "root", {"role": "assistant", "content": "newer"}
        )

        assert [row["session_id"] for row in db.rows] == ["child", "child"]
        assert [row["content"] for row in db.rows] == ["older", "newer"]
        assert _spool_files(spool_home) == []
        assert "root" not in getattr(store, "_spooled_drop_sessions", set())

    def test_failed_parent_spool_replay_retries_on_future_child_append(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 1)
        db = CompressionRerouteDb(replay_failures=1)
        store = _make_store(db)

        store.append_to_transcript(
            "root", {"role": "user", "content": "older"}
        )
        store.append_to_transcript(
            "root", {"role": "assistant", "content": "newer"}
        )
        assert len(_spool_files(spool_home)) == 1

        store.append_to_transcript(
            "child", {"role": "assistant", "content": "future"}
        )

        assert [row["content"] for row in db.rows] == [
            "older",
            "newer",
            "future",
        ]
        assert _spool_files(spool_home) == []

    def test_failed_spool_replay_follows_tip_rotation_before_younger_rows(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(SessionStore, "_MAX_PENDING_PER_SESSION", 1)
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            store = _make_store(db)
            real_append = store._append_transcript_message
            older_failures = 2

            def _fail_initial_and_first_replay(session_id, message, **kwargs):
                nonlocal older_failures
                if message.get("content") == "older" and older_failures:
                    older_failures -= 1
                    raise RuntimeError("older row not durable yet")
                return real_append(session_id, message, **kwargs)

            store._append_transcript_message = _fail_initial_and_first_replay
            store.append_to_transcript(
                "root", {"role": "user", "content": "older"}
            )
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="child",
                source="telegram",
                messages=[{"role": "user", "content": "first summary"}],
                require_compression_lease=False,
            )
            store.append_to_transcript(
                "root", {"role": "assistant", "content": "newer"}
            )
            assert len(_spool_files(spool_home)) == 1

            db.publish_compression_child(
                parent_session_id="child",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "second summary"}],
                require_compression_lease=False,
            )
            store.append_to_transcript(
                "root", {"role": "assistant", "content": "future"}
            )

            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["second summary", "older", "newer", "future"]
            assert _spool_files(spool_home) == []
            assert store._entries["route"].session_id == "tip"
        finally:
            db.close()

    def test_startup_recovery_follows_unique_compression_lineage(
        self, spool_home
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="child",
                source="telegram",
                messages=[{"role": "user", "content": "first summary"}],
                require_compression_lease=False,
            )
            db.publish_compression_child(
                parent_session_id="child",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "second summary"}],
                require_compression_lease=False,
            )
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "older"}
            )

            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["second summary", "older"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_startup_recovery_preserves_transcript_spool_sequence(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "summary"}],
                require_compression_lease=False,
            )
            # Filename order is intentionally different from creation/seq order.
            file_ids = iter(["f" * 32, "0" * 32, "8" * 32])
            monkeypatch.setattr(
                shutdown_flush.uuid,
                "uuid4",
                lambda: SimpleNamespace(hex=next(file_ids)),
            )
            for i in range(3):
                shutdown_flush.spool_dropped_transcript_message(
                    "root", {"role": "assistant", "content": f"older-{i}"}
                )

            assert shutdown_flush.recover_pending_to_db(db) == 3
            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["summary", "older-0", "older-1", "older-2"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_startup_recovery_stops_after_oldest_transcript_failure(
        self, spool_home
    ):
        class FailsOldestDb:
            def __init__(self):
                self.rows = []

            def append_message(self, **kwargs):
                if kwargs.get("content") == "older":
                    raise RuntimeError("oldest row still unavailable")
                self.rows.append(kwargs)

        db = FailsOldestDb()
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "older"}
        )
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 2

    def test_startup_transcript_failure_blocks_newer_ordinary_payload(
        self, spool_home
    ):
        class FailsTranscriptDb:
            def __init__(self):
                self.rows = []

            def append_message(self, **kwargs):
                if kwargs.get("content") == "older":
                    raise RuntimeError("oldest transcript row still unavailable")
                self.rows.append(kwargs)

        db = FailsTranscriptDb()
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "older"}
        )
        shutdown_flush.flush_pending_to_file(
            {"route": {"text": "future", "session_id": "live"}}
        )

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 2

    def test_startup_equal_legacy_order_is_ambiguous_and_fails_closed(
        self, spool_home
    ):
        flush_dir = spool_home / "pending_messages"
        flush_dir.mkdir()
        for name, content in (("0", "newer"), ("f", "older")):
            (flush_dir / f"pending-{name * 32}.json").write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 100,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {"role": "assistant", "content": content},
                        },
                    }
                ),
                encoding="utf-8",
            )
        db = BrokenThenHealedDb()
        db.broken = False

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 2

    def test_startup_different_legacy_times_are_ambiguous_across_restarts(
        self, spool_home
    ):
        flush_dir = spool_home / "pending_messages"
        flush_dir.mkdir()
        for name, ts, content in (
            ("f", 200, "older"),
            ("0", 100, "newer-after-clock-rollback"),
        ):
            (flush_dir / f"pending-{name * 32}.json").write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": ts,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {"role": "assistant", "content": content},
                        },
                    }
                ),
                encoding="utf-8",
            )

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert db.get_messages_as_conversation("live") == []
            assert len(_spool_files(spool_home)) == 2
        finally:
            db.close()

    def test_startup_unknown_lineage_target_blocks_younger_sessions(
        self, spool_home
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            db.end_session("root", "compression")
            db.create_session(
                "child-a", source="telegram", parent_session_id="root"
            )
            db.create_session(
                "child-b", source="telegram", parent_session_id="root"
            )
            db.create_session("independent", source="telegram")
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "unresolved-older"}
            )
            shutdown_flush.flush_pending_to_file(
                {
                    "independent-route": {
                        "text": "younger-independent",
                        "session_id": "independent",
                    }
                }
            )

            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert db.get_messages_as_conversation("child-a") == []
            assert db.get_messages_as_conversation("child-b") == []
            assert db.get_messages_as_conversation("independent") == []
            assert len(_spool_files(spool_home)) == 2
        finally:
            db.close()

    def test_startup_route_only_malformed_payload_blocks_all_younger_rows(
        self, spool_home
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        shutdown_flush._write_payload(
            flush_dir,
            {
                "session_key": "route-only",
                "reason": "shutdown",
                "ts": 1,
                "data": {},
            },
        )
        shutdown_flush.flush_pending_to_file(
            {"other-route": {"text": "younger", "session_id": "live"}}
        )

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert db.get_messages_as_conversation("live") == []
            assert len(_spool_files(spool_home)) == 2
        finally:
            db.close()

    def test_startup_failure_does_not_block_independent_session(
        self, spool_home
    ):
        class FailsSessionA:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                if kwargs["session_id"] == "session-a":
                    raise RuntimeError("session A unavailable")
                self.rows.append(kwargs)

        db = FailsSessionA()
        shutdown_flush.spool_dropped_transcript_message(
            "session-a", {"role": "assistant", "content": "older-a"}
        )
        shutdown_flush.flush_pending_to_file(
            {"route-b": {"text": "newer-b", "session_id": "session-b"}}
        )

        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["newer-b"]
        remaining = [json.loads(path.read_text()) for path in _spool_files(spool_home)]
        assert len(remaining) == 1
        assert remaining[0]["session_key"] == "session-a"

    def test_startup_duplicate_current_spool_id_fails_closed(
        self, spool_home
    ):
        pending_dir = spool_home / "pending_messages"
        pending_dir.mkdir(parents=True, exist_ok=True)
        for index, content in enumerate(("first", "second"), start=1):
            payload = {
                "session_key": "live",
                "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                "ts": 100 + index,
                "seq": index,
                "_spool": {
                    "id": "duplicate-id",
                    "created_ns": 100 + index,
                    "order": 100 + index,
                    "writer_id": "writer",
                    "writer_seq": index,
                },
                "data": {
                    "session_id": "live",
                    "message": {"role": "assistant", "content": content},
                },
            }
            (pending_dir / f"pending-{index}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert db.get_messages_as_conversation("live") == []
            assert len(_spool_files(spool_home)) == 2
        finally:
            db.close()

    def test_publisher_does_not_overwrite_when_uuid_repeats(
        self, spool_home, monkeypatch
    ):
        class RepeatedUuid:
            hex = "a" * 32

        monkeypatch.setattr(shutdown_flush.uuid, "uuid4", lambda: RepeatedUuid())

        first_path = shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "older"}
        )
        second_path = shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )

        assert first_path is not None
        assert second_path is not None
        assert first_path != second_path
        payloads = sorted(
            (json.loads(path.read_text()) for path in _spool_files(spool_home)),
            key=lambda payload: payload["_spool"]["order"],
        )
        assert [payload["data"]["message"]["content"] for payload in payloads] == [
            "older",
            "newer",
        ]

    def test_durable_order_survives_restart_with_backward_wall_clock(
        self, spool_home, monkeypatch
    ):
        monkeypatch.setattr(shutdown_flush.time, "time_ns", lambda: 200)
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "older"}
        )

        # Simulate a fresh process whose wall clock moved backward.
        monkeypatch.setattr(shutdown_flush, "_PENDING_SPOOL_LAST_CREATED_NS", 0)
        monkeypatch.setattr(shutdown_flush, "_PENDING_SPOOL_SEQ", itertools.count())
        monkeypatch.setattr(shutdown_flush, "_PENDING_SPOOL_WRITER_ID", "new-writer")
        monkeypatch.setattr(shutdown_flush.time, "time_ns", lambda: 100)
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            assert shutdown_flush.recover_pending_to_db(db) == 2
            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["older", "newer"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_startup_new_spool_requires_idempotent_override_support(
        self, spool_home
    ):
        db = StrictLegacyAppendDb()
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "once"}
        )

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 1

    def test_startup_unlink_failure_retries_without_duplicate_row(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            spool_path = shutdown_flush.spool_dropped_transcript_message(
                "live", {"role": "assistant", "content": "once"}
            )
            assert spool_path is not None
            original_unlink = Path.unlink
            failed = False

            def fail_spool_unlink_once(path, *args, **kwargs):
                nonlocal failed
                if path == spool_path and not failed:
                    failed = True
                    raise OSError("injected unlink failure")
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_spool_unlink_once)
            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert spool_path.exists()
            monkeypatch.setattr(Path, "unlink", original_unlink)

            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["once"]
            assert not spool_path.exists()
        finally:
            db.close()

    def test_startup_unlink_retry_survives_an_additional_tip_rotation(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            spool_path = shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "once"}
            )
            assert spool_path is not None
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="child",
                source="telegram",
                messages=[{"role": "user", "content": "first summary"}],
                require_compression_lease=False,
            )
            original_unlink = Path.unlink
            failed = False

            def fail_spool_unlink_once(path, *args, **kwargs):
                nonlocal failed
                if path == spool_path and not failed:
                    failed = True
                    raise OSError("injected unlink failure before next rotation")
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_spool_unlink_once)
            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert spool_path.exists()
            monkeypatch.setattr(Path, "unlink", original_unlink)

            db.publish_compression_child(
                parent_session_id="child",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "second summary"}],
                require_compression_lease=False,
            )
            assert shutdown_flush.recover_pending_to_db(db) == 1

            assert [
                row["content"] for row in db.get_messages_as_conversation("child")
            ] == ["first summary", "once"]
            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["second summary"]
            assert not spool_path.exists()
        finally:
            db.close()

    def test_startup_legacy_unlink_failure_remains_retryable(
        self, spool_home, monkeypatch
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'a' * 32}.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "session_key": "live",
                    "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                    "ts": 1,
                    "seq": 0,
                    "data": {
                        "session_id": "live",
                        "message": {"role": "assistant", "content": "once"},
                    },
                }
            ),
            encoding="utf-8",
        )
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            original_unlink = Path.unlink
            failed = False

            def fail_one_payload_unlink(path, *args, **kwargs):
                nonlocal failed
                if path.suffix == ".json" and not failed:
                    failed = True
                    raise OSError("injected legacy cleanup failure")
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_one_payload_unlink)
            assert shutdown_flush.recover_pending_to_db(db) == 1
            monkeypatch.setattr(Path, "unlink", original_unlink)
            shutdown_flush.recover_pending_to_db(db)

            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["once"]
            assert not legacy_path.exists()
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_startup_legacy_commit_marker_failure_retries_exactly_once(
        self, spool_home, monkeypatch
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'8' * 32}.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "session_key": "live",
                    "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                    "ts": 1,
                    "seq": 0,
                    "data": {
                        "session_id": "live",
                        "message": {"role": "assistant", "content": "once"},
                    },
                }
            ),
            encoding="utf-8",
        )
        real_write_state = shutdown_flush._write_pending_spool_format_state
        failed = False

        def fail_first_committed_state(flush_dir_arg, state):
            nonlocal failed
            records = state.get("legacy_files", {})
            if not failed and any(
                record.get("state") == "committed"
                for record in records.values()
                if isinstance(record, dict)
            ):
                failed = True
                raise OSError("injected committed-marker failure")
            return real_write_state(flush_dir_arg, state)

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            monkeypatch.setattr(
                shutdown_flush,
                "_write_pending_spool_format_state",
                fail_first_committed_state,
            )
            assert shutdown_flush.recover_pending_to_db(db) == 1
            monkeypatch.setattr(
                shutdown_flush,
                "_write_pending_spool_format_state",
                real_write_state,
            )
            assert shutdown_flush.recover_pending_to_db(db) == 1

            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["once"]
            assert _spool_files(spool_home) == []
            assert not legacy_path.exists()
        finally:
            db.close()

    def test_startup_legacy_migration_requires_idempotent_override_support(
        self, spool_home
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'9' * 32}.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "session_key": "live",
                    "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                    "ts": 1,
                    "seq": 0,
                    "data": {
                        "session_id": "live",
                        "message": {"role": "assistant", "content": "once"},
                    },
                }
            ),
            encoding="utf-8",
        )
        db = StrictLegacyAppendDb()

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert not legacy_path.exists()
        receipts = [
            path
            for path in flush_dir.iterdir()
            if path.name.startswith(".legacy-receipt-")
        ]
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text())["data"]["message"][
            "content"
        ] == "once"
        assert len(_spool_files(spool_home)) == 1

    def test_startup_ordinary_payload_reroutes_from_closed_root(self, spool_home):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            shutdown_flush.flush_pending_to_file(
                {"route": {"text": "older", "session_id": "root"}}
            )
            db.publish_compression_child(
                parent_session_id="root",
                child_session_id="tip",
                source="telegram",
                messages=[{"role": "user", "content": "summary"}],
                require_compression_lease=False,
            )

            assert shutdown_flush.recover_pending_to_db(db) == 1
            assert [
                row["content"] for row in db.get_messages_as_conversation("tip")
            ] == ["summary", "older"]
            assert _spool_files(spool_home) == []
        finally:
            db.close()

    def test_startup_recovery_keeps_spool_when_lineage_is_ambiguous(
        self, spool_home
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("root", source="telegram")
            db.end_session("root", "compression")
            db.create_session(
                "child-a", source="telegram", parent_session_id="root"
            )
            db.create_session(
                "child-b", source="telegram", parent_session_id="root"
            )
            shutdown_flush.spool_dropped_transcript_message(
                "root", {"role": "assistant", "content": "older"}
            )

            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert len(_spool_files(spool_home)) == 1
            assert db.get_messages_as_conversation("child-a") == []
            assert db.get_messages_as_conversation("child-b") == []
        finally:
            db.close()


class TestSpoolPrimitives:
    def test_drain_skips_other_reasons(self, spool_home):
        # A shutdown-format flush file must not be consumed by the drain.
        shutdown_flush.flush_pending_to_file({"key1": "hello"}, reason="shutdown")
        assert len(_spool_files(spool_home)) == 1
        replayed, remaining = shutdown_flush.drain_transcript_spool(
            "key1", lambda _message, _replay_key: None
        )
        assert replayed == 0
        assert len(_spool_files(spool_home)) == 1

    def test_roundtrip_order(self, spool_home):
        for i in range(3):
            shutdown_flush.spool_dropped_transcript_message(
                "s", {"role": "user", "content": f"c{i}"}
            )
        seen = []
        replayed, remaining = shutdown_flush.drain_transcript_spool(
            "s", lambda message, _replay_key: seen.append(message["content"])
        )
        assert replayed == 3
        assert remaining == 0
        assert seen == ["c0", "c1", "c2"]
        assert _spool_files(spool_home) == []

    def test_runtime_drain_stops_before_older_ordinary_payload(
        self, spool_home
    ):
        shutdown_flush.flush_pending_to_file(
            {"route": {"text": "older", "session_id": "live"}}
        )
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )
        db = BrokenThenHealedDb()
        db.broken = False
        store = _make_store(db)
        store._spooled_drop_sessions = {"live"}

        assert store._drain_spooled_drops_for_target("live") is False
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 2

    @pytest.mark.skipif(os.name != "posix", reason="POSIX flock regression")
    def test_spool_lock_serializes_processes(self, spool_home):
        flush_dir = shutdown_flush._get_flush_dir()
        acquired_marker = spool_home / "child-acquired"
        child_code = """
import os
from pathlib import Path
from gateway import shutdown_flush

with shutdown_flush._pending_spool_lock(shutdown_flush._get_flush_dir()):
    Path(os.environ["SPOOL_LOCK_MARKER"]).write_text("acquired", encoding="utf-8")
"""
        env = os.environ.copy()
        env["SPOOL_LOCK_MARKER"] = str(acquired_marker)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(Path.cwd()), env.get("PYTHONPATH", "")) if part
        )

        with shutdown_flush._pending_spool_lock(flush_dir):
            process = subprocess.Popen(
                [sys.executable, "-c", child_code],
                cwd=Path.cwd(),
                env=env,
            )
            time.sleep(0.25)
            assert process.poll() is None
            assert not acquired_marker.exists()

        assert process.wait(timeout=5) == 0
        assert acquired_marker.read_text(encoding="utf-8") == "acquired"

    def test_nested_replay_callback_can_publish_without_deadlock(self, spool_home):
        child_home = spool_home / "nested-child"
        child_code = """
from gateway import shutdown_flush

shutdown_flush.spool_dropped_transcript_message(
    "live", {"role": "assistant", "content": "older"}
)
seen = []

def replay_and_spool(message, _replay_key):
    seen.append(message["content"])
    shutdown_flush.spool_dropped_transcript_message(
        "live", {"role": "assistant", "content": "newer"}
    )

assert shutdown_flush.drain_transcript_spool("live", replay_and_spool) == (1, 0)
assert shutdown_flush.drain_transcript_spool(
    "live", lambda message, _replay_key: seen.append(message["content"])
) == (1, 0)
assert seen == ["older", "newer"]
print("nested-replay=PASS")
"""
        env = os.environ.copy()
        env["HERMES_HOME"] = str(child_home)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(Path.cwd()), env.get("PYTHONPATH", "")) if part
        )
        result = subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=Path.cwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "nested-replay=PASS" in result.stdout

    def test_visible_legacy_publication_blocks_rolling_restart_replay(
        self, spool_home
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_id = "f" * 32
        legacy_payload = {
            "session_key": "live",
            "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
            "ts": 1,
            "seq": 0,
            "data": {
                "session_id": "live",
                "message": {"role": "assistant", "content": "older"},
            },
        }
        legacy_temp = flush_dir / f".pending-{legacy_id}_stalled.tmp"
        legacy_temp.write_text(json.dumps(legacy_payload), encoding="utf-8")
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )

        seen = []
        replay = lambda message, _replay_key: seen.append(message["content"])
        assert shutdown_flush.drain_transcript_spool("live", replay) == (0, 1)
        assert seen == []

        legacy_temp.replace(flush_dir / f"pending-{legacy_id}.json")
        assert shutdown_flush.drain_transcript_spool("live", replay) == (0, 1)
        assert seen == []
        assert len(_spool_files(spool_home)) == 2

    def test_late_legacy_publication_after_scan_fails_closed(self, spool_home):
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "newer"}
        )
        flush_dir = shutdown_flush._get_flush_dir()
        published_late = False

        class PublishesLegacyDuringAppend:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                nonlocal published_late
                self.rows.append(kwargs)
                if not published_late:
                    published_late = True
                    legacy_payload = {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": "older-legacy",
                            },
                        },
                    }
                    (flush_dir / f"pending-{'e' * 32}.json").write_text(
                        json.dumps(legacy_payload), encoding="utf-8"
                    )

        db = PublishesLegacyDuringAppend()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["newer"]

        # The late payload has no cross-generation ordering proof. Preserve it
        # rather than appending it after the already durable current-format row.
        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert [row["content"] for row in db.rows] == ["newer"]
        assert len(_spool_files(spool_home)) == 1

    def test_consumed_legacy_allowance_rejects_reused_filename(self, spool_home):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'d' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        class RecordingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        write_legacy("allowed-before-cutover")
        db = RecordingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["allowed-before-cutover"]

        write_legacy("late-reuse")
        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert [row["content"] for row in db.rows] == ["allowed-before-cutover"]
        assert legacy_path.exists()

    def test_late_legacy_barrier_still_allows_younger_durable_publication(
        self, spool_home
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'7' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        class RecordingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        write_legacy("pre-cutover")
        db = RecordingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        write_legacy("late-reuse")

        younger_path = shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "younger"}
        )

        assert younger_path is not None and younger_path.exists()
        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert [row["content"] for row in db.rows] == ["pre-cutover"]
        assert len(_spool_files(spool_home)) == 2

    def test_deleted_cutover_marker_does_not_reauthorize_legacy_filename(
        self, spool_home
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'c' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            write_legacy("pre-cutover")
            assert shutdown_flush.recover_pending_to_db(db) == 1
            shutdown_flush.spool_dropped_transcript_message(
                "live", {"role": "assistant", "content": "current"}
            )
            assert shutdown_flush.recover_pending_to_db(db) == 1

            write_legacy("late-reused")
            (flush_dir / shutdown_flush._PENDING_SPOOL_FORMAT_STATE_NAME).unlink()

            assert shutdown_flush.recover_pending_to_db(db) == 0
            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["pre-cutover", "current"]
            assert legacy_path.exists()
        finally:
            db.close()

    def test_legacy_path_replacement_during_append_is_preserved(self, spool_home):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'b' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        class ReplacingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)
                write_legacy("late-replacement")

        write_legacy("older")
        db = ReplacingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["older"]
        assert legacy_path.exists()

        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert [row["content"] for row in db.rows] == ["older"]
        assert legacy_path.exists()

    def test_late_legacy_during_replay_blocks_younger_current_row(self, spool_home):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'6' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        write_legacy("older")
        younger_path = shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "younger"}
        )
        assert younger_path is not None

        class ReplacingDb:
            def __init__(self):
                self.rows = []
                self.replaced = False

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)
                if not self.replaced:
                    self.replaced = True
                    write_legacy("late-replacement")

        db = ReplacingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["older"]
        assert legacy_path.exists()
        assert younger_path.exists()

    def test_legacy_replacement_between_snapshot_and_copy_preserves_snapshot(
        self, spool_home, monkeypatch
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'9' * 32}.json"

        def write_legacy(content):
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        real_write_state = shutdown_flush._write_pending_spool_format_state
        replaced = False

        def replace_after_marker(flush_dir_arg, state):
            nonlocal replaced
            real_write_state(flush_dir_arg, state)
            if not replaced:
                replaced = True
                write_legacy("late-replacement")

        class RecordingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        write_legacy("older-snapshot")
        monkeypatch.setattr(
            shutdown_flush,
            "_write_pending_spool_format_state",
            replace_after_marker,
        )
        db = RecordingDb()

        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["older-snapshot"]
        preserved_payloads = []
        for path in flush_dir.iterdir():
            try:
                preserved_payloads.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        assert any(
            ((payload.get("data") or {}).get("message") or {}).get("content")
            == "late-replacement"
            for payload in preserved_payloads
        )

    def test_identical_legacy_replacement_between_copy_and_quarantine_is_preserved(
        self, spool_home, monkeypatch
    ):
        flush_dir = shutdown_flush._get_flush_dir()
        legacy_path = flush_dir / f"pending-{'8' * 32}.json"

        def write_legacy():
            legacy_path.write_text(
                json.dumps(
                    {
                        "session_key": "live",
                        "reason": shutdown_flush.TRANSCRIPT_CAP_DROP_REASON,
                        "ts": 1,
                        "seq": 0,
                        "data": {
                            "session_id": "live",
                            "message": {
                                "role": "assistant",
                                "content": "same-content",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        real_write_state = shutdown_flush._write_pending_spool_format_state
        replaced = False

        def replace_after_marker(flush_path, state):
            nonlocal replaced
            real_write_state(flush_path, state)
            if not replaced and any(
                record.get("state") == "pending"
                for record in state.get("legacy_files", {}).values()
            ):
                replaced = True
                write_legacy()

        write_legacy()
        monkeypatch.setattr(
            shutdown_flush,
            "_write_pending_spool_format_state",
            replace_after_marker,
        )

        class RecordingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        db = RecordingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 1
        assert [row["content"] for row in db.rows] == ["same-content"]
        preserved_sources = [
            path
            for path in flush_dir.iterdir()
            if path == legacy_path or path.name.startswith(".legacy-receipt-")
        ]
        assert preserved_sources
        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert [row["content"] for row in db.rows] == ["same-content"]

    def test_corrupt_format_cutover_state_fails_closed(self, spool_home):
        shutdown_flush.spool_dropped_transcript_message(
            "live", {"role": "assistant", "content": "preserve-me"}
        )
        format_state = (
            shutdown_flush._get_flush_dir()
            / shutdown_flush._PENDING_SPOOL_FORMAT_STATE_NAME
        )
        format_state.write_text("{corrupt", encoding="utf-8")

        class RecordingDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        db = RecordingDb()
        assert shutdown_flush.recover_pending_to_db(db) == 0
        assert db.rows == []
        assert len(_spool_files(spool_home)) == 1

    def test_publication_and_replay_are_serialized_across_writers(
        self, spool_home, monkeypatch
    ):
        from utils import atomic_json_write as real_atomic_json_write

        older_entered = threading.Event()
        release_older = threading.Event()
        newer_published = threading.Event()
        drain_done = threading.Event()

        def controlled_atomic_json_write(path, payload, **kwargs):
            message = ((payload.get("data") or {}).get("message") or {})
            content = message.get("content")
            if content == "older":
                older_entered.set()
                assert release_older.wait(timeout=5)
            result = real_atomic_json_write(path, payload, **kwargs)
            if content == "newer":
                newer_published.set()
            return result

        monkeypatch.setattr("utils.atomic_json_write", controlled_atomic_json_write)
        class IdempotentFakeDb:
            def __init__(self):
                self.rows = []

            def append_message(self, idempotency_key=None, **kwargs):
                self.rows.append(kwargs)

        db = IdempotentFakeDb()

        older_writer = threading.Thread(
            target=shutdown_flush.spool_dropped_transcript_message,
            args=("live", {"role": "assistant", "content": "older"}),
        )
        newer_writer = threading.Thread(
            target=shutdown_flush.spool_dropped_transcript_message,
            args=("live", {"role": "assistant", "content": "newer"}),
        )

        def drain_after_newer_publication():
            assert newer_published.wait(timeout=5)
            shutdown_flush.recover_pending_to_db(db)
            drain_done.set()

        drainer = threading.Thread(target=drain_after_newer_publication)
        older_writer.start()
        assert older_entered.wait(timeout=5)
        newer_writer.start()
        drainer.start()

        # The old implementation lets the newer writer and drainer pass the
        # stalled older publication. The fixed implementation keeps both
        # behind the shared spool lock until the older file is visible.
        drain_done.wait(timeout=0.25)
        release_older.set()
        older_writer.join(timeout=5)
        newer_writer.join(timeout=5)
        drainer.join(timeout=5)
        assert not older_writer.is_alive()
        assert not newer_writer.is_alive()
        assert not drainer.is_alive()

        shutdown_flush.recover_pending_to_db(db)
        assert [row["content"] for row in db.rows] == ["older", "newer"]
        assert _spool_files(spool_home) == []

    def test_direct_drain_unlink_failure_uses_replay_key_exactly_once(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            spool_path = shutdown_flush.spool_dropped_transcript_message(
                "live", {"role": "assistant", "content": "once"}
            )
            assert spool_path is not None
            original_unlink = Path.unlink
            failed = False

            def fail_spool_unlink_once(path, *args, **kwargs):
                nonlocal failed
                if path == spool_path and not failed:
                    failed = True
                    raise OSError("injected unlink failure")
                return original_unlink(path, *args, **kwargs)

            def replay(message, replay_key):
                db.append_message(
                    session_id="live",
                    role=message["role"],
                    content=message["content"],
                    idempotency_key=replay_key,
                )

            monkeypatch.setattr(Path, "unlink", fail_spool_unlink_once)
            assert shutdown_flush.drain_transcript_spool("live", replay) == (0, 1)
            monkeypatch.setattr(Path, "unlink", original_unlink)
            assert shutdown_flush.drain_transcript_spool("live", replay) == (1, 0)

            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["once"]
            live_session = db.get_session("live")
            assert live_session is not None
            assert live_session["message_count"] == 1
        finally:
            db.close()

    def test_append_idempotency_key_is_bound_to_session_and_payload(self, spool_home):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("session-a", source="telegram")
            db.create_session("session-b", source="telegram")
            first_id = db.append_message(
                "session-a",
                "assistant",
                "first",
                idempotency_key="shared-replay-id",
            )
            assert db.append_message(
                "session-a",
                "assistant",
                "first",
                idempotency_key="shared-replay-id",
            ) == first_id

            with pytest.raises(RuntimeError, match="idempotency.*conflict"):
                db.append_message(
                    "session-a",
                    "assistant",
                    "changed",
                    idempotency_key="shared-replay-id",
                )
            with pytest.raises(RuntimeError, match="idempotency.*conflict"):
                db.append_message(
                    "session-b",
                    "assistant",
                    "first",
                    idempotency_key="shared-replay-id",
                )

            assert [
                row["content"] for row in db.get_messages_as_conversation("session-a")
            ] == ["first"]
            assert db.get_messages_as_conversation("session-b") == []
        finally:
            db.close()

    def test_runtime_unlink_failure_retries_without_duplicate_row(
        self, spool_home, monkeypatch
    ):
        db = SessionDB(db_path=spool_home / "state.db")
        try:
            db.create_session("live", source="telegram")
            store = _make_store(db)
            spool_path = shutdown_flush.spool_dropped_transcript_message(
                "live", {"role": "assistant", "content": "once"}
            )
            assert spool_path is not None
            store._spooled_drop_sessions = {"live"}
            original_unlink = Path.unlink
            failed = False

            def fail_spool_unlink_once(path, *args, **kwargs):
                nonlocal failed
                if path == spool_path and not failed:
                    failed = True
                    raise OSError("injected unlink failure")
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_spool_unlink_once)
            assert store._drain_spooled_drops_for_target("live") is False
            assert spool_path.exists()
            monkeypatch.setattr(Path, "unlink", original_unlink)

            assert store._drain_spooled_drops_for_target("live") is True
            assert [
                row["content"] for row in db.get_messages_as_conversation("live")
            ] == ["once"]
            assert not spool_path.exists()
        finally:
            db.close()
