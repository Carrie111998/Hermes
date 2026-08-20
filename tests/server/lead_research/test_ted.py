from __future__ import annotations

import json

import httpx
import pytest

from server.lead_research.candidates import CandidateRecord
from server.lead_research.models import DiscoveryQuery
from server.lead_research.providers.ted import TedVerifier, _identity_match, _search_term
from server.lead_research.registry import build_registry


def candidate(name: str, country: str = "PL", **data) -> CandidateRecord:
    return CandidateRecord(
        dataset_id="private-buyers",
        version="2026-08",
        source_record_id="rec-1",
        company_name=name,
        normalized_name=name.casefold(),
        country=country,
        domain=data.pop("domain", None),
        data={"categories": ["kitchen-appliances"], **data},
    )


@pytest.fixture()
def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        campaign_id="campaign-1",
        seller_countries=["TR"],
        target_countries=["PL"],
        sector_ids=["kitchen-appliances"],
        hs_codes=[],
        buyer_types=["distributor"],
        max_records=10,
    )


def notice(publication: str, winners: list[str], country: str = "POL", **extra) -> dict:
    return {
        "publication-number": publication,
        "winner-name": {"pol": winners},
        "winner-country": [country],
        "notice-title": {"eng": "Poland-Warsaw: kitchen-appliances supply"},
        **extra,
    }


def verifier(notices: list[dict], calls: list | None = None) -> TedVerifier:
    def respond(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(200, json={"notices": notices})

    return TedVerifier(httpx.Client(transport=httpx.MockTransport(respond)))


# A partner surname inside somebody else's company name is the false positive
# that a bare substring test lets through, and a wrong lead is worse than none.
def test_shared_word_is_not_an_identity_match():
    assert _identity_match(
        candidate("DUKAT"), ["CADXPERT P. GURGA M. DUKAT SPÓŁKA KOMANDYTOWA"]
    ) is None


def test_legal_form_suffix_does_not_block_a_real_match():
    assert _identity_match(candidate("Leroy Merlin"), ["LEROY MERLIN, S.L."]) == "LEROY MERLIN, S.L."
    assert _identity_match(
        candidate("PFF"), ["PFF SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"]
    ) is not None


def test_unrelated_winners_produce_no_evidence(query):
    bundle = verifier([notice("1-2025", ["Britenet sp. z o.o.", "THEISEN TECH"])]).verify(
        query, candidate("DUKAT")
    )
    assert bundle.sources == []
    assert bundle.independent_source_count == 0


def test_matching_winner_yields_a_cited_independent_source(query):
    bundle = verifier([notice("65180-2025", ["DUKAT SPÓŁKA AKCYJNA"])]).verify(
        query, candidate("Dukat")
    )
    (source,) = bundle.sources
    assert source.provenance_url == "https://ted.europa.eu/en/notice/-/detail/65180-2025"
    assert source.classification == "independent"
    assert source.facts["country"] == ["PL"]
    assert "public procurement supplier" in source.facts["buyer_role"]
    assert source.facts["product_term"] == ["kitchen-appliances"]


# TED reports alpha-3 and candidates carry alpha-2; a mismatch must stay silent
# rather than assert a country the notice does not support.
def test_country_fact_is_withheld_when_the_winner_sits_elsewhere(query):
    bundle = verifier([notice("7-2025", ["Dukat sp. z o.o."], country="DEU")]).verify(
        query, candidate("Dukat")
    )
    assert "country" not in bundle.sources[0].facts


def test_domain_fact_requires_the_notice_to_carry_the_same_host(query):
    notices = [notice("8-2025", ["Dukat sp. z o.o."],
                      **{"winner-internet-address": ["https://www.dukat.example/en"]})]
    bundle = verifier(notices).verify(query, candidate("Dukat", domain="dukat.example"))
    assert bundle.sources[0].facts["domain"] == ["dukat.example"]

    bundle = verifier(notices).verify(query, candidate("Dukat", domain="other.example"))
    assert "domain" not in bundle.sources[0].facts


def test_punctuation_in_a_company_name_cannot_break_the_expert_query(query):
    calls: list = []
    verifier([], calls).verify(query, candidate("ISP / Asia 4 Y (Trading), s.r.o."))
    assert calls, "the verifier must still issue a search"
    assert not set('/(),~"') & set(_search_term("ISP / Asia 4 Y (Trading), s.r.o."))
    assert _search_term("ISP / Asia 4 Y (Trading)") == "ISP Asia 4 Y Trading"
    assert _search_term("///") == ""


def test_a_name_with_nothing_searchable_costs_no_request(query):
    calls: list = []
    bundle = verifier([], calls).verify(query, candidate("///"))
    assert calls == []
    assert bundle.sources == []


def test_one_search_per_candidate(query):
    calls: list = []
    verifier([notice("9-2025", ["Dukat sp. z o.o."])], calls).verify(query, candidate("Dukat"))
    assert len(calls) == 1
    assert calls[0]["query"] == 'winner-name ~ ("Dukat") AND winner-country IN ("POL")'


def test_ted_is_a_verifier_in_the_registry_without_any_credential():
    provider = build_registry().get("ted")
    assert callable(getattr(provider, "verify", None))
    assert provider.definition.adapter_mode == "live"
    assert provider.health().status == "active"
