#!/usr/bin/env python3
"""Phase 0 Workflow v1 runtime proof using production seams and no real launch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _dispatch_disabled() -> bool:
    try:
        return (
            _run("hermes", "config", "get", "kanban.dispatch_in_gateway").lower()
            == "false"
        )
    except (OSError, subprocess.CalledProcessError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-dispatch-disabled", action="store_true")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    if args.require_dispatch_disabled:
        checks["generic_dispatch_kill_switch"] = _dispatch_disabled()

    with tempfile.TemporaryDirectory(prefix="hermes-workflow-v1-") as raw:
        root = Path(raw)
        os.environ["HERMES_HOME"] = str(root / ".hermes")
        os.environ["HERMES_KANBAN_BOARD"] = "workflow-v1-phase-0"
        # This standalone synthetic harness owns only its temporary database;
        # do not inherit a parent delegate's live-board mutation guard.
        os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_execution import (
            LeafSpec,
            begin_workflow_controller_epoch,
            get_workflow_controller_state,
            reconcile_execution_leaves,
            register_execution_leaf,
            run_workflow_controller_tick,
            set_workflow_broker_ready,
            set_workflow_dispatch_enabled,
            validate_execution_readiness,
        )
        from hermes_cli.kanban_workflow_runtime import (
            RecordingLauncher,
            dispatch_execution_leaf,
            materialize_context_capsule,
            submit_evidence_proposal,
            submit_result_proposal,
            run_workflow_runtime_tick,
        )

        kb.init_db()
        repo = root / "repo"
        _run("git", "init", "-b", "main", str(repo))
        _run("git", "config", "user.email", "phase0@example.invalid", cwd=repo)
        _run("git", "config", "user.name", "Workflow Phase 0", cwd=repo)
        (repo / "src").mkdir()
        (repo / "src" / "feature.py").write_text(
            "class Feature:\n    pass\n", encoding="utf-8"
        )
        _run("git", "add", ".", cwd=repo)
        _run("git", "commit", "-m", "pin", cwd=repo)
        pin = _run("git", "rev-parse", "HEAD", cwd=repo)
        spec = LeafSpec(
            repository="synthetic/workflow",
            campaign_issue="1",
            leaf_id="phase-0",
            version=1,
            objective="Exercise the production runtime seam.",
            exclusions=("No external writes.",),
            allowed_paths=("src/**",),
            dependencies=(),
            acceptance_checks=(f"{sys.executable} -m compileall -q src",),
            hazards=("All state is temporary.",),
            human_gates=(),
            pin_sha=pin,
            first_evidence_seconds=60,
            wall_clock_budget_seconds=180,
        )
        capsule = materialize_context_capsule(
            repo,
            spec=spec,
            relevant_files=("src/feature.py",),
            symbols=("Feature",),
            governing_decisions=("Phase 0 uses production seams.",),
            base_assumptions=("The repository is disposable.",),
        )

        with kb.connect() as conn:
            initial = get_workflow_controller_state(conn)
            checks["workflow_defaults_paused_unready"] = (
                not initial.dispatch_enabled and not initial.broker_ready
            )
            epoch = "phase-0-controller"
            begin_workflow_controller_epoch(
                conn, controller_epoch=epoch, actor="phase-0"
            )
            run_workflow_controller_tick(
                conn, controller_epoch=epoch, process_alive=lambda _pid: False
            )
            state = get_workflow_controller_state(conn)
            state = set_workflow_broker_ready(
                conn,
                ready=True,
                expected_version=state.version,
                actor="phase-0",
                reason="synthetic launcher",
            )
            state = set_workflow_dispatch_enabled(
                conn,
                enabled=True,
                expected_version=state.version,
                actor="phase-0",
                reason="temporary runtime proof",
            )

            task_id = register_execution_leaf(
                conn, spec=spec, capsule=capsule, assignee="coder", workspace_path=repo
            )
            duplicate_id = register_execution_leaf(
                conn, spec=spec, capsule=capsule, assignee="coder", workspace_path=repo
            )
            checks["duplicate_registration_converges"] = duplicate_id == task_id

            launcher = RecordingLauncher(pid=515151)
            launched = dispatch_execution_leaf(
                conn, task_id, controller_epoch=epoch, launcher=launcher
            )
            checks["production_reserve_launch_running"] = (
                launched.status == "running" and len(launcher.invocations) == 1
            )
            checks["duplicate_reserve_rejected"] = (
                dispatch_execution_leaf(
                    conn, task_id, controller_epoch=epoch, launcher=launcher
                ).status
                == "rejected"
                and len(launcher.invocations) == 1
            )
            assert launched.run_id is not None and launched.fence is not None
            stale_evidence = submit_evidence_proposal(
                conn,
                task_id,
                run_id=launched.run_id,
                fence="stale",
                controller_epoch=epoch,
            )
            checks["stale_evidence_denied"] = (
                not stale_evidence.accepted and stale_evidence.reason == "stale_fence"
            )
            stale_result = submit_result_proposal(
                conn,
                task_id,
                run_id=launched.run_id,
                fence=launched.fence,
                controller_epoch="stale",
                proposal={"status": "done"},
            )
            checks["stale_result_denied"] = (
                not stale_result.accepted
                and stale_result.reason == "stale_controller_epoch"
            )

            (repo / "src" / "feature.py").write_text(
                "class Feature:\n    phase0 = True\n", encoding="utf-8"
            )
            evidence = submit_evidence_proposal(
                conn,
                task_id,
                run_id=launched.run_id,
                fence=launched.fence,
                controller_epoch=epoch,
            )
            checks["controller_computed_evidence"] = evidence.accepted
            result = submit_result_proposal(
                conn,
                task_id,
                run_id=launched.run_id,
                fence=launched.fence,
                controller_epoch=epoch,
                proposal={
                    "status": "done",
                    "summary": "worker proposal",
                    "proposed_follow_ups": ["create successor"],
                },
            )
            checks["controller_computed_acceptance_to_review"] = (
                result.accepted and result.status == "review"
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            try:
                kb.create_task(
                    conn,
                    title="forbidden successor",
                    assignee="coder",
                    parents=(task_id,),
                    created_by="coder",
                )
                successor_denied = False
            except PermissionError:
                successor_denied = True
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            checks["worker_successor_surface_denied"] = (
                successor_denied and before == after
            )

            original_body = kb.get_task(conn, task_id).body
            envelope = json.loads(original_body)
            envelope["capsule"]["symbols"] = ["Tampered"]
            conn.execute(
                "UPDATE tasks SET body=? WHERE id=?", (json.dumps(envelope), task_id)
            )
            conn.commit()
            checks["capsule_tamper_detected"] = (
                "capsule_hash_mismatch"
                in validate_execution_readiness(conn, task_id).blockers
            )
            conn.execute("UPDATE tasks SET body=? WHERE id=?", (original_body, task_id))
            conn.commit()

            report = reconcile_execution_leaves(
                conn, process_alive=lambda pid: pid == 515151
            )
            checks["restart_adopts_canonical_attempt"] = (
                task_id in report.adopted_task_ids
            )

            state = get_workflow_controller_state(conn)
            state = set_workflow_dispatch_enabled(
                conn,
                enabled=False,
                expected_version=state.version,
                actor="phase-0",
                reason="kill switch proof",
            )
            fail_if_called = RecordingLauncher(
                error=AssertionError("launcher invoked while dispatch paused")
            )
            checks["workflow_dispatch_kill_switch"] = (
                run_workflow_runtime_tick(
                    conn, controller_epoch=epoch, launcher=fail_if_called
                )
                == ()
                and fail_if_called.invocations == []
            )

            # A new commit changes the workspace base while the frozen capsule
            # remains pinned, forcing invalidation/rematerialization.
            _run("git", "add", ".", cwd=repo)
            _run("git", "commit", "-m", "phase0 drift", cwd=repo)
            drift = validate_execution_readiness(conn, task_id)
            checks["base_drift_detected"] = "pin_sha_drift" in drift.blockers

        worker_launched = (
            launcher.processes_created > 0 or fail_if_called.processes_created > 0
        )

    failed = sorted(name for name, passed in checks.items() if not passed)
    print(
        json.dumps(
            {
                "status": "pass" if not failed else "fail",
                "checks": checks,
                "failed": failed,
                "persistent_state_mutated": False,
                "worker_launched": worker_launched,
                "sentinel_invocations": len(launcher.invocations),
                "sentinel_processes_created": launcher.processes_created,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
