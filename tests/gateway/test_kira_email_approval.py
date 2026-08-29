"""Kira v2 direct-body approval gate regression tests.

The comprehensive direct-sender contract checks live in test_kira_v2_contract.py.
This file keeps the historic gateway test path on v2 semantics.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.platforms.google_chat.kira_email_approval import KiraApprovalError, KiraEmailApprovalGate

OPS = "spaces/gtr-ops"
RICHARD = "users/richard-immutable"


def _gate(tmp_path: Path, *, clock=lambda: 1_000.0, ttl_seconds=600):
    return KiraEmailApprovalGate(
        tmp_path / "approval.sqlite3", ops_space=OPS, approvers={RICHARD}, now=clock,
        ttl_seconds=ttl_seconds,
    )


def test_v2_rejects_email_identity_and_unverified_credential(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    request = gate.create(recipient="vendor@example.com", subject="Quote", body="Exact", created_by="kira-service")
    denied, status = gate.decide(
        request_id=request["id"], draft_hash=request["draft_hash"], decision="approve",
        actor_user_id="richard@goldentouchremodeling.com", actor_email="richard@goldentouchremodeling.com",
        space=OPS, event_id="bad", verified_credential=True,
    )
    assert denied == "DENIED"
    assert status and status["status"] == "PENDING"
    denied, status = gate.decide(
        request_id=request["id"], draft_hash=request["draft_hash"], decision="approve",
        actor_user_id=RICHARD, space=OPS, event_id="unverified", verified_credential=False,
    )
    assert denied == "DENIED"
    assert status and status["status"] == "PENDING"


def test_v2_expiration_and_status_redaction(tmp_path: Path) -> None:
    clock = [1_000.0]
    gate = _gate(tmp_path, clock=lambda: clock[0], ttl_seconds=30)
    request = gate.create(recipient="vendor@example.com", subject="Quote", body="Exact body", created_by="kira-service")
    clock[0] += 31
    status = gate.status(request["id"])
    assert status["status"] == "EXPIRED"
    assert "Exact body" not in repr(status)


def test_v2_configuration_requires_immutable_user_resource_ids(tmp_path: Path) -> None:
    with pytest.raises(KiraApprovalError, match="configured GTR Ops"):
        KiraEmailApprovalGate(tmp_path / "x.sqlite3", ops_space="", approvers={RICHARD})
    with pytest.raises(KiraApprovalError, match="resource IDs"):
        KiraEmailApprovalGate(tmp_path / "y.sqlite3", ops_space=OPS, approvers={"name@example.com"})
