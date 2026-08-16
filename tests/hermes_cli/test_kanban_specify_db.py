"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def test_specify_promotes_triage_to_todo(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kb.connect() as conn:
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Refined: rough idea",
            body="**Goal**\nDo the thing.",
            author="specifier-bot",
        )
    assert ok is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kb.connect() as conn, pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kb.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        kb.specify_triage_task(
            conn, tid1, title="A-spec", body="b", author="ace"
        )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kb.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []




def test_specify_event_captures_prior_title_and_body(kanban_home):
    """A specify pass overwrites title/body in place; the 'specified'
    event must carry the exact prior values so the human's original ask
    stays recoverable."""
    with kb.connect() as conn:
        tid = _create_triage(
            conn, title="human's original ask", body="what I actually wanted"
        )
        kb.specify_triage_task(
            conn,
            tid,
            title="Refined: machine title",
            body="machine body",
            author="specifier-bot",
        )
        events = [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
    assert len(events) == 1
    payload = events[0].payload
    assert set(payload["changed_fields"]) == {"title", "body"}
    assert payload["prior"] == {
        "title": "human's original ask",
        "body": "what I actually wanted",
    }


def test_specify_twice_leaves_both_prior_versions_recoverable(kanban_home):
    """The re-promotion path can specify the same card repeatedly. Every
    pass must leave its own prior snapshot, so replaying the event log
    reconstructs the full chain back to the human's text."""
    with kb.connect() as conn:
        tid = _create_triage(conn, title="v0 title", body="v0 body")
        kb.specify_triage_task(conn, tid, title="v1 title", body="v1 body")
        # Simulate a re-promotion back into triage (what the loop did).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        kb.specify_triage_task(conn, tid, title="v2 title", body="v2 body")
        events = [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
    assert [e.payload["prior"] for e in events] == [
        {"title": "v0 title", "body": "v0 body"},
        {"title": "v1 title", "body": "v1 body"},
    ]


def test_specify_prior_omits_unchanged_fields(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="same title", body="old body")
        kb.specify_triage_task(conn, tid, title="same title", body="new body")
        events = [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
    assert events[0].payload["changed_fields"] == ["body"]
    assert events[0].payload["prior"] == {"body": "old body"}
