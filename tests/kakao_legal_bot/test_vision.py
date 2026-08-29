"""업로드 자료의 제미나이 비전 분석 — 요약이 세 곳에 심어지는지.

네트워크는 목입니다. 검증 대상은 배관: 분석 결과가 uploads.summary(실시간
화면) · 대화 기록 시스템 메시지(AI 문맥) · 상담방/변호사 카톡에 닿는지,
그리고 분석 불가 상황이 접수 자체를 깨지 않는지.
"""

from __future__ import annotations

import base64
import io
import time

import pytest

pytest.importorskip("fastapi")

from kakao_legal_bot.app.uploads import upload_token_for  # noqa: E402
from kakao_legal_bot.app.vision import (  # noqa: E402
    MAX_ANALYZE_BYTES,
    build_request,
    describe_file,
    mime_for,
)

from .test_webhook import client  # noqa: E402, F401


# ── 어떤 파일을 분석하는가 ───────────────────────────────────────────────
def test_images_pdfs_and_recordings_are_analyzable():
    assert mime_for("차용증.jpg") == "image/jpeg"
    assert mime_for("계약서.PDF") == "application/pdf"
    assert mime_for("통화녹음.m4a") == "audio/mp4"
    assert mime_for("x", "image/png") == "image/png"


def test_unknown_formats_are_skipped_not_errored():
    assert mime_for("문서.hwp") == ""
    assert mime_for("압축.zip", "application/zip") == ""


def test_the_request_carries_the_file_inline():
    body = build_request("gemini-3.7-flash", "image/jpeg", b"jpegdata")
    part = body["contents"][0]["parts"][0]["inline_data"]
    assert part["mime_type"] == "image/jpeg"
    assert base64.b64decode(part["data"]) == b"jpegdata"
    prompt = body["contents"][0]["parts"][1]["text"]
    assert "추측하지 말고" in prompt  # 판독 불가는 판독 불가라고 말하게


@pytest.mark.asyncio
async def test_no_gemini_key_means_quiet_skip(settings, tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    assert await describe_file(settings, path, "a.jpg") == ""  # 키 없음 → 조용히


@pytest.mark.asyncio
async def test_oversized_files_are_not_sent_to_the_api(settings, tmp_path, monkeypatch):
    object.__setattr__(settings, "gemini_api_key", "test-key")
    called = []
    monkeypatch.setattr("httpx.AsyncClient.post", lambda *a, **k: called.append(1))
    path = tmp_path / "big.jpg"
    path.write_bytes(b"x" * (MAX_ANALYZE_BYTES + 1))
    assert await describe_file(settings, path, "big.jpg") == ""
    assert called == []


# ── 업로드 → 분석 → 세 곳 반영 ───────────────────────────────────────────
def upload_one(client, filename: str = "차용증.jpg") -> str:
    services = client.services
    services.db.upsert_room("room-1", "상담방", "direct")
    token = upload_token_for(services.db, "room-1")
    client.post(
        f"/u/{token}",
        files=[("files", (filename, io.BytesIO(b"jpegdata"), "image/jpeg"))],
    )
    return token


def wait_for(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_analysis_reaches_screen_context_and_kakao(client, monkeypatch):
    async def fake_describe(settings, path, filename, content_type=""):  # noqa: ANN001
        return "차용증. 채권자 홍길동, 채무자 김철수, 3천만원, 변제기 2026-01-01."

    monkeypatch.setattr("kakao_legal_bot.app.vision.describe_file", fake_describe)
    services = client.services

    upload_one(client)

    assert wait_for(lambda: bool(services.db.list_uploads("room-1")[0]["summary"]))
    row = services.db.list_uploads("room-1")[0]
    assert "3천만원" in row["summary"]  # ① 실시간 화면용

    def context_has_analysis() -> bool:
        return any(
            m.role == "system" and "[자료 분석]" in m.text and "3천만원" in m.text
            for m in services.db.recent_messages("room-1")
        )

    assert wait_for(context_has_analysis)  # ② 다음 턴부터 AI 가 안다

    assert wait_for(
        lambda: any("확인했습니다" in t and "3천만원" in t for t in client.fake_sender.texts)
    )  # ③ 상담자에게 읽은 내용을 되돌려준다
    assert wait_for(
        lambda: any("자료 분석 결과" in n for n in client.fake_sender.lawyer_notes)
    )

    # 실시간 방 화면에도 요약이 보인다.
    page = client.get("/admin/rooms/room-1?token=admin-token").text
    assert "3천만원" in page


def test_a_failed_analysis_never_breaks_the_intake(client, monkeypatch):
    async def broken(settings, path, filename, content_type=""):  # noqa: ANN001
        raise RuntimeError("api down")

    monkeypatch.setattr("kakao_legal_bot.app.vision.describe_file", broken)
    services = client.services

    upload_one(client)

    # 접수 자체(저장·목록·확인 메시지)는 멀쩡해야 한다.
    assert len(services.db.list_uploads("room-1")) == 1
    assert any("접수했습니다" in t for t in client.fake_sender.texts)
    time.sleep(0.1)
    assert services.db.list_uploads("room-1")[0]["summary"] == ""
