from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from server.config import Settings
from server.lead_research.candidates import CandidateRecord
from server.lead_research.models import DiscoveryQuery
from server.lead_research.providers.bright_data import BrightDataVerifier, _fact_matches
from server.lead_research.registry import build_registry


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "interfaze.db",
        brightdata_enabled=True,
        brightdata_api_key="",
        brightdata_unlocker_zone="test-zone",
    )


@pytest.fixture()
def candidate() -> CandidateRecord:
    return CandidateRecord(
        dataset_id="private-buyers",
        version="2026-08",
        source_record_id="acme-de",
        company_name="Acme Handel GmbH",
        normalized_name="acme handel gmbh",
        country="DE",
        domain="acme.example",
        data={
            "buyer_types": ["distributor"],
            "categories": ["heat pumps"],
            "provenance_url": "https://private-corpus.example/acme",
        },
    )


@pytest.fixture()
def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        campaign_id="campaign-1",
        seller_countries=["TR"],
        target_countries=["DE"],
        sector_ids=["industrial-machinery"],
        buyer_types=["importer"],
    )


def test_bright_data_is_unavailable_without_secret(settings):
    registry = build_registry(settings=settings)
    item = next(item for item in registry.list() if item.source_id == "brightdata-web")

    health = registry.get(item.source_id).health()

    assert health.status == "unavailable"
    assert health.reason == "credential_required"


def test_bright_data_is_unavailable_with_whitespace_only_secret(settings):
    registry = build_registry(settings=Settings(
        database_path=settings.database_path,
        brightdata_enabled=True,
        brightdata_api_key="  \t  ",
        brightdata_unlocker_zone="test-zone",
    ))

    health = registry.get("brightdata-web").health()

    assert health.status == "unavailable"
    assert health.reason == "credential_required"


def test_bright_data_is_unavailable_when_disabled(settings):
    registry = build_registry(
        settings=Settings(
            database_path=settings.database_path,
            brightdata_enabled=False,
            brightdata_api_key="do-not-return-this-key",
            brightdata_unlocker_zone="test-zone",
        )
    )

    health = registry.get("brightdata-web").health()

    assert health.status == "unavailable"
    assert health.reason == "disabled"
    assert "do-not-return-this-key" not in json.dumps(health.model_dump(mode="json"))


def test_explicit_provider_injection_remains_provider_neutral(settings):
    injected = object()

    registry = build_registry(settings=settings, providers={"brightdata-web": injected})

    assert registry.get("brightdata-web") is injected


def test_verifier_returns_cited_sources(candidate, query):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["url"] == "https://acme.example":
            markdown = "Acme Handel GmbH supplies industrial machinery in Germany."
        else:
            markdown = (
                "[Acme importer profile](https://directory.example/companies/acme) "
                "Acme Handel GmbH is a German importer and distributor of heat pumps."
            )
        return httpx.Response(200, text=markdown)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    verifier = BrightDataVerifier("test-key", "test-zone", client)

    bundle = verifier.verify(query, candidate)

    assert bundle.sources
    assert all(source.provenance_url.startswith("https://") for source in bundle.sources)
    assert all(source.raw_hash for source in bundle.sources)
    assert {source.classification for source in bundle.sources} == {"official", "independent"}
    assert bundle.independent_source_count == 1
    assert all(source.facts for source in bundle.sources)
    assert candidate.data["provenance_url"] not in {
        source.provenance_url for source in bundle.sources
    }
    assert len(requests) <= 4
    assert all(request.url == "https://api.brightdata.com/request" for request in requests)
    assert all(request.headers["Authorization"] == "Bearer test-key" for request in requests)
    assert all(json.loads(request.content)["zone"] == "test-zone" for request in requests)
    assert all(json.loads(request.content)["format"] == "raw" for request in requests)
    assert all(json.loads(request.content)["data_format"] == "markdown" for request in requests)
    assert all(set(request.extensions["timeout"].values()) == {45.0} for request in requests)


def test_candidate_hints_do_not_become_evidence(candidate, query):
    raw_pages = ["Acme Handel GmbH", "No corroborating result links were returned."]

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=raw_pages.pop(0) if raw_pages else "No corroborating result links were returned.",
        )

    verifier = BrightDataVerifier(
        "test-key",
        "test-zone",
        httpx.Client(transport=httpx.MockTransport(respond)),
    )

    bundle = verifier.verify(query, candidate)

    assert bundle.independent_source_count == 0
    assert {source.provenance_url for source in bundle.sources} == {"https://acme.example"}
    assert all("buyer_role" not in source.facts for source in bundle.sources)
    assert all("product_term" not in source.facts for source in bundle.sources)


def test_source_hash_is_derived_from_retrieved_content(candidate, query):
    markdown = "Acme Handel GmbH is an importer."
    no_domain_candidate = CandidateRecord(
        **{**candidate.__dict__, "domain": None},
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"[Acme company record](https://directory.example/acme) {markdown}",
        )

    verifier = BrightDataVerifier(
        "test-key",
        "test-zone",
        httpx.Client(transport=httpx.MockTransport(respond)),
    )

    bundle = verifier.verify(query, no_domain_candidate)

    expected = hashlib.sha256(
        f"[Acme company record](https://directory.example/acme) {markdown}".encode()
    ).hexdigest()
    assert bundle.sources[0].raw_hash == expected


def test_generic_buyer_product_result_without_candidate_identity_is_rejected(candidate, query):
    no_domain_candidate = CandidateRecord(**{**candidate.__dict__, "domain": None})
    markdown = (
        "[German industrial machinery importers]"
        "(https://generic.example/importers) DE Germany distributor importer heat pumps"
    )
    verifier = BrightDataVerifier(
        "test-key",
        "test-zone",
        httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=markdown)
        )),
    )

    bundle = verifier.verify(query, no_domain_candidate)

    assert bundle.sources == []
    assert bundle.independent_source_count == 0


def test_identity_from_one_result_does_not_validate_a_different_result(candidate, query):
    no_domain_candidate = CandidateRecord(**{**candidate.__dict__, "domain": None})
    markdown = (
        "[Acme Handel GmbH](https://identity.example/acme) company profile "
        "[German machinery importer](https://generic.example/importers) "
        "distributor importer heat pumps"
    )
    verifier = BrightDataVerifier(
        "test-key",
        "test-zone",
        httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=markdown)
        )),
    )

    bundle = verifier.verify(query, no_domain_candidate)

    assert [source.provenance_url for source in bundle.sources] == [
        "https://identity.example/acme"
    ]
    assert bundle.sources[0].facts == {"company_name": ["Acme Handel GmbH"]}
    assert bundle.independent_source_count == 1


def test_explicit_candidate_alias_establishes_result_identity(candidate, query):
    alias_candidate = CandidateRecord(**{
        **candidate.__dict__,
        "domain": None,
        "data": {**candidate.data, "aliases": ["Acme Waerme"]},
    })
    markdown = (
        "[Acme Waerme](https://alias.example/acme) "
        "distributor of heat pumps"
    )
    verifier = BrightDataVerifier(
        "test-key",
        "test-zone",
        httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=markdown)
        )),
    )

    bundle = verifier.verify(query, alias_candidate)

    assert [source.provenance_url for source in bundle.sources] == [
        "https://alias.example/acme"
    ]
    assert bundle.sources[0].facts["company_name"] == ["Acme Handel GmbH"]
    assert bundle.sources[0].facts["buyer_role"] == ["distributor"]
    assert bundle.sources[0].facts["product_term"] == ["heat pumps"]
    assert bundle.independent_source_count == 1


# A closed company must be retired, but a false positive removes a live company
# from every future run, so the signal is narrow and identity-gated.
@pytest.mark.parametrize("text", [
    "Acme Handel GmbH is permanently closed",
    "Acme Handel GmbH has ceased operations",
    "Acme Handel GmbH went out of business last year",
    "Acme Handel GmbH is in liquidation",
    "Acme Handel GmbH filed for bankruptcy",
])
def test_closure_phrases_retire_the_candidate(candidate, text):
    facts = _fact_matches(text, candidate, [], [], "independent")
    assert facts.get("lifecycle_status") == ["closed"]


@pytest.mark.parametrize("text", [
    "Acme Handel GmbH is closed on Sundays",
    "Acme Handel GmbH closed a funding round",
    "Acme Handel GmbH closed its Berlin branch",
    "Acme Handel GmbH announced a closed beta",
])
def test_ordinary_uses_of_closed_do_not_retire_a_company(candidate, text):
    """"Closed" alone is common prose; only business-ending phrases count."""
    assert "lifecycle_status" not in _fact_matches(text, candidate, [], [], "independent")


def test_closure_of_another_company_does_not_retire_this_one(candidate):
    """An official page may mention some other firm's collapse."""
    facts = _fact_matches(
        "Unrelated Trading Ltd has ceased operations", candidate, [], [], "official"
    )
    assert "lifecycle_status" not in facts
