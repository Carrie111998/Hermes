from __future__ import annotations

from copy import deepcopy

from gateway.review_continuation import (
    create_review_continuations,
    resolve_review_continuation_settings,
)


class FakeBridge:
    def __init__(self, tasks: list[dict]):
        self.tasks = deepcopy(tasks)
        self.created_specs: list[dict] = []

    def list_tasks(self, **_kwargs):
        return deepcopy(self.tasks)

    def create_task(self, spec):
        captured = deepcopy(spec)
        self.created_specs.append(captured)
        created = {
            "task_id": f"fix-{len(self.created_specs)}",
            "status": "created",
            "spec": captured,
            "runtime": {},
            "result": None,
        }
        self.tasks.append(created)
        return deepcopy(created)


def _review_task(
    *,
    task_id: str = "review-1",
    summary: str = (
        "Primary finding: unsafe input reaches the shell.\n"
        "Severity: high\nClassification: real_now"
    ),
    depth: int = 0,
    opted_in: bool = True,
) -> dict:
    metadata = {
        "continuation_kind": "review",
        "continuation_chain_depth": depth,
    }
    if opted_in:
        metadata["on_findings"] = {
            "enabled": True,
            "fix_objective_template": "Fix these review findings:\n{findings}",
        }
    return {
        "task_id": task_id,
        "status": "succeeded",
        "spec": {
            "task_id": task_id,
            "objective": "Audit the command boundary",
            "worker": "codex",
            "workspace": {"repository": "C:/repo", "isolation": "shared"},
            "metadata": metadata,
        },
        "runtime": {},
        "result": {"summary": summary},
    }


def _settings(**overrides) -> dict:
    return {
        "enabled": True,
        "max_chain": 2,
        "require_severity": "high",
        **overrides,
    }


def test_eligible_review_with_high_finding_creates_one_fix_task():
    bridge = FakeBridge([_review_task()])

    assert create_review_continuations(bridge, _settings()) == 1
    assert len(bridge.created_specs) == 1
    successor = bridge.created_specs[0]
    assert successor["parent_task_id"] == "review-1"
    assert successor["metadata"]["continuation_kind"] == "fix"
    assert successor["metadata"]["source_review"] == "review-1"
    assert successor["metadata"]["continuation_chain_depth"] == 1
    assert "unsafe input reaches the shell" in successor["objective"]
    assert successor["idempotency_key"].startswith(
        "review-continuation:review-1:"
    )


def test_review_with_only_theoretical_low_findings_creates_none():
    bridge = FakeBridge(
        [
            _review_task(
                summary=(
                    "Finding: a hypothetical issue could exist.\n"
                    "Severity: low\nClassification: theoretical"
                )
            )
        ]
    )

    assert create_review_continuations(bridge, _settings()) == 0
    assert bridge.created_specs == []


def test_missing_on_findings_signal_creates_none():
    bridge = FakeBridge([_review_task(opted_in=False)])

    assert create_review_continuations(bridge, _settings()) == 0
    assert bridge.created_specs == []


def test_replay_is_idempotent():
    bridge = FakeBridge([_review_task()])

    assert create_review_continuations(bridge, _settings()) == 1
    assert create_review_continuations(bridge, _settings()) == 0
    assert len(bridge.created_specs) == 1


def test_chain_depth_cap_prevents_review_fix_loop():
    bridge = FakeBridge([_review_task(depth=2)])

    assert create_review_continuations(bridge, _settings(max_chain=2)) == 0
    assert bridge.created_specs == []


def test_fix_task_cannot_spawn_a_review_continuation():
    task = _review_task()
    task["spec"]["metadata"]["continuation_kind"] = "fix"
    bridge = FakeBridge([task])

    assert create_review_continuations(bridge, _settings()) == 0
    assert bridge.created_specs == []


def test_reads_actionable_finding_from_markdown_artifact(tmp_path):
    artifact = tmp_path / "review.md"
    artifact.write_text(
        "# Findings\n\nPrimary finding: unsafe deserialization.\n"
        "Severity: critical\nClassification: real_now\n",
        encoding="utf-8",
    )
    task = _review_task(summary="Review completed; see the attached report.")
    task["result"]["artifacts"] = [str(artifact)]
    bridge = FakeBridge([task])

    assert create_review_continuations(bridge, _settings()) == 1
    assert "unsafe deserialization" in bridge.created_specs[0]["objective"]


def test_config_off_is_no_op():
    bridge = FakeBridge([_review_task()])
    settings = resolve_review_continuation_settings(
        {"worker_bridge": {"review_continuation": {"enabled": False}}}
    )

    assert create_review_continuations(bridge, settings) == 0
    assert bridge.created_specs == []


def test_config_defaults():
    assert resolve_review_continuation_settings({}) == {
        "enabled": True,
        "max_chain": 2,
        "require_severity": "high",
    }
