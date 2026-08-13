import json

import pytest

from devflow_delegation import cli
from tests.devflow_delegation.conftest import make_delegate_kwargs


@pytest.fixture
def queue_mode(hermes_root, allowlist_file):
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"critic": {"mode": "queue"}}), encoding="utf-8")
    return hermes_root


def test_delegate_subcommand_queues_and_prints_result(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    rc = cli.main(["delegate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status=queued" in out and "request_id=dwr_" in out


def test_delegate_dry_run_flag(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    rc = cli.main(["delegate", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reason=dry_run" in out


def test_delegate_bad_json_exits_2(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{nope"))
    assert cli.main(["delegate"]) == 2


def test_status_subcommand(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "total=1" in out


def test_reconcile_subcommand_runs(queue_mode, capsys):
    rc = cli.main(["reconcile"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "adopted=" in out and "rewritten=" in out


def test_transition_subcommand(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    rid = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    rc = cli.main(["transition", "--request-id", rid, "--to", "TRIAGED", "--actor", "test"])
    assert rc == 0
    assert "TRIAGED" in capsys.readouterr().out


def test_transition_cli_passes_preread_state_as_expected_state(
    queue_mode, capsys, monkeypatch
):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    rid = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    observed = {}

    def capture_transition(_ledger, _bus, request_id, to_state, **kwargs):
        observed.update(request_id=request_id, to_state=to_state, **kwargs)
        return to_state

    monkeypatch.setattr(cli, "transition", capture_transition)
    assert cli.main([
        "transition", "--request-id", rid, "--to", "TRIAGED", "--actor", "test"
    ]) == 0
    assert observed["expected_from_state"] == "REQUESTED"


def test_transition_illegal_exits_2(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    rid = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    rc = cli.main(["transition", "--request-id", rid, "--to", "MERGED", "--actor", "test"])
    assert rc == 2


@pytest.mark.parametrize(("to_state", "gateway_command"), (
    ("PLANNED", "/ddp-approve"),
    ("DECLINED", "/ddp-decline"),
))
def test_transition_cli_rejects_unauthenticated_human_decision_edges(
    queue_mode, capsys, monkeypatch, to_state, gateway_command
):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition

    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="fixture")

    rc = cli.main(["transition", "--request-id", rid, "--to", to_state, "--actor", "test"])

    assert rc == 2
    assert gateway_command in capsys.readouterr().err
    assert ledger.get_request(rid)["state"] == "TRIAGED"
    assert [item["to_state"] for item in ledger.transitions_for(rid)] == ["TRIAGED"]
    assert ledger.human_decision_for(rid, "test") is None


def _requested_env(created_at, key, title):
    from devflow_delegation import contract

    payload = {
        "schema_version": contract.SCHEMA_VERSION,
        "type": contract.MSG_TYPE,
        "idempotency_key": key,
        "source": {"agent": "critic", "kind": "critic", "finding_id": "F"},
        "kind": "bug",
        "title": title,
        "problem_statement": "Health query scans all sessions without a LIMIT.",
        "evidence": [{"kind": "test_failure", "ref": "r", "summary": "s"}],
        "target": {"repo": "hermes", "subsystem": "gateway-health"},
        "severity": "high",
        "priority": "P1",
        "confidence": 0.94,
        "acceptance_criteria": ["a"],
        "safety_notes": [],
    }
    env = contract.parse_request(payload).to_envelope()
    env["created_at"] = created_at
    return env


def test_status_reports_oldest_requested_row(queue_mode, capsys):
    # Positive control for the "oldest REQUESTED" clause: inject two REQUESTED
    # rows with distinct created_at, then assert status surfaces the OLDER one.
    # If status used the DESC list_requests(limit=1), it would print the 2021
    # row and this would fail.
    from devflow_delegation.emitter import DelegationEmitter

    em = DelegationEmitter()
    em.ledger.adopt_envelope(_requested_env("2021-01-01T00:00:00+00:00", "k-new", "Newer problem"))
    old = _requested_env("2020-01-01T00:00:00+00:00", "k-old", "Older problem")
    em.ledger.adopt_envelope(old)

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "oldest_requested=2020-01-01T00:00:00+00:00" in out
    assert f"request_id={old['request_id']}" in out


def test_delegate_bad_kwargs_exits_2(queue_mode, capsys, monkeypatch):
    # delegate() raises TypeError on an unknown kwarg; the handler must map that
    # to exit 2 (bad input), NOT the catch-all exit 1. Positive control: without
    # the `except TypeError` branch the error would fall through to main()'s
    # `except Exception` and return 1.
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"not_a_real_kwarg": 1})))
    assert cli.main(["delegate"]) == 2


def test_executor_cli_requires_synthetic_only_flag(queue_mode, capsys):
    assert cli.main(["executor"]) == 2
    assert "--synthetic-only is required" in capsys.readouterr().err


def test_synthetic_executor_cli_has_no_pr_authority(queue_mode, capsys):
    # The production CLI intentionally supplies no PR client. A synthetic flag
    # can expose status/no-op behavior but cannot create a remote PR.
    assert cli.main(["executor", "--synthetic-only"]) == 0
    out = capsys.readouterr().out
    assert "mode=shadow" in out
    assert "pr_client=none" in out


def test_executor_cli_forwards_synthetic_only_to_the_tick(queue_mode, capsys, monkeypatch):
    # F4 regression: --synthetic-only required the flag but did not honor
    # it -- run_executor_tick was called with no target-posture filter, so
    # a canary_real target would have been processed. Prove the CLI now
    # forwards synthetic_only=True (the underlying filtering behavior is
    # covered directly on run_executor_tick in test_executor.py).
    import devflow_delegation.executor as executor_mod

    captured = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return {"processed": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(executor_mod, "run_executor_tick", _capture)
    assert cli.main(["executor", "--synthetic-only"]) == 0
    assert captured.get("synthetic_only") is True
    assert captured.get("mode") == "shadow"
    assert captured.get("pr_client") is None


def test_executor_shadow_cli_runs_without_pr_authority(queue_mode, capsys):
    # No eligible canary target in the SAMPLE allowlist (hermes is live_gateway) ->
    # a safe no-op that never constructs a PR client.
    assert cli.main(["executor-shadow"]) == 0
    out = capsys.readouterr().out
    assert "mode=shadow" in out
    assert "processed=0" in out


def test_executor_canary_cli_refuses_without_understanding_flag(queue_mode, capsys, monkeypatch):
    import devflow_delegation.executor as executor_mod

    def _boom(*a, **k):
        raise AssertionError("GhPrClient must not be constructed without the flag")

    monkeypatch.setattr(executor_mod, "GhPrClient", _boom)
    rc = cli.main(["executor-canary", "--request-id", "dwr_x"])
    assert rc == 2
    assert "--i-understand-this-opens-a-real-pr" in capsys.readouterr().err


def test_executor_canary_cli_requires_request_id(queue_mode, capsys):
    rc = cli.main(["executor-canary", "--i-understand-this-opens-a-real-pr"])
    assert rc == 2
    assert "--request-id" in capsys.readouterr().err


def test_executor_canary_cli_unknown_request_id_fails_loudly(queue_mode, capsys, monkeypatch):
    # H2: an unknown --request-id used to fall through to run_executor_tick,
    # which found no matching PLANNED row and returned a silent processed=0
    # -- indistinguishable from "no eligible target". That is a footgun at
    # exactly the moment a canary is being run, so this now refuses BEFORE
    # the tick (and before GhPrClient is ever constructed) with exit 2 and a
    # diagnostic naming the unknown id. This intentionally supersedes the
    # prior "safe no-op" expectation for this case; the wrong-state case
    # (see the next test) is refused the same way.
    import devflow_delegation.executor as executor_mod

    def _boom(*_a, **_k):
        raise AssertionError("unknown request -> GhPrClient must never be constructed")

    monkeypatch.setattr(executor_mod, "GhPrClient", _boom)
    rc = cli.main([
        "executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", "dwr_missing",
    ])
    assert rc == 2
    assert "unknown request: dwr_missing" in capsys.readouterr().err


def test_executor_canary_cli_validated_without_shadow_artifact_fails_loudly(queue_mode, capsys, monkeypatch):
    # H2 (updated for the bounded canary resume): a designated request that
    # is VALIDATED WITHOUT a shadow artifact was never shadow-verified by
    # this executor, so it must still fail loudly rather than silently
    # reporting processed=0 or being silently resumed. This is fail-closed:
    # VALIDATED alone is not proof of shadow verification.
    import devflow_delegation.executor as executor_mod
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(rid, "operator", "approve", "fixture setup", f"token-{rid}")
    transition(ledger, None, rid, "PLANNED", actor="operator")
    # Force VALIDATED WITHOUT ever running a shadow tick (set_state here is
    # only test setup -- no shadow artifact is ever recorded).
    ledger.set_state(rid, "VALIDATED")
    assert ledger.artifacts_for(rid) == []

    def _boom(*_a, **_k):
        raise AssertionError("VALIDATED-without-shadow request -> GhPrClient must never be constructed")

    monkeypatch.setattr(executor_mod, "GhPrClient", _boom)
    rc = cli.main([
        "executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", rid,
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"request {rid} is VALIDATED but was not shadow-verified" in err
    assert ledger.get_request(rid)["state"] == "VALIDATED"


def test_executor_canary_cli_validated_with_pr_attempt_fails_loudly(queue_mode, capsys, monkeypatch):
    # Fix C: a designated request that is VALIDATED and carries a `shadow`
    # artifact (which alone would make it look resumable) but ALSO a
    # `pr_attempt` artifact -- durable evidence a PR was already attempted,
    # see the comment on DelegationLedger.count_prs_for_target_since -- must
    # still be refused fail-loudly, with a diagnostic naming the reason, and
    # must never construct a PR client. Resuming past that risks opening a
    # duplicate PR if the earlier tick was hard-killed between recording
    # pr_attempt and transitioning to PR_OPEN.
    import devflow_delegation.executor as executor_mod
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(rid, "operator", "approve", "fixture setup", f"token-{rid}")
    transition(ledger, None, rid, "PLANNED", actor="operator")
    transition(ledger, None, rid, "BUILDING", actor="operator")
    transition(ledger, None, rid, "VALIDATED", actor="operator")
    ledger.add_artifact(rid, "shadow", "paths=1 lines=1 branch=ddp-x title=t")
    ledger.add_artifact(rid, "pr_attempt", "ddp-x-a1")

    def _boom(*_a, **_k):
        raise AssertionError("has-pr-attempt request -> GhPrClient must never be constructed")

    monkeypatch.setattr(executor_mod, "GhPrClient", _boom)
    rc = cli.main([
        "executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", rid,
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert rid in err
    assert "pr_attempt" in err or "already" in err.lower()
    assert ledger.get_request(rid)["state"] == "VALIDATED"


def test_executor_canary_cli_wrong_state_fails_loudly(queue_mode, capsys, monkeypatch):
    # A designated request in any state other than PLANNED or a
    # shadow-verified VALIDATED (e.g. still TRIAGED) must fail loudly rather
    # than silently reporting processed=0.
    import devflow_delegation.executor as executor_mod
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="operator")

    def _boom(*_a, **_k):
        raise AssertionError("wrong-state request -> GhPrClient must never be constructed")

    monkeypatch.setattr(executor_mod, "GhPrClient", _boom)
    rc = cli.main([
        "executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", rid,
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"request {rid} is in state TRIAGED, not PLANNED" in err
    assert "or a VALIDATED request that was shadow-verified" in err
    assert ledger.get_request(rid)["state"] == "TRIAGED"


def test_executor_canary_cli_reaches_a_pr_with_a_fake_client(
    hermes_root, allowlist_file, tmp_path_factory, capsys, monkeypatch
):
    # H6: the happy path -- executor-canary reaching a real PR and printing
    # pr=<url> -- was only covered at the run_executor_tick level, never at
    # the CLI layer. Drive it through cli.main() end-to-end against a REAL
    # temp git repo + bare remote (the same fixture-repo pattern used by
    # test_executor.py), with a FAKE GhPrClient injected -- never a real
    # client, never a network call.
    import io

    import devflow_delegation.executor as executor_mod
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition
    from tests.devflow_delegation.test_executor import _fixture_repo, _write_source_command

    # The fixture repo lives under its OWN tmp_path_factory root, distinct
    # from hermes_root's tmp_path -- _validate_target_boundary refuses a
    # checkout nested inside (or equal to) get_default_hermes_root(), which
    # hermes_root monkeypatches to its own tmp_path.
    fixture_root = tmp_path_factory.mktemp("ddp-canary-cli")
    repo = _fixture_repo(fixture_root)

    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"explicit": {"mode": "queue"}}), encoding="utf-8")

    allowlist = json.loads(allowlist_file.read_text(encoding="utf-8"))
    allowlist["targets"]["canary_real"] = {
        "repo": "canary_real",
        "checkout_path": str(repo),
        "default_branch": "main",
        "remote": "origin",
        "allowed_globs": ["src/**"],
        "denied_globs": ["**/.env", "secrets/**"],
        "worktree_base": str(fixture_root / "worktrees"),
        "test_commands": [["python", "-c", "print('tests passed')"]],
        "command_timeout_seconds": 30,
        "required_checks": ["test"],
        "risk_ceiling": "low",
        "max_autonomous_action": "create_pr",
        "executor_enabled": True,
        "canary_real": True,
        "pr_budget": 1,
        "implementation_command": list(_write_source_command()),
        "github_repo": "example/canary-fixture",
        "live_gateway_imports": False,
    }
    allowlist_file.write_text(json.dumps(allowlist), encoding="utf-8")

    # Queue + approve a PLANNED request from the "explicit" source kind
    # (EXECUTOR_ALLOWED_SOURCES) against the canary_real target.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(make_delegate_kwargs(
        source={"agent": "operator", "kind": "explicit", "finding_id": "f-1"},
        target={"repo": "canary_real", "subsystem": "src"},
        severity="low",
    ))))
    assert cli.main(["delegate"]) == 0

    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(rid, "operator", "approve", "fixture setup", f"token-{rid}")
    transition(ledger, None, rid, "PLANNED", actor="operator")

    class _FakeGhPrClient:
        def create_pr(self, **kwargs):
            return {"number": 99, "url": "https://example.test/pr/99"}

    monkeypatch.setattr(executor_mod, "GhPrClient", _FakeGhPrClient)

    rc = cli.main(["executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", rid])
    out = capsys.readouterr().out

    assert rc == 0
    assert "mode=canary" in out
    assert "processed=1" in out
    assert "pr=https://example.test/pr/99" in out


def test_executor_canary_cli_resumes_a_shadow_validated_request_with_a_fake_client(
    hermes_root, allowlist_file, tmp_path_factory, capsys, monkeypatch
):
    # CLI-level headline flow: executor-shadow advances a request to
    # VALIDATED and records a shadow artifact; a SUBSEQUENT executor-canary
    # call designating that same request_id must resume it and reach a real
    # (fake, injected) PR -- driven end-to-end through cli.main(), never a
    # real client, never a network call.
    import io

    import devflow_delegation.executor as executor_mod
    from devflow_delegation.emitter import DelegationEmitter
    from devflow_delegation.lifecycle import transition
    from tests.devflow_delegation.test_executor import _fixture_repo, _write_source_command

    fixture_root = tmp_path_factory.mktemp("ddp-canary-resume-cli")
    repo = _fixture_repo(fixture_root)

    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"explicit": {"mode": "queue"}}), encoding="utf-8")

    allowlist = json.loads(allowlist_file.read_text(encoding="utf-8"))
    allowlist["targets"]["canary_real"] = {
        "repo": "canary_real",
        "checkout_path": str(repo),
        "default_branch": "main",
        "remote": "origin",
        "allowed_globs": ["src/**"],
        "denied_globs": ["**/.env", "secrets/**"],
        "worktree_base": str(fixture_root / "worktrees"),
        "test_commands": [["python", "-c", "print('tests passed')"]],
        "command_timeout_seconds": 30,
        "required_checks": ["test"],
        "risk_ceiling": "low",
        "max_autonomous_action": "create_pr",
        "executor_enabled": True,
        "canary_real": True,
        "pr_budget": 1,
        "implementation_command": list(_write_source_command()),
        "github_repo": "example/canary-fixture",
        "live_gateway_imports": False,
    }
    allowlist_file.write_text(json.dumps(allowlist), encoding="utf-8")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(make_delegate_kwargs(
        source={"agent": "operator", "kind": "explicit", "finding_id": "f-1"},
        target={"repo": "canary_real", "subsystem": "src"},
        severity="low",
    ))))
    assert cli.main(["delegate"]) == 0

    ledger = DelegationEmitter().ledger
    rid = ledger.list_requests()[0]["request_id"]
    transition(ledger, None, rid, "TRIAGED", actor="operator")
    assert ledger.record_human_decision(rid, "operator", "approve", "fixture setup", f"token-{rid}")
    transition(ledger, None, rid, "PLANNED", actor="operator")

    # First: a real shadow tick via the CLI advances it to VALIDATED and
    # records a shadow artifact. No PR client is ever constructed for this.
    assert cli.main(["executor-shadow", "--request-id", rid]) == 0
    capsys.readouterr()  # discard the shadow tick's stdout
    assert ledger.get_request(rid)["state"] == "VALIDATED"
    assert any(a["kind"] == "shadow" for a in ledger.artifacts_for(rid))

    class _FakeGhPrClient:
        def create_pr(self, **kwargs):
            return {"number": 100, "url": "https://example.test/pr/100"}

    monkeypatch.setattr(executor_mod, "GhPrClient", _FakeGhPrClient)

    rc = cli.main(["executor-canary", "--i-understand-this-opens-a-real-pr", "--request-id", rid])
    out = capsys.readouterr().out

    assert rc == 0
    assert "mode=canary" in out
    assert "processed=1" in out
    assert "pr=https://example.test/pr/100" in out
    assert ledger.get_request(rid)["state"] == "MERGE_PENDING"


def test_gate_cli_records_shadow_decision_without_authority(queue_mode, allowlist_file, capsys, monkeypatch):
    allowlist = json.loads(allowlist_file.read_text(encoding="utf-8"))
    allowlist["targets"]["fixture"] = {
        "repo": "fixture",
        "checkout_path": "/fixture",
        "allowed_globs": ["agent-src/**"],
        "risk_ceiling": "medium",
    }
    allowlist_file.write_text(json.dumps(allowlist), encoding="utf-8")
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps(make_delegate_kwargs(
            target={"repo": "fixture", "subsystem": "autonomy-gate"},
        ))),
    )
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    em = DelegationEmitter()
    request_id = em.ledger.list_requests()[0]["request_id"]
    em.ledger.close()

    rc = cli.main([
        "gate", "--request-id", request_id, "--action", "merge",
        "--changed-path", "agent-src/devflow_delegation/gate.py",
        "--changed-lines", "10", "--reversible",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=shadow" in out
    assert "eligible=True" in out
    assert "authorized=False" in out
    assert "reason=shadow_mode" in out
    recorded = DelegationEmitter().ledger.artifacts_for(request_id)
    assert [item["kind"] for item in recorded] == ["autonomy_gate"]


def test_gate_cli_rejects_unknown_request_without_creating_artifact(queue_mode, capsys):
    rc = cli.main([
        "gate", "--request-id", "dwr_unknown", "--action", "merge",
        "--changed-path", "agent-src/devflow_delegation/gate.py",
        "--changed-lines", "10", "--reversible",
    ])

    assert rc == 2
    assert "unknown request" in capsys.readouterr().err


def test_gate_cli_never_creates_operator_sentinel(queue_mode, hermes_root, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    request_id = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    sentinel = hermes_root / "devflow" / ".autonomy_enabled"
    assert not sentinel.exists()

    rc = cli.main([
        "gate", "--request-id", request_id, "--action", "deploy",
        "--changed-path", "agent-src/devflow_delegation/gate.py",
        "--changed-lines", "10", "--reversible",
    ])

    assert rc == 0
    assert "authorized=False" in capsys.readouterr().out
    assert not sentinel.exists()


def test_gate_cli_requires_observed_diff_facts(queue_mode, capsys):
    rc = cli.main(["gate", "--request-id", "dwr_unknown", "--action", "merge"])

    assert rc == 2
    assert "--changed-path" in capsys.readouterr().err


def test_gate_cli_live_gateway_target_is_never_eligible(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    request_id = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    rc = cli.main([
        "gate", "--request-id", request_id, "--action", "merge",
        "--changed-path", "agent-src/devflow_delegation/gate.py",
        "--changed-lines", "10", "--reversible",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "eligible=False" in out
    assert "authorized=False" in out
    assert "reason=live_gateway_target" in out


def test_gate_cli_reads_but_never_acts_on_an_operator_sentinel(queue_mode, hermes_root, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    request_id = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    sentinel = hermes_root / "devflow" / ".autonomy_enabled"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    rc = cli.main([
        "gate", "--request-id", request_id, "--action", "merge",
        "--changed-path", "agent-src/devflow_delegation/gate.py",
        "--changed-lines", "10", "--reversible",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=enabled" in out
    assert "authorized=False" in out
    assert "reason=live_gateway_target" in out
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"
