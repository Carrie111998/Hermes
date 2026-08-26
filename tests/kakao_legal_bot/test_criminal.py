"""형사 인테이크 — 죄명 확정 → 구성요건 → 6하원칙 질문.

민사와 갈리는 지점이 하나 있습니다. 미수·예비음모·상습범·과실범은 **그 죄에
처벌규정이 있을 때만** 문제되므로, 데이터에 없는 것을 일반론으로 메우면
고소장에 없는 죄가 들어갑니다. 아래 테스트는 그 "모르면 모른다고 한다"를
주로 붙잡아 둡니다.
"""

from __future__ import annotations

import pytest

from kakao_legal_bot.app import criminal
from kakao_legal_bot.app.criminal import (
    CRIMINAL_DIR,
    Punishability,
    all_crimes,
    crime_elements_for,
    crime_name_index,
    criminal_stats,
    find_crime,
    find_crimes,
    load_problems,
)
from kakao_legal_bot.app.intake import (
    COMPLEX,
    CRIMINAL_INTAKE_FORM,
    INTAKE_FORM,
    is_criminal_doc,
    tier_for,
)
from kakao_legal_bot.app.tools import TurnState, build_intake_tools


def tools_for(state: TurnState) -> dict:
    return {tool.name: tool for tool in build_intake_tools(state, "김변호사")}


# ── 데이터 ───────────────────────────────────────────────────────────────
def test_the_shipped_data_loads_without_complaints():
    """`--check` 가 통과해야 배포할 수 있습니다."""
    assert load_problems() == ()
    assert criminal_stats()["crimes"] >= 10


def test_every_crime_carries_what_a_question_needs():
    for crime in all_crimes():
        assert crime.article, crime.name
        assert crime.objective, crime.name
        assert crime.questions, crime.name


def test_special_statutes_use_the_same_shape():
    """특별형법은 `법률` 만 다릅니다 — 별도 코드 경로가 없어야 합니다."""
    special = [c for c in all_crimes() if c.statute != "형법"]
    assert special, "특별형법 예시가 하나는 있어야 형식을 보여줄 수 있습니다."
    for crime in special:
        assert crime.objective and crime.questions


def test_the_index_lists_names_only_so_the_prompt_does_not_balloon():
    index = crime_name_index()
    assert "절도" in index and "형법" in index
    # 구성요건 본문은 색인에 없다 — 죄명이 정해진 뒤 도구로 꺼낸다.
    assert "불법영득의사" not in index
    assert len(index) < 2000


# ── 죄명 찾기 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("물건을 훔쳐갔어요", "절도"),
        ("투자금을 돌려받지 못했습니다", "사기"),
        ("맞았어요", "폭행"),
        ("단톡방에 퍼뜨렸어요", "정보통신망 명예훼손"),
        ("절도", "절도"),
    ],
)
def test_everyday_words_map_to_a_crime(said, expected):
    crime = find_crime(said)
    assert crime is not None and crime.name == expected


def test_an_unknown_crime_is_not_forced_into_a_wrong_one():
    assert find_crime("우주선 무단 발사") is None
    assert find_crime("") is None


def test_ambiguous_words_come_back_as_candidates_not_a_guess():
    """'명예를 훼손' 은 형법과 정보통신망법 둘 다에 걸립니다."""
    candidates = find_crimes("인터넷에 글을 올려서 명예를 훼손했어요")
    names = {c.name for c in candidates}
    assert {"명예훼손", "정보통신망 명예훼손"} <= names


# ── 처벌규정: 모르면 모른다고 한다 ───────────────────────────────────────
def test_a_missing_provision_reads_as_not_punishable():
    assert Punishability(known=True, punishable=False).text.startswith("처벌규정 없음")


def test_an_absent_field_reads_as_needs_checking_not_as_no():
    """`null` 은 '처벌 안 됨'이 아니라 '데이터에 없음'입니다."""
    unknown = Punishability(known=False)
    assert "확인 필요" in unknown.text
    assert "처벌규정 없음" not in unknown.text


def test_a_punishable_provision_carries_its_article():
    assert Punishability(known=True, punishable=True, basis="형법 제342조").text == (
        "처벌 (형법 제342조)"
    )


def test_a_punishable_provision_without_an_article_still_asks_to_be_checked():
    assert "확인 필요" in Punishability(known=True, punishable=True).text


def test_the_elements_block_states_all_four_provisions_explicitly():
    text = crime_elements_for("절도")
    assert "미수: 처벌 (형법 제342조)" in text
    assert "예비·음모: 처벌규정 없음" in text
    assert "상습범 가중: 처벌 (형법 제332조)" in text
    assert "과실범: 처벌규정 없음" in text


def test_a_null_provision_surfaces_as_needs_checking_in_the_block():
    text = crime_elements_for("강제추행")
    assert "상습범 가중: 확인 필요" in text


def test_unverified_data_carries_a_warning_the_model_must_pass_on():
    text = crime_elements_for("절도")
    assert "미검증" in text
    assert "변호사" in text


def test_the_block_warns_against_generalising_about_the_four_provisions():
    text = crime_elements_for("사기")
    assert "처벌규정이 있는 경우에만" in text


def test_procedure_facts_are_spelled_out():
    theft = crime_elements_for("절도")
    assert "공소시효: 7년" in theft
    insult = crime_elements_for("모욕")
    assert "친고죄 — 고소기간" in insult
    assault = crime_elements_for("폭행")
    assert "반의사불벌" in assault


def test_an_unknown_crime_yields_nothing_rather_than_a_template():
    assert crime_elements_for("우주선 무단 발사") == ""


# ── 잘못된 데이터 ────────────────────────────────────────────────────────
def test_broken_rows_are_reported_not_swallowed(tmp_path, monkeypatch):
    """변호사님이 파일을 채우다 실수하면 `--check` 가 잡아야 합니다."""
    (tmp_path / "형법각론.jsonl").write_text(
        "# 주석\n"
        '{"죄명":"가짜죄","조문":"제1조","객관적_구성요건":["요건"],"질문항목":["언제"]}\n'
        '{"죄명":"가짜죄","조문":"제2조","객관적_구성요건":["요건"]}\n'
        '{"조문":"제3조"}\n'
        "{망가진 줄\n"
        '{"죄명":"요건없음","조문":"","객관적_구성요건":[]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(criminal, "CRIMINAL_DIR", tmp_path)
    criminal.reload_crimes()
    try:
        problems = "\n".join(load_problems())
        assert "JSON 오류" in problems
        assert "이미 있습니다" in problems  # 중복 죄명
        assert "'죄명' 이 비어 있습니다" in problems
        assert "객관적_구성요건이 없습니다" in problems
        assert "조문이 없습니다" in problems
        assert [c.name for c in all_crimes()] == ["가짜죄", "요건없음"]
    finally:
        monkeypatch.setattr(criminal, "CRIMINAL_DIR", CRIMINAL_DIR)
        criminal.reload_crimes()


def test_the_real_data_is_back_after_that():
    assert load_problems() == ()
    assert find_crime("절도") is not None


# ── 문서 갈래 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("doc", ["고소장", "고발장", "진정서", "변호인의견서"])
def test_criminal_documents_are_recognised(doc):
    assert is_criminal_doc(doc)


@pytest.mark.parametrize("doc", ["내용증명", "소장", "준비서면", "합의서"])
def test_civil_documents_are_not(doc):
    assert not is_criminal_doc(doc)


def test_a_complaint_is_priced_like_a_pleading():
    assert tier_for("절도 고소장").key == COMPLEX


# ── 도구 동작 ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_complaint_gets_the_criminal_form_and_the_elements():
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler(
        {"doc_kind": "고소장", "case_type": "물건을 훔쳐갔어요"}
    )

    assert CRIMINAL_INTAKE_FORM in result
    assert INTAKE_FORM not in result  # 민사 폼이 아니다
    assert "불법영득의사" in result  # 구성요건이 함께 왔다
    assert "공소시효" in result
    assert "범죄사실" in result  # 형사 보고서 뼈대

    action = state.intake_actions[0]
    assert action.kind == "start"
    assert action.case_type == "절도"
    assert action.track == "criminal"


@pytest.mark.asyncio
async def test_a_criminal_case_type_switches_the_track_even_without_a_criminal_doc():
    """'합의서' 인데 내용이 상해면 구성요건으로 물어야 합니다."""
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler(
        {"doc_kind": "합의서", "case_type": "상해"}
    )

    assert CRIMINAL_INTAKE_FORM in result
    assert state.intake_actions[0].track == "criminal"


@pytest.mark.asyncio
async def test_a_complaint_without_a_crime_asks_to_fix_the_crime_first():
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler({"doc_kind": "고소장"})

    assert CRIMINAL_INTAKE_FORM in result
    assert "죄명 확정이 먼저" in result
    assert "get_crime_elements" in result
    assert "절도" in result  # 색인이 함께 왔다


@pytest.mark.asyncio
async def test_the_civil_path_is_untouched():
    state = TurnState()
    result = await tools_for(state)["start_document_intake"].handler(
        {"doc_kind": "내용증명", "case_type": "전세금을 못 받고 있어요"}
    )

    assert INTAKE_FORM in result
    assert CRIMINAL_INTAKE_FORM not in result
    assert "청구원인 요건사실" in result
    assert state.intake_actions[0].track == "civil"


@pytest.mark.asyncio
async def test_fetching_elements_records_the_crime_as_the_case_type():
    state = TurnState()
    result = await tools_for(state)["get_crime_elements"].handler({"crime": "사기"})

    assert "기망행위" in result
    assert [a.kind for a in state.intake_actions] == ["case_type"]
    assert state.intake_actions[0].case_type == "사기"
    assert state.intake_actions[0].track == "criminal"


@pytest.mark.asyncio
async def test_an_unknown_crime_tells_the_model_not_to_invent_an_article():
    state = TurnState()
    result = await tools_for(state)["get_crime_elements"].handler({"crime": "우주선 무단 발사"})

    assert "지어내지 마세요" in result
    assert "escalate_to_lawyer" in result
    assert state.intake_actions == []  # 아무것도 기록하지 않는다


@pytest.mark.asyncio
async def test_an_ambiguous_crime_asks_one_more_question_instead_of_picking():
    state = TurnState()
    result = await tools_for(state)["get_crime_elements"].handler(
        {"crime": "명예를 훼손 인터넷에 글"}
    )

    assert "좁혀지지 않습니다" in result
    assert "정보통신망 명예훼손" in result
    assert state.intake_actions == []


# ── 저장 ─────────────────────────────────────────────────────────────────
def test_the_track_is_remembered_so_a_later_turn_asks_the_right_way(db):
    db.upsert_room("room-1")
    intake = db.open_intake("room-1", "고소장", "절도", track="criminal")
    assert intake["track"] == "criminal"
    assert db.active_intake("room-1")["track"] == "criminal"


def test_an_intake_defaults_to_the_civil_track(db):
    db.upsert_room("room-1")
    assert db.open_intake("room-1", "내용증명", "대여금")["track"] == "civil"


def test_reopening_does_not_wipe_the_track(db):
    """두 번째 호출에서 track 을 안 넘겨도 형사 인테이크가 민사로 바뀌면 안 됩니다."""
    db.upsert_room("room-1")
    db.open_intake("room-1", "고소장", "절도", track="criminal")
    again = db.open_intake("room-1", "고소장", "")
    assert again["track"] == "criminal"


def test_the_prompt_prefix_advertises_the_crimes(settings):
    from kakao_legal_bot.app.agent import LegalAgent

    agent = LegalAgent(settings, llm=None)  # type: ignore[arg-type]
    prefix = agent.stable_prefix()
    assert "절도" in prefix
    assert "get_crime_elements" in prefix
    assert "대여금" in prefix  # 민사 색인도 그대로


def test_the_prefix_stays_byte_identical_between_calls(settings):
    """캐시가 걸리려면 앞부분이 한 바이트도 달라지면 안 됩니다."""
    from kakao_legal_bot.app.agent import LegalAgent

    agent = LegalAgent(settings, llm=None)  # type: ignore[arg-type]
    assert agent.stable_prefix() == agent.stable_prefix()
