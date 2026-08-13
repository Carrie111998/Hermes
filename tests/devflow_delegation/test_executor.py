import json
import subprocess
from pathlib import Path

import pytest

from devflow_delegation.allowlist import Allowlist, TargetConfig
from devflow_delegation.contract import parse_request
from devflow_delegation.executor import ExecutorError, run_executor_tick
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


def _request(*, source_kind="explicit", severity="low", title="Update synthetic fixture"):
    return parse_request({
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "test:fixture:v1",
        "source": {"agent": "operator", "kind": source_kind, "finding_id": "fixture-1"},
        "kind": "task",
        "title": title,
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

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client, mode="canary", request_id=request.request_id,
    )

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
    # mode="canary" now requires a designated request_id (F6): auto-selection
    # across multiple PLANNED rows is exclusively a shadow-mode behavior.
    # This test exercises that selection/skip logic in shadow mode, which
    # still auto-selects and still exercises the same _target_is_eligible
    # skip path; it stops at VALIDATED instead of MERGE_PENDING because
    # shadow never pushes or opens a PR.
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

    result = run_executor_tick(ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None, pr_client=FakePrClient())

    assert result["processed"] == 1
    # Selection scans every bounded PLANNED row; newest-first ledger ordering
    # may locate the eligible row before the ineligible one, but the latter
    # must never prevent eligible work from running.
    assert result["skipped"] in {0, 1}
    assert ledger.get_request(rejected.request_id)["state"] == "PLANNED"
    assert ledger.get_request(accepted.request_id)["state"] == "VALIDATED"


def test_executor_rejects_out_of_scope_changes_and_releases_lease(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    command = ("python", "-c", "from pathlib import Path; Path('outside.txt').write_text('bad'); print('implemented')")

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=command)), None,
        pr_client=FakePrClient(), mode="canary", request_id=request.request_id,
    )

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    assert ledger.lease_for_request(request.request_id) is None


def test_executor_requires_a_real_pr_before_pr_open(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient({}), mode="canary", request_id=request.request_id,
    )

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    assert "PR_OPEN" not in [item["to_state"] for item in ledger.transitions_for(request.request_id)]


def test_canary_requires_a_pr_client_before_any_mutation(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        mode="canary", request_id=request.request_id,
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


def _canary_target(repo, tmp_path, *, command, **overrides):
    overrides.setdefault("pr_budget", 1)
    return _target(
        repo, tmp_path, command=command,
        synthetic_fixture=False, canary_real=True, risk_ceiling="low",
        **overrides,
    )


def test_canary_real_target_reaches_merge_pending_with_injected_client(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    client = FakePrClient()

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "MERGE_PENDING"
    assert client.calls[0]["repo"] == "example/fixture"
    kinds = {i["kind"] for i in ledger.artifacts_for(request.request_id)}
    assert {"pr", "pr_number"} <= kinds


def test_canary_real_still_refuses_the_live_checkout(tmp_path, monkeypatch):
    from events import paths

    repo = _fixture_repo(tmp_path)
    # Point the live Hermes root AT the fixture checkout so the refusal must fire.
    monkeypatch.setattr(paths, "get_default_hermes_root", lambda: repo)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), mode="canary", request_id=request.request_id,
    )

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    # Pin the actual refusal reason, not just the terminal state (F8): many
    # paths now reach FAILED (out-of-scope diff, missing PR result, lease
    # races, ...), so asserting only the state is one refactor away from a
    # false green on the most safety-critical property in this module.
    last_transition = ledger.transitions_for(request.request_id)[-1]
    assert last_transition["to_state"] == "FAILED"
    assert "refuses the live Hermes checkout" in (last_transition["evidence_ref"] or "")


def test_canary_real_shadow_records_shadow_and_opens_no_pr(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    client = FakePrClient()

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client,  # default shadow: client must stay unused
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert client.calls == []
    kinds = {i["kind"] for i in ledger.artifacts_for(request.request_id)}
    assert "shadow" in kinds and "pr" not in kinds


def test_non_explicit_source_real_target_is_skipped(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, rejected = _planned_ledger(tmp_path, source_kind="arch-review")

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), mode="canary", request_id=rejected.request_id,
    )

    assert result["processed"] == 0
    assert ledger.get_request(rejected.request_id)["state"] == "PLANNED"


def test_canary_pr_budget_is_fail_closed_after_one_pr(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, first = _planned_ledger(tmp_path)

    # A second explicit PLANNED request on the same target.
    second = _request()
    second.idempotency_key = "test:second:v1"
    ledger.insert_request(second)
    transition(ledger, None, second.request_id, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(
        second.request_id, "operator", "approve", "fixture setup", f"token-{second.request_id}"
    )
    transition(ledger, None, second.request_id, "PLANNED", actor="operator")

    allowlist = _allowlist(_canary_target(repo, tmp_path, command=_write_source_command()))

    # First canary consumes the budget of 1.
    r1 = run_executor_tick(ledger, allowlist, None, pr_client=FakePrClient(), mode="canary", request_id=first.request_id)
    assert r1["processed"] == 1
    assert ledger.get_request(first.request_id)["state"] == "MERGE_PENDING"

    # Second canary is refused with no transition (still PLANNED, no new PR artifact).
    r2 = run_executor_tick(ledger, allowlist, None, pr_client=FakePrClient(), mode="canary", request_id=second.request_id)
    assert r2 == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(second.request_id)["state"] == "PLANNED"
    assert [i["kind"] for i in ledger.artifacts_for(second.request_id)] == []


def test_shadow_ignores_pr_budget(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    # Pre-seed a PR artifact so the budget of 1 is already spent.
    ledger.add_artifact(request.request_id, "pr", "https://example.test/pr/already")

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
    )  # default shadow — budget must not block

    assert result["processed"] == 1
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"


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

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=command)), None,
        pr_client=FakePrClient(), mode="canary", request_id=request.request_id,
    )

    assert result["errors"] == 0
    payload = json.loads(observed.read_text(encoding="utf-8"))
    assert payload["request_id"] == request.request_id
    assert payload["request"]["title"] == "Update synthetic fixture"
    refs = [item["ref"] for item in ledger.artifacts_for(request.request_id)]
    assert all(".ddp_request.json" not in ref for ref in refs)


def test_canary_pr_body_and_label_carry_do_not_merge_marker(tmp_path):
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    client = FakePrClient()

    run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client, mode="canary", request_id=request.request_id,
    )

    call = client.calls[0]
    assert call["label"] == "devflow-canary"
    assert f"request-id: {request.request_id}" in call["body"]
    assert "Do not auto-merge" in call["body"]
    # No leakage: the body must not contain absolute paths or the worktree base.
    assert str(tmp_path) not in call["body"]


def test_concurrent_tick_cannot_mark_an_in_flight_request_failed(tmp_path):
    # F2 regression: the lease is the mutual-exclusion primitive, not the
    # BUILDING transition. Simulate a second, overlapping tick by acquiring
    # the request's lease directly first (as tick A would have), then
    # invoke run_executor_tick as tick B. Before the fix, B transitioned
    # PLANNED -> BUILDING (which is legal against the ledger's *state*
    # column, since that column knows nothing about leases) and only then
    # discovered the lease conflict via an uncaught sqlite3.IntegrityError
    # -- either stranding the row at BUILDING (unretryable) or, on a
    # slightly different race, marking A's in-flight request FAILED. B must
    # now back off as soon as it fails to acquire the lease, before ever
    # touching lifecycle state.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    held = ledger.acquire_lease(request.request_id, "other-worker", expires_in_seconds=600)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(),
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"
    # No BUILDING/FAILED transition was ever recorded by the losing tick;
    # only the setup transitions from _planned_ledger (TRIAGED, PLANNED) exist.
    assert [item["to_state"] for item in ledger.transitions_for(request.request_id)] == ["TRIAGED", "PLANNED"]
    # The original holder's lease is untouched by the losing tick.
    current_lease = ledger.lease_for_request(request.request_id)
    assert current_lease is not None
    assert current_lease["lease_id"] == held["lease_id"]


def test_shadow_run_survives_diff_line_count_failure(tmp_path, monkeypatch):
    # F3 regression: _diff_line_count is a cosmetic diagnostic that runs
    # AFTER the request is already VALIDATED. Any failure there (index
    # lock, timeout, odd path) must not turn an otherwise-successful shadow
    # run into a lost/FAILED one -- it must fall back to lines=-1 (unknown)
    # and still record the shadow artifact.
    import devflow_delegation.executor as executor_mod

    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    def _boom(*_args, **_kwargs):
        raise ExecutorError("git diff numstat failed (123): fatal: index lock")

    monkeypatch.setattr(executor_mod, "_diff_line_count", _boom)

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    shadow = next(i["ref"] for i in ledger.artifacts_for(request.request_id) if i["kind"] == "shadow")
    assert "lines=-1" in shadow


def test_synthetic_only_flag_excludes_canary_real_targets(tmp_path):
    # F4 regression: run_executor_tick(synthetic_only=True) -- what
    # `executor --synthetic-only` now passes -- must exclude canary_real
    # targets even when they are otherwise fully eligible, so the
    # "synthetic-only" CLI command can never touch a real target.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), synthetic_only=True,
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 1}
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"

    # The same request on a synthetic_fixture target IS eligible under the
    # same flag -- proving the exclusion is specific to canary_real, not a
    # blanket refusal.
    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), synthetic_only=True,
    )
    assert result == {"processed": 1, "errors": 0, "skipped": 0}


class _CreatesRemotePrThenRaises:
    """Simulates gh pr create succeeding, then gh pr view raising (F5)."""

    def create_pr(self, **kwargs):
        raise ExecutorError("gh pr view failed (1): fatal: no such ref")


def test_pr_attempt_artifact_preserves_budget_when_pr_client_raises(tmp_path):
    # F5 regression: if the PR client raises AFTER the real remote PR was
    # created (e.g. `gh pr create` succeeds but the follow-up `gh pr view`
    # fails), the ledger must still count that attempt against the budget --
    # otherwise a real, ledger-invisible PR would let a second canary open
    # another one against a budget that still reads as unspent.
    repo = _fixture_repo(tmp_path)
    ledger, first = _planned_ledger(tmp_path)
    second = _request()
    second.idempotency_key = "test:pr-attempt-second:v1"
    ledger.insert_request(second)
    transition(ledger, None, second.request_id, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(
        second.request_id, "operator", "approve", "fixture setup", f"token-{second.request_id}"
    )
    transition(ledger, None, second.request_id, "PLANNED", actor="operator")

    allowlist = _allowlist(_canary_target(repo, tmp_path, command=_write_source_command()))

    r1 = run_executor_tick(
        ledger, allowlist, None, pr_client=_CreatesRemotePrThenRaises(),
        mode="canary", request_id=first.request_id,
    )
    assert r1["errors"] == 1
    assert ledger.get_request(first.request_id)["state"] == "FAILED"
    kinds = {i["kind"] for i in ledger.artifacts_for(first.request_id)}
    assert "pr_attempt" in kinds and "pr" not in kinds

    # The second designated request is refused: the budget of 1 was already
    # consumed by the first request's pr_attempt, even though it has no
    # "pr" artifact.
    r2 = run_executor_tick(
        ledger, allowlist, None, pr_client=FakePrClient(), mode="canary", request_id=second.request_id,
    )
    assert r2 == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(second.request_id)["state"] == "PLANNED"


def test_canary_without_designated_request_id_is_a_safe_noop(tmp_path):
    # F6 regression: mode="canary" with request_id=None must never
    # auto-select a PLANNED row -- a real PR requires a DESIGNATED request.
    # The CLI already enforces --request-id, but this is defense-in-depth
    # for any direct caller of run_executor_tick.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=FakePrClient(), mode="canary",
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"
    assert ledger.lease_for_request(request.request_id) is None
    # No BUILDING/FAILED transition was ever recorded; only the setup
    # transitions from _planned_ledger (TRIAGED, PLANNED) exist.
    assert [item["to_state"] for item in ledger.transitions_for(request.request_id)] == ["TRIAGED", "PLANNED"]


def test_title_with_newlines_and_control_chars_is_sanitized_in_commit_and_pr_title(tmp_path):
    # H1 regression: the raw envelope title (producer-supplied free text) was
    # sanitized ONLY for the shadow artifact. It flowed RAW into the git
    # commit message and the canary PR title, so a newline/control character
    # in a title could land in both. _safe_title is now the ONE sanitizer
    # shared by all three surfaces.
    repo = _fixture_repo(tmp_path)
    dirty_title = "Bad title\nrm -rf /\x07\x1b[31mred\x1b[0m"
    ledger, request = _planned_ledger(tmp_path, title=dirty_title)
    client = FakePrClient()

    result = run_executor_tick(
        ledger, _allowlist(_canary_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}

    # 1) PR title carries no raw newline or control character.
    pr_title = client.calls[0]["title"]
    assert "\n" not in pr_title
    assert "\x07" not in pr_title and "\x1b" not in pr_title
    assert pr_title.startswith("Bad title rm -rf /")

    # 2) The pushed commit's message (on the bare remote, since the local
    # worktree/branch are removed by the executor's cleanup) carries no raw
    # newline injected from the title and no control characters either.
    branch = next(i["ref"] for i in ledger.artifacts_for(request.request_id) if i["kind"] == "branch")
    bare = tmp_path / "fixture-remote.git"
    log = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "-1", "--format=%s", branch],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert log.returncode == 0, log.stderr
    subject = log.stdout.strip()
    assert subject == "[ddp] Bad title rm -rf /[31mred[0m"
    assert "\x07" not in subject and "\x1b" not in subject


def test_canary_resumes_a_shadow_validated_request_and_opens_one_pr(tmp_path):
    # Headline flow (the bug this task fixes): a shadow tick advances a
    # request to VALIDATED and records a `shadow` artifact; a SUBSEQUENT
    # canary tick, designated at that same request_id, must be able to pick
    # it back up, rebuild + revalidate, and open exactly one PR.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    shadow_result = run_executor_tick(ledger, allowlist, None)  # default mode="shadow"
    assert shadow_result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert any(item["kind"] == "shadow" for item in ledger.artifacts_for(request.request_id))

    client = FakePrClient()
    result = run_executor_tick(
        ledger, allowlist, None, pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "MERGE_PENDING"
    assert len(client.calls) == 1  # exactly one PR opened
    kinds = {item["kind"] for item in ledger.artifacts_for(request.request_id)}
    assert {"pr", "pr_number"} <= kinds
    prs = [item for item in ledger.artifacts_for(request.request_id) if item["kind"] == "pr"]
    assert len(prs) == 1


def test_canary_does_not_resume_a_validated_request_without_a_shadow_artifact(tmp_path):
    # Fail-closed: VALIDATED alone is not proof of shadow verification. Force
    # VALIDATED WITHOUT ever running a shadow tick (so no shadow artifact
    # exists) and confirm canary refuses to touch it.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    transition(ledger, None, request.request_id, "BUILDING", actor="operator")
    transition(ledger, None, request.request_id, "VALIDATED", actor="operator")
    assert ledger.artifacts_for(request.request_id) == []

    client = FakePrClient()
    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=_write_source_command())), None,
        pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert client.calls == []
    assert ledger.lease_for_request(request.request_id) is None


def test_shadow_mode_does_not_resume_a_shadow_verified_validated_request(tmp_path):
    # Resume is canary-only: a shadow tick designated at a request that is
    # already VALIDATED (with a shadow artifact from an earlier shadow tick)
    # must be a safe no-op, never re-entering the pipeline.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"

    result = run_executor_tick(ledger, allowlist, None, request_id=request.request_id)  # shadow again

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"


def test_canary_without_request_id_does_not_resume_a_validated_request(tmp_path):
    # Resume is designated-only: mode="canary" with request_id=None must
    # never auto-select a VALIDATED (shadow-verified) row either, matching
    # the existing PLANNED behavior asserted by
    # test_canary_without_designated_request_id_is_a_safe_noop.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"

    result = run_executor_tick(ledger, allowlist, None, pr_client=FakePrClient(), mode="canary")

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"


def test_canary_resume_still_refused_when_pr_budget_exhausted(tmp_path):
    # The durable per-window PR budget precheck applies identically on the
    # resume path: an exhausted budget must refuse with no transition.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    ledger.add_artifact(request.request_id, "pr", "https://example.test/pr/already")  # spend budget of 1

    client = FakePrClient()
    result = run_executor_tick(
        ledger, allowlist, None, pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert client.calls == []


def test_canary_resume_still_refuses_the_live_checkout(tmp_path, monkeypatch):
    # The live-Hermes-checkout refusal in _validate_target_boundary applies
    # identically on the resume path, and the request must land at FAILED
    # (not stranded at VALIDATED with no failure record) with the resume
    # never having (illegally) touched BUILDING first.
    from events import paths

    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"

    monkeypatch.setattr(paths, "get_default_hermes_root", lambda: repo)

    result = run_executor_tick(
        ledger, allowlist, None, pr_client=FakePrClient(), mode="canary", request_id=request.request_id,
    )

    assert result["errors"] == 1
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    last_transition = ledger.transitions_for(request.request_id)[-1]
    assert last_transition["to_state"] == "FAILED"
    assert last_transition["from_state"] == "VALIDATED"
    assert "refuses the live Hermes checkout" in (last_transition["evidence_ref"] or "")


def test_canary_does_not_resume_a_validated_request_with_a_pr_attempt_artifact(tmp_path):
    # Fix A: a VALIDATED + shadow row that also carries `pr_attempt` evidence
    # (durably recorded immediately before an earlier canary tick invoked the
    # PR client -- see the comment on
    # DelegationLedger.count_prs_for_target_since) must NOT be resumable.
    # Resuming past that risks a second, duplicate PR for the same request if
    # the earlier tick was hard-killed between recording pr_attempt and
    # transitioning to PR_OPEN. pr_budget=2 isolates this from the separate
    # budget precheck: budget alone (count=1 < 2) would NOT block this tick,
    # so only the resumability predicate can be doing the refusing.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_canary_target(repo, tmp_path, command=_write_source_command(), pr_budget=2))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    ledger.add_artifact(request.request_id, "pr_attempt", "ddp-some-branch-a1")

    client = FakePrClient()
    result = run_executor_tick(
        ledger, allowlist, None, pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert client.calls == []  # no PR client call -- never even reached create_pr
    assert ledger.lease_for_request(request.request_id) is None


def test_canary_does_not_resume_a_validated_request_with_a_pr_artifact(tmp_path):
    # Same as above for a `pr` artifact (the request already reached a real,
    # ledger-recorded PR in an earlier canary run).
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_canary_target(repo, tmp_path, command=_write_source_command(), pr_budget=2))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    ledger.add_artifact(request.request_id, "pr", "https://example.test/pr/already")

    client = FakePrClient()
    result = run_executor_tick(
        ledger, allowlist, None, pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 0, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    assert client.calls == []
    assert ledger.lease_for_request(request.request_id) is None


def test_canary_resume_refuses_when_state_changes_between_selection_and_lease(tmp_path):
    # Fix B: on the fresh path, transition(..., "BUILDING") doubles as the
    # first authoritative re-read of the request's state after the lease is
    # held -- it raises IllegalTransitionError if the row moved under the
    # candidate snapshot. The resume path skips that transition (VALIDATED
    # -> BUILDING is not a legal edge), so without a restored post-lease
    # check it would trust a pre-lease snapshot straight through the push
    # and PR creation. Simulate the race by mutating the row's state OUT
    # from under the tick inside a patched acquire_lease -- i.e. exactly
    # between candidate selection (a stale VALIDATED+shadow snapshot) and
    # the lease-held re-read Fix B restores.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    allowlist = _allowlist(_target(repo, tmp_path, command=_write_source_command()))

    run_executor_tick(ledger, allowlist, None)  # shadow -> VALIDATED + shadow artifact
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"

    real_acquire_lease = ledger.acquire_lease

    def _acquire_then_mutate(*args, **kwargs):
        lease = real_acquire_lease(*args, **kwargs)
        # Simulate another process moving the row out of VALIDATED in the
        # window between the candidate snapshot and the lease-held read.
        ledger.set_state(request.request_id, "FAILED", terminal_reason="external")
        return lease

    ledger.acquire_lease = _acquire_then_mutate

    client = FakePrClient()
    result = run_executor_tick(
        ledger, allowlist, None, pr_client=client, mode="canary", request_id=request.request_id,
    )

    assert result == {"processed": 1, "errors": 1, "skipped": 0}
    # Must refuse BEFORE any push or PR-client call, not merely fail later.
    assert client.calls == []
    assert ledger.get_request(request.request_id)["state"] == "FAILED"
    assert ledger.lease_for_request(request.request_id) is None


def test_diff_line_count_reflects_a_fully_deleted_tracked_file(tmp_path):
    # H3 regression: `git add -N` (intent-to-add) stages a full DELETION of
    # an already-tracked file, so a subsequent UNSTAGED `git diff --numstat`
    # (no HEAD) compares an already-empty index entry to an already-absent
    # worktree file and reports NOTHING for it -- silently undercounting
    # deleted lines in the shadow artifact's diagnostic. Diffing against HEAD
    # picks up the deletion correctly.
    repo = _fixture_repo(tmp_path)
    ledger, request = _planned_ledger(tmp_path)
    # src/seed.txt ("seed\n", 1 line) is deleted outright by the
    # implementation command -- no replacement file is written.
    command = ("python", "-c", "import os; os.remove('src/seed.txt'); print('implemented')")

    result = run_executor_tick(
        ledger, _allowlist(_target(repo, tmp_path, command=command)), None,
    )

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    shadow = next(i["ref"] for i in ledger.artifacts_for(request.request_id) if i["kind"] == "shadow")
    # 1 line removed, 0 added -> lines=1. The pre-fix implementation reports
    # lines=0 here because the unstaged diff sees no difference once the
    # deletion is already staged by `git add -N`.
    assert "lines=1" in shadow
