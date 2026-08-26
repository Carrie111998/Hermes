"""A skewed multi-market corpus must not collapse into its biggest market.

The deployed corpus is a customer export, and customer exports are lopsided:
the real kitchen-appliance list carries 579 AE rows against 107 DE rows. A run
that simply took the top of the pool would hand the operator a page of Emirati
distributors and call five markets covered.

The existing acceptance gate proves the balance rule on a fixture with roughly
even markets, where "balanced" and "top of the pool" produce the same answer.
This one skews the corpus the way the real one is skewed, so the two answers
diverge and only the balanced one passes.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from server.lead_research.candidates import CandidateRepository
from server.lead_research.providers.corpus import CorpusProvider, corpus_definition
from server.lead_research.registry import ProviderRegistry
from tests.server.test_clean_demo_e2e import make_clean_demo


# Proportional to the shipped corpus (AE 579 / SA 541 / IQ 274 / EG 152 / DE 107),
# scaled down so the test stays fast. The point is the ratio, not the volume.
MARKET_SKEW = {"AE": 58, "SA": 54, "IQ": 27, "EG": 15, "DE": 11}
TARGET_COUNTRIES = tuple(MARKET_SKEW)
RESULT_LIMIT = 15


@pytest.fixture()
def corpus_only_registry() -> ProviderRegistry:
    """Only the corpus verifier — the credential-free path an operator starts on."""
    definition = corpus_definition().model_copy(update={"default_enabled": True})
    provider = CorpusProvider()
    provider.definition = definition
    return ProviderRegistry([definition], {definition.source_id: provider})


@pytest.fixture()
def skewed_candidates() -> bytes:
    header = b"source_record_id,company_name,country,categories,buyer_types\n"
    rows = [
        f"{country.lower()}-{index:03d},{country} Appliance Importer {index:03d},"
        f"{country},household-appliances,distributor\n".encode()
        for country, count in MARKET_SKEW.items()
        for index in range(count)
    ]
    return header + b"".join(rows)


def _run_campaign(client, headers, company_id):
    created = client.post("/api/v1/research-campaigns", headers=headers, json={
        "name": "Skewed multi-market appliance buyers",
        "seller_countries": ["TR"],
        "target_countries": list(TARGET_COUNTRIES),
        "sector_ids": ["household-appliances"],
        "buyer_types": ["distributor"],
        "enabled_source_ids": [corpus_definition().source_id],
    })
    assert created.status_code == 201, created.text
    campaign = created.json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )
    assert started.status_code == 202, started.text
    settled = client.app.state.lead_research.wait_until_settled(
        company_id, campaign["id"], timeout=180,
    )
    assert settled is not None and settled["status"] == "succeeded", settled
    return campaign, settled


@pytest.fixture()
def skewed_run(tmp_path: Path, corpus_only_registry, skewed_candidates):
    db, client, headers, company_id = make_clean_demo(
        tmp_path,
        corpus_only_registry,
        target_countries=TARGET_COUNTRIES,
        db_name="skewed-corpus.db",
    )
    imported = CandidateRepository(db).import_file(
        corpus_definition().source_id, "skew-v1", "skewed.csv", skewed_candidates,
        assertion_manifest={
            "purpose": "curated_buyers",
            "asserted_fields": [
                "company_identity", "target_presence",
                "product_sector_relevance", "buyer_membership",
            ],
            "sector_ids": ["household-appliances"],
            "product_terms": [],
            "publisher_label": "Skewed customer export",
            "curated_at": 1787616000.0,
            "freshness_unknown": False,
            "curation_note": "Synthetic export shaped like the shipped corpus.",
        },
    )
    assert imported.record_count == sum(MARKET_SKEW.values())
    campaign, settled = _run_campaign(client, headers, company_id)

    def view(name: str):
        response = client.get(
            f"/api/v1/research-campaigns/{campaign['id']}/results?view={name}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    metrics = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/metrics", headers=headers,
    ).json()[0]
    return client, headers, campaign, metrics, view("active"), view("outside_limit")


def test_every_target_market_is_represented(skewed_run) -> None:
    """AE outnumbers DE five to one; DE must still reach the operator."""
    *_, active, _overflow = skewed_run
    by_country = Counter(row["country"] for row in active)
    assert set(by_country) == set(TARGET_COUNTRIES), (
        f"markets missing from the results: {set(TARGET_COUNTRIES) - set(by_country)}"
    )


def test_no_market_crowds_out_another(skewed_run) -> None:
    """The spread between the largest and smallest market share stays at most one."""
    *_, active, _overflow = skewed_run
    by_country = Counter(row["country"] for row in active)
    assert max(by_country.values()) - min(by_country.values()) <= 1, by_country


def test_result_cap_holds_and_the_overflow_is_kept_not_dropped(skewed_run) -> None:
    _, _, _, metrics, active, overflow = skewed_run
    assert len(active) <= RESULT_LIMIT, len(active)
    assert metrics["qualified_leads"] == len(active)
    # Everything the run qualified but could not show has to be reachable,
    # otherwise a capped result page is indistinguishable from a thin pool.
    assert metrics["strong_fit_pool"] == len(active) + metrics["outside_result_limit"]
    assert len(overflow) == metrics["outside_result_limit"], (
        f"{metrics['outside_result_limit']} leads counted as overflow but "
        f"{len(overflow)} returned by the outside_limit view"
    )
    assert not ({row["id"] for row in active} & {row["id"] for row in overflow})


def test_corpus_only_evidence_is_declared_as_incomplete(skewed_run) -> None:
    """With no web verifier the run must say so rather than imply confirmation."""
    *_, active, _overflow = skewed_run
    for row in active:
        assert row["source_ids"] == [corpus_definition().source_id], row["source_ids"]
        assert "independent_source" in row["missing_evidence"], row["missing_evidence"]
        assert not row["official_domains"], row["official_domains"]


def test_unverifiable_dimensions_are_unknown_not_zero(skewed_run) -> None:
    """A curated list cannot speak to intent or scale; scoring must not invent it."""
    *_, active, _overflow = skewed_run
    for row in active:
        dimensions = row["score_dimensions"]
        assert dimensions["buying_intent"] is None, dimensions
        assert dimensions["trade_activity"] is None, dimensions
        assert row["unknown_weight"] > 0, row
