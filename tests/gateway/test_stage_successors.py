from __future__ import annotations

from copy import deepcopy

import gateway.stage_successors as stage_successors_module
from gateway.stage_successors import (
    MAX_SUMMARY_CHARS,
    create_stage_successors,
    resolve_stage_successor_settings,
)


_DEFAULT_NEXT = object()


class FakeBridge:
    def __init__(self, tasks: list[dict]):
        self.tasks = deepcopy(tasks)
        self.created_specs: list[dict] = []
        self.idempotency_keys: set[str] = set()

    def list_tasks(self, **_kwargs):
        return deepcopy(self.tasks)

    def create_task(self, spec):
        captured = deepcopy(spec)
        key = captured["idempotency_key"]
        if key in self.idempotency_keys:
            return next(
                deepcopy(task)
                for task in self.tasks
                if (task.get("spec") or {}).get("idempotency_key") == key
            )
        self.idempotency_keys.add(key)
        self.created_specs.append(captured)
        created = {
            "task_id": f"stage-{len(self.created_specs)}",
            "status": "created",
            "spec": captured,
            "runtime": {},
            "result": None,
        }
        self.tasks.append(created)
        return deepcopy(created)


def _task(
    *,
    task_id: str = "plan-1",
    status: str = "succeeded",
    depth: int = 0,
    opted_in: bool = True,
    next_stage: object = _DEFAULT_NEXT,
    summary: str = "Use the approved three-step implementation plan.",
) -> dict:
    metadata: dict = {"successor_chain_depth": depth, "preserve_me": True}
    if opted_in:
        metadata["on_success"] = {
            "enabled": True,
            "next": next_stage
            if next_stage is not _DEFAULT_NEXT
            else {
                "worker": "codex",
                "objective_template": (
                    "Implement plan {parent_task_id}.\n\nPlan summary:\n{summary}"
                ),
                "metadata": {
                    "authored": True,
                    "on_success": {
                        "enabled": True,
                        "next": {
                            "worker": "reviewer",
                            "objective_template": "Review {parent_task_id}",
                        },
                    },
                },
                "priority": 73,
            },
        }
    return {
        "task_id": task_id,
        "status": status,
        "spec": {
            "task_id": task_id,
            "worker": "fable",
            "objective": "Write a plan",
            "workspace": {"repository": "C:/repo", "isolation": "shared"},
            "priority": 10,
            "metadata": metadata,
        },
        "runtime": {},
        "result": {"summary": summary},
    }


def _settings(**overrides) -> dict:
    return {"enabled": True, "max_chain": 4, **overrides}


def test_opted_in_success_creates_authored_stage_successor():
    bridge = FakeBridge([_task()])

    assert create_stage_successors(bridge, _settings()) == 1
    successor = bridge.created_specs[0]
    assert successor["task_id"] is None
    assert successor["idempotency_key"] == "stage-successor:plan-1"
    assert successor["parent_task_id"] == "plan-1"
    assert successor["worker"] == "codex"
    assert successor["priority"] == 73
    assert successor["workspace"] == {
        "repository": "C:/repo",
        "isolation": "shared",
    }
    assert "Implement plan plan-1" in successor["objective"]
    assert "approved three-step" in successor["objective"]
    assert successor["metadata"]["continuation_kind"] == "stage"
    assert successor["metadata"]["source_stage"] == "plan-1"
    assert successor["metadata"]["successor_chain_depth"] == 1
    assert successor["metadata"]["authored"] is True
    assert "preserve_me" not in successor["metadata"]
    assert successor["metadata"]["on_success"]["enabled"] is True


def test_explicit_opt_in_is_required():
    bridge = FakeBridge([_task(opted_in=False)])

    assert create_stage_successors(bridge, _settings()) == 0
    assert bridge.created_specs == []


def test_replay_and_existing_source_stage_are_idempotent():
    bridge = FakeBridge([_task()])

    assert create_stage_successors(bridge, _settings()) == 1
    assert create_stage_successors(bridge, _settings()) == 0
    assert len(bridge.created_specs) == 1

    existing = _task(task_id="existing", opted_in=False)
    existing["spec"]["metadata"] = {"source_stage": "plan-2"}
    bridge = FakeBridge([_task(task_id="plan-2"), existing])
    assert create_stage_successors(bridge, _settings()) == 0


def test_chain_cap_and_non_success_status_stop_creation():
    bridge = FakeBridge(
        [
            _task(task_id="capped", depth=4),
            _task(task_id="failed", status="failed"),
        ]
    )

    assert create_stage_successors(bridge, _settings(max_chain=4)) == 0


def test_accepted_parent_is_a_satisfied_success_status():
    bridge = FakeBridge([_task(status="accepted")])

    assert create_stage_successors(bridge, _settings()) == 1


def test_workspace_override_and_default_priority():
    bridge = FakeBridge(
        [
            _task(
                next_stage={
                    "worker": "codex",
                    "objective_template": "Build {parent_task_id}",
                    "workspace": {"repository": "D:/other"},
                }
            )
        ]
    )

    assert create_stage_successors(bridge, _settings()) == 1
    successor = bridge.created_specs[0]
    assert successor["workspace"] == {"repository": "D:/other"}
    assert successor["priority"] == 50


def test_summary_is_truncated_before_rendering():
    bridge = FakeBridge(
        [
            _task(
                summary="x" * (MAX_SUMMARY_CHARS + 500),
                next_stage={
                    "worker": "codex",
                    "objective_template": "{summary}",
                },
            )
        ]
    )

    assert create_stage_successors(bridge, _settings()) == 1
    assert bridge.created_specs[0]["objective"] == "x" * MAX_SUMMARY_CHARS


def test_malformed_next_fields_are_ignored():
    malformed = [
        None,
        "codex",
        {},
        {"worker": "", "objective_template": "Build"},
        {"worker": "codex", "objective_template": ""},
        {
            "worker": "codex",
            "objective_template": "Build",
            "metadata": "not-an-object",
        },
    ]
    bridge = FakeBridge(
        [
            _task(task_id=f"bad-{index}", next_stage=value)
            for index, value in enumerate(malformed)
        ]
    )

    assert create_stage_successors(bridge, _settings()) == 0
    assert bridge.created_specs == []


def test_config_disable_defaults_and_bounds():
    assert resolve_stage_successor_settings({}) == {
        "enabled": True,
        "max_chain": 4,
    }
    assert resolve_stage_successor_settings(
        {"worker_bridge": {"stage_successors": {"enabled": False, "max_chain": -3}}}
    ) == {"enabled": False, "max_chain": 0}
    assert resolve_stage_successor_settings(
        {"worker_bridge": {"stage_successors": {"max_chain": "invalid"}}}
    ) == {"enabled": True, "max_chain": 4}


class RejectingBridge(FakeBridge):
    def __init__(self, tasks, reject_parent):
        super().__init__(tasks)
        self.reject_parent = reject_parent

    def create_task(self, spec):
        if spec.get("parent_task_id") == self.reject_parent:
            raise ValueError("repository does not exist")
        return super().create_task(spec)


def test_unservable_parent_does_not_abort_pass_and_warns_once(caplog):
    stage_successors_module._reported_skips.clear()
    bridge = RejectingBridge(
        [_task(task_id="gone"), _task(task_id="healthy")],
        reject_parent="gone",
    )

    with caplog.at_level("WARNING", logger="gateway.run"):
        assert create_stage_successors(bridge, _settings()) == 1
        assert create_stage_successors(bridge, _settings()) == 0

    assert bridge.created_specs[0]["parent_task_id"] == "healthy"
    warnings = [
        record
        for record in caplog.records
        if "stage successor skipped for gone" in record.getMessage()
    ]
    assert len(warnings) == 1
    stage_successors_module._reported_skips.clear()