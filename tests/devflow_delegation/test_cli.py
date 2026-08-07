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


def test_transition_illegal_exits_2(queue_mode, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(make_delegate_kwargs())))
    cli.main(["delegate"])
    from devflow_delegation.emitter import DelegationEmitter

    rid = DelegationEmitter().ledger.list_requests()[0]["request_id"]
    rc = cli.main(["transition", "--request-id", rid, "--to", "MERGED", "--actor", "test"])
    assert rc == 2


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
