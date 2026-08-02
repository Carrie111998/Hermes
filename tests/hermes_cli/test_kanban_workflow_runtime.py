"""Behavior tests for the Workflow v1 production runtime tracer bullet."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_workflow_runtime as workflow_runtime
from hermes_cli.kanban_execution import (
    LeafSpec,
    begin_workflow_controller_epoch,
    get_workflow_controller_state,
    register_execution_leaf,
    run_workflow_controller_tick,
    set_workflow_broker_ready,
    set_workflow_dispatch_enabled,
)
from hermes_cli.kanban_workflow_runtime import (
    CIObservation,
    GitHubProjectionConflict,
    GitHubSnapshot,
    HermesWorkflowLauncher,
    LaunchFailure,
    ProjectionRequest,
    RecordingLauncher,
    ReviewObservation,
    WorkflowProductionCoordinator,
    begin_review_closeout,
    close_reviewed_leaf,
    dispatch_execution_leaf,
    ingest_github_snapshot,
    ingest_worker_progress,
    ingest_worker_proposal,
    materialize_context_capsule,
    project_github_status,
    record_ci_result,
    record_review_verdict,
    record_workflow_failure,
    reconcile_runtime_reservations,
    run_workflow_runtime_tick,
    submit_evidence_proposal,
    submit_result_proposal,
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "runtime@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Runtime Test"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text(
        "class Feature:\n    pass\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "pin"], cwd=repo, check=True, capture_output=True
    )
    pin = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    spec = LeafSpec(
        repository="nous/runtime",
        campaign_issue="1",
        leaf_id="slice-a",
        version=1,
        objective="Change the feature.",
        exclusions=("No release.",),
        allowed_paths=("src/**",),
        dependencies=(),
        acceptance_checks=(
            "python -c \"compile(open('src/feature.py').read(), 'src/feature.py', 'exec')\"",
        ),
        hazards=("Remain scoped.",),
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
        governing_decisions=("Pinned source is authoritative.",),
        base_assumptions=("Disposable repository.",),
    )
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=spec,
            capsule=capsule,
            assignee="coder",
            workspace_path=repo,
            branch_name="main",
        )
        epoch = "runtime-test-epoch"
        begin_workflow_controller_epoch(conn, controller_epoch=epoch)
        run_workflow_controller_tick(
            conn, controller_epoch=epoch, process_alive=lambda _pid: False
        )
        state = get_workflow_controller_state(conn)
        state = set_workflow_broker_ready(
            conn,
            ready=True,
            expected_version=state.version,
            actor="test",
            reason="sentinel",
        )
        set_workflow_dispatch_enabled(
            conn,
            enabled=True,
            expected_version=state.version,
            actor="test",
            reason="sentinel",
        )
    return repo, task_id, epoch, spec


def test_materializer_rejects_missing_non_file_escape_and_unresolved_symbol(
    runtime, tmp_path
):
    repo, _task_id, _epoch, spec = runtime
    with pytest.raises(ValueError, match="missing"):
        materialize_context_capsule(
            repo, spec=spec, relevant_files=("src/missing.py",), symbols=("Feature",)
        )
    with pytest.raises(ValueError, match="regular file"):
        materialize_context_capsule(
            repo, spec=spec, relevant_files=("src",), symbols=("Feature",)
        )
    with pytest.raises(ValueError, match="escape"):
        materialize_context_capsule(
            repo, spec=spec, relevant_files=("../secret",), symbols=("Feature",)
        )
    with pytest.raises(ValueError, match="symbol"):
        materialize_context_capsule(
            repo,
            spec=spec,
            relevant_files=("src/feature.py",),
            symbols=("MissingSymbol",),
        )


def test_reserve_launch_running_saga_and_duplicate_rejection(runtime):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=43210)
    with kb.connect() as conn:
        outcome = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert outcome.status == "running"
        assert outcome.pid == 43210
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert (
            task is not None and task.status == "running" and task.worker_pid == 43210
        )
        assert run is not None and run.status == "running" and run.worker_pid == 43210
        assert run.reserved_at is not None and run.spawned_at is not None
        assert run.launch_id and run.process_identity
        assert launcher.invocations[0].cwd == str(repo.resolve())
        assert launcher.invocations[0].toolsets == ("terminal", "file")
        assert "must not create successors" in launcher.invocations[0].prompt.lower()
        assert (
            dispatch_execution_leaf(
                conn, task_id, controller_epoch=epoch, launcher=launcher
            ).status
            == "rejected"
        )
        assert len(launcher.invocations) == 1


def test_final_reservation_rechecks_live_branch_after_initial_readiness(
    runtime, monkeypatch
):
    repo, task_id, epoch, _spec = runtime
    authorized_branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    acquire = workflow_runtime._acquire_workspace_reservation

    def acquire_then_switch(*args, **kwargs):
        acquired = acquire(*args, **kwargs)
        if acquired:
            subprocess.run(
                ["git", "-C", str(repo), "switch", "-c", "pilot/unapproved"],
                check=True,
                capture_output=True,
            )
        return acquired

    monkeypatch.setattr(
        workflow_runtime, "_acquire_workspace_reservation", acquire_then_switch
    )
    with kb.connect() as conn:
        rejected = dispatch_execution_leaf(
            conn,
            task_id,
            controller_epoch=epoch,
            launcher=RecordingLauncher(pid=33332),
        )
        assert rejected.status == "rejected"
        assert "final_readiness_failed:branch_mismatch" in str(rejected.reason)
        assert kb.latest_run(conn, task_id, allow_execution_leaf=True) is None

    monkeypatch.setattr(workflow_runtime, "_acquire_workspace_reservation", acquire)
    subprocess.run(
        ["git", "-C", str(repo), "switch", authorized_branch],
        check=True,
        capture_output=True,
    )
    with kb.connect() as conn:
        accepted = dispatch_execution_leaf(
            conn,
            task_id,
            controller_epoch=epoch,
            launcher=RecordingLauncher(pid=33332),
        )
        assert accepted.status == "running"


def test_prelaunch_rechecks_branch_after_invocation_materialization(
    runtime, monkeypatch
):
    repo, task_id, epoch, _spec = runtime
    authorized_branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    build_invocation = workflow_runtime._invocation

    def invocation_then_switch(*args, **kwargs):
        invocation = build_invocation(*args, **kwargs)
        subprocess.run(
            ["git", "-C", str(repo), "switch", "-c", "pilot/prelaunch-drift"],
            check=True,
            capture_output=True,
        )
        return invocation

    monkeypatch.setattr(workflow_runtime, "_invocation", invocation_then_switch)
    launcher = RecordingLauncher(pid=33331)
    with kb.connect() as conn:
        rejected = dispatch_execution_leaf(
            conn,
            task_id,
            controller_epoch=epoch,
            launcher=launcher,
        )
        assert rejected.status == "launch_failed"
        assert "prelaunch_readiness_failed:branch_mismatch" in str(rejected.reason)
        assert launcher.invocations == []
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task is not None and task.status == "ready" and task.claim_lock is None
        assert run is not None and run.status == "failed"

    subprocess.run(
        ["git", "-C", str(repo), "switch", authorized_branch],
        check=True,
        capture_output=True,
    )


def test_spawn_to_pid_persistence_crash_is_ambiguous_and_keeps_reservation(runtime):
    _repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(
        pid=33333, after_spawn_error=RuntimeError("crash after spawn")
    )
    with kb.connect() as conn:
        outcome = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert outcome.status == "ambiguous"
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task is not None and task.status == "ready" and task.claim_lock
        assert run is not None and run.status == "reserved" and run.worker_pid is None
        report = reconcile_runtime_reservations(conn, process_alive=lambda _pid: False)
        assert task_id in report.quarantined_task_ids
        assert "pidless_reservation_ambiguous" in report.findings
        assert (
            dispatch_execution_leaf(
                conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=9)
            ).status
            == "rejected"
        )


def test_post_spawn_activation_failure_retains_handle_and_ownership(runtime):
    _repo, task_id, epoch, _spec = runtime

    def pause_after_spawn(conn):
        conn.execute("UPDATE workflow_controller_state SET dispatch_enabled=0")
        conn.commit()

    with kb.connect() as conn:
        launcher = RecordingLauncher(
            pid=33334, on_launch=lambda: pause_after_spawn(conn)
        )
        outcome = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert outcome.status == "ambiguous"
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert (
            task is not None
            and task.status == "ready"
            and task.current_run_id == run.id
        )
        assert task.claim_lock and task.worker_pid == 33334
        assert run is not None and run.status == "reserved" and run.worker_pid == 33334
        assert run.launch_id and run.process_identity
        assert run.quarantine_reason == "activation_cas_failed"
        assert (
            dispatch_execution_leaf(
                conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=9)
            ).status
            == "rejected"
        )


def test_positive_pre_spawn_failure_releases_reservation(runtime):
    _repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(
        error=LaunchFailure("exec failed", process_created=False)
    )
    with kb.connect() as conn:
        assert (
            dispatch_execution_leaf(
                conn, task_id, controller_epoch=epoch, launcher=launcher
            ).status
            == "launch_failed"
        )
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task is not None and task.status == "ready" and task.claim_lock is None
        assert (
            run is not None and run.status == "failed" and run.outcome == "spawn_failed"
        )


def test_controller_computes_evidence_and_acceptance_then_moves_to_review(runtime):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=44444)
        )
        assert launched.run_id and launched.fence
        stale = submit_evidence_proposal(
            conn, task_id, run_id=launched.run_id, fence="stale", controller_epoch=epoch
        )
        assert not stale.accepted and stale.reason == "stale_fence"
        (repo / "src" / "feature.py").write_text(
            "class Feature:\n    enabled = True\n", encoding="utf-8"
        )
        evidence = submit_evidence_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert evidence.accepted
        stale_result = submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch="old",
            proposal={"status": "done"},
        )
        assert (
            not stale_result.accepted
            and stale_result.reason == "stale_controller_epoch"
        )
        result = submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            proposal={"status": "done", "summary": "proposal only"},
        )
        assert result.accepted and result.status == "review"
        assert result.checks and result.checks[0].returncode == 0
        assert kb.get_task(conn, task_id).status == "review"
        assert (
            kb.latest_run(conn, task_id, allow_execution_leaf=True).status
            == "reviewing"
        )
        assert not kb.complete_task(
            conn,
            task_id,
            expected_run_id=launched.run_id,
            expected_claim_lock=launched.fence,
        )
        assert not kb.block_task(
            conn,
            task_id,
            reason="worker direct",
            expected_run_id=launched.run_id,
            expected_claim_lock=launched.fence,
        )


def test_generic_completion_and_block_are_denied_for_any_protected_state(runtime):
    _repo, task_id, _epoch, _spec = runtime
    with kb.connect() as conn:
        assert not kb.complete_task(conn, task_id)
        assert not kb.block_task(conn, task_id, reason="generic")
        assert kb.block_task(
            conn,
            task_id,
            reason="controller quarantine",
            force_execution_admin=True,
        )


def test_materialization_source_hash_drift_fails_closed(runtime):
    _repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        envelope = json.loads(task.body)
        envelope["capsule"]["materialization"]["source_tree_hash"] = "0" * 64
        from hermes_cli.kanban_execution import _hash_payload

        body = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE tasks SET body=?, capsule_hash=? WHERE id=?",
            (body, _hash_payload(envelope["capsule"]), task_id),
        )
        conn.commit()
        outcome = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=7)
        )
        assert outcome.status == "rejected"
        assert "capsule_source_drift" in (outcome.reason or "")


def test_production_launcher_scrubs_env_and_persists_receipt(runtime, monkeypatch):
    repo, task_id, epoch, _spec = runtime
    captured = {}

    class Proc:
        pid = 24680

    real_popen = subprocess.Popen

    def popen(cmd, **kwargs):
        if cmd and Path(str(cmd[0])).name == "git":
            return real_popen(cmd, **kwargs)
        captured.update(cmd=cmd, **kwargs)
        return Proc()

    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/must-not-leak")
    monkeypatch.setenv("UNRELATED_REPOSITORY", "/secret/repo")
    profile_home = Path(os.environ["HERMES_HOME"])
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / ".env").write_text("GH_TOKEN=must-not-load\n", encoding="utf-8")
    (profile_home / "auth.json").write_text(
        json.dumps({
            "version": 1,
            "active_provider": "openai-codex",
            "credential_pool": {
                "openai-codex": [{"access_token": "provider-only"}],
                "copilot": [{"access_token": "must-not-copy"}],
            },
            "providers": {
                "openai-codex": {"tokens": {"access_token": "provider-only"}},
                "copilot": {"tokens": {"access_token": "must-not-copy"}},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        HermesWorkflowLauncher,
        "_process_start_identity",
        staticmethod(lambda _pid: "linux-start:123"),
    )
    with kb.connect() as conn:
        outcome = dispatch_execution_leaf(
            conn,
            task_id,
            controller_epoch=epoch,
            launcher=HermesWorkflowLauncher(model="gpt-test", provider="openai-codex"),
        )
        assert outcome.status == "running"
        assert captured["cwd"] == str(repo.resolve())
        assert captured["env"].get("GH_TOKEN") is None
        assert captured["env"].get("SSH_AUTH_SOCK") is None
        assert captured["env"].get("UNRELATED_REPOSITORY") is None
        worker_home = Path(captured["env"]["HERMES_HOME"])
        assert worker_home != profile_home
        assert captured["env"]["HOME"] == str(worker_home)
        assert captured["env"]["GH_CONFIG_DIR"] == str(worker_home / "gh")
        assert not (worker_home / ".env").exists()
        narrowed_auth = json.loads((worker_home / "auth.json").read_text())
        assert set(narrowed_auth["credential_pool"]) == {"openai-codex"}
        assert set(narrowed_auth["providers"]) == {"openai-codex"}
        assert captured["env"]["TERMINAL_CWD"] == str(repo.resolve())
        assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(repo.resolve())
        assert captured["env"]["HERMES_KANBAN_TASK"] == task_id
        assert captured["env"]["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
        assert captured["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert captured["cmd"][1:4] == ["chat", "-Q", "--source"]
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "gpt-test"
        assert (
            captured["cmd"][captured["cmd"].index("--provider") + 1] == "openai-codex"
        )
        assert "--ignore-user-config" in captured["cmd"]
        assert "workflow-worker" in captured["cmd"]
        assert str(repo.resolve()) in captured["cmd"][-1]
        assert 'status must be exactly "done"' in captured["cmd"][-1]
        assert "before the first-evidence budget expires" in captured["cmd"][-1]
        assert (kb.kanban_home() / "kanban" / "workflow-worker-progress").is_dir()
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert Path(run.launch_receipt_path).parent.is_dir()
        receipt = json.loads(Path(run.launch_receipt_path).read_text(encoding="utf-8"))
        assert receipt["pid"] == 24680
        assert receipt["cwd"] == str(repo.resolve())
        assert receipt["process_start_identity"] == "linux-start:123"


def test_no_launch_mode_reserves_without_invoking_sentinel(runtime):
    _repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(error=AssertionError("launcher must not be invoked"))
    with kb.connect() as conn:
        outcome = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher, no_launch=True
        )
        assert outcome.status == "reserved"
        assert launcher.invocations == []


def _commit_worker_change(repo: Path, value: str = "enabled = True") -> str:
    (repo / "src" / "feature.py").write_text(
        f"class Feature:\n    {value}\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "src/feature.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "worker proposal"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_real_worker_proposal_file_is_bounded_fenced_and_ingested_once(runtime):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=50001)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        invocation = launcher.invocations[0]
        assert invocation.proposal_path in invocation.prompt
        _commit_worker_change(repo)
        proposal_path = Path(invocation.proposal_path)
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(
            json.dumps({
                "status": "done",
                "summary": "worker finished",
                "changed_files": ["src/feature.py"],
                "checks": [],
            }),
            encoding="utf-8",
        )

        stale = ingest_worker_proposal(
            conn, task_id, run_id=launched.run_id, fence="stale", controller_epoch=epoch
        )
        assert not stale.accepted and stale.reason == "stale_fence"
        # The production runtime tick discovers the real worker channel; tests
        # do not need to smuggle the proposal through a direct Python call.
        assert (
            run_workflow_runtime_tick(
                conn,
                controller_epoch=epoch,
                launcher=RecordingLauncher(pid=59999),
                max_launch=0,
            )
            == ()
        )
        assert kb.get_task(conn, task_id).status == "review"
        assert not proposal_path.exists()
        duplicate = ingest_worker_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert not duplicate.accepted and duplicate.reason == "attempt_not_running"


def test_terminal_proposal_published_before_expiry_is_consumed_after_poll_delay(
    runtime, monkeypatch
):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=50009)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert launched.run_id is not None and launched.fence is not None
        _commit_worker_change(repo)
        proposal_path = Path(launcher.invocations[0].proposal_path)
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(
            json.dumps({"status": "done", "summary": "timely publication"}),
            encoding="utf-8",
        )
        info = proposal_path.stat()
        expiry = int(max(info.st_mtime, info.st_ctime))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires=? WHERE id=?",
                (expiry, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires=?, started_at=?, "
                "max_runtime_seconds=? WHERE id=?",
                (expiry, expiry - 60, 60, launched.run_id),
            )
        monkeypatch.setattr(workflow_runtime.time, "time", lambda: expiry + 2)

        outcome = ingest_worker_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )

        assert outcome.accepted and outcome.status == "review"
        assert not proposal_path.exists()


def test_terminal_proposal_created_after_expiry_is_rejected(runtime):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=50010)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert launched.run_id is not None and launched.fence is not None
        _commit_worker_change(repo)
        expiry = int(workflow_runtime.time.time()) - 5
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires=? WHERE id=?", (expiry, task_id)
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires=? WHERE id=?",
                (expiry, launched.run_id),
            )
        proposal_path = Path(launcher.invocations[0].proposal_path)
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(
            json.dumps({"status": "done", "summary": "late publication"}),
            encoding="utf-8",
        )

        outcome = ingest_worker_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )

        assert not outcome.accepted and outcome.reason == "lease_expired"
        assert proposal_path.exists()
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"


def test_review_and_ci_closeout_are_bound_to_one_exact_candidate_sha(runtime):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=50002)
        )
        candidate_sha = _commit_worker_change(repo)
        proposed = submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            proposal={"status": "done", "summary": "ready"},
        )
        assert proposed.accepted
        review = begin_review_closeout(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            required_ci=True,
        )
        assert review.candidate_sha == candidate_sha
        assert len(review.diff_digest) == 64
        assert not record_review_verdict(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            reviewer="coder",
            candidate_sha="0" * 40,
            diff_digest=review.diff_digest,
            checklist={"scope": True},
            approved=True,
        ).accepted
        assert record_review_verdict(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            reviewer="independent-reviewer",
            candidate_sha=candidate_sha,
            diff_digest=review.diff_digest,
            checklist={"scope": True, "tests": True},
            approved=True,
        ).accepted
        assert not record_ci_result(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            candidate_sha="f" * 40,
            check_suite="protected",
            conclusion="success",
        ).accepted
        assert record_ci_result(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            candidate_sha=candidate_sha,
            check_suite="protected",
            conclusion="success",
        ).accepted
        closed = close_reviewed_leaf(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert closed.accepted and closed.status == "done"
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert run.status == "done" and run.outcome == "completed"


def test_head_change_invalidates_exact_sha_review(runtime):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=50003)
        )
        candidate_sha = _commit_worker_change(repo)
        assert submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            proposal={"status": "done", "summary": "ready"},
        ).accepted
        review = begin_review_closeout(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            required_ci=False,
        )
        assert record_review_verdict(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            reviewer="reviewer",
            candidate_sha=candidate_sha,
            diff_digest=review.diff_digest,
            checklist={"scope": True},
            approved=True,
        ).accepted
        _commit_worker_change(repo, "enabled = False")
        closed = close_reviewed_leaf(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert not closed.accepted and closed.reason == "candidate_head_changed"


def test_failure_classes_have_independent_counts_and_retry_policies(runtime):
    _repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=50004)
        )
        first = record_workflow_failure(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            failure_class="launch",
            detail="runner unavailable",
        )
        second = record_workflow_failure(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            failure_class="launch",
            detail="runner unavailable",
        )
        ambiguity = record_workflow_failure(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            failure_class="scope_ambiguity",
            detail="contract unclear",
        )
        assert (first.count, first.action) == (1, "retry")
        assert (second.count, second.action) == (2, "inspect_infrastructure")
        assert (ambiguity.count, ambiguity.action) == (1, "quarantine")


def test_canonical_github_snapshot_and_verified_projection_seams(runtime):
    _repo, task_id, _epoch, _spec = runtime
    snapshot = GitHubSnapshot(
        repository_node_id="R_repo",
        issue_node_id="I_issue",
        project_item_id="PVTI_item",
        source_updated_at="2026-08-02T08:00:00Z",
        source_version="etag-1",
        issue={"title": "Campaign", "body": "acceptance", "state": "OPEN"},
        project={"status": "Now", "priority": "High", "dependencies": []},
        pull_requests=(),
    )
    with kb.connect() as conn:
        first = ingest_github_snapshot(conn, task_id=task_id, snapshot=snapshot)
        same = ingest_github_snapshot(conn, task_id=task_id, snapshot=snapshot)
        assert first.changed and first.material_change and len(first.content_hash) == 64
        assert not same.changed and same.version == first.version

        class Writer:
            def __init__(self):
                self.updated_at = snapshot.source_updated_at
                self.status = "Now"

            def write_status(self, *, project_item_id, status, expected_updated_at):
                assert project_item_id == "PVTI_item"
                if expected_updated_at != self.updated_at:
                    raise GitHubProjectionConflict("stale GitHub Project item")
                self.status = status
                self.updated_at = "2026-08-02T08:01:00Z"

            def read_status(self, *, project_item_id):
                return {"status": self.status, "updated_at": self.updated_at}

        projected = project_github_status(
            conn,
            task_id=task_id,
            writer=Writer(),
            status="Done",
            expected_updated_at=snapshot.source_updated_at,
        )
        assert projected.status == "Done" and projected.verified


class _Inspector:
    def __init__(self, state: str):
        self.state = state
        self.identities = []

    def inspect(self, identity):
        self.identities.append(identity)
        return self.state


def _progress_payload(task_id, launched, epoch, sequence, **updates):
    payload = {
        "schema": "hermes.workflow-progress.v1",
        "task_id": task_id,
        "run_id": launched.run_id,
        "fence": launched.fence,
        "controller_epoch": epoch,
        "state": "running",
        "sequence": sequence,
        "summary": "meaningful artifact delta",
    }
    payload.update(updates)
    return payload


def _publish_progress(invocation, sequence, payload):
    directory = Path(invocation.progress_directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".progress-{sequence}.tmp"
    final = directory / f"progress-{sequence}.json"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(final)
    return final


def test_production_tick_consumes_sequenced_progress_and_duplicate_does_not_renew(
    runtime,
):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=51001)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        invocation = launcher.invocations[0]
        assert invocation.progress_directory in invocation.prompt
        assert "before the first-evidence budget expires" in invocation.prompt
        assert "during long work" in invocation.prompt
        (repo / "src" / "feature.py").write_text(
            "class Feature:\n    progress = 1\n", encoding="utf-8"
        )
        first_file = _publish_progress(
            invocation, 1, _progress_payload(task_id, launched, epoch, 1)
        )
        run_workflow_runtime_tick(
            conn, controller_epoch=epoch, launcher=RecordingLauncher(), max_launch=0
        )
        assert not first_file.exists()
        first = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert first.last_evidence_digest
        first_expiry = first.claim_expires

        duplicate_file = _publish_progress(
            invocation, 2, _progress_payload(task_id, launched, epoch, 2)
        )
        outcomes = ingest_worker_progress(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert outcomes[0].reason == "duplicate_evidence" and not outcomes[0].accepted
        assert not duplicate_file.exists()
        assert (
            kb.latest_run(conn, task_id, allow_execution_leaf=True).claim_expires
            == first_expiry
        )

        (repo / "src" / "feature.py").write_text(
            "class Feature:\n    progress = 2\n", encoding="utf-8"
        )
        _publish_progress(invocation, 3, _progress_payload(task_id, launched, epoch, 3))
        outcomes = ingest_worker_progress(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert outcomes[0].accepted and outcomes[0].reason == "evidence_accepted"
        rows = conn.execute(
            "SELECT sequence, outcome FROM workflow_progress_proposals WHERE run_id=? ORDER BY sequence",
            (launched.run_id,),
        ).fetchall()
        assert [(row["sequence"], row["outcome"]) for row in rows] == [
            (1, "evidence_accepted"),
            (2, "duplicate_evidence"),
            (3, "evidence_accepted"),
        ]


def test_progress_published_before_expiry_renews_after_poll_delay(runtime, monkeypatch):
    repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=51009)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        assert launched.run_id is not None and launched.fence is not None
        (repo / "src" / "feature.py").write_text(
            "class Feature:\n    timely = True\n", encoding="utf-8"
        )
        path = _publish_progress(
            launcher.invocations[0],
            1,
            _progress_payload(task_id, launched, epoch, 1),
        )
        info = path.stat()
        expiry = int(max(info.st_mtime, info.st_ctime))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires=? WHERE id=?", (expiry, task_id)
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires=? WHERE id=?",
                (expiry, launched.run_id),
            )
        monkeypatch.setattr(workflow_runtime.time, "time", lambda: expiry + 2)

        outcomes = ingest_worker_progress(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )

        assert outcomes[0].accepted and outcomes[0].reason == "evidence_accepted"
        assert not path.exists()
        refreshed = kb.get_task(conn, task_id)
        assert refreshed is not None and refreshed.claim_expires is not None
        assert refreshed.claim_expires > expiry + 2


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"fence": "stale"}, "invalid_progress_proposal"),
        ({"controller_epoch": "old"}, "invalid_progress_proposal"),
        ({"state": "reviewing"}, "invalid_progress_proposal"),
        ({"run_id": 999}, "invalid_progress_proposal"),
    ],
)
def test_progress_channel_rejects_wrong_fence_epoch_run_or_state(
    runtime, mutation, expected
):
    _repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=51002)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        path = _publish_progress(
            launcher.invocations[0],
            1,
            _progress_payload(task_id, launched, epoch, 1, **mutation),
        )
        outcome = ingest_worker_progress(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )[0]
        assert outcome.reason == expected and not outcome.accepted
        assert path.exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_progress_proposals WHERE run_id=?",
                (launched.run_id,),
            ).fetchone()[0]
            == 0
        )


def test_progress_channel_rejects_symlink_non_utf8_and_oversize(runtime, tmp_path):
    _repo, task_id, epoch, _spec = runtime
    launcher = RecordingLauncher(pid=51003)
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=launcher
        )
        directory = Path(launcher.invocations[0].progress_directory)
        directory.mkdir(parents=True)
        target = tmp_path / "target.json"
        target.write_text(
            json.dumps(_progress_payload(task_id, launched, epoch, 1)), encoding="utf-8"
        )
        (directory / "progress-1.json").symlink_to(target)
        (directory / "progress-2.json").write_bytes(b"\xff\xfe")
        (directory / "progress-3.json").write_bytes(b"{" + b"x" * (16 * 1024) + b"}")
        outcomes = ingest_worker_progress(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
        )
        assert [outcome.reason for outcome in outcomes] == [
            "invalid_progress_proposal",
            "invalid_progress_proposal",
            "invalid_progress_proposal",
        ]


def _prepare_approved_closeout(
    conn, repo, task_id, epoch, *, pid=52001, required_ci=False
):
    launched = dispatch_execution_leaf(
        conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=pid)
    )
    candidate_sha = _commit_worker_change(repo)
    assert submit_result_proposal(
        conn,
        task_id,
        run_id=launched.run_id,
        fence=launched.fence,
        controller_epoch=epoch,
        proposal={"status": "done", "summary": "ready"},
    ).accepted
    coordinate = begin_review_closeout(
        conn,
        task_id,
        run_id=launched.run_id,
        fence=launched.fence,
        controller_epoch=epoch,
        required_ci=required_ci,
    )
    assert record_review_verdict(
        conn,
        task_id,
        run_id=launched.run_id,
        fence=launched.fence,
        controller_epoch=epoch,
        reviewer="reviewer",
        candidate_sha=candidate_sha,
        diff_digest=coordinate.diff_digest,
        checklist={"scope": True},
        approved=True,
    ).accepted
    if required_ci:
        assert record_ci_result(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            candidate_sha=candidate_sha,
            check_suite="protected",
            conclusion="success",
        ).accepted
    return launched, coordinate


@pytest.mark.parametrize(
    ("state", "reason"),
    [("alive", "worker_still_alive"), ("unknown", "worker_state_unknown")],
)
def test_closeout_keeps_pid_and_reservation_when_worker_not_confirmed_dead(
    runtime, state, reason
):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched, _coordinate = _prepare_approved_closeout(conn, repo, task_id, epoch)
        outcome = close_reviewed_leaf(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            process_inspector=_Inspector(state),
        )
        assert not outcome.accepted and outcome.reason == reason
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task.status == "review" and task.worker_pid == 52001
        assert task.claim_lock == launched.fence
        assert run.status == "reviewing" and run.worker_pid == 52001
        assert (
            run.quarantine_reason == reason and run.failure_class == "scope_ambiguity"
        )


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_closeout_quarantines_dirty_tracked_or_untracked_workspace(runtime, dirty_kind):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched, _coordinate = _prepare_approved_closeout(conn, repo, task_id, epoch)
        if dirty_kind == "tracked":
            (repo / "src" / "feature.py").write_text(
                "dirty tracked\n", encoding="utf-8"
            )
        else:
            (repo / "untracked.txt").write_text("dirty untracked\n", encoding="utf-8")
        outcome = close_reviewed_leaf(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            process_inspector=_Inspector("dead"),
        )
        assert not outcome.accepted and outcome.reason == "workspace_dirty"
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task.status == "review" and task.worker_pid == 52001
        assert (
            run.quarantine_reason == "workspace_dirty"
            and run.failure_class == "content"
        )


def test_confirmed_dead_clean_closeout_releases_and_clears_pid(runtime):
    repo, task_id, epoch, _spec = runtime
    inspector = _Inspector("dead")
    with kb.connect() as conn:
        launched, _coordinate = _prepare_approved_closeout(conn, repo, task_id, epoch)
        outcome = close_reviewed_leaf(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            process_inspector=inspector,
        )
        assert outcome.accepted and outcome.status == "done"
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert task.worker_pid is None and task.claim_lock is None
        assert run.worker_pid is None and run.claim_lock is None
        assert inspector.identities[0]["process_start_identity"] == "synthetic:52001"


def test_production_coordinator_ingests_reviews_ci_projects_and_closes(runtime):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=53001)
        )
        _commit_worker_change(repo)
        assert submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            proposal={"status": "done", "summary": "ready"},
        ).accepted

        snapshot = GitHubSnapshot(
            repository_node_id="R",
            issue_node_id="I",
            project_item_id="P",
            source_updated_at="v1",
            source_version="etag-1",
            issue={"state": "OPEN"},
            project={"status": "Review"},
            pull_requests=(),
        )

        class Adapter:
            enabled = True

            def __init__(self):
                self.status = "Review"
                self.updated_at = "v1"
                self.coordinates = []

            def fetch_snapshot(self, task):
                return snapshot

            def requires_protected_ci(self, task, observed):
                return True

            def verify_review(self, task, coordinate):
                self.coordinates.append((
                    coordinate.candidate_sha,
                    coordinate.diff_digest,
                ))
                return ReviewObservation(
                    "independent",
                    coordinate.candidate_sha,
                    coordinate.diff_digest,
                    {"scope": True, "tests": True},
                )

            def protected_ci(self, task, coordinate):
                return CIObservation(coordinate.candidate_sha, "protected", "success")

            def projection_request(self, task, observed, coordinate):
                return ProjectionRequest("Done", observed.source_updated_at)

            def write_status(self, *, project_item_id, status, expected_updated_at):
                assert (project_item_id, expected_updated_at) == ("P", "v1")
                self.status, self.updated_at = status, "v2"

            def read_status(self, *, project_item_id):
                return {"status": self.status, "updated_at": self.updated_at}

        adapter = Adapter()
        run_workflow_runtime_tick(
            conn,
            controller_epoch=epoch,
            launcher=RecordingLauncher(),
            max_launch=0,
            coordinator=WorkflowProductionCoordinator(
                adapter, process_inspector=_Inspector("dead")
            ),
        )
        assert kb.get_task(conn, task_id).status == "done"
        assert len(adapter.coordinates) == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_github_snapshots WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )
        projection = conn.execute(
            "SELECT status, verified FROM workflow_github_projections WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert (projection["status"], projection["verified"]) == ("Done", 1)


def test_production_coordinator_records_typed_adapter_failure(runtime):
    repo, task_id, epoch, _spec = runtime
    with kb.connect() as conn:
        launched = dispatch_execution_leaf(
            conn, task_id, controller_epoch=epoch, launcher=RecordingLauncher(pid=53002)
        )
        _commit_worker_change(repo)
        assert submit_result_proposal(
            conn,
            task_id,
            run_id=launched.run_id,
            fence=launched.fence,
            controller_epoch=epoch,
            proposal={"status": "done", "summary": "ready"},
        ).accepted

        class Adapter:
            enabled = True

            def fetch_snapshot(self, task):
                return GitHubSnapshot(
                    "R",
                    "I",
                    "P",
                    "v1",
                    "etag",
                    {"state": "OPEN"},
                    {"status": "Review"},
                    (),
                )

            def requires_protected_ci(self, task, snapshot):
                return False

            def verify_review(self, task, coordinate):
                return ReviewObservation(
                    "reviewer", "0" * 40, coordinate.diff_digest, {"scope": True}
                )

            def protected_ci(self, task, coordinate):
                return None

            def projection_request(self, task, snapshot, coordinate):
                return None

            def write_status(self, **kwargs):
                raise AssertionError("projection must not run")

            def read_status(self, **kwargs):
                raise AssertionError("projection must not run")

        run_workflow_runtime_tick(
            conn,
            controller_epoch=epoch,
            launcher=RecordingLauncher(),
            max_launch=0,
            coordinator=WorkflowProductionCoordinator(Adapter()),
        )
        count = conn.execute(
            "SELECT count FROM workflow_failure_counts WHERE task_id=? AND failure_class='content'",
            (task_id,),
        ).fetchone()
        assert count["count"] == 1
        run = kb.latest_run(conn, task_id, allow_execution_leaf=True)
        assert run.failure_class == "content"
