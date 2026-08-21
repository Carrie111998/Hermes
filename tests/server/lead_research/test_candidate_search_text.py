"""The corpus is scanned once at import, not once per campaign.

`select` used to read a country's rows, JSON-decode each one, rebuild the string
a product term matches against and fold it — every run. The corpus is immutable,
so that produced an identical string every time, and the work is the expensive
part: `normalize_name` runs an NFKD decomposition per field per row. Ten
campaigns into one country did it ten times.

The value is written at import now, and selection filters on it in the database
instead of after decoding everything. Rows imported before the column existed
still work: they fall back to computing it, so no corpus needs a backfill to
stay correct — only to get faster.
"""
from __future__ import annotations

import json

import pytest

from server.db import Database
from server.lead_research.candidates import (
    CandidateRepository, like_pattern, search_text,
)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "candidates.db")


def _corpus(rows) -> bytes:
    return "\n".join(json.dumps(row) for row in rows).encode()


ATLAS = {
    "source_record_id": "atlas-1",
    "company_name": "Atlas Kitchens GmbH",
    "country": "DE",
    "domain": "https://atlas.example.test",
    "categories": ["Built-in ovens"],
    "aliases": ["Atlas Kitchen"],
}


def _import(repo, rows, version="1"):
    repo.import_file("kitchen-appliances", version, "candidates.jsonl", _corpus(rows))


def test_import_stores_the_match_text(db):
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])

    stored = db.one("SELECT search_text FROM candidate_records")["search_text"]

    assert stored
    assert "built in oven" in stored


def test_selection_matches_against_the_stored_text(db):
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])

    selected = repo.select(countries=["DE"], product_terms=["oven"], limit=10)

    assert [record.source_record_id for record in selected] == ["atlas-1"]


def test_a_term_at_the_end_of_the_text_still_matches(db):
    """The SQL pattern has to pad exactly as the word-boundary check pads.

    `oven` is the last word of `... built in oven`, so an unpadded LIKE found
    nothing and selection silently returned an empty corpus.
    """
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])

    assert repo.select(countries=["DE"], product_terms=["oven"], limit=10)


def test_a_wildcard_in_a_term_does_not_match_everything(db):
    """`100% cotton` reaching SQL unescaped would select the whole corpus."""
    repo = CandidateRepository(db)
    _import(repo, [ATLAS, {**ATLAS, "source_record_id": "nordic-1",
                           "company_name": "Nordic Textiles AB",
                           "domain": "https://nordic.example.test",
                           "categories": ["100% cotton fabrics"], "aliases": []}])

    matched = repo.select(countries=["DE"], product_terms=["100% cotton"], limit=10)

    assert [record.source_record_id for record in matched] == ["nordic-1"]


def test_an_unstored_row_is_still_selected(db):
    """A corpus imported before the column must not vanish from selection."""
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])
    db.execute("UPDATE candidate_records SET search_text=NULL")

    selected = repo.select(countries=["DE"], product_terms=["oven"], limit=10)

    assert [record.source_record_id for record in selected] == ["atlas-1"]


def test_an_unstored_row_is_still_counted_by_the_diagnostic(db):
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])
    db.execute("UPDATE candidate_records SET search_text=NULL")

    counts = repo.term_match_counts(countries=["DE"], product_terms=["oven", "tiles"])

    assert counts == {"oven": 1, "tiles": 0}


def test_backfill_fills_only_what_is_missing_and_repeats_safely(db):
    repo = CandidateRepository(db)
    _import(repo, [ATLAS])
    db.execute("UPDATE candidate_records SET search_text=NULL")

    assert repo.backfill_search_text() == 1
    assert repo.backfill_search_text() == 0
    assert db.one("SELECT search_text FROM candidate_records")["search_text"] == search_text(
        "atlas kitchens gmbh", {"aliases": ["Atlas Kitchen"], "categories": ["Built-in ovens"]}
    )


def test_backfill_batches_without_losing_rows(db):
    repo = CandidateRepository(db)
    _import(repo, [
        {**ATLAS, "source_record_id": f"row-{index}",
         "company_name": f"Atlas {index} GmbH",
         "domain": f"https://atlas-{index}.example.test", "aliases": []}
        for index in range(7)
    ])
    db.execute("UPDATE candidate_records SET search_text=NULL")

    assert repo.backfill_search_text(batch=2) == 7
    assert db.one(
        "SELECT COUNT(*) AS n FROM candidate_records WHERE search_text IS NULL"
    )["n"] == 0


def test_the_stored_text_is_what_the_matcher_expects():
    """Import and read-time fallback must not be able to drift apart."""
    text = search_text("atlas kitchens gmbh", {"categories": ["Built-in Ovens"]})

    assert like_pattern("oven").strip("%").strip() in text
