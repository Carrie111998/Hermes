"""사건파일 — 완결 상담의 4종 합본, 가명화 색인, 위키 작업열.

발송이 끝난 사건은 상담보고서·대화 전체·LLM 초안·변호사 최종본이 모두
존재하는 유일한 순간입니다. 그 넷이 한 파일로 남고, 실명을 지운 사본이
상담사례 검색에 들어가는지를 봅니다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kakao_legal_bot.app.casefile import (
    archive_case,
    build_casefile,
    casefile_markdown,
    redact,
)
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.workflows import send_draft

from .conftest import FakeAgent, FakeSender


def finished_case(db) -> int:
    """보고서·대화·초안·최종본이 전부 있는 발송 완료 사건 하나."""
    db.upsert_room("room-c", "테스트방", "direct")
    db.set_room_label("room-c", "홍길동-2026-08-29")
    db.get_or_create_consultation("room-c", "홍길동")
    db.add_message("room-c", "user", "3천만원을 빌려줬는데 안 갚아요. 010-1234-5678 입니다",
                   archive=True, archive_sender="홍길동")
    db.add_message("room-c", "bot", "변제기를 알려주세요", sender="모아", archive=True)
    draft_id = db.create_draft(
        "room-c", "내용증명", "대여금 반환 청구", "LLM이 쓴 초안 본문",
        client_email="hong@example.com", status="pending_review",
    )
    intake = db.open_intake("room-c", "내용증명", "대여금")
    db.update_intake(
        int(intake["id"]),
        report="# 상담보고서\n민법 제598조 소비대차. 변제기 도과.",
        missing="이자 약정",
        status="confirmed",
        draft_id=draft_id,
    )
    # 변호사가 본문을 고치고 승인·발송까지 갔다.
    db.update_draft(draft_id, body="변호사가 고친 최종본", status="sent",
                    lawyer_note="기한 3주로 수정")
    return draft_id


# ── llm_body 스냅샷 ──────────────────────────────────────────────────────
def test_the_llm_draft_survives_the_lawyers_edits(db):
    draft_id = db.create_draft("room-1", "내용증명", "제목", "초안 원본")
    db.update_draft(draft_id, body="변호사 수정본")
    draft = db.get_draft(draft_id)
    assert draft.body == "변호사 수정본"
    assert draft.llm_body == "초안 원본"  # 무엇을 고쳤는지가 남는다


def test_a_worker_delivery_also_snapshots_the_original(db):
    draft_id = db.create_draft("room-1", "소장", "제목", "", status="pending_generation")
    db.claim_draft_jobs(1)
    db.complete_draft_generation(draft_id, "워커가 쓴 초안")
    db.update_draft(draft_id, body="변호사 수정본")
    assert db.get_draft(draft_id).llm_body == "워커가 쓴 초안"


# ── 4종 합본 ─────────────────────────────────────────────────────────────
def test_the_casefile_carries_all_four_parts_in_order(settings, db):
    draft_id = finished_case(db)
    _, text = casefile_markdown(db, settings, db.get_draft(draft_id))

    report = text.index("## 1. 상담보고서")
    chat = text.index("## 2. 대화 전체")
    llm = text.index("## 3. LLM 초안")
    final = text.index("## 4. 최종 문서")
    assert report < chat < llm < final

    assert "민법 제598조" in text                 # 보고서 (법령 인용 포함)
    assert "미확인 사항: 이자 약정" in text
    assert "3천만원을 빌려줬는데" in text          # 대화
    assert "LLM이 쓴 초안 본문" in text            # 초안 원본
    assert "변호사가 고친 최종본" in text          # 최종본
    assert "[변호사 메모] 기한 3주로 수정" in text
    assert "kind: 상담사례" in text               # 위키 raw 층 frontmatter


def test_the_casefile_is_named_after_the_room_label(settings, db):
    draft_id = finished_case(db)
    path = build_casefile(db, settings, draft_id)
    assert path.name == f"홍길동-2026-08-29-사건{draft_id}.md"
    assert path.parent == settings.casefile_dir


def test_a_case_without_a_report_still_files(settings, db):
    db.upsert_room("room-n", "방", "direct")
    draft_id = db.create_draft("room-n", "내용증명", "제목", "본문", status="sent")
    _, text = casefile_markdown(db, settings, db.get_draft(draft_id))
    assert "상담보고서 없음" in text


# ── 가명화 ───────────────────────────────────────────────────────────────
def test_redaction_strips_names_and_contact_details():
    text = "채권자 홍길동(hong@example.com, 010-1234-5678, 900101-1234567)이 청구"
    cleaned = redact(text, ["홍길동"])
    assert "홍길동" not in cleaned and "상담자A" in cleaned
    assert "hong@example.com" not in cleaned
    assert "010-1234-5678" not in cleaned
    assert "900101-1234567" not in cleaned


def test_redaction_does_not_touch_the_original_file(settings, db):
    draft_id = finished_case(db)
    path = archive_case(db, settings, draft_id)
    raw = path.read_text(encoding="utf-8")
    # 원본 사건파일은 변호사의 기록 — 실명 그대로.
    assert "홍길동" in raw and "010-1234-5678" in raw


def test_the_search_copy_is_redacted(settings, db):
    from kakao_legal_bot.app.rag.store import RagStore

    draft_id = finished_case(db)
    archive_case(db, settings, draft_id)

    store = RagStore(settings.rag_path("cases"))
    try:
        hits = store.search("대여금 변제기", top_k=5)
        assert hits, "상담사례 색인에서 검색이 되어야 한다"
        joined = " ".join(hit.text for hit in hits)
        assert "홍길동" not in joined          # 실명은 색인에 없다
        assert "010-1234-5678" not in joined
    finally:
        store.close()


# ── 위키 작업열 ──────────────────────────────────────────────────────────
def test_archiving_appends_a_codex_wiki_job(settings, db):
    draft_id = finished_case(db)
    path = archive_case(db, settings, draft_id)

    jobs_file = settings.casefile_dir / "_wiki-jobs.jsonl"
    jobs = [json.loads(line) for line in jobs_file.read_text(encoding="utf-8").splitlines()]
    job = jobs[-1]
    assert job["kind"] == "consult_case"
    assert job["raw"] == str(path)
    assert job["case_type"] == "대여금"
    assert "상담사례" in job["wiki"]


# ── 발송 훅 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sending_the_final_document_files_the_case(settings, db, monkeypatch):
    async def fake_send_email(settings, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr("kakao_legal_bot.app.workflows.send_email", fake_send_email)

    draft_id = finished_case(db)
    db.update_draft(draft_id, status="approved")
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=FakeSender(),
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(1),
    )

    ok, message = await send_draft(services, draft_id)

    assert ok is True
    assert "사건파일 보관" in message
    files = list(settings.casefile_dir.glob("*.md"))
    assert len(files) == 1
    assert settings.rag_path("cases").exists()  # 상담사례 색인까지 만들어졌다


@pytest.mark.asyncio
async def test_the_casefile_switch_can_turn_it_off(settings, db, monkeypatch):
    async def fake_send_email(settings, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr("kakao_legal_bot.app.workflows.send_email", fake_send_email)
    object.__setattr__(settings, "casefile_enabled", False)

    draft_id = finished_case(db)
    db.update_draft(draft_id, status="approved")
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=FakeSender(),
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(1),
    )
    ok, _ = await send_draft(services, draft_id)
    assert ok is True
    assert not settings.casefile_dir.exists()


# ── 위키 스키마 층 ───────────────────────────────────────────────────────
def test_the_vault_schema_command_installs_agents_md(tmp_path, monkeypatch):
    from kakao_legal_bot.app.wiki import build

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    vault = tmp_path / "vault"
    code = build.main(["schema", "--wiki", str(vault / "wiki")])
    assert code == 0
    agents = (vault / "AGENTS.md").read_text(encoding="utf-8")
    assert "가명화" in agents            # 상담사례 규칙이 스키마에 들어 있다
    assert "raw/" in agents and "index.md" in agents
    assert (vault / "index.md").exists() and (vault / "log.md").exists()


def test_consult_case_is_a_known_wiki_kind():
    from kakao_legal_bot.app.wiki.note import KINDS, WikiNote

    assert "상담사례" in KINDS
    note = WikiNote(title="사례", kind="상담사례", written_on="2026-08-29")
    assert note.missing_required() == []
