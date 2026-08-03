"""Behavioral tests for immutable Hermes root-action approval state."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from gateway.root_action_approval import (
    PendingRootAction,
    RootActionApprovalStore,
    RootActionProtocolError,
    RootActionProposal,
    canonical_json,
    signed_decision_payload,
)


def _timestamp(offset: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _payload(**overrides):
    payload = {
        "action_id": "act-123",
        "parameter_digest": hashlib.sha256(b"canonical parameters").hexdigest(),
        "preview": "Restore caddy from the verified same-run snapshot.",
        "expires_at": _timestamp(300),
    }
    payload.update(overrides)
    return payload


def _pending() -> PendingRootAction:
    return PendingRootAction(
        proposal=RootActionProposal.from_payload(_payload()),
        callback_url="https://pythia.internal/root-action/callback",
        callback_secret="decision-secret",
        chat_id="-100123",
    )


def test_canonical_json_is_stable():
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_proposal_accepts_only_immutable_projection():
    proposal = RootActionProposal.from_payload(_payload())
    assert proposal.action_id == "act-123"
    assert proposal.preview.startswith("Restore caddy")
    with pytest.raises(RootActionProtocolError, match="unsupported"):
        RootActionProposal.from_payload(_payload(action_kind="restore_same_run"))
    with pytest.raises(RootActionProtocolError, match="unsupported"):
        RootActionProposal.from_payload(_payload(parameters={"shell": "rm -rf /"}))


def test_expired_and_malformed_digest_proposals_are_rejected():
    with pytest.raises(RootActionProtocolError, match="expired"):
        RootActionProposal.from_payload(_payload(expires_at=_timestamp(-1)))
    with pytest.raises(RootActionProtocolError, match="parameter_digest"):
        RootActionProposal.from_payload(_payload(parameter_digest="not-a-digest"))


def test_store_rejects_wrong_chat_without_consuming_and_consumes_once(tmp_path):
    store = RootActionApprovalStore(tmp_path / "approvals.json")
    pending = _pending()
    assert store.put(pending) is True
    with pytest.raises(RootActionProtocolError, match="chat"):
        store.consume(
            pending.proposal.action_id,
            decision="approve",
            principal="telegram:42",
            chat_id="-100999",
        )
    assert store.get(pending.proposal.action_id) is not None
    locked = store.consume(
        pending.proposal.action_id,
        decision="approve",
        principal="telegram:42",
        chat_id="-100123",
    )
    assert locked.decision == "approve"
    with pytest.raises(RootActionProtocolError, match="locked"):
        store.consume(
            pending.proposal.action_id,
            decision="deny",
            principal="telegram:42",
            chat_id="-100123",
        )


def test_decision_lock_and_tombstone_survive_restart(tmp_path):
    path = tmp_path / "approvals.json"
    first = RootActionApprovalStore(path)
    pending = _pending()
    first.put(pending)
    first.consume(
        pending.proposal.action_id,
        decision="deny",
        principal="telegram:42",
        chat_id="-100123",
    )
    restarted = RootActionApprovalStore(path)
    assert restarted.get(pending.proposal.action_id).decision == "deny"
    assert len(restarted.pending_deliveries()) == 1
    restarted.acknowledge(pending.proposal.action_id)
    replayed = RootActionApprovalStore(path)
    assert replayed.get(pending.proposal.action_id).acknowledged is True
    assert replayed.put(pending) is False

def test_username_target_is_rebound_to_numeric_telegram_chat(tmp_path):
    path = tmp_path / "approvals.json"
    store = RootActionApprovalStore(path)
    pending = _pending()
    pending = PendingRootAction(
        proposal=pending.proposal,
        callback_url=pending.callback_url,
        callback_secret=pending.callback_secret,
        chat_id="@operator",
    )
    store.put(pending)
    store.bind_chat_id(pending.proposal.action_id, "-100123")
    assert store.get(pending.proposal.action_id).chat_id == "-100123"
    with pytest.raises(RootActionProtocolError, match="numeric"):
        store.bind_chat_id(pending.proposal.action_id, "@operator")

def test_signed_decision_contains_derived_identity_and_preserves_digest():
    pending = _pending()
    body, signature = signed_decision_payload(
        pending,
        decision="deny",
        principal="telegram:42",
        chat_id="-100123",
        decided_at=_timestamp(),
    )
    decoded = json.loads(body)
    assert decoded == {
        "action_id": pending.proposal.action_id,
        "parameter_digest": pending.proposal.parameter_digest,
        "decision": "deny",
        "approval_identity": {
            "principal": "telegram:42",
            "chat_id": "-100123",
        },
        "decided_at": decoded["decided_at"],
    }
    expected = hmac.new(
        b"decision-secret", canonical_json(decoded), hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)
