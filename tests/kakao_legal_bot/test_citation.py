"""법령·판례 인용 정규화.

이 파일이 이 데이터베이스에서 가장 중요한 테스트입니다. '민28'과 '민법 제28조'
가 다른 낱말로 남으면 백링크도, 허브노트도, 그래프 검색도 전부 헛돕니다.
그래서 실제 법률문서에서 쓰이는 표기를 하나하나 못 박아 둡니다.
"""

from __future__ import annotations

import pytest

from kakao_legal_bot.app.wiki.citation import (
    extract_citations,
    normalise_law_name,
    parse_case,
    parse_cases,
    parse_date,
    parse_statute,
    parse_statutes,
)


def displays(text: str, default_law: str = "") -> list[str]:
    return [ref.display for ref in parse_statutes(text, default_law)]


def keys(text: str, default_law: str = "") -> list[str]:
    return [ref.key for ref in parse_statutes(text, default_law)]


# ── 같은 조문은 어떻게 적혀도 같은 키 ────────────────────────────────────
@pytest.mark.parametrize(
    "written",
    [
        "민28",
        "민 28조",
        "민28조",
        "민법28",
        "민법28조",
        "민법 28조",
        "민법 제28조",
        "민법제28조",
        "민법 제 28 조",
        "「민법」 제28조",
    ],
)
def test_every_way_of_writing_it_lands_on_one_key(written):
    assert keys(written) == ["민법 제28조"]


def test_the_paragraph_is_kept_but_does_not_split_the_key():
    """허브노트는 조 단위로 모여야 쓸모가 있습니다."""
    ref = parse_statute("민법 제28조 제1항")
    assert ref is not None
    assert ref.key == "민법 제28조"
    assert ref.display == "민법 제28조 제1항"


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("민법 제28조 제1항", "민법 제28조 제1항"),
        ("민법 제28조 1항", "민법 제28조 제1항"),
        ("민법 제28조①", "민법 제28조 제1항"),
        ("민법 제28조 ②", "민법 제28조 제2항"),
        ("민법 제28조 제1항 제2호", "민법 제28조 제1항 제2호"),
        ("민법 제28조 제1항 제2호 가목", "민법 제28조 제1항 제2호 가목"),
    ],
)
def test_paragraphs_items_and_subitems(written, expected):
    assert displays(written) == [expected]


def test_a_branch_article_is_not_the_same_as_the_parent():
    """제148조의2(음주운전)는 제148조와 다른 조문입니다."""
    assert keys("도로교통법 제148조의2") == ["도로교통법 제148조의2"]
    assert keys("도로교통법 제148조") == ["도로교통법 제148조"]


# ── 약칭 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("alias", "full"),
    [
        ("민", "민법"),
        ("형", "형법"),
        ("민소", "민사소송법"),
        ("형소", "형사소송법"),
        ("주임법", "주택임대차보호법"),
        ("특경법", "특정경제범죄 가중처벌 등에 관한 법률"),
        ("정통망법", "정보통신망 이용촉진 및 정보보호 등에 관한 법률"),
    ],
)
def test_abbreviations_resolve_to_the_official_name(alias, full):
    assert normalise_law_name(alias) == full
    assert keys(f"{alias} 제3조") == [f"{full} 제3조"]


def test_the_official_name_written_out_in_full_is_recognised():
    assert keys("정보통신망 이용촉진 및 정보보호 등에 관한 법률 제70조 제2항") == [
        "정보통신망 이용촉진 및 정보보호 등에 관한 법률 제70조"
    ]


def test_a_law_not_in_the_table_is_still_picked_up():
    """표에 없다고 조문을 놓치면 안 됩니다."""
    assert keys("자동차관리법 제26조") == ["자동차관리법 제26조"]


# ── 문맥 ─────────────────────────────────────────────────────────────────
def test_the_law_carries_across_a_comma():
    assert keys("민법 제28조, 제29조") == ["민법 제28조", "민법 제29조"]


def test_the_same_law_phrase_means_the_one_just_named():
    assert keys("민법 제618조에 따르면 … 같은 법 제623조도 본다.") == [
        "민법 제618조",
        "민법 제623조",
    ]


def test_a_default_law_covers_a_commentary_on_one_statute():
    """민법 주석서에서는 '제618조'만 적는 것이 보통입니다."""
    assert keys("제618조의 임대차는", default_law="민법") == ["민법 제618조"]
    assert keys("제618조의 임대차는") == []


def test_the_law_does_not_leak_across_lines():
    """앞 문단이 형법 이야기였다고 다음 문단의 조문까지 형법일 수는 없습니다."""
    text = "형법 제355조를 본다.\n제618조의 임대차는"
    assert keys(text, default_law="민법") == ["형법 제355조", "민법 제618조"]


@pytest.mark.parametrize("noise", ["이상 28조", "계약서 제3조에 따라", "총 28조로 이루어진"])
def test_numbers_that_are_not_citations_are_left_alone(noise):
    assert parse_statutes(noise) == []


def test_an_old_law_citation_is_flagged():
    """'구 민법'은 연혁조문입니다 — 지금 적용되는 규정이 아닙니다."""
    ref = parse_statute("구 민법 제28조")
    assert ref is not None and ref.historic
    assert parse_statute("민법 제28조").historic is False


# ── 판례 ─────────────────────────────────────────────────────────────────
def test_a_full_citation_yields_court_number_and_date():
    ref = parse_case("대법원 2018. 3. 15. 선고 2017다12345 판결")
    assert ref is not None
    assert ref.key == "2017다12345"
    assert ref.court == "대법원"
    assert ref.decided_on == "2018-03-15"


@pytest.mark.parametrize(
    "written",
    ["대법원 2017다12345", "대판 2017다12345", "대법 2017다12345", "2017다12345"],
)
def test_a_case_number_is_the_key_however_the_court_is_written(written):
    ref = parse_case(written)
    assert ref is not None and ref.key == "2017다12345"


def test_the_court_name_is_normalised_for_display():
    assert parse_case("대판 2017다12345").display == "대법원 2017다12345"
    assert parse_case("헌재 2015헌마123").display == "헌법재판소 2015헌마123"


def test_an_en_banc_decision_is_marked():
    """전원합의체 판결인지는 인용할 때 반드시 밝혀야 합니다."""
    ref = parse_case("대법원 2013. 5. 16. 선고 2012다202819 전원합의체 판결")
    assert ref is not None and ref.en_banc
    assert "전원합의체" in ref.display


def test_a_list_of_case_numbers_expands():
    refs = parse_cases("대판 2017다12345, 12346")
    assert [ref.key for ref in refs] == ["2017다12345", "2017다12346"]
    assert all(ref.court == "대법원" for ref in refs)


def test_lower_courts_keep_their_names():
    ref = parse_case("서울고등법원 2016나1234 판결")
    assert ref is not None and ref.court == "서울고등법원"


def test_a_date_is_not_mistaken_for_a_case_number():
    assert parse_cases("2018년 12월에 있었던 일") == []
    assert parse_cases("2018년도 28회 시험") == []


def test_dates_in_several_shapes():
    assert parse_date("2018. 3. 15.") == "2018-03-15"
    assert parse_date("2018년 3월 15일") == "2018-03-15"
    assert parse_date("선고일 없음") == ""


# ── 한꺼번에 ─────────────────────────────────────────────────────────────
def test_a_paragraph_of_real_prose():
    text = (
        "임대차보증금 반환에 관하여는 민법 제618조 이하가 적용되고, 같은 법 제623조의 "
        "임대인의 의무가 문제된다. 대법원 2017. 8. 29. 선고 2016다212524 판결은 이를 "
        "긍정하였다. 주임법 제3조 제1항의 대항력도 함께 본다."
    )
    citations = extract_citations(text)
    assert citations.statute_keys() == [
        "민법 제618조",
        "민법 제623조",
        "주택임대차보호법 제3조",
    ]
    assert citations.case_keys() == ["2016다212524"]
    assert "주택임대차보호법 제3조 제1항" in citations.statute_displays()
    assert citations.case_displays() == ["대법원 2016다212524"]


def test_the_most_specific_form_wins_in_the_summary_list():
    """한 문서에서 제28조와 제28조 제1항이 모두 나오면 자세한 쪽을 적습니다."""
    citations = extract_citations("민법 제28조 … 민법 제28조 제1항 …")
    assert citations.statute_displays() == ["민법 제28조 제1항"]
    assert citations.statute_keys() == ["민법 제28조"]


def test_empty_input_is_not_an_error():
    assert extract_citations("").statute_keys() == []
    assert parse_statute("") is None
    assert parse_case("") is None
