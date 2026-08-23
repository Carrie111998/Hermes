"""Korean retrieval: the bigram index is what makes 부분 일치 work at all."""

from __future__ import annotations

from pathlib import Path

import pytest

from kakao_legal_bot.app.rag.ingest import ingest_path, read_docx
from kakao_legal_bot.app.rag.store import RagStore, chunk_text, cosine
from kakao_legal_bot.app.rag.tokenize import index_tokens, match_query


@pytest.fixture
def store(tmp_path: Path) -> RagStore:
    rag = RagStore(tmp_path / "rag.sqlite3")
    yield rag
    rag.close()


def test_bigrams_are_generated_for_hangul():
    tokens = index_tokens("임대차보증금")
    assert "임대" in tokens
    assert "대차" in tokens
    assert "보증" in tokens


def test_short_words_are_kept_whole():
    assert "계약" in index_tokens("계약 해지")


def test_latin_and_digits_survive():
    tokens = index_tokens("2018다255648 판결 LTV 규정")
    assert "2018" in tokens
    assert "255648" in tokens
    assert "ltv" in tokens


def test_match_query_is_or_joined_and_quoted():
    expression = match_query("보증금 반환")
    assert expression.startswith('"')
    assert " OR " in expression


def test_empty_query_produces_no_expression():
    assert match_query("!!!") == ""


def test_substring_search_finds_a_compound_word(store: RagStore):
    store.upsert_document(
        "민법주해.txt",
        "민법주해 임대차편",
        ["임대차보증금반환청구권은 임대차가 종료한 때에 발생한다."],
    )
    hits = store.search("보증금 반환", top_k=3)
    assert hits
    assert "임대차보증금반환청구권" in hits[0].text


def test_unrelated_query_returns_nothing(store: RagStore):
    store.upsert_document("a.txt", "임대차", ["임대차보증금 반환에 관한 설명"])
    assert store.search("자동차 등록 절차 배기량") == []


def test_reingesting_the_same_content_is_a_no_op(store: RagStore):
    chunks = ["같은 내용입니다."]
    assert store.upsert_document("a.txt", "제목", chunks, sha="abc") == 1
    assert store.upsert_document("a.txt", "제목", chunks, sha="abc") == 0
    assert store.stats()["chunks"] == 1


def test_changed_content_replaces_old_chunks(store: RagStore):
    store.upsert_document("a.txt", "제목", ["옛날 내용"], sha="v1")
    store.upsert_document("a.txt", "제목", ["새로운 내용"], sha="v2")
    assert store.stats()["chunks"] == 1
    assert store.search("옛날") == []
    assert store.search("새로운")


def test_deleting_a_document_clears_the_index(store: RagStore):
    store.upsert_document("a.txt", "제목", ["삭제될 내용"])
    assert store.delete_document("a.txt") is True
    assert store.search("삭제될") == []
    assert store.stats() == {"documents": 0, "chunks": 0, "embedded": 0}


def test_malformed_query_does_not_raise(store: RagStore):
    store.upsert_document("a.txt", "제목", ["내용"])
    assert store.search('") OR (""') == []


def test_embedding_rerank_prefers_the_similar_chunk(store: RagStore):
    store.upsert_document("a.txt", "계약", ["계약 해지 조항 설명"], sha="1")
    store.upsert_document("b.txt", "계약", ["계약 갱신 조항 설명"], sha="2")
    ids = [hit.chunk_id for hit in store.search("계약 조항", top_k=5)]
    assert len(ids) == 2

    store.set_embedding(ids[0], [0.0, 1.0])
    store.set_embedding(ids[1], [1.0, 0.0])
    ranked = store.search_with_embedding("계약 조항", [1.0, 0.0], top_k=2)
    assert ranked[0].chunk_id == ids[1]


def test_cosine_edge_cases():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_chunking_respects_the_size_budget():
    text = "\n\n".join("문단" * 200 for _ in range(5))
    chunks = chunk_text(text, size=900, overlap=100)
    assert chunks
    assert all(len(chunk) <= 1000 for chunk in chunks)


def test_chunking_short_text_is_one_chunk():
    assert chunk_text("짧은 글", 900) == ["짧은 글"]


def test_ingest_a_text_file(store: RagStore, tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "메모.md").write_text("상가임대차보호법 관련 실무 메모입니다.", encoding="utf-8")

    count = ingest_path(store, corpus / "메모.md", corpus, 900, 100)
    assert count == 1
    assert store.search("상가임대차")


def test_ingest_jsonl_records(store: RagStore, tmp_path: Path):
    path = tmp_path / "조문.jsonl"
    path.write_text(
        '{"title": "민법", "locator": "제618조", "text": "임대차는 당사자 일방이 상대방에게 목적물을 사용하게 할 것을 약정한다."}\n',
        encoding="utf-8",
    )
    assert ingest_path(store, path, tmp_path, 900, 100) == 1
    hits = store.search("임대차 목적물 사용")
    assert hits
    assert hits[0].locator == "제618조"
    assert "민법" in hits[0].citation


def test_read_docx_extracts_paragraph_text(tmp_path: Path):
    from kakao_legal_bot.app.docxgen import build_docx

    path = tmp_path / "초안.docx"
    path.write_bytes(build_docx("내용증명", "첫째 문단입니다.\n\n둘째 문단입니다."))
    text = read_docx(path)
    assert "첫째 문단입니다." in text
    assert "둘째 문단입니다." in text
