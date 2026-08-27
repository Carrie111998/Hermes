"""Behavior tests for Kira's immutable Google Chat email approval gate."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugins.platforms.google_chat.kira_email_approval import (
    KiraApprovalError,
    KiraEmailApprovalGate,
)


OPS = "spaces/gtr-ops"
RICHARD = "richard@goldentouchremodeling.com"
JUSTIN = "jadkins@clearplanconsulting.com"


def _gate(tmp_path: Path, *, now=lambda: 1_000.0, ttl_seconds: int = 600) -> KiraEmailApprovalGate:
    return KiraEmailApprovalGate(
        tmp_path / "approval.sqlite3",
        ops_space=OPS,
        approvers={RICHARD, JUSTIN},
        now=now,
        ttl_seconds=ttl_seconds,
    )


def _draft(gate: KiraEmailApprovalGate) -> dict:
    return gate.create(
        recipient="vendor@example.com",
        subject="Required quote",
        body="Please send the quote.",
    )


def _approve(gate: KiraEmailApprovalGate, status: dict, *, event_id="event-1") -> tuple[str, dict | None]:
    return gate.decide(
        status["id"],
        decision="approve",
        payload_hash=status["payload_sha256"],
        actor_principal=RICHARD,
        source_space=OPS,
        chat_event_id=event_id,
    )


class _Provider:
    def __init__(self, *, owner="rlord@goldentouchremodeling.com", timeout=False):
        self.owner = owner
        self.timeout = timeout
        self.created: list[dict] = []
        self.sent: list[dict] = []

    async def get_profile(self, *, account: str) -> dict:
        assert account == "rlord"
        return {"data": {"emailAddress": self.owner}}

    async def create_email_draft(self, *, account: str, arguments: dict) -> dict:
        assert account == "rlord"
        self.created.append(arguments)
        return {"data": {"id": "gmail-draft-1"}}

    async def send_draft(self, *, account: str, arguments: dict) -> dict:
        assert account == "rlord"
        self.sent.append(arguments)
        if self.timeout:
            raise asyncio.TimeoutError()
        return {"data": {"id": "gmail-message-1", "threadId": "thread-1"}}


def test_authorized_approval_is_exact_and_provider_send_is_single_use(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)

    outcome, approved = _approve(gate, created)
    assert outcome == "approved"
    assert approved and approved["state"] == "APPROVED"

    provider = _Provider()
    sent = asyncio.run(gate.send(created["id"], provider))
    assert sent["state"] == "SENT"
    assert sent["provider_message_id"] == "gmail-message-1"
    assert provider.created == [{
        "user_id": "me", "recipient_email": "vendor@example.com",
        "subject": "Required quote", "body": "Please send the quote.", "is_html": False,
    }]
    assert provider.sent == [{"user_id": "me", "draft_id": "gmail-draft-1"}]

    replay = asyncio.run(gate.send(created["id"], provider))
    assert replay["state"] == "SENT"
    assert len(provider.created) == len(provider.sent) == 1


def test_unauthorized_wrong_space_and_changed_hash_never_authorize(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)

    outcome, status = gate.decide(
        created["id"], decision="approve", payload_hash=created["payload_sha256"],
        actor_principal="imposter@example.com", source_space=OPS, chat_event_id="wrong-user",
    )
    assert outcome == "unauthorized"
    assert status and status["state"] == "PENDING"

    outcome, status = gate.decide(
        created["id"], decision="approve", payload_hash=created["payload_sha256"],
        actor_principal=RICHARD, source_space="spaces/other", chat_event_id="wrong-space",
    )
    assert outcome == "wrong_space"
    assert status and status["state"] == "PENDING"

    outcome, status = gate.decide(
        created["id"], decision="approve", payload_hash="0" * 64,
        actor_principal=RICHARD, source_space=OPS, chat_event_id="changed-payload",
    )
    assert outcome == "changed_hash"
    assert status and status["state"] == "PENDING"
    assert {event["action"] for event in status["audit"]} >= {
        "created", "unauthorized", "wrong_space", "hash_rejected",
    }


def test_concurrent_clicks_create_one_authorization(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)

    def click(number: int) -> tuple[str, dict | None]:
        return _approve(gate, created, event_id=f"event-{number}")

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(click, [1, 2]))

    assert sorted(outcome for outcome, _ in outcomes) == ["approved", "replayed"]
    status = gate.status(created["id"])
    assert status["state"] == "APPROVED"
    assert [event["action"] for event in status["audit"]].count("approved") == 1


def test_expiration_rejects_click_and_never_sends(tmp_path: Path) -> None:
    clock = [1_000.0]
    gate = _gate(tmp_path, now=lambda: clock[0], ttl_seconds=30)
    created = _draft(gate)
    clock[0] = 1_031.0

    outcome, status = _approve(gate, created)
    assert outcome == "replayed"
    assert status and status["state"] == "EXPIRED"
    result = asyncio.run(gate.send(created["id"], _Provider()))
    assert result["state"] == "EXPIRED"


def test_provider_identity_or_timeout_fails_closed_without_retry(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)
    _approve(gate, created)

    wrong_owner = _Provider(owner="wrong@example.com")
    failed = asyncio.run(gate.send(created["id"], wrong_owner))
    assert failed["state"] == "FAILED"
    assert failed["provider_message_id"] is None
    assert wrong_owner.created == wrong_owner.sent == []

    timed = _draft(gate)
    _approve(gate, timed, event_id="event-timeout")
    timeout_provider = _Provider(timeout=True)
    failed_timeout = asyncio.run(gate.send(timed["id"], timeout_provider))
    assert failed_timeout["state"] == "FAILED"
    assert len(timeout_provider.created) == len(timeout_provider.sent) == 1
    replay = asyncio.run(gate.send(timed["id"], timeout_provider))
    assert replay["state"] == "FAILED"
    assert len(timeout_provider.created) == len(timeout_provider.sent) == 1


def test_status_redacts_email_body_and_audit_is_append_only(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)
    status = gate.status(created["id"])
    rendered = repr(status)
    assert "vendor@example.com" not in rendered
    assert "Required quote" not in rendered
    assert "Please send the quote." not in rendered
    assert status["audit"][0]["action"] == "created"

    with gate._connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM audit_events")
    with gate._connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute("INSERT OR REPLACE INTO audit_events(event_id,draft_id,timestamp,action,payload_sha256) "
                     "SELECT event_id,draft_id,timestamp,'forged',payload_sha256 FROM audit_events LIMIT 1")


def test_success_status_redacts_local_content_and_keeps_provider_evidence(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    created = _draft(gate)
    _approve(gate, created)
    provider = _Provider()
    sent = asyncio.run(gate.send(created["id"], provider))
    assert sent["state"] == "SENT"
    assert sent["provider_thread_id"] == "thread-1"
    assert sent["audit"][-1]["provider_thread_id"] == "thread-1"
    rendered = repr(sent)
    assert "vendor@example.com" not in rendered
    assert "Required quote" not in rendered
    assert "Please send the quote." not in rendered


def test_approval_must_match_the_draft_persisted_ops_space(tmp_path: Path) -> None:
    old_gate = KiraEmailApprovalGate(
        tmp_path / "approval.sqlite3", ops_space="spaces/old", approvers={RICHARD},
    )
    created = _draft(old_gate)
    new_gate = KiraEmailApprovalGate(
        tmp_path / "approval.sqlite3", ops_space=OPS, approvers={RICHARD},
    )
    outcome, status = new_gate.decide(
        created["id"], decision="approve", payload_hash=created["payload_sha256"],
        actor_principal=RICHARD, source_space=OPS, chat_event_id="moved-space",
    )
    assert outcome == "wrong_space"
    assert status and status["state"] == "PENDING"


def test_gate_requires_explicit_ops_space_and_principal_allowlist(tmp_path: Path) -> None:
    with pytest.raises(KiraApprovalError, match="Ops space"):
        KiraEmailApprovalGate(tmp_path / "a.sqlite3", ops_space="", approvers={RICHARD})
    with pytest.raises(KiraApprovalError, match="allowlist"):
        KiraEmailApprovalGate(tmp_path / "b.sqlite3", ops_space=OPS, approvers=set())
