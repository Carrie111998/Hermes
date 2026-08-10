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

    # Seed the REALISTIC artifact sequence a real executor tick writes -- see
    # devflow_delegation.executor.run_executor_tick -- not just the terminal
    # artifact. Seeding a single artifact per request (the old shape of this
    # test) made every assertion below pass for reasons unrelated to
    # production behavior: latest_artifact is trivially the only row present,
    # and the "no absolute path leaks" check never saw the one artifact kind
    # ("worktree") that actually carries an absolute local path.
    worktree_ref = str(tmp_path / "devflow-worktrees" / "ddp-abc-a1")
    ledger.add_artifact(shadow_rid, "worktree", worktree_ref)
    ledger.add_artifact(shadow_rid, "branch", "ddp-abc-a1")
    ledger.add_artifact(shadow_rid, "validation", "test:ok")
    ledger.add_artifact(shadow_rid, "shadow", "paths=1 lines=2 branch=ddp-abc-a1 title=Shadow projection check")

    ledger.add_artifact(pr_rid, "worktree", worktree_ref)
    ledger.add_artifact(pr_rid, "branch", "ddp-def-a1")
    ledger.add_artifact(pr_rid, "validation", "test:ok")
    ledger.add_artifact(pr_rid, "pr", "https://github.com/acme/sandbox/pull/7")
    ledger.add_artifact(pr_rid, "pr_number", "7")

    # Fetch the leak-relevant artifacts BY KIND from the ledger directly --
    # not via latest_artifact -- since run_executor_tick writes "pr_number"
    # after "pr", making pr_number (not pr) the real latest_artifact for a
    # completed canary request. Relying on latest_artifact here would never
    # exercise the "pr" ref (the one that actually carries a GitHub URL) at
    # all.
    shadow_ref = next(a["ref"] for a in ledger.artifacts_for(shadow_rid) if a["kind"] == "shadow")
    pr_ref = next(a["ref"] for a in ledger.artifacts_for(pr_rid) if a["kind"] == "pr")
    ledger.close()

    data = read_devflow(ledger_path=db, now=datetime.now(timezone.utc).isoformat())

    by_id = {r["request_id"]: r for r in data["requests"]}
    assert by_id[shadow_rid]["latest_artifact"]["kind"] == "shadow"
    assert by_id[shadow_rid]["latest_artifact"]["ref"] == shadow_ref
    # Real projection shape: pr_number is written after pr in
    # run_executor_tick, so pr_number -- not pr -- is the latest_artifact
    # for a completed canary request.
    assert by_id[pr_rid]["latest_artifact"]["kind"] == "pr_number"
    assert by_id[pr_rid]["latest_artifact"]["ref"] == "7"

    # No absolute path leaks in the two artifact kinds that actually carry
    # request-facing content: the shadow summary and the PR URL.
    assert str(tmp_path) not in shadow_ref
    assert ":\\" not in shadow_ref
    assert str(tmp_path) not in pr_ref
    assert ":\\" not in pr_ref

    # known gap: the "worktree" artifact (worktree_ref, seeded above)
    # intentionally still carries an absolute local path. It is never the
    # latest_artifact for a request that completes normally (branch,
    # validation, and shadow/pr always follow it) -- but a request that
    # fails before writing any further artifact (e.g. the implementation
    # command itself fails right after worktree creation) WOULD project
    # "worktree" as its latest_artifact, leaking that local path through
    # this read-only API. This is pre-existing Stage-2 behavior on a
    # loopback-only surface (Hermes Canvas binds 127.0.0.1 only) and is
    # tracked as a follow-up; it is not asserted against or fixed here.
