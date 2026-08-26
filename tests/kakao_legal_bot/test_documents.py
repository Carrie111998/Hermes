"""Document generation, e-mail assembly, and the approval gate.

The gate is the important one: a model-written legal document must not be
able to reach a client's inbox without the lawyer having approved it.
"""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO

import pytest

from kakao_legal_bot.app.docxgen import build_docx, safe_filename
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.mailer import Attachment, MailError, build_message
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.tools import DraftRequest
from kakao_legal_bot.app.workflows import create_draft, send_draft

from .conftest import FakeAgent, FakeSender


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


# ── .docx ────────────────────────────────────────────────────────────────
def test_docx_is_a_valid_zip_with_the_required_parts():
    data = build_docx("내용증명", "첫 문단\n\n둘째 문단")
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names
        document = archive.read("word/document.xml").decode("utf-8")
    assert "내용증명" in document
    assert "첫 문단" in document


def test_docx_escapes_xml_metacharacters():
    document = build_docx("제목", "채권자 <홍길동> & 채무자")
    with zipfile.ZipFile(BytesIO(document)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "&lt;홍길동&gt;" in xml
    assert "&amp;" in xml


def test_docx_renders_markdown_headings_as_bold_runs():
    document = build_docx("", "# 청구취지\n\n본문")
    with zipfile.ZipFile(BytesIO(document)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "<w:b/>" in xml
    assert "청구취지" in xml
    assert "# 청구취지" not in xml


def test_safe_filename_keeps_hangul_and_drops_separators():
    assert safe_filename("내용증명/보증금 반환") == "내용증명보증금 반환.docx"
    assert safe_filename("") == "document.docx"


# ── e-mail ───────────────────────────────────────────────────────────────
def test_message_carries_the_attachment_and_reply_to(settings):
    object.__setattr__(settings, "smtp_from", "office@example.com")
    object.__setattr__(settings, "lawyer_email", "kim@example.com")

    message = build_message(
        settings,
        to="hong@example.com",
        subject="[김변호사] 내용증명",
        body="첨부드립니다.",
        attachments=[Attachment(filename="내용증명.docx", content=b"PK\x03\x04")],
    )
    assert message["To"] == "hong@example.com"
    assert message["Reply-To"] == "kim@example.com"
    assert "김변호사" in message["From"]
    assert [part.get_filename() for part in message.iter_attachments()] == ["내용증명.docx"]


def test_message_refuses_a_bad_recipient(settings):
    object.__setattr__(settings, "smtp_from", "office@example.com")
    with pytest.raises(MailError):
        build_message(settings, to="not-an-email", subject="s", body="b")


def test_message_refuses_without_a_sender(settings):
    with pytest.raises(MailError, match="SMTP_FROM"):
        build_message(settings, to="hong@example.com", subject="s", body="b")


# ── approval gate ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unapproved_drafts_never_leave_the_building(wiring):
    services, _sender = wiring
    draft_id = services.db.create_draft(
        "room-1", "내용증명", "제목", "본문", client_email="hong@example.com"
    )
    ok, message = await send_draft(services, draft_id)
    assert ok is False
    assert "승인 전" in message


@pytest.mark.asyncio
async def test_send_without_an_address_is_refused(wiring):
    services, _sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "제목", "본문")
    services.db.update_draft(draft_id, status="approved")
    ok, message = await send_draft(services, draft_id)
    assert ok is False
    assert "이메일이 없습니다" in message


@pytest.mark.asyncio
async def test_approved_draft_is_sent_and_marked(wiring, monkeypatch):
    services, _sender = wiring
    sent: list = []

    async def fake_send_email(settings, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("kakao_legal_bot.app.workflows.send_email", fake_send_email)

    draft_id = services.db.create_draft(
        "room-1", "내용증명", "보증금 반환 청구", "본문", client_email="hong@example.com"
    )
    services.db.update_draft(draft_id, status="approved", lawyer_note="기한 3주로 수정")

    ok, message = await send_draft(services, draft_id)

    assert ok is True
    assert "hong@example.com" in message
    assert sent[0]["to"] == "hong@example.com"
    assert "기한 3주로 수정" in sent[0]["body"]
    assert sent[0]["attachments"][0].filename == "보증금 반환 청구.docx"

    draft = services.db.get_draft(draft_id)
    assert draft.status == "sent"
    assert draft.sent_at


@pytest.mark.asyncio
async def test_missing_draft_is_reported(wiring):
    services, _sender = wiring
    ok, message = await send_draft(services, 9999)
    assert ok is False
    assert "찾을 수 없습니다" in message


# ── draft creation ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_draft_queues_it_and_pings_the_lawyer(wiring):
    services, sender = wiring
    services.db.upsert_room("room-1")
    consultation = services.db.get_or_create_consultation("room-1", "홍길동")
    services.db.update_consultation(int(consultation["id"]), client_email="hong@example.com")

    draft_id = await create_draft(
        services,
        "room-1",
        DraftRequest(kind="내용증명", title="보증금 반환", instructions="3천만원 반환 청구"),
        int(consultation["id"]),
    )

    draft = services.db.get_draft(draft_id)
    assert draft.status == "pending_review"
    assert draft.client_email == "hong@example.com"
    assert any("초안 준비됨" in note for note in sender.lawyer_notes)


@pytest.mark.asyncio
async def test_a_failed_generation_still_queues_the_request(wiring):
    services, _sender = wiring

    async def explode(*args, **kwargs):
        raise RuntimeError("모델 다운")

    services.agent.draft_document = explode
    services.db.upsert_room("room-1")

    draft_id = await create_draft(
        services, "room-1", DraftRequest(kind="합의서", title="합의서", instructions="합의 조건 정리")
    )
    body = services.db.get_draft(draft_id).body
    assert "자동 초안 생성 실패" in body
    assert "합의 조건 정리" in body
