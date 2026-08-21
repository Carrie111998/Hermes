"""The trap in front of every user of the brief page.

Discovery matched terms with AND, as substrings, after a normalisation that left
separators alone. Three consequences, all of which returned zero candidates and
none of which said so:

- picking a second product guaranteed nothing matched, because no company name
  contains two different product names;
- a sector id spelled `household-appliances` matched a corpus whose category
  said "Household Appliances" not at all;
- and the campaign then ran, succeeded, and reported no leads, which is
  indistinguishable from a market with no buyers in it.

The plan doc worked around all of this by telling operators which exact string
to type. That is a workaround for a defect, not a configuration guide.
"""
from __future__ import annotations

import json

import pytest

from server.db import Database
from server.lead_research.candidates import CandidateRepository, searchable_term


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "terms.db")
    repository = CandidateRepository(db)
    repository.import_file(
        "buyers", "1", "candidates.jsonl",
        b"\n".join(json.dumps(row).encode() for row in [
            {
                "source_record_id": "spaced",
                "company_name": "Spaced Trading",
                "country": "DE",
                "categories": ["Household Appliances"],
            },
            {
                "source_record_id": "hyphenated",
                "company_name": "Hyphen Trading",
                "country": "DE",
                "categories": ["household-appliances"],
            },
            {
                "source_record_id": "machinery",
                "company_name": "Machinery Trading",
                "country": "DE",
                "categories": ["industrial-machinery"],
            },
            {
                "source_record_id": "oven-named",
                "company_name": "Built-in Oven Series",
                "country": "DE",
                "categories": ["household-appliances"],
            },
        ]),
    )
    return repository


def _ids(records) -> set[str]:
    return {record.source_record_id for record in records}


# ── any term, not every term ──────────────────────────────────────────────────

def test_two_terms_no_longer_guarantee_zero_candidates(repo):
    """The regression this file exists for.

    A campaign's sector ids, HS codes and product names describe one scope in
    alternative words. Requiring all of them meant selecting a second product
    could never match anything.
    """
    selected = repo.select(
        countries=["DE"],
        product_terms=["household-appliances", "built-in oven series"],
        limit=10,
    )

    assert _ids(selected) == {"spaced", "hyphenated", "oven-named"}


def test_a_single_term_behaves_exactly_as_before(repo):
    assert _ids(repo.select(
        countries=["DE"], product_terms=["industrial-machinery"], limit=10,
    )) == {"machinery"}


def test_a_term_matching_nothing_does_not_suppress_one_that_matches(repo):
    """The old AND let one unmatched term veto the whole search."""
    selected = repo.select(
        countries=["DE"],
        product_terms=["household-appliances", "term-nobody-imported"],
        limit=10,
    )

    assert _ids(selected) == {"spaced", "hyphenated", "oven-named"}


def test_terms_that_all_match_nothing_still_select_nothing(repo):
    assert repo.select(
        countries=["DE"], product_terms=["nothing-here", "also-nothing"], limit=10,
    ) == []


def test_no_terms_still_means_no_filter(repo):
    assert len(repo.select(countries=["DE"], product_terms=[], limit=10)) == 4


# ── separators and case ───────────────────────────────────────────────────────

def test_a_hyphenated_sector_id_matches_a_spaced_category(repo):
    """The mismatch that cost the corpus an entire re-import.

    `household-appliances` is the canonical sector id the brief page offers; a
    customer file spells its category however it likes.
    """
    assert _ids(repo.select(
        countries=["DE"], product_terms=["household-appliances"], limit=10,
    )) == {"spaced", "hyphenated", "oven-named"}


def test_a_spaced_term_matches_a_hyphenated_category(repo):
    assert _ids(repo.select(
        countries=["DE"], product_terms=["Household Appliances"], limit=10,
    )) == {"spaced", "hyphenated", "oven-named"}


@pytest.mark.parametrize("term", [
    "HOUSEHOLD-APPLIANCES", "Household_Appliances", "  household appliances  ",
])
def test_case_and_separator_spelling_do_not_decide_a_search(repo, term):
    assert len(repo.select(countries=["DE"], product_terms=[term], limit=10)) == 3


def test_normalisation_folds_separators_and_case_but_keeps_the_word(repo):
    assert searchable_term("Household-Appliances") == "household appliances"
    assert searchable_term("household_appliances") == "household appliances"
    assert searchable_term(" Kitchen  Appliances ") == "kitchen appliances"


# ── the diagnostic ────────────────────────────────────────────────────────────

def test_per_term_counts_name_the_term_that_matched_nothing(repo):
    """What turns "zero leads" into "this term is spelled wrong"."""
    counts = repo.term_match_counts(
        countries=["DE"],
        product_terms=["household-appliances", "industrial-machinery", "typo-term"],
    )

    assert counts == {
        "household-appliances": 3, "industrial-machinery": 1, "typo-term": 0,
    }


def test_per_term_counts_are_scoped_to_the_market_asked_about(repo):
    assert repo.term_match_counts(
        countries=["FR"], product_terms=["household-appliances"],
    ) == {"household-appliances": 0}


def test_no_terms_produce_no_counts(repo):
    assert repo.term_match_counts(countries=["DE"], product_terms=[]) == {}


def test_counts_ignore_superseded_corpus_versions(repo):
    """A corrected corpus must not be counted twice."""
    repo.import_file(
        "buyers", "2", "candidates.jsonl",
        json.dumps({
            "source_record_id": "hyphenated",
            "company_name": "Hyphen Trading",
            "country": "DE",
            "categories": ["household-appliances"],
        }).encode(),
    )

    assert repo.term_match_counts(
        countries=["DE"], product_terms=["household-appliances"],
    ) == {"household-appliances": 1}


# ── the run and the estimate say so before and after ──────────────────────────

def _campaign_harness(tmp_path, sector_ids):
    from server.db import json_dump, now
    from server.lead_research.models import (
        CampaignConfig, DatasetDefinition, ProviderHealth, VerificationBundle,
    )
    from server.lead_research.registry import ProviderRegistry
    from server.lead_research.service import LeadResearchService

    db = Database(tmp_path / "campaign.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_1", "Tenant", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "1", "candidates.jsonl",
        json.dumps({
            "source_record_id": "buyer-de-1",
            "company_name": "Atlas Handel",
            "country": "DE",
            "categories": ["household-appliances"],
        }).encode(),
    )
    definition = DatasetDefinition(
        source_id="silent-source", display_name="Silent", publisher="Tests",
        access_tier="public", entity_levels=["named_company"],
        capabilities=["candidate_verification"], adapter_mode="live", default_enabled=True,
    )

    class Silent:
        def __init__(self):
            self.definition = definition

        def health(self):
            return ProviderHealth(status="active")

        def discover(self, query):
            from server.lead_research.models import DiscoveryEstimate
            return DiscoveryEstimate(kind="reported", low=10, high=40, basis="fixture")

        def verify(self, query, candidate):
            return VerificationBundle(candidate_source_record_id=candidate.source_record_id)

    config = CampaignConfig(
        name="Search", target_countries=["DE"], sector_ids=sector_ids,
        buyer_types=["distributor"], enabled_source_ids=[definition.source_id],
    )
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("camp_1", "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    service = LeadResearchService(
        db, registry=ProviderRegistry([definition], {definition.source_id: Silent()}),
        verify_workers=1,
    )
    return db, service, config


def test_a_market_that_matched_nothing_records_why(tmp_path):
    """Zero leads used to be indistinguishable from a market with no buyers."""
    db, service, _ = _campaign_harness(tmp_path, ["kitchen-appliances"])

    service.run("cmp_1", "camp_1")

    issue = db.one(
        "SELECT issue_type,data FROM research_issues "
        "WHERE company_id='cmp_1' AND issue_type='no_candidates_selected'"
    )
    assert issue is not None, "a search that matched nothing said nothing"
    data = json.loads(issue["data"])
    assert data["country"] == "DE"
    assert data["term_matches"] == {"kitchen-appliances": 0}


def test_a_market_that_matched_something_records_no_such_issue(tmp_path):
    db, service, _ = _campaign_harness(tmp_path, ["household-appliances"])

    service.run("cmp_1", "camp_1")

    assert db.one(
        "SELECT id FROM research_issues WHERE issue_type='no_candidates_selected'"
    ) is None


def test_the_estimate_reports_what_the_corpus_can_actually_supply(tmp_path):
    """A provider range described a source in general and promised nothing real.

    An estimate could read `available` with a healthy range while selection
    matched nothing at all.
    """
    _, service, config = _campaign_harness(tmp_path, ["kitchen-appliances"])

    estimate = service.estimate(config, "cmp_1")

    assert estimate.status == "available", "the provider range is still reported"
    assert estimate.corpus_candidates == 0
    assert estimate.unmatched_terms == ["kitchen-appliances"]


def test_the_estimate_counts_the_candidates_a_good_term_finds(tmp_path):
    _, service, config = _campaign_harness(tmp_path, ["household-appliances"])

    estimate = service.estimate(config, "cmp_1")

    assert estimate.corpus_candidates == 1
    assert estimate.unmatched_terms == []


def test_an_estimate_without_a_tenant_reports_not_computed_rather_than_zero(tmp_path):
    """None and 0 mean different things and must not be confused."""
    _, service, config = _campaign_harness(tmp_path, ["household-appliances"])

    estimate = service.estimate(config)

    assert estimate.corpus_candidates is None
