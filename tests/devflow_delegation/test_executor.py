import json
import subprocess
from pathlib import Path

import pytest

from devflow_delegation.allowlist import Allowlist, TargetConfig
from devflow_delegation.contract import parse_request
from devflow_delegation.executor import run_executor_tick
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import transition


class FakePrClient:
    def __init__(self, result=None):
        self.result = result if result is not None else {"number": 42, "url": "https://example.test/pr/42"}
        self.calls = []

    def create_pr(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _run(args, *, cwd, env=None):
    result = subprocess.run(
        args, cwd=str(cwd), env=env, shell=False, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _fixture_repo(tmp_path):
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _run(["git", "init", "--initial-branch", "main"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.test"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "src/seed.txt"], cwd=repo)
    _run(["git", "commit", "-m", "seed"], cwd=repo)
    bare = tmp_path / "fixture-remote.git"
    _run(["git", "init", "--bare", str(bare)], cwd=repo)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=repo)
    _run(["git", "push", "-u", "origin", "main"], cwd=repo)
    return repo


def _target(repo, tmp_path, *, command, **overrides):
    values = dict(
        repo="fixture",
        checkout_path=str(repo),
        default_branch="main",
        remote="origin",
        allowed_globs=("src/**",),
        denied_globs=("**/.env", "secrets/**"),
        worktree_base=str(tmp_path / "worktrees"),
        test_commands=(("python", "-c", "print('tests passed')"),),
        command_timeout_seconds=10,
        required_checks=("test",),
        risk_ceiling="medium",
        max_autonomous_action="create_pr",
        executor_enabled=True,
        synthetic_fixture=True,
        implementation_command=command,
        github_repo="example/fixture",
        live_gateway_imports=False,
    )
    values.update(overrides)
    return TargetConfig(**values)


def _request(*, source_kind="explicit", severity="low"):
    return parse_request({
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "test:fixture:v1",
        "source": {"agent": "operator", "kind": source_kind, "finding_id": "fixture-1"},
        "kind": "task",
        "title": "Update synthetic fixture",
        "problem_statement": "Exercise the isolated executor.",
        "evidence": [{"kind": "test", "summary": "synthetic"}],
        "target": {"repo": "fixture", "subsystem": "src"},
        "severity": severity,
        "priority": "P3",
        "confidence": 1.0,
        "acceptance_criteria": ["A scoped fixture edit exists"],
        "safety_notes": ["Synthetic only"],
    })


def _planned_ledger(tmp_path, **request_overrides):
    ledger = DelegationLedger(tmp_path / "devflow" / "ledger.db")
    request = _request(**request_overrides)
    ledger.insert_request(request)
    transition(ledger, None, request.request_id, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(
        request.request_id, "operator", "approve", "fixture setup", f"token-{request.request_id}"
    )
    transition(ledger, None, request.request_id, "PLANNED", actor="operator")
    return ledger, request


def _allowlist(target):
    return Allowlist(version="test", targets={"fixture": target})


def _write_source_command():
    return (
        "python", "-c",
        "import os,json,pathlib; p=pathlib.Path(os.environ['DDP_REQUEST_PATH']); "
        "assert json.loads(p.read_text())['request_id']; "
        "pathlib.Path('src/generated.txt').write_text('generated\\n'); print('implemented')",
    )


def test_shadow_runs_without_a_pr_client_to_validated(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    client = FakePrClient()

    # Default mode is shadow; pass the client to prove it is never used in shadow.
    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert ledger.lease_for_request(request.request_id) is None
    assert client.calls == []  # no push, no PR in shadow
    kinds = {item["kind"] for item in ledger.artifacts_for(request.request_id)}
    assert {"branch", "worktree", "validation", "shadow"} <= kinds
    assert "pr" not in kinds and "pr_number" not in kinds
    shadow = next(i["ref"] for i in ledger.artifacts_for(request.request_id) if i["kind"] == "shadow")
    assert shadow.startswith("paths=1 lines=")
    assert "branch=ddp-" in shadow
    assert "title=Update synthetic fixture" in shadow


def test_shadow_artifact_leaks_no_absolute_path(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None)

    shadow = next(i["ref"] for i in ledger.artifacts_for(request.request_id) if i["kind"] == "shadow")
    assert str(tmp_path) not in shadow
    assert ":\\" not in shadow and "/worktrees/" not in shadow


def test_unknown_mode_is_a_safe_noop(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), mode="bogus",
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"


def test_executor_advances_an_explicit_synthetic_request_to_merge_pending(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    client = FakePrClient()

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None, pr_client=client, mode="canary")

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "MERGE_PENDING"
    assert ledger.lease_for_request(request.request_id) is None
    assert client.calls[0]["repo"] == "example/fixture"
    assert client.calls[0]["branch"].startswith("ddp-")
    kinds = {item["kind"] for item in ledger.artifacts_for(request.request_id)}
    assert {"branch", "worktree", "validation", "pr", "pr_number"} <= kinds
    assert not Path(_target(repo, tmp_path, command=_write_source_command()).worktree_base).exists() or not list(Path(_target(repo, tmp_path, command=_write_source_command()).worktree_base).iterdir())


def test_executor_refuses_live_or_disabled_targets_without_a_lease(tmp_path, monkeypatch):
    from events import paths

    live_root = tmp_path / "live"
    live_root.mkdir()
    monkeypatch.setattr(paths, "get_default_hermes_root", lambda: live_root)
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    target = _target(repo, tmp_path, command=_write_source_command(), executor_enabled=False)

    result = run_executor_tick(ledger, _allowlist(target), None, pr_client=FakePrClient())

    assert result["processed"] == 0
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"
    assert ledger.lease_for_request(request.request_id) is None


def test_executor_skips_ineligible_before_eligible_work(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, rejected = _planned_ledger(tmp_path, source_kind="arch-review")
    accepted = _request()
    accepted.idempotency_key = "test:accepted:v1"
    ledger.insert_request(accepted)
    transition(ledger, None, accepted.request_id, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(
        accepted.request_id, "operator", "approve", "fixture setup", f"token-{accepted.request_id}"
    )
    transition(ledger, None, accepted.request_id, "PLANNED", actor="operator")

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None, pr_client=FakePrClient(), mode="canary")

    assert result["processed"] == 1
    # Selection scans every bounded PLANNED row; newest-first ledger ordering
    # may locate the eligible row before the ineligible one, but the latter
    # must never prevent eligible work from running.
    assert result["skipped"] in {0, 1}
    assert ledger.get_request(rejected.request_id)["state"] == "PLANNED"
    assert ledger.get_request(accepted.request_id)["state"] == "MERGE_PENDING"


def test_executor_rejects_out_of_scope_changes_and_releases_lease(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    command = ("python", "-c", "from pathlib import Path; Path('outside.txt').write_text('bad'); print('implemented')")

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=command)), None, pr_client=FakePrClient(), mode="canary")

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    assert ledger.lease_for_request(request.request_id) is None


def test_executor_requires_a_real_pr_before_pr_open(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient({}), mode="canary",
    )

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    assert "PR_OPEN" not in [item["to_state"] for item in ledger.transitions_for(request.request_id)]


def test_canary_requires_a_pr_client_before_any_mutation(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        mode="canary",
    )

    assert result["processed"] == 0
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"
    assert ledger.lease_for_request(request.request_id) is None


def test_executor_rejects_above_target_risk_ceiling(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path, severity="critical")

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None, pr_client=FakePrClient())

    assert result["processed"] == 0
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"


def test_executor_metadata_file_is_not_committed(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    observed = tmp_path / "observed.json"
    command = (
        "python", "-c",
        "import os,pathlib; src=pathlib.Path(os.environ['DDP_REQUEST_PATH']); "
        f"pathlib.Path(r'{observed}').write_text(src.read_text()); "
        "pathlib.Path('src/generated.txt').write_text('ok'); print('implemented')",
    )

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=command)), None, pr_client=FakePrClient(), mode="canary")

    assert result["errors"] == 0
    payload = json.loads(observed.read_text(encoding="utf-8"))
    assert payload["request_id"] == request.request_id
    assert payload["request"]["title"] == "Update synthetic fixture"
    refs = [item["ref"] for item in ledger.artifacts_for(request.request_id)]
    assert all(".ddp_request.json" not in ref for ref in refs)
