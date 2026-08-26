"""Drafts produced by the Codex worker on the lawyer's own PC.

The queue has to survive the things that actually happen: the PC being
switched off, the worker dying mid-document, two workers polling at once,
and a delivery that arrives after the lawyer already edited the draft.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kakao_legal_bot.app.iris import IrisClient  # noqa: E402
from kakao_legal_bot.app.pipeline import Pipeline  # noqa: E402
from kakao_legal_bot.app.services import Services  # noqa: E402
from kakao_legal_bot.app.tools import DraftRequest  # noqa: E402
from kakao_legal_bot.app.workflows import create_draft, send_draft  # noqa: E402

try:
    from kakao_legal_bot.app.main import create_app
except RuntimeError as exc:  # pragma: no cover
    pytest.skip(f"fastapi extras missing: {exc}", allow_module_level=True)

from .conftest import FakeAgent, FakeSender  # noqa: E402

HEADERS = {"X-Worker-Token": "worker-token"}


@pytest.fixture
def worker_settings(settings):
    object.__setattr__(settings, "draft_generator", "worker")
    object.__setattr__(settings, "draft_worker_token", "worker-token")
    object.__setattr__(settings, "public_base_url", "https://moa.test")
    return settings


@pytest.fixture
def wiring(worker_settings, db):
    sender = FakeSender()
    services = Services(
        settings=worker_settings,
        db=db,
        iris=IrisClient(worker_settings),
        sender=sender,
        agent=FakeAgent("답변"),
        semaphore=asyncio.Semaphore(1),
    )
    return services, sender


@pytest.fixture
def client(wiring):
    services, sender = wiring
    app = create_app(services)
    app.state.pipeline = Pipeline(services)
    with TestClient(app) as test_client:
        test_client.services = services
        test_client.fake_sender = sender
        yield test_client


async def queue_a_draft(services, room_id: str = "room-1") -> int:
    services.db.upsert_room(room_id)
    services.db.add_message(room_id, "user", "전세금을 못 받고 있습니다")
    return await create_draft(
        services,
        room_id,
        DraftRequest(kind="내용증명", title="보증금 반환 청구", instructions="3천만원, 2주 기한"),
    )


# ── queueing ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_mode_queues_instead_of_generating(wiring):
    services, sender = wiring
    services.agent.draft_document = None  # would explode if called

    draft_id = await queue_a_draft(services)

    draft = services.db.get_draft(draft_id)
    assert draft.status == "pending_generation"
    assert draft.body == ""
    assert draft.instructions == "3천만원, 2주 기한"
    assert "전세금을 못 받고 있습니다" in draft.transcript
    assert any("초안 요청 접수" in note for note in sender.lawyer_notes)


@pytest.mark.asyncio
async def test_llm_mode_is_untouched(settings, db):
    """The default path still generates on the server."""
    sender = FakeSender()
    services = Services(
        settings=settings,  # draft_generator defaults to "llm"
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변"),
        semaphore=asyncio.Semaphore(1),
    )
    draft_id = await queue_a_draft(services)

    draft = db.get_draft(draft_id)
    assert draft.status == "pending_review"
    assert draft.body == "초안 본문"


# ── the worker round trip ────────────────────────────────────────────────
def test_worker_claims_then_delivers(client):
    draft_id = asyncio.run(queue_a_draft(client.services))

    jobs = client.get("/drafts/queue", headers=HEADERS).json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == draft_id
    assert jobs[0]["kind"] == "내용증명"
    assert jobs[0]["instructions"] == "3천만원, 2주 기한"
    assert "전세금" in jobs[0]["transcript"]

    # Claimed, not merely listed — a second poll gets nothing.
    assert client.get("/drafts/queue", headers=HEADERS).json()["jobs"] == []
    assert client.services.db.get_draft(draft_id).status == "generating"

    ok = client.post(
        f"/drafts/{draft_id}/result",
        json={"body": "내용증명\n\n1. 귀하는 …"},
        headers=HEADERS,
    )
    assert ok.json()["ok"] is True

    draft = client.services.db.get_draft(draft_id)
    assert draft.status == "pending_review"
    assert draft.body.startswith("내용증명")


def test_delivery_notifies_the_lawyer_with_a_review_link(client):
    draft_id = asyncio.run(queue_a_draft(client.services))
    client.get("/drafts/queue", headers=HEADERS)
    client.post(f"/drafts/{draft_id}/result", json={"body": "완성된 문서"}, headers=HEADERS)

    for _ in range(50):
        if any("초안 준비됨" in note for note in client.fake_sender.lawyer_notes):
            break
        __import__("time").sleep(0.01)

    ready = [n for n in client.fake_sender.lawyer_notes if "초안 준비됨" in n]
    assert ready
    assert f"/admin/drafts/{draft_id}" in ready[0]
    assert f"/승인 {draft_id}" in ready[0]


def test_worker_endpoints_require_the_token(client):
    asyncio.run(queue_a_draft(client.services))
    assert client.get("/drafts/queue").status_code == 401
    assert client.post("/drafts/1/result", json={"body": "x"}).status_code == 401
    assert client.post("/drafts/1/fail", json={"error": "x"}).status_code == 401


def test_empty_delivery_is_rejected(client):
    draft_id = asyncio.run(queue_a_draft(client.services))
    client.get("/drafts/queue", headers=HEADERS)
    response = client.post(f"/drafts/{draft_id}/result", json={"body": "   "}, headers=HEADERS)
    assert response.status_code == 400
    assert client.services.db.get_draft(draft_id).status == "generating"


def test_a_late_delivery_cannot_overwrite_the_lawyers_edit(client):
    """The lawyer edited and approved while a stale worker was still running."""
    draft_id = asyncio.run(queue_a_draft(client.services))
    client.get("/drafts/queue", headers=HEADERS)
    client.post(f"/drafts/{draft_id}/result", json={"body": "첫 번째 초안"}, headers=HEADERS)
    client.services.db.update_draft(draft_id, body="변호사가 고친 본문", status="approved")

    late = client.post(f"/drafts/{draft_id}/result", json={"body": "늦게 온 초안"}, headers=HEADERS)

    assert late.status_code == 409
    assert client.services.db.get_draft(draft_id).body == "변호사가 고친 본문"


# ── failure handling ─────────────────────────────────────────────────────
def test_a_failed_job_goes_back_in_the_queue(client):
    draft_id = asyncio.run(queue_a_draft(client.services))
    client.get("/drafts/queue", headers=HEADERS)

    response = client.post(
        f"/drafts/{draft_id}/fail", json={"error": "codex 시간 초과"}, headers=HEADERS
    )
    assert response.json()["status"] == "pending_generation"
    assert client.services.db.get_draft(draft_id).last_error == "codex 시간 초과"

    # And it is handed out again.
    assert client.get("/drafts/queue", headers=HEADERS).json()["jobs"][0]["id"] == draft_id


def test_repeated_failure_stops_retrying_and_tells_the_lawyer(client):
    draft_id = asyncio.run(queue_a_draft(client.services))

    for _ in range(3):
        client.get("/drafts/queue", headers=HEADERS)
        response = client.post(f"/drafts/{draft_id}/fail", json={"error": "계속 실패"}, headers=HEADERS)

    assert response.json()["status"] == "generation_failed"
    assert client.get("/drafts/queue", headers=HEADERS).json()["jobs"] == []

    for _ in range(50):
        if any("자동 작성에 실패" in note for note in client.fake_sender.lawyer_notes):
            break
        __import__("time").sleep(0.01)
    assert any("자동 작성에 실패" in note for note in client.fake_sender.lawyer_notes)


def test_a_worker_that_died_mid_job_does_not_strand_the_request(client):
    draft_id = asyncio.run(queue_a_draft(client.services))
    client.get("/drafts/queue", headers=HEADERS)  # claimed, then the PC dies
    assert client.get("/drafts/queue", headers=HEADERS).json()["jobs"] == []

    assert client.services.db.requeue_stale_draft_jobs(older_than_s=-1) == 1
    assert client.get("/drafts/queue", headers=HEADERS).json()["jobs"][0]["id"] == draft_id


# ── it still cannot skip the lawyer ──────────────────────────────────────
@pytest.mark.asyncio
async def test_a_queued_draft_cannot_be_emailed(wiring):
    services, _sender = wiring
    draft_id = await queue_a_draft(services)
    services.db.update_draft(draft_id, client_email="hong@example.com")

    ok, message = await send_draft(services, draft_id)

    assert ok is False
    assert "승인 전" in message


def test_health_reports_the_draft_queue(client):
    asyncio.run(queue_a_draft(client.services))
    body = client.get("/health").json()
    assert body["draft_generator"] == "worker"
    assert body["draft_jobs_queued"] == 1


# ── the PC being off is a normal state, not an error ─────────────────────
def test_jobs_wait_quietly_while_the_pc_is_off(client):
    first = asyncio.run(queue_a_draft(client.services, "room-1"))
    second = asyncio.run(queue_a_draft(client.services, "room-2"))

    assert client.services.db.draft_queue_depth() == 2

    # The worker comes back hours later and finds both.
    jobs = client.get("/drafts/queue?limit=5", headers=HEADERS).json()["jobs"]
    assert [job["id"] for job in jobs] == [first, second]
