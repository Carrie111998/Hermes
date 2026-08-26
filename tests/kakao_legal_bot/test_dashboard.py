"""변호사 업무 대시보드와 카카오톡 초안 전달.

이메일은 상담자에게 가는 길이고, 변호사님은 재판 사이에 휴대폰으로 봅니다.
그래서 완성된 초안은 카톡방으로 가고, 대시보드는 한 페이지에 다 담습니다.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kakao_legal_bot.app.config import Settings  # noqa: E402
from kakao_legal_bot.app.iris import IrisClient  # noqa: E402
from kakao_legal_bot.app.pipeline import Pipeline  # noqa: E402
from kakao_legal_bot.app.services import Services  # noqa: E402
from kakao_legal_bot.app.workflows import notify_draft_ready  # noqa: E402

try:  # the admin forms need python-multipart (the `web` extra)
    from kakao_legal_bot.app.main import create_app
except RuntimeError as exc:  # pragma: no cover - depends on the install
    pytest.skip(f"fastapi extras missing: {exc}", allow_module_level=True)

from .conftest import FakeAgent, FakeSender  # noqa: E402


@pytest.fixture
def wiring(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
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
        test_client.sender = sender
        yield test_client


# ── 링크 ─────────────────────────────────────────────────────────────────
def test_a_link_carries_the_token_so_one_tap_opens_it(monkeypatch):
    """휴대폰에서 토큰을 타이핑하게 하면 대시보드는 열리지 않습니다."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://moa.example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret token")
    settings = Settings()

    assert settings.admin_url() == "https://moa.example.com/admin?token=s3cret%20token"
    assert settings.admin_url("/drafts/12").endswith("/admin/drafts/12?token=s3cret%20token")


def test_no_public_url_means_no_link_rather_than_a_broken_one(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "t")
    assert Settings().admin_url() == ""


def test_no_admin_token_means_no_link_either(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://moa.example.com")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert Settings().admin_url() == ""


# ── 카카오톡으로 초안 보내기 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_finished_draft_arrives_in_the_lawyers_room_in_full(wiring):
    services, sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금 반환", "문서 본문입니다.")

    await notify_draft_ready(services, draft_id)

    assert len(sender.lawyer_notes) == 2  # 안내 + 본문
    assert f"초안 준비됨 #{draft_id}" in sender.lawyer_notes[0]
    assert "승인: /승인" in sender.lawyer_notes[0]
    # 본문은 따로 와야 그것만 복사할 수 있습니다.
    assert sender.lawyer_notes[1].endswith("문서 본문입니다.")


@pytest.mark.asyncio
async def test_a_long_draft_is_linked_not_pasted(wiring):
    """스무 통짜리 카톡 벽은 읽히지 않습니다."""
    services, sender = wiring
    object.__setattr__(services.settings, "draft_kakao_max_chars", 50)
    body = "가" * 400
    draft_id = services.db.create_draft("room-1", "소장", "대여금", body)

    await notify_draft_ready(services, draft_id)

    assert len(sender.lawyer_notes) == 1
    assert "길어서 링크로" in sender.lawyer_notes[0]
    assert "400자" in sender.lawyer_notes[0]


@pytest.mark.asyncio
async def test_link_only_mode_sends_the_notice_without_the_body(wiring):
    services, sender = wiring
    object.__setattr__(services.settings, "draft_delivery", "link")
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금", "본문")

    await notify_draft_ready(services, draft_id)

    assert len(sender.lawyer_notes) == 1
    assert "본문" not in sender.lawyer_notes[0].split("---")[0]


@pytest.mark.asyncio
async def test_delivery_can_be_switched_off_entirely(wiring):
    services, sender = wiring
    object.__setattr__(services.settings, "draft_delivery", "off")
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금", "본문")

    await notify_draft_ready(services, draft_id)

    assert sender.lawyer_notes == []


@pytest.mark.asyncio
async def test_a_missing_draft_does_not_raise(wiring):
    services, sender = wiring
    await notify_draft_ready(services, 999)
    assert sender.lawyer_notes == []


@pytest.mark.asyncio
async def test_the_notice_says_how_to_get_a_review_page_when_there_is_none(wiring):
    """PUBLIC_BASE_URL 이 없으면 링크가 없습니다 — 막다른 길로 두지 않습니다."""
    services, sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금", "본문")

    await notify_draft_ready(services, draft_id)

    assert "PUBLIC_BASE_URL" in sender.lawyer_notes[0]


@pytest.mark.asyncio
async def test_the_notice_links_straight_to_the_draft_when_it_can(wiring):
    services, sender = wiring
    object.__setattr__(services.settings, "public_base_url", "https://moa.example.com")
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금", "본문")

    await notify_draft_ready(services, draft_id)

    assert f"https://moa.example.com/admin/drafts/{draft_id}?token=admin-token" in (
        sender.lawyer_notes[0]
    )


# ── /초안 12 로 다시 받기 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_lawyer_can_ask_for_a_draft_again_from_the_room(wiring):
    from kakao_legal_bot.app.commands import handle_command
    from kakao_legal_bot.app.trigger import Action, Decision

    from .conftest import make_event

    services, sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "보증금", "다시 보고 싶은 본문")

    await handle_command(
        services,
        make_event(f"/초안 {draft_id}", sender_id="lawyer-uid"),
        Decision(action=Action.COMMAND, command="draft_list", args=str(draft_id), is_lawyer=True),
    )

    assert any("다시 보고 싶은 본문" in note for note in sender.lawyer_notes)


@pytest.mark.asyncio
async def test_asking_for_a_draft_that_is_still_being_written_says_so(wiring):
    from kakao_legal_bot.app.commands import handle_command
    from kakao_legal_bot.app.trigger import Action, Decision

    from .conftest import make_event

    services, sender = wiring
    draft_id = services.db.create_draft(
        "room-1", "소장", "대여금", "", None, "", "pending_generation", "지시"
    )

    await handle_command(
        services,
        make_event(f"/초안 {draft_id}", sender_id="lawyer-uid"),
        Decision(action=Action.COMMAND, command="draft_list", args=str(draft_id), is_lawyer=True),
    )

    assert any("아직 작성 중" in text for text in sender.texts)
    assert sender.lawyer_notes == []


# ── 대시보드 ─────────────────────────────────────────────────────────────
def test_the_dashboard_needs_the_token(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin?token=admin-token").status_code == 200


def test_the_dashboard_leads_with_what_needs_the_lawyer(client):
    db = client.services.db
    db.upsert_room("room-1", "홍길동")
    db.create_draft("room-1", "내용증명", "보증금 반환 청구", "본문")

    page = client.get("/admin?token=admin-token").text

    assert "업무 현황" in page
    assert "검토 대기 초안" in page
    assert "보증금 반환 청구" in page
    assert "김변호사" in page


def test_an_empty_desk_says_so_instead_of_showing_nothing(client):
    page = client.get("/admin?token=admin-token").text
    assert "검토하실 초안이 없습니다" in page


def test_the_dashboard_shows_intakes_in_progress(client):
    db = client.services.db
    db.upsert_room("room-1", "홍길동")
    db.open_intake("room-1", "고소장", "절도", track="criminal")

    page = client.get("/admin?token=admin-token").text

    assert "진행 중 인테이크" in page
    assert "고소장" in page
    assert "절도" in page
    assert "(형사)" in page
    assert "폼 보냄" in page


def test_the_dashboard_shows_a_stalled_worker_job(client):
    db = client.services.db
    db.upsert_room("room-1")
    db.create_draft("room-1", "소장", "대여금", "", None, "", "pending_generation", "지시")

    page = client.get("/admin?token=admin-token").text

    assert "작성 대기" in page
    assert "PC가 켜져 있는지" in page


def test_a_failed_draft_is_shown_with_its_error(client):
    db = client.services.db
    db.upsert_room("room-1")
    draft_id = db.create_draft(
        "room-1", "소장", "대여금", "", None, "", "pending_generation", "지시"
    )
    db.claim_draft_jobs(1)
    db.fail_draft_generation(draft_id, "코덱스 실행 실패", max_attempts=1)

    page = client.get("/admin?token=admin-token").text

    assert "작성 실패" in page
    assert "코덱스 실행 실패" in page


def test_escalated_consultations_are_listed(client):
    db = client.services.db
    db.upsert_room("room-1", "홍길동")
    consultation = db.get_or_create_consultation("room-1", "홍길동")
    db.update_consultation(int(consultation["id"]), status="awaiting_lawyer")

    page = client.get("/admin?token=admin-token").text

    assert "변호사 확인 요청" in page
    assert "홍길동" in page


def test_the_room_list_shows_the_last_thing_said(client):
    db = client.services.db
    db.upsert_room("room-1", "홍길동")
    db.add_message("room-1", "user", "전세금을 못 받고 있어요", "홍길동")

    page = client.get("/admin?token=admin-token").text

    assert "상담방" in page
    assert "전세금을 못 받고 있어요" in page
    assert "상담자:" in page


def test_todays_counters_only_count_today(client):
    db = client.services.db
    db.upsert_room("room-1")
    db.add_message("room-1", "user", "오늘 질문")
    db.log_answer("room-1", "오늘 질문", "오늘 답변")
    # 어제 것은 세지 않는다.
    db._exec(  # noqa: SLF001 — 테스트에서 시간을 되돌리는 유일한 방법
        "UPDATE answers SET created_at = ? WHERE id = 1", (time.time() - 86400 * 2,)
    )

    snapshot = db.dashboard_snapshot(time.time() - 3600)
    assert snapshot["today"]["questions"] == 1
    assert snapshot["today"]["answers"] == 0


def test_the_snapshot_survives_an_empty_database(db):
    snapshot = db.dashboard_snapshot(time.time() - 3600)
    assert snapshot["today"]["questions"] == 0
    assert snapshot["rooms"] == []
    assert snapshot["drafts_by_status"] == {}
