"""Focused tests for the checklist-bearing Kanban task contract."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


VALID_BODY = """## Execution checklist
- [x] Inspect the implementation.

## Closeout criteria
- [x] Attach reproducible evidence.
"""
UNCHECKED_BODY = """## Execution checklist
- [ ] Inspect the implementation.

## Closeout criteria
- [x] Attach reproducible evidence.
"""


def test_create_supplies_both_sections_when_body_is_omitted(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="generated", created_by="cli")
        body = kb.get_task(conn, task_id).body
    assert "## Execution checklist" in body
    assert "## Closeout criteria" in body
    assert "- [ ]" in body


@pytest.mark.parametrize(
    "body, message",
    [
        ("## Execution checklist\n- [ ] one", "missing required checklist section"),
        ("## Execution checklist\n- [ ] one\n\n## Closeout criteria\ntext", "malformed checklist item"),
        ("## Execution checklist\n* [ ] one\n\n## Closeout criteria\n- [ ] two", "malformed checklist item"),
    ],
)
def test_create_rejects_missing_or_malformed_checklists(tmp_path, monkeypatch, body, message):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        with pytest.raises(kb.ChecklistContractError, match=message):
            kb.create_task(conn, title="invalid", body=body, created_by="cli")


def test_completion_rejects_unchecked_or_unsupported_criteria(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="evidence", body=UNCHECKED_BODY, created_by="worker")
        with pytest.raises(kb.ChecklistContractError, match="unchecked"):
            kb.complete_task(conn, task_id, metadata={"checklist_evidence": {"execution": ["x"], "closeout": ["x"]}})

        checked = kb.create_task(conn, title="evidence 2", body=VALID_BODY, created_by="worker")
        with pytest.raises(kb.ChecklistContractError, match="non-empty evidence"):
            kb.complete_task(conn, checked, metadata={"checklist_evidence": {"execution": [], "closeout": []}})


def test_completion_accepts_checked_criteria_with_per_item_evidence(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="accepted", body=VALID_BODY, created_by="worker")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Completed the work and verified the result.",
            metadata={
                "checklist_evidence": {
                    "execution": ["pytest focused contract test passed"],
                    "closeout": ["completion event persisted with evidence"],
                }
            },
        )
        assert kb.get_task(conn, task_id).status == "done"
