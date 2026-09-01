from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from hermes_cli.planning_plane_sync import (
    PLANE_PROJECTION_SCHEMA,
    PROJECTION_STATE_FILE,
    PROPOSED_CHANGES_FILE,
    PlanningSchemaError,
    build_plane_projection,
    read_planning_state,
    sync_planning_to_plane,
)


class FakePlaneClient:
    def __init__(self, projection: Mapping[str, Any] | None = None) -> None:
        self.projection = dict(projection or {}) if projection else None
        self.reads: list[str] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def read_projection(self, project_key: str) -> Mapping[str, Any] | None:
        self.reads.append(project_key)
        return self.projection

    def update_projection(self, project_key: str, projection: Mapping[str, Any]) -> None:
        payload = dict(projection)
        self.updates.append((project_key, payload))
        self.projection = payload


def write_planning_files(
    root: Path,
    *,
    current_phase: str = "phase-1",
    blocker_status: str = "open",
    next_action: str = "Implement the sync canary",
    resume_point: str = "resume from progress checkpoint",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task_plan.md").write_text(
        f"""---
schema: hermes_task_plan.v1
project_key: BOS-42
project_name: BOS Planning Files Canary
status: active
current_phase: {current_phase}
phases:
  - id: phase-1
    title: Foundation
    status: done
  - id: phase-2
    title: Projection
    status: active
evidence_links:
  - file:///receipts/plan.md
---
# Plan
""",
        encoding="utf-8",
    )
    (root / "findings.md").write_text(
        f"""---
schema: hermes_findings.v1
blockers:
  - id: blocker-1
    title: Awaiting Plane project key
    status: {blocker_status}
    owner: human
    evidence:
      - file:///receipts/blocker.md
findings:
  - id: finding-1
    title: Projection should stay compact
    status: open
    severity: medium
    evidence:
      - file:///receipts/finding.md
---
# Findings
""",
        encoding="utf-8",
    )
    (root / "progress.md").write_text(
        f"""---
schema: hermes_progress.v1
next_action: {next_action!r}
resume_point: {resume_point!r}
evidence_links:
  - file:///receipts/progress.md
---
# Progress
""",
        encoding="utf-8",
    )


def test_planning_files_are_canonical_state_after_context_reset(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir)

    state = read_planning_state(planning_dir)
    projection = build_plane_projection(state)

    assert state.project_key == "BOS-42"
    assert state.current_phase == "phase-1"
    assert state.resume_point == "resume from progress checkpoint"
    assert state.next_action == "Implement the sync canary"
    assert projection["schema"] == PLANE_PROJECTION_SCHEMA
    assert projection["current_phase"] == "phase-1"
    assert projection["blockers"] == [
        {
            "id": "blocker-1",
            "title": "Awaiting Plane project key",
            "status": "open",
            "owner": "human",
            "evidence": ["file:///receipts/blocker.md"],
        }
    ]
    assert projection["evidence_links"] == [
        "file:///receipts/plan.md",
        "file:///receipts/blocker.md",
        "file:///receipts/finding.md",
        "file:///receipts/progress.md",
    ]
    assert "source_hash" in projection


def test_phase_transition_changes_projection_hash_and_updates_plane(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir, current_phase="phase-1")
    client = FakePlaneClient()

    first = sync_planning_to_plane(planning_dir, client)
    write_planning_files(planning_dir, current_phase="phase-2")
    second = sync_planning_to_plane(planning_dir, client)

    assert first.updated_plane is True
    assert second.updated_plane is True
    assert first.projection_hash != second.projection_hash
    assert client.updates[-1][1]["current_phase"] == "phase-2"


def test_resolved_blocker_is_removed_from_human_plane_projection(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir, blocker_status="resolved")

    projection = build_plane_projection(read_planning_state(planning_dir))

    assert projection["blockers"] == []
    assert "file:///receipts/blocker.md" in projection["evidence_links"]


def test_projection_hash_idempotency_prevents_duplicate_plane_updates(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir)
    client = FakePlaneClient()

    first = sync_planning_to_plane(planning_dir, client)
    second = sync_planning_to_plane(planning_dir, client)

    assert first.updated_plane is True
    assert second.updated_plane is False
    assert second.reason == "projection_hash_unchanged"
    assert len(client.updates) == 1
    state = json.loads((planning_dir / PROJECTION_STATE_FILE).read_text(encoding="utf-8"))
    assert state["last_applied_hash"] == first.projection_hash


def test_human_plane_change_becomes_proposal_not_silent_overwrite(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir)
    client = FakePlaneClient()
    first = sync_planning_to_plane(planning_dir, client)
    assert first.updated_plane is True

    client.projection = {
        **(client.projection or {}),
        "current_phase": "human-edited-phase",
        "source_hash": "human-plane-edit",
    }
    second = sync_planning_to_plane(planning_dir, client)

    assert second.updated_plane is False
    assert second.proposed_planning_change is True
    assert len(client.updates) == 1
    proposal = (planning_dir / PROPOSED_CHANGES_FILE).read_text(encoding="utf-8")
    assert "human-edited-phase" in proposal
    assert "Review this proposal and update task_plan.md, findings.md, or progress.md" in proposal

    third = sync_planning_to_plane(planning_dir, client)
    assert third.proposed_planning_change is True
    assert (planning_dir / PROPOSED_CHANGES_FILE).read_text(encoding="utf-8") == proposal


def test_missing_frontmatter_schema_fails_closed(tmp_path: Path) -> None:
    planning_dir = tmp_path / "plan"
    write_planning_files(planning_dir)
    (planning_dir / "progress.md").write_text("# no frontmatter\n", encoding="utf-8")

    with pytest.raises(PlanningSchemaError, match="progress.md must start"):
        read_planning_state(planning_dir)
