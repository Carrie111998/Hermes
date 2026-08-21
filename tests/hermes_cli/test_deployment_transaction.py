from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import deployment_plan
from hermes_cli import deployment_transaction as txmod


OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40


def _plan(tmp_path: Path, *, target_generation: str | None = None):
    repo = tmp_path / "repo"
    repo.mkdir()
    return deployment_plan.plan_from_mapping(
        {
            "schema_version": 1,
            "mode": "cli-only",
            "kind": "git-venv",
            "canonical_checkout": str(repo),
            "automatic_local_provisioning": True,
            "target_generation": target_generation,
        },
        hermes_home=tmp_path / "home",
        source="test",
        project_root=repo,
    )


def test_second_transaction_cannot_overlap_same_deployment(monkeypatch, tmp_path):
    plan = _plan(tmp_path)
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: OLD_SHA)

    first = txmod.DeploymentUpdateTransaction(plan).acquire()
    second = txmod.DeploymentUpdateTransaction(plan)
    with pytest.raises(txmod.DeploymentTransactionBusy, match="already held"):
        second.acquire()

    first.abort("test cleanup")


def test_authority_replacement_after_admission_fails_closed(monkeypatch, tmp_path):
    plan = _plan(tmp_path)
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: OLD_SHA)
    tx = txmod.DeploymentUpdateTransaction(plan).acquire()

    durable = json.loads(tx.lock_path.read_text(encoding="utf-8"))
    durable["operation_id"] = "replacement-owner"
    tx.lock_path.write_text(json.dumps(durable), encoding="utf-8")

    with pytest.raises(txmod.DeploymentTransactionStale, match="changed after admission"):
        tx.assert_current()

    # A stale owner must not delete the replacement authority.
    assert tx.lock_path.exists()


def test_required_target_generation_is_verified_before_success(monkeypatch, tmp_path):
    plan = _plan(tmp_path, target_generation=NEW_SHA)
    generations = iter((OLD_SHA, OTHER_SHA, OTHER_SHA))
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: next(generations))
    tx = txmod.DeploymentUpdateTransaction(plan).acquire()

    with pytest.raises(txmod.DeploymentGenerationMismatch, match="authority required"):
        tx.verify_committed_generation()

    tx.abort("generation mismatch")
    receipt = json.loads(tx.receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "aborted"
    assert receipt["target_generation"] == NEW_SHA


def test_verified_settlement_persists_exact_permit_and_generation(monkeypatch, tmp_path):
    plan = _plan(tmp_path, target_generation=NEW_SHA)
    generations = iter((OLD_SHA, NEW_SHA))
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: next(generations))
    tx = txmod.DeploymentUpdateTransaction(plan).acquire()

    committed = tx.verify_committed_generation()
    tx.settle_verified(committed)

    receipt = json.loads(tx.receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "verified"
    assert receipt["operation_id"] == tx.permit.operation_id
    assert receipt["plan_digest"] == plan.digest
    assert receipt["permit_digest"] == tx.permit.digest
    assert receipt["admitted_generation"] == OLD_SHA
    assert receipt["committed_generation"] == NEW_SHA
    assert not tx.lock_path.exists()


def test_unsettled_scope_records_abort_instead_of_false_success(monkeypatch, tmp_path):
    plan = _plan(tmp_path)
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: OLD_SHA)

    with txmod.DeploymentUpdateTransaction(plan):
        pass

    receipt = json.loads(
        (plan.hermes_home / txmod.TRANSACTION_RECEIPT_NAME).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "aborted"
    assert "without verified settlement" in receipt["reason"]


def test_run_wrapper_carries_one_authority_across_mutation(monkeypatch, tmp_path):
    plan = _plan(tmp_path, target_generation=NEW_SHA)
    generations = iter((OLD_SHA, NEW_SHA))
    monkeypatch.setattr(txmod, "_git_generation", lambda checkout: next(generations))
    observed = []

    def mutation():
        lock = plan.hermes_home / txmod.TRANSACTION_LOCK_NAME
        payload = json.loads(lock.read_text(encoding="utf-8"))
        observed.append((payload["plan_digest"], payload["admitted_generation"]))
        return 37

    assert txmod.run_under_deployment_authority(plan, mutation) == 37
    assert observed == [(plan.digest, OLD_SHA)]
