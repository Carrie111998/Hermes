from __future__ import annotations

import asyncio
from pathlib import Path

from plugins.platforms.google_chat.kira_email_approval import (
    KiraEmailApprovalGate, KiraProviderRejected, canonical_payload_bytes,
)

OPS = "spaces/gtr-ops"
RICHARD = "users/richard-immutable"
JUSTIN = "users/justin-immutable"


class FakeRouter:
    binding_fingerprint = "router-v1"
    def __init__(self, *, email="rlord@goldentouchremodeling.com"):
        self.email, self.calls = email, []
    async def get_profile(self, *, account):
        assert account == "rlord"; return {"emailAddress": self.email}
    async def send_new(self, *, recipient, subject, body):
        self.calls.append(("new", recipient, subject, body)); return {"id": "message-1", "threadId": "thread-1"}
    async def send_reply(self, *, recipient, thread_id, body):
        self.calls.append(("reply", recipient, thread_id, body)); return {"id": "message-2", "threadId": thread_id}


def gate(tmp_path: Path, *, now=lambda: 1_000.0):
    return KiraEmailApprovalGate(tmp_path / "kira.sqlite", ops_space=OPS, approvers={RICHARD, JUSTIN}, now=now)


def approve(store, request, *, actor=RICHARD):
    return store.decide(request_id=request["id"], draft_hash=request["draft_hash"], decision="approve", actor_user_id=actor, actor_email="rlord@goldentouchremodeling.com", space=OPS, event_id="click-" + request["id"], verified_credential=True)


def test_new_mail_sends_only_stored_direct_body_and_no_draft_methods(tmp_path):
    store = gate(tmp_path); request = store.create(recipient="vendor@example.com", subject="Quote", body="Exact\nbody", created_by="kira-service")
    approve(store, request); router = FakeRouter()
    result = asyncio.run(store.send(request["id"], router))
    assert result["status"] == "SENT"
    assert router.calls == [("new", "vendor@example.com", "Quote", "Exact\nbody")]
    assert "Exact\nbody" not in repr(result)


def test_reply_uses_bound_thread_and_never_supplies_subject(tmp_path):
    store = gate(tmp_path); store.bind_inbound_thread(thread_id="threads/inbound-1", recipient="vendor@example.com")
    request = store.create(recipient="vendor@example.com", subject="", body="Exact reply", thread_id="threads/inbound-1", created_by="kira-service")
    approve(store, request); router = FakeRouter()
    result = asyncio.run(store.send(request["id"], router))
    assert result["status"] == "SENT"
    assert router.calls == [("reply", "vendor@example.com", "threads/inbound-1", "Exact reply")]


def test_email_is_not_an_authorization_key_and_hash_binds_creator_and_expiry(tmp_path):
    store = gate(tmp_path); request = store.create(recipient="vendor@example.com", subject="Quote", body="Exact", created_by="kira-service")
    denied, _ = store.decide(request_id=request["id"], draft_hash=request["draft_hash"], decision="approve", actor_user_id="", actor_email="rlord@goldentouchremodeling.com", space=OPS, event_id="email-only", verified_credential=True)
    assert denied == "DENIED" and store.status(request["id"])["status"] == "PENDING"
    with store._connect() as conn: persisted = conn.execute("SELECT * FROM email_requests WHERE id=?", (request["id"],)).fetchone()
    assert persisted is not None
    altered = dict(persisted); altered["created_by"] = "other"
    assert request["draft_hash"] != store.hash_for_fields(altered)
    assert canonical_payload_bytes(**{key: persisted[key] for key in ("id", "created_by", "expires_at", "recipient", "subject", "body", "thread_id", "mode")}).startswith(b"kira-email-v2\0id:")


def test_concurrent_send_has_one_claim_and_no_second_invocation(tmp_path):
    store = gate(tmp_path); request = store.create(recipient="vendor@example.com", subject="Quote", body="Exact", created_by="kira-service")
    approve(store, request); router = FakeRouter()
    async def concurrently():
        return await asyncio.gather(store.send(request["id"], router), store.send(request["id"], router))
    first, second = asyncio.run(concurrently())
    assert len(router.calls) == 1
    assert {first["status"], second["status"]} == {"SENT"}


def test_timeout_is_failed_unknown_and_no_second_invocation(tmp_path):
    class TimeoutRouter(FakeRouter):
        async def send_new(self, **kwargs): self.calls.append(("new",)); raise asyncio.TimeoutError()
    store = gate(tmp_path); request = store.create(recipient="vendor@example.com", subject="Quote", body="Exact", created_by="kira-service")
    approve(store, request); router = TimeoutRouter()
    assert asyncio.run(store.send(request["id"], router))["status"] == "FAILED_UNKNOWN"
    assert asyncio.run(store.send(request["id"], router))["status"] == "FAILED_UNKNOWN"
    assert len(router.calls) == 1


def test_profile_mismatch_and_proven_no_message_rejection_fail_before_or_after_send(tmp_path):
    store = gate(tmp_path); request = store.create(recipient="vendor@example.com", subject="Quote", body="Exact", created_by="kira-service")
    approve(store, request); wrong = FakeRouter(email="wrong@example.com")
    assert asyncio.run(store.send(request["id"], wrong))["status"] == "FAILED" and wrong.calls == []
    class RejectingRouter(FakeRouter):
        async def send_new(self, **kwargs): raise KiraProviderRejected("invalid", no_message_created=True)
    other = store.create(recipient="other@example.com", subject="Quote", body="Exact", created_by="kira-service")
    approve(store, other)
    assert asyncio.run(store.send(other["id"], RejectingRouter()))["status"] == "FAILED"
