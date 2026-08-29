"""증거 사진·파일 접수 + 실시간 방 화면.

알림 기반 수신에서는 "사진을 보냈습니다"라는 글자만 오므로, 봇은 전용
업로드 링크를 안내하고, 올라온 파일은 변호사 알림·실시간 화면·AI 문맥에
동시에 반영되어야 합니다.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest

pytest.importorskip("fastapi")

from kakao_legal_bot.app.iris import IrisClient  # noqa: E402
from kakao_legal_bot.app.pipeline import Pipeline  # noqa: E402
from kakao_legal_bot.app.services import Services  # noqa: E402
from kakao_legal_bot.app.uploads import (  # noqa: E402
    clean_filename,
    media_kind,
    room_for_upload_token,
    upload_guidance,
    upload_token_for,
)

from .conftest import FakeAgent, FakeSender  # noqa: E402
from .test_webhook import client  # noqa: E402, F401 — TestClient 픽스처 재사용


# ── 미디어 자리표시 문구 감지 ────────────────────────────────────────────
def test_media_notices_are_recognised():
    assert media_kind("사진을 보냈습니다") == "사진"
    assert media_kind("동영상을 보냈습니다") == "동영상"
    assert media_kind("파일을 보냈습니다") == "파일"
    assert media_kind("음성메시지를 보냈습니다") == "음성"
    assert media_kind("사진") == "사진"


def test_ordinary_questions_are_not_media():
    assert media_kind("사진을 찍어서 보내드릴까요?") is None
    assert media_kind("계약서 파일을 보냈는데 문제가 있어요") is None
    assert media_kind("") is None


# ── 방별 업로드 토큰 ─────────────────────────────────────────────────────
def test_the_upload_token_is_stable_and_reversible(db):
    token = upload_token_for(db, "room-1")
    assert upload_token_for(db, "room-1") == token  # 링크는 변하지 않는다
    assert room_for_upload_token(db, token) == "room-1"
    assert room_for_upload_token(db, "없는토큰없는토큰") == ""
    assert room_for_upload_token(db, "../../etc") == ""


def test_guidance_carries_the_link_when_the_server_has_an_address(settings, db):
    object.__setattr__(settings, "public_base_url", "https://moa.test")
    text = upload_guidance(settings, db, "room-1", "사진")
    assert "https://moa.test/u/" in text
    assert "열람할 수 없" in text


def test_guidance_falls_back_to_email_without_an_address(settings, db):
    text = upload_guidance(settings, db, "room-1", "파일")
    assert "/이메일" in text and "/u/" not in text


def test_voice_guidance_teaches_the_keyboard_mic(settings, db):
    object.__setattr__(settings, "public_base_url", "https://moa.test")
    text = upload_guidance(settings, db, "room-1", "음성")
    assert "음성 입력" in text  # 키보드 마이크로 말하면 글자로 온다


def test_clean_filename_stops_path_tricks():
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("계약서 (최종).pdf") == "계약서 (최종).pdf"
    assert clean_filename("") == "파일"


# ── 파이프라인 — 사진이 오면 LLM 대신 안내가 나간다 ──────────────────────
@pytest.mark.asyncio
async def test_a_photo_notice_gets_the_upload_link_not_an_llm_answer(settings, db):
    from .conftest import make_event

    object.__setattr__(settings, "public_base_url", "https://moa.test")
    sender = FakeSender()
    agent = FakeAgent("답변")
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=agent, semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)

    await pipeline.handle(make_event("사진을 보냈습니다", room_id="room-m", direct=True))

    assert agent.calls == []  # 내용 없는 자리표시 문구로 LLM 을 부르지 않는다
    assert any("/u/" in text for text in sender.texts)


# ── 업로드 엔드포인트 ────────────────────────────────────────────────────
def test_uploading_a_file_reaches_lawyer_ai_and_disk(client):
    services = client.services
    settings = services.settings
    object.__setattr__(settings, "public_base_url", "https://moa.test")
    services.db.upsert_room("room-1", "상담방", "direct")
    services.db.set_room_label("room-1", "홍길동-2026-08-29")
    token = upload_token_for(services.db, "room-1")

    assert client.get(f"/u/{token}").status_code == 200
    assert client.get("/u/틀린토큰틀린토큰").status_code == 404

    response = client.post(
        f"/u/{token}",
        files=[("files", ("차용증.jpg", io.BytesIO(b"jpegdata"), "image/jpeg"))],
    )
    assert response.status_code == 200
    assert "접수되었습니다" in response.text

    rows = services.db.list_uploads("room-1")
    assert len(rows) == 1 and rows[0]["filename"] == "차용증.jpg"

    from kakao_legal_bot.app.uploads import stored_path

    assert stored_path(settings, rows[0]).read_bytes() == b"jpegdata"

    # AI 문맥: 다음 턴에 모델이 자료 도착을 안다.
    history = services.db.recent_messages("room-1")
    assert any(m.role == "system" and "차용증.jpg" in m.text for m in history)

    # 상담자에게 접수 확인이 나간다.
    assert any("접수했습니다" in text for text in client.fake_sender.texts)

    # 변호사 알림(백그라운드) — 잠깐 기다려 확인.
    for _ in range(50):
        if client.fake_sender.lawyer_notes:
            break
        time.sleep(0.02)
    note = client.fake_sender.lawyer_notes[0]
    assert "📎" in note and "홍길동-2026-08-29" in note and "차용증.jpg" in note


def test_oversized_files_are_rejected_not_saved(client):
    services = client.services
    object.__setattr__(services.settings, "upload_max_mb", 0)  # 한도 0 → 전부 초과
    services.db.upsert_room("room-1")
    token = upload_token_for(services.db, "room-1")

    response = client.post(
        f"/u/{token}",
        files=[("files", ("큰파일.zip", io.BytesIO(b"x" * 1024), "application/zip"))],
    )
    assert "용량 초과" in response.text
    assert services.db.list_uploads("room-1") == []


# ── 실시간 방 화면 ───────────────────────────────────────────────────────
def test_the_live_room_page_shows_transcript_and_uploads(client):
    services = client.services
    services.db.upsert_room("room-1", "상담방", "direct")
    services.db.set_room_label("room-1", "홍길동-2026-08-29")
    services.db.add_message("room-1", "user", "돈을 못 받고 있어요",
                            archive=True, archive_sender="홍길동")
    services.db.add_message("room-1", "bot", "언제 빌려주셨나요?", sender="모아", archive=True)
    services.db.add_upload("room-1", "차용증.jpg", "x.jpg", "image/jpeg", 1000)

    page = client.get("/admin/rooms/room-1?token=admin-token").text
    assert "홍길동-2026-08-29" in page
    assert "돈을 못 받고 있어요" in page and "언제 빌려주셨나요?" in page
    assert "차용증.jpg" in page
    assert "location.reload" in page  # 생중계 — 자동 새로고침


def test_the_lawyer_can_speak_into_the_room_from_the_page(client):
    client.services.db.upsert_room("room-1", "상담방", "direct")
    response = client.post(
        "/admin/rooms/room-1/say?token=admin-token",
        data={"text": "변호사입니다. 제가 이어서 안내드릴게요."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    sent = client.fake_sender.sent
    assert sent and sent[-1][0] == "room-1"
    assert sent[-1][1].startswith("[김변호사]")


def test_the_room_page_requires_the_admin_token(client):
    client.services.db.upsert_room("room-1")
    assert client.get("/admin/rooms/room-1").status_code == 401


# ── 접수 알림에 실시간 링크가 붙는다 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_the_first_alert_links_to_the_live_view(settings, db):
    from .conftest import make_event

    object.__setattr__(settings, "public_base_url", "https://moa.test")
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)

    await pipeline.handle(make_event("이혼 문의", room_id="room-a", direct=True))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if sender.lawyer_notes:
            break

    note = next(note for note in sender.lawyer_notes if "새 상담" in note)
    assert "실시간 보기" in note and "/admin/rooms/room-a" in note
