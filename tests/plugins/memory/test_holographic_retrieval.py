"""Tests for FactRetriever FTS5 query sanitization.

These tests cover the fix where raw natural-language queries passed to
FTS5 MATCH were AND-joined by default, dropping recall to zero on any
multi-word prose query. The sanitizer drops stopwords and OR-joins the
remaining content tokens as phrase literals.
"""
from __future__ import annotations

import pytest

pytest.importorskip("numpy")  # retrieval module imports numpy indirectly

from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore


# ---------------------------------------------------------------------------
# _sanitize_fts_query — unit tests (no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query,expected_tokens",
    [
        # stopwords dropped
        ("what happened with the deployment rollback", {"happened", "deployment", "rollback"}),
        # single content word passes through
        ("compaction", {"compaction"}),
        # all stopwords → falls back to raw
        ("the and of", None),  # None = sentinel for fallback-to-raw
        # empty string → empty output
        ("", ""),
        # FTS5 operator characters stripped
        ("context: length-probe", {"context", "lengthprobe"}),
        # trailing punctuation stripped by tokenizer
        ("hello, world!", {"hello", "world"}),
    ],
)
def test_sanitize_fts_query_extracts_content_tokens(query, expected_tokens):
    result = FactRetriever._sanitize_fts_query(query)

    if expected_tokens == "":
        assert result == ""
        return

    if expected_tokens is None:
        # Pathological case: all stopwords — should fall back to raw query
        assert result == query
        return

    # OR-joined phrase literals: `"tok1" OR "tok2" OR ...`
    # Extract the tokens between quotes, order-independent.
    import re
    matches = re.findall(r'"([^"]+)"', result)
    assert set(matches) == expected_tokens, f"got {result!r}"


# ---------------------------------------------------------------------------
# Integration test — actually run _fts_candidates against an in-memory DB
# ---------------------------------------------------------------------------

@pytest.fixture
def retriever_with_facts(tmp_path):
    """MemoryStore seeded with a few facts for retrieval tests."""
    db_path = tmp_path / "test_facts.db"
    store = MemoryStore(str(db_path))
    store.add_fact(
        content="The Thursday deployment rollback failed because of stale migration state.",
        category="project",
    )
    store.add_fact(
        content="Compaction settings tuned to 0.85 threshold.",
        category="tool",
    )
    store.add_fact(
        content="Venice.ai advertises availableContextTokens inside model_spec.",
        category="tool",
    )
    retriever = FactRetriever(store=store)
    yield retriever
    store.close()


def test_prefetch_recovers_prose_query(retriever_with_facts):
    """A natural-language query should now match the relevant fact.

    Before the sanitizer fix, 'what happened with the deployment rollback'
    returned zero hits because FTS5 required every token to co-occur.
    """
    results = retriever_with_facts.search(
        "what happened with the deployment rollback"
    )
    assert len(results) >= 1
    # The top hit should be the deployment rollback fact
    assert "deployment rollback" in results[0]["content"].lower()


def test_prefetch_single_keyword_still_works(retriever_with_facts):
    """Single-term queries (pre-fix working case) remain working."""
    results = retriever_with_facts.search("compaction")
    assert len(results) >= 1
    assert "Compaction" in results[0]["content"] or "compaction" in results[0]["content"].lower()


def test_prefetch_stopword_only_query_empty(retriever_with_facts):
    """Pure stopword queries return zero results but don't crash."""
    # Pass to _sanitize_fts_query directly first so we know what happens
    assert FactRetriever._sanitize_fts_query("the and of") == "the and of"
    # search() handles the likely-zero-hit case gracefully
    results = retriever_with_facts.search("the and of")
    # Either zero results or it errored-gracefully to [] — both are fine
    assert isinstance(results, list)


def test_retrieval_increments_count_for_returned_facts(retriever_with_facts):
    """When a search returns facts, those facts must have their
    retrieval_count incremented. Facts not returned by the search
    must not be affected.

    This tests the behavioral contract: retrieval is observable.
    It does not prescribe which method, SQL path, or internal
    architecture performs the increment.
    """
    results = retriever_with_facts.search("deployment rollback")
    assert len(results) >= 1

    # Read back the returned facts from the store — verify counter moved
    returned_ids = {r["fact_id"] for r in results}
    store = retriever_with_facts.store

    rows = store.list_facts(limit=50)
    for row in rows:
        if row["fact_id"] in returned_ids:
            assert row["retrieval_count"] >= 1, (
                f"fact_id={row['fact_id']} was returned by search but "
                f"retrieval_count={row['retrieval_count']} — counter did not increment"
            )
        elif "Compaction" in row["content"]:
            assert row["retrieval_count"] == 0, (
                f"fact_id={row['fact_id']} was NOT returned by search but "
                f"retrieval_count={row['retrieval_count']} — non-matched fact was incremented"
            )


def test_retrieval_count_accumulates_across_repeated_searches(retriever_with_facts):
    """retrieval_count must reflect how many times a fact has been
    retrieved — it accumulates, not resets, across repeated searches.
    """
    retriever = retriever_with_facts
    store = retriever.store

    # Search three times
    for _ in range(3):
        retriever.search("deployment rollback")

    # Read back via the public API — verify the deployment fact accumulated >= 3
    rows = store.list_facts(limit=50)
    deployment_rows = [r for r in rows if "deployment rollback" in r["content"]]
    assert len(deployment_rows) >= 1, "Deployment fact not found in store"

    for row in deployment_rows:
        assert row["retrieval_count"] >= 3, (
            f"Expected retrieval_count >= 3 after 3 searches, got {row['retrieval_count']}"
        )


def test_returned_dict_matches_db_retrieval_count(retriever_with_facts):
    """The retrieval_count in the returned result dicts must match
    what the DB holds after the increment — not the stale pre-increment value.
    """
    retriever = retriever_with_facts
    store = retriever.store

    results = retriever.search("deployment rollback")
    assert len(results) >= 1

    for r in results:
        # Read from DB after search
        db_row = store._conn.execute(
            "SELECT retrieval_count FROM facts WHERE fact_id = ?", (r["fact_id"],)
        ).fetchone()
        assert r["retrieval_count"] == db_row["retrieval_count"], (
            f"Returned dict has retrieval_count={r['retrieval_count']} "
            f"but DB has {db_row['retrieval_count']}"
        )


# ---------------------------------------------------------------------------
# Entity-based retrieval fixtures — facts with extracted entities
# ---------------------------------------------------------------------------

@pytest.fixture
def retriever_with_entities(tmp_path):
    """MemoryStore seeded with facts containing double-quoted entities
    so probe/related/reason use HRR algebra, not FTS5 fallback."""
    db_path = tmp_path / "test_entities.db"
    store = MemoryStore(str(db_path))
    store.add_fact(
        '"Depakote" reduces seizure frequency by binding to "GABA" receptors.',
        category="general",
    )
    store.add_fact(
        '"Depakote" side effects include thrombocytopenia and weight gain.',
        category="general",
    )
    store.add_fact(
        '"GABA" is the primary inhibitory neurotransmitter in the brain.',
        category="general",
    )
    retriever = FactRetriever(store=store)
    yield retriever
    store.close()


class TestRetrievalCountOnAllPaths:
    """retrieval_count must increment on every retrieval path,
    not just search()."""

    def test_probe_increments_count_for_matched_facts(self, retriever_with_entities):
        """probe() uses HRR algebra — it must still increment the counter."""
        retriever = retriever_with_entities
        store = retriever.store

        results = retriever.probe("Depakote")
        assert len(results) >= 1

        returned_ids = {r["fact_id"] for r in results}
        rows = store.list_facts(limit=50)
        for row in rows:
            if row["fact_id"] in returned_ids:
                assert row["retrieval_count"] >= 1, (
                    f"fact_id={row['fact_id']} returned by probe() "
                    f"but retrieval_count={row['retrieval_count']}"
                )
            elif "GABA is the primary" in row["content"]:
                # This fact does NOT contain "Depakote" — should not be in probe results
                assert row["retrieval_count"] == 0, (
                    f"fact_id={row['fact_id']} NOT returned by probe() "
                    f"but retrieval_count={row['retrieval_count']}"
                )

    def test_related_increments_count(self, retriever_with_entities):
        """related() must increment the counter for returned facts."""
        retriever = retriever_with_entities
        store = retriever.store

        results = retriever.related("Depakote")
        assert len(results) >= 1

        returned_ids = {r["fact_id"] for r in results}
        rows = store.list_facts(limit=50)
        for row in rows:
            if row["fact_id"] in returned_ids:
                assert row["retrieval_count"] >= 1, (
                    f"fact_id={row['fact_id']} returned by related() "
                    f"but retrieval_count={row['retrieval_count']}"
                )

    def test_reason_increments_count(self, retriever_with_entities):
        """reason() must increment the counter for returned facts."""
        retriever = retriever_with_entities
        store = retriever.store

        results = retriever.reason(["Depakote", "GABA"])
        assert len(results) >= 1

        returned_ids = {r["fact_id"] for r in results}
        rows = store.list_facts(limit=50)
        for row in rows:
            if row["fact_id"] in returned_ids:
                assert row["retrieval_count"] >= 1, (
                    f"fact_id={row['fact_id']} returned by reason() "
                    f"but retrieval_count={row['retrieval_count']}"
                )
