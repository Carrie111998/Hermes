from __future__ import annotations

from pathlib import Path

import pytest

from gateway.failure_successors import create_failure_successors
from gateway.review_continuation import create_review_continuations
from gateway.stage_successors import create_stage_successors

hermes_worker_bridge_store = pytest.importorskip("hermes_worker_bridge.store")
WorkerStore = hermes_worker_bridge_store.WorkerStore


def _find_by_source(store, key: str, source: str) -> dict:
    return next(
        task
        for task in store.list_tasks(limit=10_000)
        if ((task.get("spec") or {}).get("metadata") or {}).get(key) == source
    )


def _pipeline_plan_spec(workspace: Path) -> dict:
    return {
        "worker": "fable",
        "objective": "Create the implementation plan",
        "workspace": {"repository": str(workspace), "isolation": "shared"},
        "priority": 20,
        "metadata": {
            "successor_chain_depth": 0,
            "on_success": {
                "enabled": True,
                "next": {
                    "worker": "codex",
                    "objective_template": (
                        "Implement approved plan {parent_task_id}.\n\n{summary}"
                    ),
                    "metadata": {
                        "on_success": {
                            "enabled": True,
                            "next": {
                                "worker": "codex",
                                "objective_template": (
                                    "Review build {parent_task_id}.\n\n{summary}"
                                ),
                                "metadata": {
                                    "continuation_kind": "review",
                                    "on_findings": {
                                        "enabled": True,
                                        "fix_objective_template": (
                                            "Fix verified findings:\n{findings}"
                                        ),
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def test_real_temp_bridge_runs_plan_build_review_fix_pipeline(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = WorkerStore(home / "workers" / "bridge.db")
    stage_settings = {"enabled": True, "max_chain": 4}
    review_settings = {
        "enabled": True,
        "max_chain": 4,
        "require_severity": "high",
    }

    plan = store.create_task(_pipeline_plan_spec(workspace))
    store.update_task(
        plan["task_id"],
        status="succeeded",
        result={"summary": "Build the approved implementation in small steps."},
    )

    assert create_stage_successors(store, stage_settings) == 1
    build = _find_by_source(store, "source_stage", plan["task_id"])
    assert build["status"] == "created"
    assert build["spec"]["worker"] == "codex"
    assert build["spec"]["idempotency_key"] == (
        f"stage-successor:{plan['task_id']}"
    )
    assert create_stage_successors(store, stage_settings) == 0
    assert len(
        [
            task
            for task in store.list_tasks(limit=10_000)
            if ((task.get("spec") or {}).get("metadata") or {}).get(
                "source_stage"
            )
            == plan["task_id"]
        ]
    ) == 1

    store.update_task(
        build["task_id"],
        status="succeeded",
        result={"summary": "Implementation complete and focused tests pass."},
    )
    assert create_stage_successors(store, stage_settings) == 1
    review = _find_by_source(store, "source_stage", build["task_id"])
    assert review["spec"]["metadata"]["continuation_kind"] == "review"
    assert review["spec"]["metadata"]["successor_chain_depth"] == 2

    findings = tmp_path / "review-findings.md"
    findings.write_text(
        "# Findings\n\n"
        "Command input is not escaped.\n"
        "Severity: high\n"
        "Classification: real_now\n",
        encoding="utf-8",
    )
    store.update_task(
        review["task_id"],
        status="succeeded",
        result={
            "summary": "Review completed.",
            "artifacts": [str(findings)],
        },
    )
    assert create_review_continuations(store, review_settings) == 1
    fix = _find_by_source(store, "source_review", review["task_id"])
    assert fix["status"] == "created"
    assert fix["spec"]["idempotency_key"].startswith(
        f"review-continuation:{review['task_id']}:"
    )
    assert fix["spec"]["metadata"]["successor_chain_depth"] == 3

    store.update_task(
        fix["task_id"],
        status="failed",
        runtime={"auto_repair_attempts": 1},
        result={"error": "focused test still fails"},
    )
    fix_spec = dict(fix["spec"])
    fix_metadata = dict(fix_spec["metadata"])
    fix_metadata["auto_repair"] = 1
    fix_spec["metadata"] = fix_metadata
    store.update_task_spec(fix["task_id"], fix_spec)

    assert create_failure_successors(
        store,
        {"enabled": True, "max_chain": 4},
    ) == 1
    retry = next(
        task
        for task in store.list_tasks(limit=10_000)
        if (task.get("spec") or {}).get("parent_task_id") == fix["task_id"]
        and ((task.get("spec") or {}).get("metadata") or {}).get(
            "failure_successor"
        )
        is True
    )
    assert retry["spec"]["metadata"]["successor_chain_depth"] == 4
    assert create_stage_successors(store, stage_settings) == 0


def test_real_temp_bridge_stops_stage_chain_at_depth_cap(tmp_path):
    store = WorkerStore(tmp_path / "home" / "workers" / "bridge.db")
    task = store.create_task(
        {
            "worker": "fable",
            "objective": "At the cap",
            "workspace": {"repository": str(tmp_path)},
            "metadata": {
                "successor_chain_depth": 2,
                "on_success": {
                    "enabled": True,
                    "next": {
                        "worker": "codex",
                        "objective_template": "Must not run",
                    },
                },
            },
        }
    )
    store.update_task(task["task_id"], status="succeeded", result={"summary": "done"})

    assert create_stage_successors(
        store,
        {"enabled": True, "max_chain": 2},
    ) == 0
    assert len(store.list_tasks(limit=10_000)) == 1
