"""Regression tests for #99477: session-DB flush dedup guard.

The marker-based persistence guard is defeated when compression assembly
strips _DB_PERSISTED_MARKER from fresh message copies (#57491) and the
queue-drain path preserves an outermost history_offset=0 (#56391) —
together they re-persist the full history on every drain cycle, producing
exponential duplication (3,814 rows observed for a 15-message conversation,
893 copies of one message).

The core loop is fixed on main (1f2bd9e763); these tests pin the
defense-in-depth tail dedup in _flush_messages_to_session_db_unlocked so a
future marker-lifecycle regression cannot corrupt the session store again.

Contract: a flush that presents an already-persisted (role, content) pair
in its batch skips that row instead of re-inserting it.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def temp_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _make_agent_with_db(temp_home):
    """Minimal AIAgent-shaped object with a real SessionDB attached."""
    from hermes_state import SessionDB

    agent = MagicMock()
    agent._persist_disabled = False
    agent.session_id = "sess-dedup-test"
    agent._session_db = SessionDB(temp_home / "state.db")
    agent._session_db_created = True
    agent._ensure_db_session = MagicMock()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._db_flush_scan_prefix = None
    agent._last_persistence_error_cause = None
    # MagicMock children are truthy — getattr(...) defaults would hand the
    # lease/lock kwargs a garbage holder that append_messages_batch then
    # treats as a real (lost) lease. Pin all four to their None defaults.
    agent._active_compression_lock_holder = None
    agent._active_session_turn_lease_holder = None
    agent._active_session_turn_lease_ttl_seconds = None
    agent._pending_cli_user_message = None
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    # Create the session row so get_messages has something to page.
    agent._session_db.create_session(
        session_id="sess-dedup-test",
        source="test",
        model="test-model",
        system_prompt="",
    )
    return agent


class TestFlushDedupGuard:
    def test_already_persisted_pair_is_skipped(self, temp_hermes_home):
        """Re-presenting a persisted (role, content) pair must not re-insert."""
        from run_agent import AIAgent, _DB_PERSISTED_MARKER

        agent = _make_agent_with_db(temp_hermes_home)

        first_messages = [
            {"role": "user", "content": "hello world", "timestamp": time.time()},
            {"role": "assistant", "content": "hi there", "timestamp": time.time()},
        ]
        AIAgent._flush_messages_to_session_db_unlocked(agent, first_messages, None)
        rows = agent._session_db.get_messages("sess-dedup-test")
        assert len(rows) == 2

        # Simulate the #99477 replay: the SAME pairs arrive again in a
        # fresh flush (markers stripped by compression copies).
        replay_messages = [
            {"role": "user", "content": "hello world", "timestamp": time.time()},
            {"role": "assistant", "content": "hi there", "timestamp": time.time()},
            {"role": "user", "content": "brand new message", "timestamp": time.time()},
        ]
        AIAgent._flush_messages_to_session_db_unlocked(agent, replay_messages, None)

        rows = agent._session_db.get_messages("sess-dedup-test")
        contents = [r.get("content") for r in rows]
        # The two replayed pairs are skipped; only the new message lands.
        assert contents.count("hello world") == 1
        assert contents.count("hi there") == 1
        assert contents.count("brand new message") == 1
        assert len(rows) == 3

    def test_new_pairs_all_persist(self, temp_hermes_home):
        """Without pre-existing duplicates every row lands exactly once."""
        from run_agent import AIAgent

        agent = _make_agent_with_db(temp_hermes_home)
        messages = [
            {"role": "user", "content": "one", "timestamp": time.time()},
            {"role": "assistant", "content": "two", "timestamp": time.time()},
            {"role": "user", "content": "three", "timestamp": time.time()},
        ]
        AIAgent._flush_messages_to_session_db_unlocked(agent, messages, None)
        rows = agent._session_db.get_messages("sess-dedup-test")
        assert len(rows) == 3

    def test_exponential_replay_stays_bounded(self, temp_hermes_home):
        """The #99477 shape: many replays of the same tail must not amplify.

        Reproduces the feedback loop at the flush seam — every cycle
        re-presents the full history with markers stripped.  Without the
        dedup guard each cycle would double the row count; with it, the
        store stays at one copy per unique pair.
        """
        from run_agent import AIAgent

        agent = _make_agent_with_db(temp_hermes_home)
        base = [
            {"role": "user", "content": f"msg-{i}", "timestamp": time.time()}
            for i in range(5)
        ]
        # Drain cycle 0: everything is new.
        AIAgent._flush_messages_to_session_db_unlocked(agent, list(base), None)
        # Drain cycles 1..6: full history re-presented with markers stripped
        # (compression copies defeat the marker guard — this is the loop).
        for _ in range(6):
            AIAgent._flush_messages_to_session_db_unlocked(
                agent,
                [dict(m, **{k: v for k, v in m.items() if k != "_DB_PERSISTED_MARKER"})
                 for m in base],
                None,
            )
        rows = agent._session_db.get_messages("sess-dedup-test")
        # Invariant: one row per unique pair — no amplification across cycles.
        assert len(rows) == 5
        contents = [r.get("content") for r in rows]
        assert len(set(contents)) == 5

    def test_tool_rows_not_deduped(self, temp_hermes_home):
        """Tool rows replay legitimately (same content, different call id) —
        the guard only inspects user/assistant string rows."""
        from run_agent import AIAgent

        agent = _make_agent_with_db(temp_hermes_home)
        messages = [
            {"role": "user", "content": "run it", "timestamp": time.time()},
            {
                "role": "tool",
                "content": "result payload",
                "tool_call_id": "call-1",
                "timestamp": time.time(),
            },
            {
                "role": "tool",
                "content": "result payload",
                "tool_call_id": "call-2",
                "timestamp": time.time(),
            },
        ]
        AIAgent._flush_messages_to_session_db_unlocked(agent, messages, None)
        rows = agent._session_db.get_messages("sess-dedup-test")
        tool_rows = [r for r in rows if r.get("role") == "tool"]
        assert len(tool_rows) == 2

    def test_probe_failure_never_blocks_flush(self, temp_hermes_home):
        """A get_messages failure must fall through to a normal write."""
        from run_agent import AIAgent

        agent = _make_agent_with_db(temp_hermes_home)
        messages = [
            {"role": "user", "content": "first", "timestamp": time.time()},
        ]
        AIAgent._flush_messages_to_session_db_unlocked(agent, messages, None)

        # Second flush where the tail probe raises — must still write.
        agent._session_db.get_messages = MagicMock(
            side_effect=RuntimeError("db is locked")
        )
        second = [
            {"role": "user", "content": "second", "timestamp": time.time()},
        ]
        ok = AIAgent._flush_messages_to_session_db_unlocked(agent, second, None)
        assert ok is True
