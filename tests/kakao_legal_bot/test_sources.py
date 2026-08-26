"""실제 자료 파일에서 노트 만들기 — 온주 HTML · 주석서 · 단행본.

아래 조각들은 변호사님이 주신 샘플에서 그대로 떼어 온 것입니다. 실제 자료의
표기가 바뀌면 여기서 먼저 깨지도록 두었습니다 — 조문 번호 하나가 어긋나면
그래프의 연결이 통째로 끊기기 때문입니다.
"""

from __future__ import annotations

import pytest

from kakao_legal_bot.app.wiki.citation import local_aliases, parse_cases, parse_statutes
from kakao_legal_bot.app.wiki.links import defined_terms, heading_terms, margin_headings
from kakao_legal_bot.app.wiki.sources import (
    main_law_of,
    parse_markdown_source,
    parse_onju_html,
    read_source,
)

# ── 온주 주석서 HTML (고용보험법 제2조, 노호창, 2022. 3. 2) ────────────────
ONJU = """<input type="hidden" id="hdnTitle" value="고용보험법 / 제2조 [정의]" />
<input type="hidden" id="hdnSubTitle" value="노호창" />
<input type="hidden" id="hdnMainTitle" value="고용보험법" />
<input type="hidden" id="hdnJoTitle" value="제2조" />
<input type="hidden" id="hdnPubDate" value="2022. 3. 2" />
<div class="onju_preview_law">
  <div class="normal">
    <b>제2조 (정의)</b><br/> 이 법에서 사용하는 용어의 뜻은 다음과 같다.
    [개정 2008.12.31, 2011.7.21, 2021.1.5] [[시행일 2021.7.1]]<br />
    1. &ldquo;피보험자&rdquo;란 다음 각 목에 해당하는 사람을 말한다.<br />
    5. "보수"란 「소득세법」 제20조에 따른 근로소득에서 뺀 금액을 말한다.
  </div>
</div>
<div class="onju_preview_onju">
  <div class="viewmode">
    <div class="edit_box"><div class="grayname"> </div><div class="title_1">Ⅰ. 서론</div></div>
    <div class="edit_box"><div class="grayname">1 </div>
      <div class="doc_content">정의규정은 용어의 뜻을 정하는 규정이다.</div></div>
    <div class="edit_box"><div class="grayname"> </div><div class="title_2">1. 피보험자</div></div>
    <div class="edit_box"><div class="grayname">2 </div>
      <div class="doc_content">첫째는
        <a href="javascript:hyperlink('0','고용보험및산업재해보상보험의보험료징수등에관한법률','48X2')">제48조의2 제1항</a>
        이다.<div class="miju_num" onmouseover="open_miju('Miju1')"><a name="Miu1" href="#Mi1">1)</a>
          <div id='Miju1' class='miju_box'><div class="miju_box_line">떠 있는 각주 상자입니다.</div></div>
        </div></div></div>
  </div>
  <div class="miju">
    <div class="mi_content"><a name="Mi1" href="#Miu1" class="black">1) </a>
      <a href=javascript:hyperlink('1','서울행정법원','99구27275')>서울행정법원 2000. 7. 14. 선고 99구27275 판결</a>.
      상고는 기각되었다(대법원 2001. 4. 16. 선고 2001두977 판결).<br /></div>
  </div>
</div>"""


@pytest.fixture
def onju():
    return parse_onju_html(ONJU, "raw/주석서/온주-고용보험법-제2조.html")


def test_the_bibliographic_facts_come_from_the_hidden_fields(onju):
    assert onju.title == "고용보험법 제2조"
    assert onju.kind == "주석서"
    assert onju.extra["author"] == "노호창"
    assert onju.written_on == "2022-03-02"
    assert onju.extra["main_law"] == "고용보험법"


def test_the_effective_date_is_read_from_the_marker(onju):
    """온주는 시행일을 ``[[시행일 2021.7.1]]`` 로 적습니다 — 링크가 아닙니다."""
    assert onju.effective_on == "2021-07-01"
    assert onju.amended_on == "2021-01-05"


def test_the_effective_marker_does_not_become_a_keyword(onju):
    assert "시행일 2021.7.1" not in onju.keywords
    assert "[[시행일" not in onju.body


def test_the_subheadings_survive(onju):
    """변환된 .md 는 '1. 피보험자' 같은 소제목을 통째로 잃습니다."""
    assert "### Ⅰ. 서론" in onju.body
    assert "#### 1. 피보험자" in onju.body


def test_the_margin_numbers_survive(onju):
    """방주번호는 쪽수 대신 쓰는 안정된 인용 좌표입니다."""
    assert "[1] 정의규정은" in onju.body
    assert "[2] 첫째는" in onju.body


def test_the_statute_text_is_kept_separately(onju):
    assert "## 조문" in onju.body
    assert "제2조 (정의)" in onju.body
    assert "</div" not in onju.body  # 태그 찌꺼기


def test_a_link_gives_the_law_and_the_article_together(onju):
    """본문 글자를 읽어 짐작하는 것보다 링크가 정확합니다."""
    assert any("제48조의2" in value for value in onju.statutes)
    assert any("보험료징수" in value for value in onju.statutes)


def test_the_article_this_commentary_is_about_comes_first(onju):
    assert onju.statutes[0] == "고용보험법 제2조"


def test_footnote_cases_are_picked_up(onju):
    assert "99구27275" in onju.cases
    assert "2001두977" in onju.cases


def test_the_hovering_footnote_copy_is_not_counted_twice(onju):
    assert onju.body.count("떠 있는 각주 상자") == 0
    assert onju.body.count("99구27275") == 1


def test_the_defined_terms_become_keywords(onju):
    assert "피보험자" in onju.keywords
    assert "보수" in onju.keywords


# ── 주석서 마크다운 (주석 민사소송법 제5조, 2023.11 제9판) ────────────────
COMMENTARY_MD = """# 주석 민사소송법 / 편집대표 : 민일영 / 한국사법행정학회 / 발간연도 : 2023.11 (제9판)

## 저자정보

- **이름:** 황진구 黃進九
- **직위:** 부장판사

## 제5조 [법인 등의 보통재판적]

민사소송법 제5조 제1항은 법인을 비롯한 단체(민사소송법 제52조)의 보통재판적을
규정하고 있다. 지방자치단체인 시, 군, 구(지방자치법 제2조)의 보통재판적도
공법인으로 이 규정의 적용을 받는다(지방자치법 제3조 제1항). 이것은 민·상법이
법인의 주소는 주된 사무소와 본점의 소재지에 있다고 규정하는 것과 취지를 같이한다
(민법 제36조, 상법 제171조). 상법에서 전속관할의 규정을 두고 있는 경우에는
(상법 제186조, 제240조, 제248조 제2항, 제380조) 민사소송법 제5조가 적용되지 않는다.
법인이 스스로 원고가 되는 경우에까지 적용되지는 않는다.3) 대법원 1980. 6. 12.자
80마158 결정. 과거 판례는 조리에 맞는다고 하였다.4) 대법원 2000. 6. 9. 선고
98다35037 판결, 대법원 2001. 1. 16. 선고 99다62388 판결.
그런데 2022. 7. 5. 시행된 국제사법 제4조는 특별관할 규정을 신설하였다.
"""


@pytest.fixture
def commentary():
    return parse_markdown_source(COMMENTARY_MD, "raw/주석서/주석민사소송법-제5조.md")


def test_the_edition_and_date_are_read_from_the_title_line(commentary):
    assert commentary.written_on == "2023-11-01"
    assert commentary.extra["edition"] == "제9판"


def test_the_author_is_read_from_the_author_block(commentary):
    assert commentary.extra["author"].startswith("황진구")


def test_the_book_title_gives_the_main_law_without_the_word_commentary(commentary):
    assert commentary.extra["main_law"] == "민사소송법"


def test_the_law_carries_across_a_list_of_articles(commentary):
    """'상법 제186조, 제240조, 제248조 제2항' — 뒤의 둘도 상법입니다."""
    keys = [ref.key for ref in parse_statutes(COMMENTARY_MD)]
    assert "상법 제240조" in keys
    assert "상법 제380조" in keys


def test_a_sentence_tail_does_not_become_part_of_the_law_name():
    """'…볼 것이다 국제사법 제10조' 에서 법령명은 국제사법입니다."""
    keys = [ref.key for ref in parse_statutes("소재국의 재판권에 전속한다고 볼 것이다 국제사법 제10조")]
    assert keys == ["국제사법 제10조"]


def test_two_digit_case_numbers_are_recognised(commentary):
    """2000년 이전 사건은 '80마158' 처럼 두 자리로 씁니다."""
    assert "80마158" in commentary.cases
    assert "98다35037" in commentary.cases
    assert "99다62388" in commentary.cases


def test_a_decision_not_a_judgment_is_still_a_case():
    ref = parse_cases("대법원 1980. 6. 12.자 80마158 결정")[0]
    assert ref.key == "80마158" and ref.court == "대법원"


# ── 단행본 (조문해설 도시 및 주거환경정비법, 전재우, 2020년 2월) ───────────
BOOK_MD = """# 조문해설

# 도시 및

주거환경정비법

# 변호사 전재우

박영사

2020년 2월

기본계획 수립 · 고시 (법 제4조~제7조)
추진위원회 구성 및 승인 (법 제31조)

# 제2조(정의)

1. "정비구역"이란 정비사업을 계획적으로 시행하기 위하여 제16조에 따라 지정
   고시된 구역을 말한다.
4. "정비기반시설"이란 도로·상하수도(「국토의 계획 및 이용에 관한 법률」
   제2조제9호에 따른 공동구를 말한다)를 말한다.

정비구역지정 없이 행하여진 추진위원회 구성승인처분은 무효이다.1)
1) 대법원 2010. 9. 30. 선고 2010두9358 판결.
"""


@pytest.fixture
def book():
    return parse_markdown_source(BOOK_MD, "raw/서적/조문해설-도시정비법.md")


def test_the_law_a_practitioner_book_is_about_is_found(book):
    assert book.extra["main_law"] == "도시 및 주거환경정비법"


def test_the_bare_word_law_means_the_book_s_own_statute():
    """실무서는 '이하 법이라 한다'고 해 두고 줄곧 '법 제4조'라고만 씁니다."""
    keys = [
        ref.key
        for ref in parse_statutes("기본계획 수립 · 고시 (법 제4조~제7조)", default_law="도시정비법")
    ]
    assert keys == ["도시 및 주거환경정비법 제4조", "도시 및 주거환경정비법 제7조"]


def test_a_bare_article_with_no_law_anywhere_is_dropped():
    """짐작해서 붙이면 없는 조문이 그래프에 생깁니다."""
    assert parse_statutes("(법 제4조)") == []


def test_a_bracketed_law_name_is_taken_whole():
    keys = [ref.key for ref in parse_statutes("도로·상하수도(「국토의 계획 및 이용에 관한 법률」 제2조제9호)")]
    assert keys == ["국토의 계획 및 이용에 관한 법률 제2조"]


def test_the_defined_terms_of_a_statute_become_keywords(book):
    assert "정비구역" in book.keywords
    assert "정비기반시설" in book.keywords


def test_the_publication_month_is_read(book):
    assert book.written_on == "2020-02-01"


# ── 문서가 스스로 정하는 약칭 ────────────────────────────────────────────
def test_a_document_local_abbreviation_is_learned():
    text = (
        "「고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률」"
        '(이하 "고용산재보험료징수법"이라 한다) 제5조제1항에 따라 …'
        "그리고 고용산재보험료징수법 제49조의2 제1항에 따라 가입한 자영업자"
    )
    assert local_aliases(text) == {
        "고용산재보험료징수법": "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률"
    }
    keys = [ref.key for ref in parse_statutes(text)]
    assert keys == [
        "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률 제5조",
        "고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률 제49조의2",
    ]


def test_the_law_does_not_carry_across_two_sentences():
    """한 문장 건너뛴 조문까지 앞의 법령명으로 읽으면 없는 조문이 생깁니다."""
    text = (
        "고용산재보험료징수법 제49조의2제1항에 따라 고용보험에 가입하거나 가입된 것으로 "
        '보는 자영업자(이하 "자영업자인 피보험자"라 한다) 2. "이직"이란 고용관계가 '
        "끝나게 되는 것(제77조의2제1항에 따른 예술인의 경우)을 말한다."
    )
    keys = [ref.key for ref in parse_statutes(text, default_law="고용보험법")]
    assert "고용보험법 제77조의2" in keys


# ── 방주번호 ─────────────────────────────────────────────────────────────
def test_margin_numbers_are_read_as_the_books_skeleton():
    text = "[3315] 1. 서 설\n채권은 채권자의 만족을 통한 소멸을 목표로 한다.\n[3316] 2. 강제적 실현\n"
    assert margin_headings(text) == [("3315", "서 설"), ("3316", "강제적 실현")]
    assert "서 설" in heading_terms(text)


def test_a_quoted_verb_is_not_a_defined_term():
    """'"안다"라 한다' 같은 것을 키워드로 잡으면 그래프가 쓰레기로 찹니다."""
    assert defined_terms('"안다"라 한다') == []
    assert defined_terms('"정비구역"이란 …') == ["정비구역"]


# ── 파일 하나 읽기 ───────────────────────────────────────────────────────
def test_html_and_markdown_go_down_the_right_path(tmp_path):
    (tmp_path / "a.html").write_text(ONJU, encoding="utf-8")
    (tmp_path / "b.md").write_text(COMMENTARY_MD, encoding="utf-8")

    assert read_source(tmp_path / "a.html").title == "고용보험법 제2조"
    assert read_source(tmp_path / "b.md").extra["main_law"] == "민사소송법"


def test_a_file_saved_as_md_but_holding_onju_html_is_still_read_as_html(tmp_path):
    path = tmp_path / "온주.md"
    path.write_text(ONJU, encoding="utf-8")
    assert read_source(path).extra["author"] == "노호창"


def test_the_main_law_can_come_from_the_folder_name():
    assert main_law_of("", "raw/주석서/민법/제618조.md") == "민법"
    assert main_law_of("", "raw/서적/조문해설-도시정비법.md") == "도시 및 주거환경정비법"
    assert main_law_of("", "raw/무엇/아무거나.md") == ""


def test_the_most_cited_law_is_the_last_resort():
    """제목에도 경로에도 없으면 본문에서 가장 많이 인용된 법으로 봅니다."""
    text = "민법 제404조, 민법 제405조, 민법 제406조를 본다. 상법 제171조도 있다."
    assert main_law_of(text, "raw/x/y.md") == "민법"
