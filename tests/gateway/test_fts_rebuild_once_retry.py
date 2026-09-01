"""Gateway FTS rebuild must not burn its one-shot on a deferred attempt (#100108)."""

import threading

from gateway.session import SessionStore


class TestRebuildFtsOnceRetry:
    def test_foreign_holder_skip_does_not_consume_the_attempt(self):
        class FakeDb:
            def __init__(self):
                self.holders = [(4242, "/tmp/state.db")]
                self.rebuilds = 0
                self._fts_stale = False

            def _foreign_state_db_holders(self):
                return list(self.holders)

            def rebuild_fts(self, *, timeout_seconds=None):
                self.rebuilds += 1
                return 1

        store = object.__new__(SessionStore)
        store._db = FakeDb()
        store._fts_rebuild_attempted = False
        store._fts_rebuild_retry_after = 0.0
        store._fts_stale_retry_thread = None
        store._fts_stale_retry_stop = threading.Event()

        assert store._rebuild_fts_once() is False
        assert store._fts_rebuild_attempted is False
        assert store._db.rebuilds == 0

        store._db.holders = []
        store._fts_rebuild_retry_after = 0.0
        assert store._rebuild_fts_once() is True
        assert store._db.rebuilds == 1
        assert store._fts_rebuild_attempted is True

    def test_admission_timeout_does_not_consume_the_attempt(self):
        class FakeDb:
            def __init__(self):
                self._fts_stale = False

            def _foreign_state_db_holders(self):
                return []

            def rebuild_fts(self, *, timeout_seconds=None):
                return 0

        store = object.__new__(SessionStore)
        store._db = FakeDb()
        store._fts_rebuild_attempted = False
        store._fts_rebuild_retry_after = 0.0
        store._fts_stale_retry_thread = None
        store._fts_stale_retry_stop = threading.Event()

        assert store._rebuild_fts_once() is False
        assert store._fts_rebuild_attempted is False
        store._fts_rebuild_retry_after = 0.0
        assert store._rebuild_fts_once() is False
        assert store._fts_rebuild_attempted is False
