"""HTTP surface: webhook, outbox relay, admin review desk.

The webhook's contract with Iris is "answer immediately, always 200" —
a slow or noisy reply here turns into retries and duplicate messages in
the client's room.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kakao_legal_bot.app.iris import IrisClient  # noqa: E402
from kakao_legal_bot.app.pipeline import Pipeline  # noqa: E402
from kakao_legal_bot.app.services import Services  # noqa: E402

try:  # the admin review form needs python-multipart (the `web` extra)
    from kakao_legal_bot.app.main import create_app
except RuntimeError as exc:  # pragma: no cover - depends on the install
    pytest.skip(f"fastapi extras missing: {exc}", allow_module_level=True)

from .conftest import FakeAgent, FakeSender  # noqa: E402


@pytest.fixture
def client(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변입니다"),
        semaphore=asyncio.Semaphore(4),
    )
    app = create_app(services)
    app.state.pipeline = Pipeline(services)
    with TestClient(app) as test_client:
        test_client.fake_sender = sender
        test_client.services = services
        yield test_client


def payload(text: str, log_id: str = "1", room: str = "room-1") -> dict:
    return {
        "msg": text,
        "room": "상담방",
        "sender": "홍길동",
        "json": {"_id": log_id, "chat_id": room, "user_id": "uid-1", "type": "1"},
    }


def test_health_reports_configuration(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["bot"] == "모아"
    assert "missing_config" in body


def test_webhook_returns_immediately(client):
    started = time.monotonic()
    response = client.post("/iris/webhook", json=payload("모아 안녕"))
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["ok"] is True
    # The whole point of handing off to a task — Iris must never wait on
    # the model. Generous bound so a loaded CI box doesn't flake.
    assert elapsed < 2.0


def test_duplicate_deliveries_are_dropped(client):
    client.post("/iris/webhook", json=payload("모아 안녕", log_id="same"))
    second = client.post("/iris/webhook", json=payload("모아 안녕", log_id="same"))
    assert second.json()["reason"] == "duplicate"


def test_malformed_body_is_not_a_500(client):
    response = client.post(
        "/iris/webhook", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_payload_without_a_room_is_rejected_softly(client):
    response = client.post("/iris/webhook", json={"msg": "안녕"})
    assert response.status_code == 200
    assert response.json()["reason"] == "no room"


def test_webhook_secret_is_enforced(settings, db):
    object.__setattr__(settings, "iris_webhook_secret", "s3cret")
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=FakeSender(),
        agent=FakeAgent("답변"),
        semaphore=asyncio.Semaphore(1),
    )
    app = create_app(services)
    app.state.pipeline = Pipeline(services)
    with TestClient(app) as test_client:
        assert test_client.post("/iris/webhook", json=payload("안녕")).status_code == 401
        ok = test_client.post(
            "/iris/webhook", json=payload("안녕"), headers={"X-Iris-Secret": "s3cret"}
        )
        assert ok.status_code == 200


def test_outbox_requires_the_token(client):
    assert client.get("/outbox").status_code == 401
    assert client.get("/outbox", headers={"X-Outbox-Token": "outbox-token"}).status_code == 200


def test_outbox_pull_and_ack_round_trip(client):
    row_id = client.services.db.enqueue_outbox("room-1", "보낼 메시지")
    headers = {"X-Outbox-Token": "outbox-token"}

    pulled = client.get("/outbox", headers=headers).json()["messages"]
    assert pulled == [{"id": row_id, "room": "room-1", "text": "보낼 메시지"}]

    acked = client.post("/outbox/ack", json={"ids": [row_id], "ok": True}, headers=headers)
    assert acked.json()["acked"] == 1
    assert client.services.db.outbox_depth() == 0


def test_admin_requires_a_token(client):
    assert client.get("/admin/drafts").status_code == 401
    assert client.get("/admin/drafts?token=admin-token").status_code == 200


def test_admin_lists_and_opens_a_draft(client):
    draft_id = client.services.db.create_draft("room-1", "내용증명", "보증금 반환", "본문입니다")

    listing = client.get("/admin/drafts?token=admin-token").text
    assert "보증금 반환" in listing

    page = client.get(f"/admin/drafts/{draft_id}?token=admin-token").text
    assert "본문입니다" in page


def test_admin_edit_and_approve(client):
    draft_id = client.services.db.create_draft("room-1", "내용증명", "제목", "옛 본문")

    response = client.post(
        f"/admin/drafts/{draft_id}?token=admin-token",
        data={
            "action": "approve",
            "title": "수정된 제목",
            "body": "변호사가 고친 본문",
            "client_email": "hong@example.com",
            "lawyer_note": "기한 확인",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    draft = client.services.db.get_draft(draft_id)
    assert draft.status == "approved"
    assert draft.title == "수정된 제목"
    assert draft.body == "변호사가 고친 본문"
    assert draft.client_email == "hong@example.com"


def test_admin_send_refuses_without_smtp(client):
    """No SMTP configured → the failure is reported, not silently swallowed."""
    draft_id = client.services.db.create_draft(
        "room-1", "내용증명", "제목", "본문", client_email="hong@example.com"
    )
    client.services.db.update_draft(draft_id, status="approved")

    response = client.post(
        f"/admin/drafts/{draft_id}?token=admin-token",
        data={
            "action": "send",
            "title": "제목",
            "body": "본문",
            "client_email": "hong@example.com",
            "lawyer_note": "",
        },
        follow_redirects=True,
    )
    assert "실패" in response.text
    assert client.services.db.get_draft(draft_id).status == "approved"
