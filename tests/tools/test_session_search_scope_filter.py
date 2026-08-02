"""Root-cause fix: session_search's discovery/browse shapes default to the
calling session's own thread scope on thread-bearing platforms, so a
progress question asked in one Discord thread can't surface another
thread's unrelated work under the same channel. See
docs/design/thread-scope-isolation.md.
"""
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_thread(db, session_id, *, thread_id, started_at, first_msg, second_msg):
    db.create_session(
        session_id, source="discord", chat_id="channel-99", thread_id=thread_id,
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
        (started_at, f"Thread {thread_id}", session_id),
    )
    db.append_message(session_id, role="user", content=first_msg)
    db.append_message(session_id, role="assistant", content=second_msg)
    db._conn.commit()


@pytest.fixture
def two_threads(db):
    now = int(time.time())
    _seed_thread(
        db, "s_thread_a", thread_id="thread-A", started_at=now - 5000,
        first_msg="investigate the ssl cert bug",
        second_msg="fixed by validating the CA bundle before provider calls",
    )
    _seed_thread(
        db, "s_thread_b", thread_id="thread-B", started_at=now - 1000,
        first_msg="unrelated feature request about ssl cert renewal automation",
        second_msg="shipped the renewal automation script",
    )
    return db


class TestDiscoveryDefaultsToOwnThread:
    def test_progress_query_from_thread_b_does_not_see_thread_a(self, two_threads):
        result = session_search(
            query="ssl cert", db=two_threads, current_session_id="s_thread_b",
        )
        import json
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is True
        session_ids = {r["session_id"] for r in payload["results"]}
        assert "s_thread_a" not in session_ids

    def test_include_unscoped_restores_cross_thread_search(self, two_threads):
        import json
        result = session_search(
            query="ssl cert", db=two_threads, current_session_id="s_thread_b",
            include_unscoped=True,
        )
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is False
        session_ids = {r["session_id"] for r in payload["results"]}
        assert "s_thread_a" in session_ids

    def test_no_current_session_id_is_unscoped(self, two_threads):
        import json
        result = session_search(query="ssl cert", db=two_threads, current_session_id=None)
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is False


class TestBrowseDefaultsToOwnThread:
    def test_browse_from_thread_b_excludes_thread_a(self, two_threads):
        import json
        result = session_search(db=two_threads, current_session_id="s_thread_b")
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is True
        session_ids = {r["session_id"] for r in payload["results"]}
        assert "s_thread_a" not in session_ids

    def test_browse_include_unscoped_sees_all_threads(self, two_threads):
        import json
        result = session_search(
            db=two_threads, current_session_id="s_thread_b", include_unscoped=True,
        )
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is False
        session_ids = {r["session_id"] for r in payload["results"]}
        assert "s_thread_a" in session_ids


class TestThreadlessPlatformsUnaffected:
    def test_cli_session_with_no_thread_id_stays_unscoped(self, db):
        now = int(time.time())
        db.create_session("s_cli_a", source="cli")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
            (now - 5000, "CLI session A", "s_cli_a"),
        )
        db.append_message("s_cli_a", role="user", content="let's talk about modpack building")
        db.append_message("s_cli_a", role="assistant", content="sure, modpack scaffolded")
        db.create_session("s_cli_b", source="cli")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
            (now - 1000, "CLI session B", "s_cli_b"),
        )
        db.append_message("s_cli_b", role="user", content="continue with modpack quests")
        db.append_message("s_cli_b", role="assistant", content="modpack quests added")
        db._conn.commit()

        import json
        result = session_search(query="modpack", db=db, current_session_id="s_cli_b")
        payload = json.loads(result)
        assert payload["scoped_to_current_thread"] is False
        session_ids = {r["session_id"] for r in payload["results"]}
        assert "s_cli_a" in session_ids


class TestSessionIdsForThreadScope:
    def test_matches_only_same_platform_chat_and_thread(self, db):
        db.create_session("s1", source="discord", chat_id="chan-1", thread_id="t-A")
        db.create_session("s2", source="discord", chat_id="chan-1", thread_id="t-B")
        db.create_session("s3", source="discord", chat_id="chan-2", thread_id="t-A")
        db.create_session("s4", source="telegram", chat_id="chan-1", thread_id="t-A")

        ids = db.session_ids_for_thread_scope(source="discord", chat_id="chan-1", thread_id="t-A")
        assert ids == {"s1"}

    def test_empty_chat_id_returns_empty_set(self, db):
        assert db.session_ids_for_thread_scope(source="discord", chat_id="") == set()
