"""자료별로 나뉜 색인을 하나처럼 검색하기.

한 파일에 다 넣으면 1.27GB에서 검색이 2초가 걸렸고, 나누니 85ms 였습니다.
5초 룰에서 2초는 답변 하나를 통째로 날리는 값이라 나누는 쪽을 택했습니다.
아래 테스트는 "나누어도 하나처럼 보인다"를 붙잡아 둡니다.
"""

from __future__ import annotations

import pytest

from kakao_legal_bot.app.config import Settings
from kakao_legal_bot.app.rag.multi import MultiRagStore
from kakao_legal_bot.app.rag.store import RagStore
from kakao_legal_bot.app.tools import TurnState, build_tools


@pytest.fixture
def library(tmp_path):
    """책 · 주석서 · 판례를 각각 다른 파일에 넣는다."""
    folder = tmp_path / "rag"
    folder.mkdir()
    books = RagStore(folder / "books.sqlite3")
    books.upsert_document(
        "민법총칙.md", "민법총칙", ["임대차보증금의 반환은 목적물 인도와 동시이행 관계에 있다."]
    )
    commentary = RagStore(folder / "commentary.sqlite3")
    commentary.upsert_document(
        "주석민법.md", "주석민법", ["임대차보증금 반환청구권의 소멸시효는 10년이다."]
    )
    cases = RagStore(folder / "cases.sqlite3")
    cases.upsert_document(
        "2018다255648.md", "대법원 2018다255648", ["임대차보증금 반환에 관한 판시사항이다."]
    )
    for store in (books, commentary, cases):
        store.close()
    yield folder


def test_every_index_in_the_folder_is_opened(library):
    store = MultiRagStore.discover(library)
    try:
        assert store.collections() == ["books", "cases", "commentary"]
    finally:
        store.close()


def test_one_search_reaches_all_of_them(library):
    store = MultiRagStore.discover(library)
    try:
        hits = store.search("임대차보증금 반환", top_k=6)
        assert {hit.collection for hit in hits} == {"books", "commentary", "cases"}
    finally:
        store.close()


def test_naming_a_collection_searches_only_that_one(library):
    """전부 뒤지지 않아도 될 때는 한 파일만 읽습니다 — 그게 속도의 전부입니다."""
    store = MultiRagStore.discover(library)
    try:
        hits = store.search("임대차보증금", top_k=6, collection="cases")
        assert hits
        assert {hit.collection for hit in hits} == {"cases"}
    finally:
        store.close()


def test_an_unknown_collection_falls_back_to_everything(library):
    """오타 하나로 빈손이 되는 것보다 전부 뒤지는 편이 낫습니다."""
    store = MultiRagStore.discover(library)
    try:
        assert store.search("임대차보증금", collection="없는자료")
    finally:
        store.close()


def test_top_k_is_respected_across_collections(library):
    store = MultiRagStore.discover(library)
    try:
        assert len(store.search("임대차보증금", top_k=2)) == 2
    finally:
        store.close()


def test_a_broken_index_does_not_take_the_others_down(library):
    store = MultiRagStore.discover(library)
    broken = store.store("cases")
    assert broken is not None
    broken.close()  # 이 컬렉션의 조회는 이제 예외를 던진다
    try:
        hits = store.search("임대차보증금 반환", top_k=6)
        assert {hit.collection for hit in hits} == {"books", "commentary"}
    finally:
        store.store("books").close()
        store.store("commentary").close()


def test_stats_add_up_and_break_down(library):
    store = MultiRagStore.discover(library)
    try:
        assert store.stats() == {
            "documents": 3,
            "chunks": 3,
            "embedded": 0,
            "collections": 3,
        }
        assert store.stats_by_collection()["books"]["documents"] == 1
    finally:
        store.close()


def test_an_existing_single_index_keeps_working_after_the_upgrade(tmp_path):
    """이미 돌고 있는 배포가 재색인 없이 그대로 떠야 합니다."""
    legacy = tmp_path / "rag.sqlite3"
    old = RagStore(legacy)
    old.upsert_document("옛자료.md", "옛자료", ["종전 색인에 들어 있던 내용이다."])
    old.close()

    store = MultiRagStore.discover(tmp_path / "rag", legacy=legacy)
    try:
        assert store.collections() == ["rag"]
        assert store.search("종전 색인")[0].collection == "rag"
    finally:
        store.close()


def test_an_empty_deployment_still_has_somewhere_to_write(tmp_path):
    store = MultiRagStore.discover(tmp_path / "rag", legacy=tmp_path / "rag.sqlite3")
    try:
        assert store.collections() == ["rag"]
        assert store.search("아무거나") == []
    finally:
        store.close()


def test_wal_sidecar_files_are_not_mistaken_for_collections(library):
    (library / "books.sqlite3-wal").write_bytes(b"")
    (library / "books.sqlite3-shm").write_bytes(b"")
    store = MultiRagStore.discover(library)
    try:
        assert store.collections() == ["books", "cases", "commentary"]
    finally:
        store.close()


# ── 도구에서 보이는 모습 ─────────────────────────────────────────────────
def tool_for(rag):
    state = TurnState()
    tools = {tool.name: tool for tool in build_tools(state=state, rag=rag, law=None)}
    return state, tools["search_local_docs"]


@pytest.mark.asyncio
async def test_the_tool_tells_the_model_which_collections_exist(library):
    store = MultiRagStore.discover(library)
    try:
        _state, tool = tool_for(store)
        assert "books" in tool.description
        assert "collection" in tool.input_schema["properties"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_tool_marks_where_each_quote_came_from(library):
    store = MultiRagStore.discover(library)
    try:
        state, tool = tool_for(store)
        result = await tool.handler({"query": "임대차보증금 반환"})
        assert "[cases]" in result or "[books]" in result
        assert state.citations
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_tool_can_narrow_to_one_collection(library):
    store = MultiRagStore.discover(library)
    try:
        _state, tool = tool_for(store)
        result = await tool.handler({"query": "임대차보증금", "collection": "commentary"})
        assert "소멸시효는 10년" in result
        assert "[cases]" not in result
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_single_index_needs_no_collection_argument(tmp_path):
    """자료가 하나뿐이면 고를 것이 없으니 인자도 만들지 않습니다."""
    store = RagStore(tmp_path / "one.sqlite3")
    store.upsert_document("a.md", "자료", ["임대차보증금 반환 내용."])
    try:
        _state, tool = tool_for(store)
        assert "collection" not in tool.input_schema["properties"]
        assert "■ 자료" in await tool.handler({"query": "임대차보증금"})
    finally:
        store.close()


# ── 설정 ─────────────────────────────────────────────────────────────────
def test_the_index_folder_defaults_next_to_the_database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAG_DIR", raising=False)
    settings = Settings()
    assert settings.rag_dir == tmp_path / "rag"
    assert settings.rag_path("books") == tmp_path / "rag" / "books.sqlite3"


def test_a_collection_name_cannot_escape_the_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings()
    assert settings.rag_path("../../etc/passwd").parent == settings.rag_dir
    assert settings.rag_path("").name == "corpus.sqlite3"


def test_the_index_folder_can_be_moved_to_a_bigger_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAG_DIR", "/srv/moa/rag")
    from pathlib import Path

    assert Settings().rag_dir == Path("/srv/moa/rag")
