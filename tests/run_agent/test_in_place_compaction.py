"""Tests for in-place context compaction (config: compression.in_place, #38763).

When ``compression.in_place`` is True, ``compress_context()`` rewrites the
message list and rebuilds the system prompt but keeps the SAME ``session_id``:
no ``end_session``, no ``parent_session_id`` child row, no ``name #N`` title
renumber, no flush-cursor reset. This eliminates the session-rotation bug
cluster (#33618 /goal loss, #14238 lost response, #33907 orphans, #45117 search
gaps, #42228 null cwd). When the flag is False (default), rotation behaves
exactly as before.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_agent(session_db, session_id, *, in_place):
    fake_api_key = "test" + "-key"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": fake_api_key}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key=fake_api_key,
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_in_place = in_place
    # Mock the compressor to return a deterministic shrunk transcript so the
    # test exercises the DB-mutation path, not summarization quality.
    def _fake_compress(
        messages,
        current_tokens=None,
        focus_topic=None,
        force=False,
        external_protected_tail=False,
    ):
        agent._test_external_protected_tail = external_protected_tail
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary of prior turns"},
            {"role": "assistant", "content": "recent reply"},
        ]

    agent.context_compressor.compress = _fake_compress
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor.compression_count = 1
    return agent


def _seed(db, sid, title, n=8):
    db.create_session(sid, "cli", model="test/model")
    db.set_session_title(sid, title)
    for i in range(n):
        db.append_message(
            session_id=sid,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg {i}",
        )


class TestInPlaceCompaction:
    def test_in_place_atomically_commits_manual_protected_tail(self):
        """Partial manual compression must persist its protected tail.

        Regression: the outer manual-compression helper split history into a
        head and tail, then called _compress_context(head). In-place compaction
        archived every active SQLite row and committed only the compressed head;
        rejoining the tail afterward repaired the in-memory list but not the
        durable session. A reload therefore lost exactly the turns selected by
        "Keep recent".
        """
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260810_partial_tail"
            _seed(db, sid, "partial-tail")
            agent = _make_agent(db, sid, in_place=True)
            head = [{"role": "user", "content": f"head {i}"} for i in range(8)]
            protected_tail = [
                {"role": "user", "content": "keep user turn 1"},
                {"role": "assistant", "content": "keep assistant reply 1"},
                {"role": "user", "content": "keep user turn 2"},
                {"role": "assistant", "content": "keep assistant reply 2"},
                {"role": "user", "content": "keep user turn 3"},
                {"role": "assistant", "content": "keep assistant reply 3"},
            ]

            compressed_head, _ = compress_context(
                agent,
                head,
                "sys",
                approx_tokens=100_000,
                protected_tail=protected_tail,
            )

            # Backward-compatible return contract: manual callers still do the
            # in-memory rejoin once after _compress_context returns.
            assert [m["content"] for m in compressed_head] == [
                "[CONTEXT COMPACTION] summary of prior turns",
                "recent reply",
            ]
            # Durability contract: the atomic in-place commit already includes
            # the protected tail, so resume/reload cannot discard it.
            reloaded = db.get_messages_as_conversation(sid)
            reloaded_contents = [m["content"] for m in reloaded]
            active_count = db.get_session(sid)["message_count"]
            # The wrapper may use a pooled worker on Windows. Close the explicit
            # fixture connection before TemporaryDirectory removes the DB file.
            db.close()

            assert reloaded_contents == [
                "[CONTEXT COMPACTION] summary of prior turns",
                "recent reply",
                "keep user turn 1",
                "keep assistant reply 1",
                "keep user turn 2",
                "keep assistant reply 2",
                "keep user turn 3",
                "keep assistant reply 3",
            ]
            assert active_count == 8
            assert agent._test_external_protected_tail is True

    def test_in_place_same_role_seam_keeps_first_protected_message_verbatim(self):
        """A user-role summary must never absorb the first protected user row."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260811_partial_tail_seam"
            _seed(db, sid, "partial-tail-seam")
            agent = _make_agent(db, sid, in_place=True)

            def _user_summary_only(messages, **kwargs):
                agent._test_external_protected_tail = kwargs.get(
                    "external_protected_tail", False
                )
                return [{
                    "role": "user",
                    "content": (
                        "[CONTEXT COMPACTION — REFERENCE ONLY] summary of older head"
                    ),
                    "_context_summary": True,
                }]

            agent.context_compressor.compress = _user_summary_only
            head = [
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
            ]
            protected_tail = [
                {"role": "user", "content": "first protected question", "timestamp": 1_786_000_001.0},
                {"role": "assistant", "content": "first protected answer", "timestamp": 1_786_000_002.0},
                {"role": "user", "content": "second protected question", "timestamp": 1_786_000_003.0},
                {"role": "assistant", "content": "second protected answer", "timestamp": 1_786_000_004.0},
                {"role": "user", "content": "third protected question", "timestamp": 1_786_000_005.0},
                {"role": "assistant", "content": "third protected answer", "timestamp": 1_786_000_006.0},
            ]

            compressed_head, _ = compress_context(
                agent,
                head,
                "sys",
                approx_tokens=100_000,
                protected_tail=protected_tail,
            )
            reloaded = db.get_messages_as_conversation(sid)
            active_count = db.get_session(sid)["message_count"]
            db.close()

            assert len(compressed_head) == 1
            assert compressed_head[0]["content"].startswith("[CONTEXT COMPACTION")
            assert "older question" not in str(compressed_head)
            assert reloaded[0]["content"].startswith("[CONTEXT COMPACTION")
            assert reloaded[1]["role"] == "assistant"
            assert reloaded[1]["display_kind"] == "compression_bridge"
            assert [
                {key: message.get(key) for key in ("role", "content", "timestamp")}
                for message in reloaded[-len(protected_tail):]
            ] == [
                {key: message.get(key) for key in ("role", "content", "timestamp")}
                for message in protected_tail
            ]
            assert active_count == 2 + len(protected_tail)
            assert agent._test_external_protected_tail is True

    def test_in_place_keeps_same_session_id(self):
        """In-place mode: id unchanged, no child row, no rename, history kept."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_120000_aaaaaa"
            _seed(db, sid, "my-research")
            agent = _make_agent(db, sid, in_place=True)
            agent._last_flushed_db_idx = 5

            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compressed, _sp = compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )

            # Identity never moved.
            assert agent.session_id == sid
            # No continuation row forked.
            child = db._conn.execute(
                "SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)
            ).fetchall()
            assert child == []
            # Session not ended; title untouched (no "#2").
            row = db.get_session(sid)
            assert row["end_reason"] is None
            assert row["title"] == "my-research"
            # DURABLE, NON-DESTRUCTIVE compaction (the core invariant, per
            # Teknium's review): the LIVE context is the compacted set, but the
            # pre-compaction turns are PRESERVED on disk (active=0), not deleted
            # — searchable + recoverable under the SAME id. A resume reloads the
            # compacted set so compaction actually shrinks the live session and
            # doesn't immediately re-compact (#38763).
            reloaded = db.get_messages_as_conversation(sid)
            assert len(reloaded) == 2
            assert [m.get("content") for m in reloaded] == [
                "[CONTEXT COMPACTION] summary of prior turns",
                "recent reply",
            ]
            assert row["message_count"] == 2  # live (active) count
            # NON-DESTRUCTIVE: the 8 seeded originals survive at active=0
            # alongside the 2 compacted rows — nothing was DELETEd.
            all_rows = db.get_messages(sid, include_inactive=True)
            assert len(all_rows) == 10
            archived = [m for m in all_rows if not m.get("active", 1)]
            assert len(archived) == 8
            # The originals remain FTS-searchable (active=0 is a content-
            # preserving UPDATE; the fts triggers don't key on active).
            hit = db._conn.execute(
                "SELECT 1 FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE m.session_id = ? AND messages_fts MATCH 'msg' AND m.active = 0 "
                "LIMIT 1",
                (sid,),
            ).fetchone()
            assert hit is not None
            # Flush identity/cursor reset so next-turn appends diff against the
            # compacted transcript (rebuilds the identity set on next flush).
            assert agent._last_flushed_db_idx == 0
            assert agent._flushed_db_message_ids == set()
            # Rotation-independent in-place signal set for the gateway.
            assert agent._last_compaction_in_place is True
            # Live transcript actually shrank.
            assert len(compressed) == 2

    def test_in_place_alternation_preserved(self):
        """The compacted list must not introduce consecutive same-role messages."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_120500_cccccc"
            _seed(db, sid, "alt")
            agent = _make_agent(db, sid, in_place=True)
            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compressed, _ = compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )
            roles = [m["role"] for m in compressed if m.get("role") != "system"]
            assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


    def test_rotation_still_preflushes(self):
        """Rotation MUST pre-flush so current-turn messages survive in the
        preserved old (parent) session before it is ended (#47202)."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            _seed(db, "rot_flush", "f")
            agent = _make_agent(db, "rot_flush", in_place=False)
            calls = {"n": 0}
            agent._flush_messages_to_session_db = lambda *a, **k: calls.__setitem__(
                "n", calls["n"] + 1
            )
            compress_context(
                agent, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert calls["n"] == 1


class TestRotationFallbackWhenFlagOff:
    def test_rotation_when_flag_off(self):
        """Rotation is now the OPT-OUT fallback (default flipped to in-place in
        #38763). With in_place=False explicitly set, legacy rotation is
        unchanged — forks a renamed continuation session."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_130000_bbbbbb"
            _seed(db, sid, "my-research")
            agent = _make_agent(db, sid, in_place=False)
            agent._last_flushed_db_idx = 5

            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )

            # Identity rotated to a fresh id.
            assert agent.session_id != sid
            # Old session ended via compression; continuation forked and
            # carries the SAME name. Compression is an internal detail — the
            # conversation didn't change topic, so it must not be renumbered
            # into "my-research #2" and shown as a separate piece of work.
            assert db.get_session(sid)["end_reason"] == "compression"
            child = db._conn.execute(
                "SELECT id, title FROM sessions WHERE parent_session_id = ?", (sid,)
            ).fetchall()
            assert len(child) == 1
            assert child[0]["title"] == "my-research"
            # The compacted child is persisted atomically at the rotation
            # boundary, so a headless process killed before finalization can
            # still resume it without duplicating the two handoff messages.
            assert agent._last_flushed_db_idx == 2
            assert [m.get("content") for m in db.get_messages_as_conversation(agent.session_id)] == [
                "[CONTEXT COMPACTION] summary of prior turns",
                "recent reply",
            ]
            # Rotation mode does NOT set the in-place signal.
            assert getattr(agent, "_last_compaction_in_place", False) is False


class TestInPlaceSignalForGateway:
    """compress_context must expose a rotation-independent flag the gateway can
    read (instead of an id-change diff) to re-baseline transcript handling."""

    def test_signal_set_on_in_place_unset_on_rotation(self):
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            # in-place → flag True
            _seed(db, "s_ip", "ip")
            a_ip = _make_agent(db, "s_ip", in_place=True)
            compress_context(
                a_ip, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert a_ip._last_compaction_in_place is True

            # rotation → flag False
            _seed(db, "s_rot", "rot")
            a_rot = _make_agent(db, "s_rot", in_place=False)
            compress_context(
                a_rot, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert a_rot._last_compaction_in_place is False


class TestInPlaceConfigDefault:
    def test_flag_defaults_on(self):
        """In-place is the default as of #38763 (rotation is now opt-out via
        compression.in_place: false)."""
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["compression"].get("in_place") is True


class TestCompactedTurnsStaySearchable:
    """Teknium's review hinges on the pre-compaction transcript staying
    DISCOVERABLE after in-place compaction. Compaction-archived rows
    (active=0, compacted=1) must surface in session_search by default, while
    rewind/undo rows (active=0, compacted=0) must stay hidden. The two share
    the active flag but are distinguished by the compacted flag."""

    def test_compacted_turns_found_by_default_search(self):
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_search"
            db.create_session(sid, "cli", model="test/model")
            for r, c in [
                ("user", "configure the HMAC secret"),
                ("assistant", "set it in config.yaml"),
                ("user", "deploy returns 403"),
                ("assistant", "rotate the HMAC"),
                ("user", "works now"),
                ("assistant", "great"),
            ]:
                db.append_message(session_id=sid, role=r, content=c)

            before = db.search_messages("HMAC", role_filter=["user", "assistant"])
            assert len(before) == 2

            db.archive_and_compact(
                sid,
                [
                    {"role": "user", "content": "[SUMMARY] earlier setup"},
                    {"role": "assistant", "content": "ok"},
                ],
            )

            # The archived originals (active=0, compacted=1) are still found by
            # the DEFAULT search — this is the durability requirement.
            after = db.search_messages("HMAC", role_filter=["user", "assistant"])
            assert {m["id"] for m in after} == {1, 4}
            # Live context still excludes them.
            assert len(db.get_messages_as_conversation(sid)) == 2

    def test_rewound_turns_stay_hidden(self):
        """Rewind/undo (active=0, compacted=0) must NOT leak into default
        search — the distinction the compacted flag preserves."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_undo"
            db.create_session(sid, "cli", model="test/model")
            db.append_message(session_id=sid, role="user", content="ZEBRAWORD remember this")
            db.append_message(session_id=sid, role="assistant", content="noted")
            db.rewind_to_message(sid, db.get_messages(sid)[0]["id"])

            assert db.search_messages("ZEBRAWORD", role_filter=["user", "assistant"]) == []
            recovered = db.search_messages(
                "ZEBRAWORD", role_filter=["user", "assistant"], include_inactive=True
            )
            assert len(recovered) == 1
