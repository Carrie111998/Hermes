"""LLM WIKI — 노트 · 링크 · 그래프 · LINT.

이 층이 하는 일은 하나입니다. **같은 것을 말하는 문서끼리 잇고, 낡은 것을
뒤로 미는 것.** 법률 데이터베이스에서 가장 위험한 오류는 빠진 자료가 아니라
개정 전 조문을 현행인 양 인용하는 것이라서, 날짜에 관한 테스트가 제일 많습니다.
"""

from __future__ import annotations

import pytest

from kakao_legal_bot.app.tools import TurnState, build_tools
from kakao_legal_bot.app.wiki.build import guess_kind, iter_notes, make_stubs
from kakao_legal_bot.app.wiki.graph import (
    CASE_ENTITY,
    KEYWORD_ENTITY,
    STATUTE_ENTITY,
    WikiGraph,
    classify,
    safe_filename,
)
from kakao_legal_bot.app.wiki.lint import (
    DANGLING_LINK,
    DUPLICATE_CASE,
    MISSING_FIELD,
    OUTDATED_SOURCE,
    REVIEW_PAIR,
    STALE_STATUTE,
    apply_supersession,
    lint,
)
from kakao_legal_bot.app.wiki.links import (
    extract_wikilinks,
    keyword_weights,
    linked_targets,
    promote_links,
)
from kakao_legal_bot.app.wiki.note import WikiNote, dump_frontmatter, parse_frontmatter


# ── frontmatter ──────────────────────────────────────────────────────────
def test_frontmatter_reads_the_forms_we_actually_write():
    text = """---
title: 임대차보증금 반환
kind: 판례
statutes: [민법 제618조, 민법 제536조]
keywords:
  - 임대차보증금
  - 대항력
verified: true
---

본문입니다.
"""
    fields, body = parse_frontmatter(text)
    assert fields["title"] == "임대차보증금 반환"
    assert fields["statutes"] == ["민법 제618조", "민법 제536조"]
    assert fields["keywords"] == ["임대차보증금", "대항력"]
    assert body.strip() == "본문입니다."


def test_a_note_without_frontmatter_is_still_a_note():
    fields, body = parse_frontmatter("# 제목\n본문")
    assert fields == {}
    assert body.startswith("# 제목")


def test_an_unclosed_frontmatter_does_not_eat_the_body():
    fields, body = parse_frontmatter("---\ntitle: 반쪽\n\n본문")
    assert fields == {}
    assert "본문" in body


def test_frontmatter_survives_a_round_trip():
    original = {"title": "제목: 콜론 포함", "statutes": ["민법 제618조"], "verified": False}
    fields, _ = parse_frontmatter(dump_frontmatter(original) + "\n\n본문")
    assert fields["title"] == "제목: 콜론 포함"
    assert fields["statutes"] == ["민법 제618조"]


# ── 노트 ─────────────────────────────────────────────────────────────────
CASE_NOTE = """---
title: 보증금반환과 동시이행
kind: 판례
decided_on: 2018-03-15
court: 대법원
case_no: 2017다12345
---

# 대법원 2018. 3. 15. 선고 2017다12345 판결 [[임대차보증금]]

민법 제618조, 같은 법 제536조. 선행 판례 대법원 2016다212524.
임대차보증금은 목적물 인도와 [[동시이행]] 관계에 있다.
"""


def test_a_note_fills_in_its_own_citations_from_the_body():
    note = WikiNote.from_markdown(CASE_NOTE, "wiki/판례/2017다12345.md").enrich()
    # 조문은 조문 순서로 적습니다 — 본문을 손보아도 frontmatter가 흔들리지 않게.
    assert note.statutes == ["민법 제536조", "민법 제618조"]
    assert note.keywords == ["임대차보증금", "동시이행"]


def test_a_case_note_does_not_cite_itself():
    """자기 사건번호가 '인용한 판례'에 들어가면 그래프가 자기 자신을 가리킵니다."""
    note = WikiNote.from_markdown(CASE_NOTE, "x.md").enrich()
    assert note.case_no == "2017다12345"
    assert note.cases == ["2016다212524"]


def test_a_note_written_by_hand_is_not_overwritten():
    text = CASE_NOTE.replace("---\n\n#", "statutes: [민법 제999조]\n---\n\n#")
    note = WikiNote.from_markdown(text, "x.md").enrich()
    assert note.statutes[0] == "민법 제999조"  # 사람이 적은 것이 앞


def test_the_as_of_date_prefers_the_one_that_decides_currency():
    statute = WikiNote(kind="법령", effective_on="2023-06-01", written_on="2020-01-01")
    assert statute.as_of == "2023-06-01"
    case = WikiNote(kind="판례", decided_on="2018-03-15", written_on="2020-01-01")
    assert case.as_of == "2018-03-15"
    book = WikiNote(kind="서적", written_on="2020-01-01")
    assert book.as_of == "2020-01-01"


def test_a_case_without_a_date_is_reported_as_incomplete():
    note = WikiNote(title="무제", kind="판례", case_no="2017다1")
    assert any("decided_on" in item for item in note.missing_required())


def test_a_statute_without_an_effective_date_is_reported():
    note = WikiNote(title="민법 제618조", kind="법령", statutes=["민법 제618조"])
    assert any("effective_on" in item for item in note.missing_required())


def test_a_wrongly_shaped_date_is_caught():
    note = WikiNote(title="x", kind="서적", written_on="2020.1.1")
    assert any("YYYY-MM-DD" in item for item in note.missing_required())


def test_entity_keys_are_normalised_for_the_graph():
    note = WikiNote(
        title="x", kind="서적", statutes=["민28", "민법 제28조 제1항"], keywords=["대항력"]
    )
    assert note.entity_keys == ["민법 제28조", "대항력"]


# ── [[링크]] ─────────────────────────────────────────────────────────────
def test_wikilinks_in_all_the_shapes_obsidian_allows():
    links = extract_wikilinks("[[대항력]] [[민법 제618조|618조]] [[임대차#보증금]]")
    assert [link.target for link in links] == ["대항력", "민법 제618조", "임대차"]
    assert links[1].alias == "618조"
    assert links[2].section == "보증금"


def test_a_keyword_marked_only_in_a_heading_counts_through_the_whole_file():
    """소제목에 한 번만 표시하셔도 그 낱말은 그 문서 전체에서 중요합니다."""
    text = "# [[대항력]]\n대항력이란 무엇인가. 대항력은 임차인을 보호한다."
    weights = keyword_weights(text)
    assert weights["대항력"] > 1


def test_code_blocks_and_urls_are_left_alone():
    text = "[[대항력]]\n```\n대항력\n```\nhttps://x.test/대항력"
    assert promote_links(text).count("[[대항력]]") == 1


def test_promoting_links_does_not_double_wrap():
    assert promote_links("[[대항력]] 대항력") == "[[대항력]] [[대항력]]"


def test_the_longer_keyword_wins_over_the_shorter_one():
    """'임대차보증금'을 '임대차'가 먼저 먹으면 낱말이 쪼개집니다."""
    text = "[[임대차보증금]] 과 [[임대차]] 는 다르다. 임대차보증금 이야기."
    promoted = promote_links(text)
    assert "[[임대차보증금]] 이야기" in promoted


def test_linked_targets_keeps_first_seen_order():
    assert linked_targets("[[나]] [[가]] [[나]]") == ["나", "가"]


# ── 그래프 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("민법 제618조", STATUTE_ENTITY),
        ("민618", STATUTE_ENTITY),
        ("2017다12345", CASE_ENTITY),
        ("대항력", KEYWORD_ENTITY),
        ("", KEYWORD_ENTITY),
    ],
)
def test_entities_are_classified_by_shape(key, kind):
    assert classify(key) == kind


NOTES = {
    "판례/2017다12345.md": CASE_NOTE,
    "주석서/임대차.md": """---
title: 주석민법 임대차
kind: 주석서
written_on: 2019-03-01
---
# [[임대차보증금]]
민법 제618조 이하. 대법원 2017다12345 판결 참조. 임대차보증금은 반환되어야 한다.
""",
    "서적/채권각론.md": """---
title: 민법강의 채권각론
kind: 서적
written_on: 2023-02-01
---
# 채권각론
민법 제536조 [[동시이행]]의 항변권.
""",
    "서적/형법각론.md": """---
title: 형법각론
kind: 서적
written_on: 2023-02-01
---
# 절도
형법 제329조.
""",
}


@pytest.fixture
def graph(tmp_path):
    store = WikiGraph(tmp_path / "graph.sqlite3")
    for path, text in NOTES.items():
        store.upsert_note(WikiNote.from_markdown(text, path).enrich())
    yield store
    store.close()


def test_the_graph_counts_what_it_holds(graph):
    stats = graph.stats()
    assert stats["notes"] == 4
    assert stats["statutes"] >= 3
    assert stats["cases"] >= 1


def test_one_statute_gathers_every_document_that_cites_it(graph):
    titles = [row["title"] for row in graph.notes_for("민법 제618조")]
    assert "보증금반환과 동시이행" in titles
    assert "주석민법 임대차" in titles


def test_the_statute_can_be_asked_for_in_any_notation(graph):
    """'민618' 로 물어도 '민법 제618조' 로 모은 것이 나와야 합니다."""
    assert [row["title"] for row in graph.notes_for("민618")] == [
        row["title"] for row in graph.notes_for("민법 제618조")
    ]


def test_a_case_gathers_the_documents_that_cite_it(graph):
    titles = [row["title"] for row in graph.notes_for("2017다12345")]
    assert "주석민법 임대차" in titles


def test_related_finds_documents_that_share_a_basis_not_a_phrase(graph):
    related = graph.related(["판례/2017다12345.md"])
    titles = [item.title for item in related]
    assert "주석민법 임대차" in titles
    assert "형법각론" not in titles  # 공통점이 없다


def test_a_shared_rare_statute_outranks_a_shared_common_one(graph):
    related = graph.related(["판례/2017다12345.md"], limit=5)
    assert related[0].title == "주석민법 임대차"  # 조문·판례·키워드를 모두 공유


def test_notes_that_were_superseded_are_kept_out_of_related(tmp_path):
    store = WikiGraph(tmp_path / "g.sqlite3")
    try:
        current = WikiNote(
            path="법령/현행.md", title="현행", kind="법령", effective_on="2023-06-01",
            statutes=["민법 제618조"],
        )
        old = WikiNote(
            path="법령/구법.md", title="구법", kind="법령", effective_on="2016-02-04",
            statutes=["민법 제618조"], superseded_by="법령/현행.md",
        )
        seed = WikiNote(path="책.md", title="책", kind="서적", statutes=["민법 제618조"])
        for note in (current, old, seed):
            store.upsert_note(note)
        titles = [item.title for item in store.related(["책.md"])]
        assert "현행" in titles
        assert "구법" not in titles
        assert "구법" in [item.title for item in store.related(["책.md"], exclude_stale=False)]
    finally:
        store.close()


def test_hubs_are_the_things_more_than_one_document_talks_about(graph):
    keys = {entity.key for entity in graph.hubs(min_notes=2)}
    assert "민법 제618조" in keys
    assert "형법 제329조" not in keys  # 한 문서에만 나온다


def test_a_hub_note_is_grouped_and_dated(graph):
    entity = graph.entity("민법 제618조")
    assert entity is not None
    text = graph.render_hub(entity)
    assert "# 민법 제618조" in text
    assert "## 판례" in text
    assert "2018-03-15" in text
    assert "[[" in text  # 옵시디언 링크


def test_hub_notes_land_in_folders_by_kind(graph, tmp_path):
    out = tmp_path / "hubs"
    written = graph.write_hubs(out, min_notes=2)
    assert written >= 2
    assert (out / "법령" / "민법 제618조.md").exists()
    assert (out / "허브 색인.md").exists()


def test_a_filename_cannot_break_out_of_the_folder():
    assert "/" not in safe_filename("민법/제618조")
    assert safe_filename("") == "무제"


def test_the_important_documents_are_the_ones_others_come_to(graph):
    """남을 많이 인용한 문서가 아니라, 남들이 찾아오는 문서가 중요합니다."""
    rows = graph.important_notes()
    assert rows
    assert rows[0]["inbound"] >= 1


def test_reindexing_a_note_replaces_its_edges(graph):
    before = graph.stats()["mentions"]
    graph.upsert_note(
        WikiNote(path="서적/형법각론.md", title="형법각론", kind="서적", statutes=["형법 제329조"])
    )
    assert graph.stats()["mentions"] <= before
    assert graph.stats()["notes"] == 4  # 새 노트가 생기지 않았다


def test_forgetting_a_note_removes_it_from_the_hubs(graph):
    assert graph.forget_note("주석서/임대차.md")
    assert "주석민법 임대차" not in [row["title"] for row in graph.notes_for("민법 제618조")]
    assert graph.forget_note("없는 파일.md") is False


def test_a_note_can_be_found_by_path_stem_or_title(graph):
    assert graph.resolve(["판례/2017다12345.md"]) == ["판례/2017다12345.md"]
    assert graph.resolve(["2017다12345"]) == ["판례/2017다12345.md"]
    assert graph.resolve(["보증금반환과 동시이행"]) == ["판례/2017다12345.md"]
    assert graph.resolve(["없는 문서"]) == []


# ── LINT ─────────────────────────────────────────────────────────────────
def notes_from(mapping: dict[str, str]) -> list[WikiNote]:
    return [WikiNote.from_markdown(text, path).enrich() for path, text in mapping.items()]


def test_the_older_version_of_a_statute_is_pushed_down():
    notes = [
        WikiNote(path="현행.md", title="현행", kind="법령", effective_on="2023-06-01",
                 statutes=["민법 제618조"]),
        WikiNote(path="구법.md", title="구법", kind="법령", effective_on="2016-02-04",
                 statutes=["민법 제618조"]),
    ]
    report = lint(notes)
    stale = report.by_code(STALE_STATUTE)
    assert len(stale) == 1
    assert stale[0].path == "구법.md"
    assert report.superseded["구법.md"] == "현행.md"


def test_supersession_is_written_onto_the_note_not_deleted():
    """연혁조문은 '그때는 어땠는가'를 묻는 사건에서 필요합니다."""
    notes = [
        WikiNote(path="현행.md", title="현행", kind="법령", effective_on="2023-06-01",
                 statutes=["민법 제618조"]),
        WikiNote(path="구법.md", title="구법", kind="법령", effective_on="2016-02-04",
                 statutes=["민법 제618조"]),
    ]
    changed = apply_supersession(notes, lint(notes))
    assert [note.path for note in changed] == ["구법.md"]
    assert notes[1].superseded_by == "현행.md"


def test_a_book_written_before_the_amendment_is_flagged():
    """이 규칙 하나가 '자신 있게 틀린 답'의 대부분을 막습니다."""
    notes = [
        WikiNote(path="법령.md", title="현행", kind="법령", effective_on="2023-06-01",
                 statutes=["민법 제618조"]),
        WikiNote(path="책.md", title="옛 교재", kind="서적", written_on="2019-03-01",
                 statutes=["민법 제618조"]),
    ]
    findings = lint(notes).by_code(OUTDATED_SOURCE)
    assert len(findings) == 1
    assert "2023-06-01" in findings[0].message


def test_a_book_written_after_the_amendment_is_not_flagged():
    notes = [
        WikiNote(path="법령.md", title="현행", kind="법령", effective_on="2023-06-01",
                 statutes=["민법 제618조"]),
        WikiNote(path="책.md", title="새 교재", kind="서적", written_on="2024-03-01",
                 statutes=["민법 제618조"]),
    ]
    assert lint(notes).by_code(OUTDATED_SOURCE) == []


def test_the_same_case_in_two_notes_is_a_problem():
    notes = [
        WikiNote(path="a.md", title="A", kind="판례", case_no="2017다1", decided_on="2018-01-01"),
        WikiNote(path="b.md", title="B", kind="판례", case_no="2017다1", decided_on="2018-01-01"),
    ]
    findings = lint(notes).by_code(DUPLICATE_CASE)
    assert len(findings) == 1 and findings[0].path == "b.md"


def test_missing_dates_are_errors_not_warnings():
    report = lint([WikiNote(path="a.md", title="A", kind="판례", case_no="2017다1")])
    assert report.errors
    assert report.by_code(MISSING_FIELD)


def test_a_keyword_that_only_one_document_uses_is_reported():
    notes = [
        WikiNote(path="a.md", title="A", kind="서적", written_on="2024-01-01",
                 keywords=["대항력", "혼자쓰는말"]),
        WikiNote(path="b.md", title="B", kind="서적", written_on="2024-01-01",
                 keywords=["대항력"]),
    ]
    findings = lint(notes, known_titles={"A", "B"}).by_code(DANGLING_LINK)
    assert [finding.message for finding in findings] == [
        "[[혼자쓰는말]] 이(가) 이 문서에만 나오고 해당 노트도 없습니다."
    ]


def test_widely_shared_keywords_are_not_called_dangling():
    """두 문서 이상이 쓰는 낱말은 허브노트가 받아 줍니다."""
    notes = [
        WikiNote(path=f"{index}.md", title=str(index), kind="서적", written_on="2024-01-01",
                 keywords=["대항력"])
        for index in range(3)
    ]
    assert lint(notes, known_titles=set()).by_code(DANGLING_LINK) == []


def test_far_apart_sources_on_one_statute_go_to_the_review_list():
    """규칙으로 모순이라 단정하지 않고, 읽어야 할 짝으로만 넘깁니다."""
    notes = [
        WikiNote(path="옛.md", title="옛", kind="서적", written_on="2015-01-01",
                 statutes=["민법 제618조"]),
        WikiNote(path="새.md", title="새", kind="서적", written_on="2024-01-01",
                 statutes=["민법 제618조"]),
    ]
    report = lint(notes)
    assert report.by_code(REVIEW_PAIR)
    assert report.to_worklist()[0]["code"] == REVIEW_PAIR


def test_a_clean_vault_reports_nothing():
    notes = [
        WikiNote(path="a.md", title="A", kind="서적", written_on="2024-01-01"),
    ]
    report = lint(notes, known_titles={"A"})
    assert report.findings == []
    assert "문제 없습니다" in report.to_markdown()


def test_the_report_reads_as_markdown():
    notes = [WikiNote(path="a.md", title="A", kind="판례", case_no="2017다1")]
    text = lint(notes).to_markdown()
    assert "# LINT" in text and "필수 항목 누락" in text


# ── 빌드 ─────────────────────────────────────────────────────────────────
def test_a_stub_carries_everything_the_machine_can_extract(tmp_path):
    raw = tmp_path / "raw" / "판례"
    raw.mkdir(parents=True)
    (raw / "2017다12345.md").write_text(
        "# 대법원 2018. 3. 15. 선고 2017다12345 판결\n민법 제618조. [[동시이행]]",
        encoding="utf-8",
    )
    wiki = tmp_path / "wiki"
    made, skipped = make_stubs(tmp_path / "raw", wiki)

    assert (made, skipped) == (1, 0)
    note = WikiNote.load(wiki / "판례" / "2017다12345.md")
    assert note.kind == "판례"
    assert note.case_no == "2017다12345"
    assert note.decided_on == "2018-03-15"
    assert note.statutes == ["민법 제618조"]
    assert note.keywords == ["동시이행"]
    assert note.source == "raw/판례/2017다12345.md"
    assert (wiki / "_wiki-jobs.jsonl").exists()


def test_stubs_do_not_overwrite_what_codex_already_wrote(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.md").write_text("# 원문", encoding="utf-8")
    wiki = tmp_path / "wiki"
    make_stubs(raw, wiki)
    (wiki / "a.md").write_text("---\ntitle: 손으로 쓴 것\nkind: 서적\n---\n본문", encoding="utf-8")

    made, skipped = make_stubs(raw, wiki)
    assert (made, skipped) == (0, 1)
    assert "손으로 쓴 것" in (wiki / "a.md").read_text(encoding="utf-8")


def test_generated_files_are_not_indexed_as_notes(tmp_path):
    """LINT 보고서가 노트로 잡히면 그래프에 유령 문서가 생깁니다."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "진짜.md").write_text("---\ntitle: 진짜\nkind: 서적\n---\n본문", encoding="utf-8")
    (wiki / "LINT.md").write_text("# LINT\n", encoding="utf-8")
    (wiki / "_lint-worklist.jsonl").write_text("{}\n", encoding="utf-8")
    (wiki / "허브.md").write_text("---\ntitle: 민법 제1조\nkind: 허브\n---\n", encoding="utf-8")

    assert [note.title for note in iter_notes(wiki)] == ["진짜"]


def test_the_folder_name_hints_at_what_kind_of_source_it_is(tmp_path):
    assert guess_kind(tmp_path / "판례" / "a.md") == "판례"
    assert guess_kind(tmp_path / "commentary" / "a.md") == "주석서"
    assert guess_kind(tmp_path / "서식" / "a.md") == "서식"
    assert guess_kind(tmp_path / "무엇" / "a.md") == "서적"


def test_a_commentary_folder_supplies_the_default_law(tmp_path):
    """민법 주석서에서는 '제618조'만 적는 것이 보통입니다."""
    wiki = tmp_path / "wiki" / "주석서" / "민법"
    wiki.mkdir(parents=True)
    (wiki / "임대차.md").write_text(
        "---\ntitle: 임대차\nkind: 주석서\nwritten_on: 2024-01-01\n---\n제618조의 임대차",
        encoding="utf-8",
    )
    notes = iter_notes(tmp_path / "wiki")
    assert notes[0].statutes == ["민법 제618조"]


# ── 검색 도구 ────────────────────────────────────────────────────────────
class FakeRag:
    """FTS5 자리에 세우는 대역 — 그래프로 건너가는 다리만 시험합니다."""

    def __init__(self, sources: list[str]) -> None:
        self.sources = sources

    def search(self, query: str, top_k: int = 6, **_: object) -> list:
        from kakao_legal_bot.app.rag.store import Hit

        return [
            Hit(chunk_id=index, text=query, title=source, source=source, locator="", score=1.0)
            for index, source in enumerate(self.sources[:top_k])
        ]


def tool_for(graph, rag=None):
    state = TurnState()
    tools = {tool.name: tool for tool in build_tools(state=state, rag=rag, law=None, graph=graph)}
    return state, tools["search_related_docs"]


@pytest.mark.asyncio
async def test_the_tool_answers_which_documents_cite_a_statute(graph):
    state, tool = tool_for(graph)
    result = await tool.handler({"anchor": "민618"})

    assert "민법 제618조 를 다루는 자료" in result
    assert "주석민법 임대차" in result
    assert "민법 제618조" in state.citations


@pytest.mark.asyncio
async def test_the_tool_walks_from_a_phrase_to_the_shared_basis(graph):
    _state, tool = tool_for(graph, FakeRag(["판례/2017다12345.md"]))
    result = await tool.handler({"query": "임대차보증금 반환"})

    assert "같은 조문·판례를 다루는 자료" in result
    assert "주석민법 임대차" in result
    assert "공통:" in result


@pytest.mark.asyncio
async def test_the_tool_marks_documents_that_predate_an_amendment(tmp_path):
    store = WikiGraph(tmp_path / "g.sqlite3")
    try:
        store.upsert_note(
            WikiNote(path="구법.md", title="구법", kind="법령", effective_on="2016-02-04",
                     statutes=["민법 제618조"], superseded_by="현행.md")
        )
        _state, tool = tool_for(store)
        result = await tool.handler({"anchor": "민법 제618조"})
        assert "개정 전 자료" in result
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_anchor_nobody_mentions_says_so(graph):
    _state, tool = tool_for(graph)
    assert "연결된 자료가 없습니다" in await tool.handler({"anchor": "민법 제9999조"})


@pytest.mark.asyncio
async def test_the_tool_is_absent_when_there_is_no_graph():
    state = TurnState()
    names = {tool.name for tool in build_tools(state=state, rag=None, law=None)}
    assert "search_related_docs" not in names
