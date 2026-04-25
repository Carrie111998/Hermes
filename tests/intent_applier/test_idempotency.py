from pathlib import Path

import pytest

from intent_applier.idempotency import IdempotencyTracker


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "applier_state.db"


class TestIdempotencyTracker:
    def test_initially_empty(self, db_path):
        tracker = IdempotencyTracker(db_path)
        assert not tracker.is_applied("k1")

    def test_mark_then_check(self, db_path):
        tracker = IdempotencyTracker(db_path)
        tracker.mark_applied("k1", message_id="m1")
        assert tracker.is_applied("k1")
        assert not tracker.is_applied("k2")

    def test_persistence_across_instances(self, db_path):
        tracker = IdempotencyTracker(db_path)
        tracker.mark_applied("k1", message_id="m1")
        tracker2 = IdempotencyTracker(db_path)
        assert tracker2.is_applied("k1")

    def test_mark_idempotent(self, db_path):
        tracker = IdempotencyTracker(db_path)
        tracker.mark_applied("k1", message_id="m1")
        tracker.mark_applied("k1", message_id="m1")  # second mark must not error
        assert tracker.is_applied("k1")

    def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "events" / "applier_state.db"
        assert not nested.parent.exists()
        IdempotencyTracker(nested)
        assert nested.parent.exists()
        assert nested.exists()

    def test_rehydrate_from_processed_dir(self, tmp_path, db_path):
        """If processed/ contains intent files, rehydrate adds their keys."""
        processed = tmp_path / "processed"
        processed.mkdir()
        import json
        for i in range(3):
            (processed / f"msg-{i}.json").write_text(
                json.dumps({"idempotency_key": f"key-{i}", "message_id": f"m-{i}"}),
                encoding="utf-8",
            )
        tracker = IdempotencyTracker(db_path)
        tracker.rehydrate_from_processed(processed)
        for i in range(3):
            assert tracker.is_applied(f"key-{i}")

    def test_rehydrate_skips_unparseable_files(self, tmp_path, db_path):
        processed = tmp_path / "processed"
        processed.mkdir()
        (processed / "bad.json").write_text("{not json", encoding="utf-8")
        (processed / "good.json").write_text(
            '{"idempotency_key": "k1", "message_id": "m1"}',
            encoding="utf-8",
        )
        tracker = IdempotencyTracker(db_path)
        tracker.rehydrate_from_processed(processed)
        assert tracker.is_applied("k1")

    def test_cross_thread_access_is_safe(self, db_path):
        """Regression: IdempotencyTracker must work when methods are called
        from a thread other than the one that created the connection.

        In production, startup() (main thread) instantiates the tracker;
        the polling thread later calls is_applied/mark_applied. Without
        check_same_thread=False, SQLite raises ProgrammingError.
        """
        import threading
        tracker = IdempotencyTracker(db_path)
        tracker.mark_applied("k1", message_id="m1")  # main thread

        results = []
        errors = []

        def worker():
            try:
                # Mimic what the gateway poll thread does
                results.append(tracker.is_applied("k1"))
                tracker.mark_applied("k2", message_id="m2")
                results.append(tracker.is_applied("k2"))
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert not errors, f"Cross-thread access failed: {errors}"
        assert results == [True, True]
