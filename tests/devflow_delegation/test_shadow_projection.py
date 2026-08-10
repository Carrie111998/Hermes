from datetime import datetime, timezone

from artifact_surface.data_api import read_devflow
from devflow_delegation.contract import parse_request
from devflow_delegation.ledger import DelegationLedger


def _seed(ledger, idem):
    req = parse_request({
        "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST", "idempotency_key": idem,
        "source": {"agent": "operator", "kind": "explicit", "finding_id": "f"},
        "kind": "task", "title": "Shadow projection check", "problem_statement": "p",
        "evidence": [{"kind": "test", "summary": "s"}], "target": {"repo": "sandbox", "subsystem": "src"},
        "severity": "low", "priority": "P3", "confidence": 1.0,
        "acceptance_criteria": ["a"], "safety_notes": [],
    })
    ledger.insert_request(req)
    return req.request_id


def test_shadow_and_pr_artifacts_project_without_path_leakage(tmp_path):
    db = tmp_path / "devflow" / "delegation_ledger.db"
    ledger = DelegationLedger(db)
    shadow_rid = _seed(ledger, "k-shadow")
    pr_rid = _seed(ledger, "k-pr")
    ledger.add_artifact(shadow_rid, "shadow", "paths=1 lines=2 branch=ddp-abc-a1 title=Shadow projection check")
    ledger.add_artifact(pr_rid, "pr", "https://github.com/acme/sandbox/pull/7")
    ledger.close()

    data = read_devflow(ledger_path=db, now=datetime.now(timezone.utc).isoformat())

    by_id = {r["request_id"]: r for r in data["requests"]}
    assert by_id[shadow_rid]["latest_artifact"]["kind"] == "shadow"
    assert by_id[pr_rid]["latest_artifact"]["kind"] == "pr"
    assert by_id[pr_rid]["latest_artifact"]["ref"] == "https://github.com/acme/sandbox/pull/7"
    # No absolute path leaks anywhere in the projected artifact refs.
    assert str(tmp_path) not in by_id[shadow_rid]["latest_artifact"]["ref"]
    assert ":\\" not in by_id[shadow_rid]["latest_artifact"]["ref"]
