"""Deterministic lead-research providers for server contract tests."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from server.agent_service import StubRunExecutor
from server.lead_research.models import (
    AgenticResearchResult,
    DatasetDefinition,
    DiscoveryEstimate,
    DiscoveryQuery,
    EvidenceEnvelope,
    ProviderHealth,
    RawPage,
    RawRecord,
    EvidenceSpan,
    ProposedFact,
    ResearchPage,
    SnapshotRef,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.quotes import spans_for_facts


def cited_source(
    *,
    provenance_url: str,
    classification: str,
    retrieved_via: str,
    facts: dict[str, list[str]],
    content: str | None = None,
    retrieved_at: float | None = None,
) -> VerificationSource:
    """A strict verifier double backed by the exact text it claims to read."""
    snapshot = content or " | ".join(
        str(value) for values in facts.values() for value in values
    )
    return VerificationSource(
        provenance_url=provenance_url,
        raw_hash=hashlib.sha256(snapshot.encode()).hexdigest(),
        classification=classification,
        retrieved_via=retrieved_via,
        facts=facts,
        snapshot_content=snapshot,
        fact_spans=spans_for_facts(snapshot, facts),
        retrieved_at=retrieved_at,
    )


def fixture_definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="fixture-directory",
        display_name="Verified buyer directory fixture",
        publisher="Interfaze test fixtures",
        jurisdiction=["global"],
        categories=["registry", "opportunity"],
        access_tier="public",
        entity_levels=["named_company", "opportunity"],
        capabilities=["organizations", "company_signals", "buying_requests"],
        # What this fake can actually speak to. Declared so completeness is
        # measured against reachable dimensions rather than all seven — an
        # undeclared source made every fixture lead look half-evidenced.
        emits=["company_name", "country", "domain", "buyer_role", "product_term"],
        freshness_days=30,
        adapter_mode="fixture",
        default_enabled=True,
    )


class DeterministicProvider:
    def __init__(self, definition: DatasetDefinition):
        self.definition = definition

    def discover(self, query: DiscoveryQuery) -> DiscoveryEstimate:
        countries = max(1, len(query.target_countries))
        return DiscoveryEstimate(
            kind="reported", low=2 * countries, high=3 * countries,
            basis="Deterministic named-company test records", confidence="high",
        )

    def fetch_page(self, query: DiscoveryQuery, cursor: str | None) -> RawPage:
        records: list[RawRecord] = []
        sectors = query.sector_ids or ["household-appliances"]
        for country in query.target_countries:
            slug = country.lower()
            records.extend([
                RawRecord(source_record_id=f"market-{slug}", payload={
                    "record_type": "market_signal", "country": country,
                    "metric": "addressable_market_value", "value": 125_000_000,
                    "currency": "EUR", "period": "2025", "sector_ids": sectors,
                    "provenance_url": f"https://data.example.test/markets/{slug}",
                }),
                RawRecord(source_record_id=f"buyer-{slug}-1", payload={
                    "record_type": "organization", "display_name": f"Atlas {country} Distribution",
                    "legal_name": f"Atlas {country} Distribution Ltd", "country": country,
                    "domain": f"atlas-{slug}.example.test", "registry_id": f"{country}-ATLAS-001",
                    "buyer_types": ["importer", "distributor"], "sector_ids": sectors,
                    "buying_intent": "active sourcing brief", "locations": 7,
                    "provenance_url": f"https://registry.example.test/{country}/ATLAS-001",
                }),
                RawRecord(source_record_id=f"buyer-{slug}-2", payload={
                    "record_type": "organization", "display_name": f"Northstar {country} Retail",
                    "legal_name": f"Northstar {country} Retail SA", "country": country,
                    "domain": f"northstar-{slug}.example.test", "registry_id": f"{country}-NORTH-002",
                    "buyer_types": ["retailer", "wholesaler"], "sector_ids": sectors,
                    "store_count": 24, "brands_carried": 18,
                    "provenance_url": f"https://registry.example.test/{country}/NORTH-002",
                }),
            ])
        snapshot_seed = f"{query.campaign_id}:{','.join(query.target_countries)}:{','.join(sectors)}"
        snapshot_id = f"snap_{hashlib.sha256(snapshot_seed.encode()).hexdigest()[:20]}"
        return RawPage(
            snapshot=SnapshotRef(snapshot_id=snapshot_id, source_id=self.definition.source_id),
            records=records[:query.max_records], source_reported_total=len(records), next_cursor=None,
        )

    def normalize(self, record: RawRecord, snapshot: SnapshotRef) -> list[EvidenceEnvelope]:
        raw = json.dumps(record.payload, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        return [EvidenceEnvelope(
            evidence_id=f"ev_{digest[:20]}", source_id=self.definition.source_id,
            source_record_id=record.source_record_id, snapshot_id=snapshot.snapshot_id,
            record_type=record.payload["record_type"], observed_at=datetime.now(timezone.utc),
            jurisdiction=record.payload.get("country"), sector_ids=record.payload.get("sector_ids", []),
            provenance_url=record.payload.get("provenance_url"), raw_hash=digest,
            method="observed", confidence=.92, payload=record.payload,
        )]

    def checkpoint(self, page: RawPage) -> str | None:
        return page.next_cursor

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="Offline deterministic contract fake")

    def verify(self, query, candidate) -> VerificationBundle:
        del query
        # The role this company turns out to have, not a fixed one. A real
        # verifier reads it off the page it fetched about this candidate, so
        # stamping every candidate "distributor" made the fake contradict any
        # corpus row that said otherwise — and eligibility reads observed roles.
        roles = [str(value) for value in candidate.data.get("buyer_types") or []] or ["distributor"]
        role_phrase = " and ".join(roles)
        # Three product ranges and a website on both pages: what a directory
        # entry for a real distributor looks like, and what it takes to clear
        # the strong-fit floor. A single term on a single page is a mention, and
        # the scoring model is built to say so.
        terms = ["household-appliances", "built-in ovens", "white goods"]
        term_phrase = ", ".join(terms)
        official_markdown = (
            f"{candidate.company_name} is a {role_phrase} of {term_phrase} "
            f"in {candidate.country}. Website: {candidate.domain}."
        )
        independent_markdown = (
            f"Registry profile for {candidate.company_name}, a {term_phrase} "
            f"{role_phrase}. Website: {candidate.domain}."
        )
        official_facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": roles,
            "product_term": terms,
        }
        independent_facts = {
            "company_name": [candidate.company_name],
            "buyer_role": roles,
            "product_term": terms,
        }
        if candidate.domain:
            official_facts["domain"] = [candidate.domain]
            independent_facts["domain"] = [candidate.domain]
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                VerificationSource(
                    provenance_url=f"https://{candidate.domain}",
                    raw_hash=hashlib.sha256(official_markdown.encode()).hexdigest(),
                    classification="official",
                    retrieved_via=f"https://{candidate.domain}",
                    facts=official_facts,
                    snapshot_content=official_markdown,
                    fact_spans=spans_for_facts(official_markdown, official_facts),
                ),
                VerificationSource(
                    provenance_url=f"https://registry.example.test/{candidate.source_record_id}",
                    raw_hash=hashlib.sha256(independent_markdown.encode()).hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example.test",
                    facts=independent_facts,
                    snapshot_content=independent_markdown,
                    fact_spans=spans_for_facts(independent_markdown, independent_facts),
                ),
            ],
            independent_source_count=1,
        )


def deterministic_provider(definition: DatasetDefinition) -> DeterministicProvider:
    return DeterministicProvider(definition)


def contract_definition() -> DatasetDefinition:
    """Public verifier contract used by cross-tenant release tests."""
    return fixture_definition().model_copy(update={
        "display_name": "Lead research contract source",
        "emits": ["company_name", "country", "domain", "buyer_role", "product_term"],
    })


class ContractProvider(DeterministicProvider):
    """Verifier with intentionally unequal scoring dimensions.

    Product evidence has breadth while buyer-role evidence is narrow. Two
    tenants assigning those dimensions different weights must therefore reach
    different scores even though they share the same public facts.
    """

    def verify(self, query, candidate) -> VerificationBundle:
        del query
        if candidate.data.get("contract_source_failure"):
            raise RuntimeError("deterministic contract source failure")
        if candidate.data.get("contract_abstain"):
            return VerificationBundle(
                candidate_source_record_id=candidate.source_record_id,
                sources=[],
                independent_source_count=0,
                requests=1,
            )

        roles = [str(value) for value in candidate.data.get("buyer_types") or []] or [
            "distributor"
        ]
        product_terms = ["industrial valve", "control valve", "process valve"]
        domain = candidate.domain or f"{candidate.source_record_id}.example.test"
        role_text = " and ".join(roles)
        product_text = ", ".join(product_terms)
        official_text = (
            f"{candidate.company_name} is a {role_text} in {candidate.country}. "
            f"Its range includes {product_text}. Website: {domain}."
        )
        registry_text = (
            f"Registry profile for {candidate.company_name}: {role_text}; "
            f"industrial valve supplier in {candidate.country}."
        )
        official_facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "domain": [domain],
            "buyer_role": roles,
            "product_term": product_terms,
        }
        registry_facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": roles,
            "product_term": ["industrial valve"],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                cited_source(
                    provenance_url=f"https://{domain}",
                    classification="official",
                    retrieved_via=f"https://{domain}",
                    facts=official_facts,
                    content=official_text,
                ),
                cited_source(
                    provenance_url=(
                        "https://registry.example.test/"
                        f"{candidate.source_record_id}"
                    ),
                    classification="independent",
                    retrieved_via="https://search.example.test",
                    facts=registry_facts,
                    content=registry_text,
                ),
            ],
            independent_source_count=1,
            requests=2,
        )


class ContractRunExecutor(StubRunExecutor):
    """Credential-free agentic gap result with exact, accepted page spans."""

    def execute(self, service, run: dict) -> dict[str, Any]:
        if run["run_type"] != "lead_research_gap":
            return super().execute(service, run)
        payload = run["payload"]
        company = payload["company_name"]
        content = f"{company} employs 450 people and serves 12 countries."
        employee_literal = "450 people"
        countries_literal = "12 countries"

        def fact(field: str, value: str, literal: str) -> ProposedFact:
            start = content.index(literal)
            return ProposedFact(
                field=field,
                value_en=value,
                original_text=literal,
                source_language="en",
                derivation_kind="observed",
                confidence=.9,
                validation_basis="deterministic contract extraction",
                page_id="contract-about",
                span=EvidenceSpan(
                    original=literal,
                    start=start,
                    end=start + len(literal),
                ),
                period="2026",
                observed_at=1_787_520_000.0,
            )

        page = ResearchPage(
            page_id="contract-about",
            source_id="agentic-web",
            canonical_url=f"https://{payload['canonical_domain']}/about",
            snapshot_content=content,
            raw_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_language="en",
            source_class="official",
            visibility="public",
            retrieved_at=datetime.now(timezone.utc),
        )
        result = AgenticResearchResult(
            pages=[page],
            facts=[
                fact("employee_count", employee_literal, employee_literal),
                fact("countries_served", countries_literal, countries_literal),
            ],
            unresolved_fields=[],
            requests_started=1,
            tokens_used=180,
            stop_reason="required_coverage",
        )
        return result.model_dump(mode="json")


@dataclass(frozen=True)
class ContractTenant:
    company_id: str
    headers: dict[str, str]
    profile_version_id: str


@dataclass(frozen=True)
class ContractScenarioOutcome:
    leads: list[dict]
    zero_result_explanation: str | None
    status: str


def _profile(name: str) -> dict:
    slug = name.casefold().replace(" ", "-")
    return {
        "identity": {"name": name, "website": f"https://{slug}.example.test"},
        "seller_countries": ["TR"],
        "products": [{
            "id": f"prd_{slug}",
            "name": "Endüstriyel vana",
            "english_name": "Industrial valve",
            "hs_codes": ["8481"],
            "sector_ids": ["industrial-machinery"],
            "emphasis": 1,
        }],
        "market_preferences": {
            "target_countries": ["DE"],
            "languages": ["de", "en"],
        },
        "hidden_label_ids": ["high_export_readiness"],
        "hidden_label_provenance": {
            "high_export_readiness": "contract test admin policy"
        },
        "playbook_versions": {"industrial-machinery": "1"},
    }


def onboard_two_companies(client, admin_headers: dict[str, str]) -> tuple[ContractTenant, ContractTenant]:
    tenants: list[ContractTenant] = []
    for name in ("Contract Tenant A", "Contract Tenant B"):
        company = client.post(
            "/api/v1/admin/companies", headers=admin_headers, json={"name": name},
        )
        assert company.status_code == 201, company.text
        admin_company_headers = {
            **admin_headers,
            "X-Company-ID": company.json()["id"],
        }
        profile = client.put(
            "/api/v1/company/research-profile",
            headers=admin_company_headers,
            json=_profile(name),
        )
        assert profile.status_code == 200, profile.text
        slug = name.casefold().replace(" ", "-")
        email = f"researcher@{slug}.example.test"
        password = "contract-test-password"
        user = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": email,
                "password": password,
                "role": "customer",
                "company_id": company.json()["id"],
            },
        )
        assert user.status_code == 201, user.text
        login = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password},
        )
        assert login.status_code == 200, login.text
        headers = {
            "Authorization": f"Bearer {login.json()['access_token']}",
            "X-Company-ID": company.json()["id"],
        }
        tenants.append(ContractTenant(
            company_id=company.json()["id"],
            headers=headers,
            profile_version_id=profile.json()["id"],
        ))
    return tenants[0], tenants[1]


def create_and_run_campaign(
    app,
    client,
    tenant: ContractTenant,
    *,
    product_terms: list[str],
    countries: list[str] | None = None,
    weights: dict[str, int] | None = None,
    enrichment: bool = True,
) -> dict:
    scoring = {"weights": weights} if weights else {}
    response = client.post(
        "/api/v1/research-campaigns",
        headers=tenant.headers,
        json={
            "name": "Contract valve buyers",
            "target_countries": countries or ["DE"],
            "product_terms": product_terms,
            "sector_ids": ["industrial-machinery"],
            "buyer_types": ["distributor"],
            "enabled_source_ids": ["fixture-directory"],
            "scoring": scoring,
            "enrichment": {
                "enabled": enrichment,
                "model_profile": "contract-decision" if enrichment else None,
                "extractor_model_profile": "contract-extractor" if enrichment else None,
                "max_pages_per_company": 2,
                "max_seconds_per_company": 10,
                "max_tokens": 500,
            },
        },
    )
    assert response.status_code == 201, response.text
    campaign = response.json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start",
        headers=tenant.headers,
    )
    assert started.status_code == 202, started.text
    settled = app.state.lead_research.wait_until_settled(
        tenant.company_id, campaign["id"], timeout=30,
    )
    assert settled is not None, "contract campaign did not settle"
    return {**campaign, "settled": settled}


def wait_for_results(app, client, tenant: ContractTenant, campaign_id: str) -> list[dict]:
    del app
    response = client.get(
        f"/api/v1/research-campaigns/{campaign_id}/results",
        headers=tenant.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def contract_scenario(
    client,
    tenant: ContractTenant,
    outcome: dict,
) -> ContractScenarioOutcome:
    """Normalize a blocked-readiness or settled-run outcome for assertions."""
    detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else {}
    return ContractScenarioOutcome(
        leads=client.get("/api/v1/leads", headers=tenant.headers).json(),
        zero_result_explanation=(
            outcome.get("zero_result_explanation")
            or detail.get("zero_result_explanation")
        ),
        status=str(outcome.get("status") or "blocked"),
    )
