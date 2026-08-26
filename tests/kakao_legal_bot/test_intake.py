"""문서작성 인테이크 — 폼 → 요건사실 문답 → 상담보고서 → 견적 → 초안.

The point of the flow is that a document is never drafted on top of facts
nobody checked. Each test below pins one way that could quietly stop being
true.
"""

from __future__ import annotations

import asyncio

import pytest

from kakao_legal_bot.app import intake as intake_states
from kakao_legal_bot.app.intake import (
    COMPLEX,
    INTAKE_FORM,
    MEDIUM,
    SIMPLE,
    TIERS,
    quote_text,
    tier_for,
)
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.knowledge import (
    available_case_keys,
    case_type_index,
    find_case_type,
    knowledge_stats,
    requisite_facts_for,
)
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.tools import TurnState, build_intake_tools
from kakao_legal_bot.app.workflows import create_draft

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


def tools_for(state: TurnState) -> dict:
    return {tool.name: tool for tool in build_intake_tools(state, "김변호사")}


# ── 요건사실 지식 ─────────────────────────────────────────────────────────
def test_all_three_uploaded_files_are_parsed():
    stats = knowledge_stats()
    assert stats["claim_sections"] >= 20  # 청구원인 사건유형
    assert stats["defense_sections"] >= 13  # 항변
    assert stats["civil_items"] == 40  # 민법 요건사실 40개


def test_the_index_stays_small_enough_to_live_in_every_prompt():
    """색인은 상주하므로 짧아야 하고, 본문은 필요할 때만 꺼내야 합니다."""
    index = case_type_index()
    assert len(index) < 1500
    assert len(requisite_facts_for("대여금")) > len(index)


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("전세금을 못 받고 있어요", "임대차보증금반환"),
        ("빌려준 돈을 안 갚습니다", "대여금"),
        ("교통사고 손해배상 받고 싶어요", "손해배상_불법행위"),
        ("인테리어 공사비를 못 받았습니다", "공사대금"),
        ("착오송금했어요", "부당이득"),
        ("대여금", "대여금"),
    ],
)
def test_everyday_words_map_to_a_case_type(said, expected):
    case = find_case_type(said)
    assert case is not None and case.key == expected


def test_an_unknown_case_type_is_not_forced_into_a_wrong_one():
    assert find_case_type("우주선 등록 절차") is None
    assert find_case_type("") is None


def test_requisite_facts_carry_claim_defence_and_civil_parts():
    text = requisite_facts_for("대여금")
    assert "청구원인 요건사실" in text
    assert "소비대차" in text
    assert "자주 나오는 항변" in text
    assert "소멸시효" in text  # 대여금에서 가장 흔한 항변
    assert "관련 민법 요건사실" in text


def test_every_case_type_actually_resolves_to_content():
    """표에 있는데 본문이 안 나오는 유형이 있으면 그 상담은 빈손이 됩니다."""
    for key in available_case_keys():
        text = requisite_facts_for(key)
        assert "청구원인 요건사실" in text, key
        assert len(text) > 200, key


# ── 등급과 비용 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("doc", "tier_key", "price"),
    [
        ("내용증명", SIMPLE, 100_000),
        ("합의서", SIMPLE, 100_000),
        ("문서송부촉탁서", MEDIUM, 200_000),
        ("증인신청서", MEDIUM, 200_000),
        ("사실조회신청서", MEDIUM, 200_000),
        ("소장", COMPLEX, 300_000),
        ("답변서", COMPLEX, 300_000),
        ("준비서면", COMPLEX, 300_000),
        ("참고서면", COMPLEX, 300_000),
    ],
)
def test_pricing_matches_the_lawyers_table(doc, tier_key, price):
    tier = tier_for(doc)
    assert tier.key == tier_key
    assert tier.price_krw == price


def test_a_qualified_document_name_still_lands_in_the_right_tier():
    assert tier_for("보증금 반환 청구 소장").key == COMPLEX
    assert tier_for("반박 준비서면").key == COMPLEX
    assert tier_for("임대인에게 보낼 내용증명").key == SIMPLE


def test_an_unknown_document_is_quoted_low_not_high():
    """비싸게 불러 놓고 깎는 것보다 싸게 부르고 변호사가 올리는 편이 낫습니다."""
    assert tier_for("무슨무슨 서면").key == SIMPLE


def test_the_quote_says_price_time_and_who_finalises_it():
    text = quote_text("소장", "김변호사")
    assert "300,000원" in text
    assert TIERS[COMPLEX].lead_time in text
    assert "김변호사" in text
    assert "부가세 별도" in text


# ── 도구 동작 ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_starting_an_intake_returns_the_form_and_the_requisite_facts():
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler(
        {"doc_kind": "내용증명", "case_type": "전세금을 못 받고 있어요"}
    )

    assert INTAKE_FORM in result
    assert "청구원인 요건사실" in result  # 요건사실이 함께 왔다
    assert "임대차" in result
    # 비용은 아직 상담자에게 말하지 않는다
    assert "지금 말하지 말고" in result

    assert [a.kind for a in state.intake_actions] == ["start"]
    assert state.intake_actions[0].case_type == "임대차보증금반환"


@pytest.mark.asyncio
async def test_an_unknown_case_type_gets_the_index_to_choose_from():
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler({"doc_kind": "내용증명"})

    assert INTAKE_FORM in result
    assert "get_requisite_facts" in result
    assert "대여금" in result  # 색인이 함께 왔다


@pytest.mark.asyncio
async def test_requisite_facts_can_be_fetched_later_when_the_type_becomes_clear():
    state = TurnState()
    result = await tools_for(state)["get_requisite_facts"].handler({"case_type": "대여금"})

    assert "소비대차" in result
    assert [a.kind for a in state.intake_actions] == ["case_type"]
    assert state.intake_actions[0].case_type == "대여금"


@pytest.mark.asyncio
async def test_a_bad_case_type_lists_the_options_instead_of_failing():
    state = TurnState()
    result = await tools_for(state)["get_requisite_facts"].handler({"case_type": "???"})

    assert "찾지 못했습니다" in result
    assert "임대차보증금반환" in result
    assert state.intake_actions == []  # 아무것도 기록하지 않는다


@pytest.mark.asyncio
async def test_submitting_the_report_returns_the_quote_to_show_the_client():
    state = TurnState()
    result = await tools_for(state)["submit_consultation_report"].handler(
        {"report": "# 상담보고서\n…", "doc_kind": "소장"}
    )

    assert "300,000원" in result
    assert "request_document_draft" in result  # 다음에 할 일을 알려준다
    assert [a.kind for a in state.intake_actions] == ["report"]
    assert state.intake_actions[0].report.startswith("# 상담보고서")


@pytest.mark.asyncio
async def test_an_empty_report_is_refused():
    state = TurnState()
    result = await tools_for(state)["submit_consultation_report"].handler(
        {"report": "   ", "doc_kind": "소장"}
    )

    assert "비어 있습니다" in result
    assert state.intake_actions == []


# ── 저장과 초안 연결 ──────────────────────────────────────────────────────
def test_one_room_runs_one_intake_at_a_time(db):
    """두 개가 동시에 돌면 반쯤 채워진 보고서가 두 개 나옵니다."""
    db.upsert_room("room-1")
    first = db.open_intake("room-1", "내용증명", "대여금")
    second = db.open_intake("room-1", "소장", "")

    assert first["id"] == second["id"]
    assert second["doc_kind"] == "소장"  # 문서 종류는 갱신되고
    assert second["case_type"] == "대여금"  # 빈 값이 기존 값을 지우지는 않는다


def test_a_closed_intake_does_not_block_the_next_one(db):
    db.upsert_room("room-1")
    first = db.open_intake("room-1", "내용증명", "대여금")
    db.update_intake(int(first["id"]), status="confirmed")

    second = db.open_intake("room-1", "소장", "매매대금")
    assert second["id"] != first["id"]
    assert db.active_intake("room-1")["id"] == second["id"]


def test_intakes_are_per_room(db):
    db.upsert_room("room-1")
    db.upsert_room("room-2")
    a = db.open_intake("room-1", "내용증명", "대여금")
    b = db.open_intake("room-2", "소장", "매매대금")

    assert a["id"] != b["id"]
    assert db.active_intake("room-1")["doc_kind"] == "내용증명"
    assert db.active_intake("room-2")["doc_kind"] == "소장"


@pytest.mark.asyncio
async def test_the_report_becomes_the_brief_the_writer_works_from(wiring):
    """보고서가 있는데도 원본 대화만 넘기면 인테이크가 헛일이 됩니다."""
    services, _sender = wiring
    services.db.upsert_room("room-1")
    intake = services.db.open_intake("room-1", "내용증명", "대여금")
    services.db.update_intake(
        int(intake["id"]),
        report="# 상담보고서\n채권자 홍길동, 채무자 김철수, 3천만원, 변제기 2026-03-01",
        status="report_review",
    )

    from kakao_legal_bot.app.tools import DraftRequest

    draft_id = await create_draft(
        services,
        "room-1",
        DraftRequest(kind="내용증명", title="대여금 반환 청구", instructions="2주 기한으로"),
    )

    draft = services.db.get_draft(draft_id)
    assert "상담보고서" in draft.instructions
    assert "변제기 2026-03-01" in draft.instructions
    assert "2주 기한으로" in draft.instructions  # 원래 지시도 남아 있다

    closed = services.db.get_intake(int(intake["id"]))
    assert closed["status"] == intake_states.CONFIRMED
    assert closed["draft_id"] == draft_id


@pytest.mark.asyncio
async def test_a_draft_without_an_intake_still_works(wiring):
    """인테이크를 건너뛴 옛 경로가 깨지면 안 됩니다."""
    services, _sender = wiring
    services.db.upsert_room("room-1")

    from kakao_legal_bot.app.tools import DraftRequest

    draft_id = await create_draft(
        services, "room-1", DraftRequest(kind="합의서", title="합의서", instructions="합의 조건")
    )

    assert services.db.get_draft(draft_id).instructions == "합의 조건"


def test_an_empty_report_is_not_treated_as_a_brief(db):
    """폼만 보내고 아직 보고서가 없는 상태에서는 붙일 것이 없습니다."""
    db.upsert_room("room-1")
    intake = db.open_intake("room-1", "내용증명", "대여금")
    assert db.active_intake("room-1")["report"] == ""
    assert intake["status"] == intake_states.FORM_SENT
