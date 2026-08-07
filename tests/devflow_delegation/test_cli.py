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
