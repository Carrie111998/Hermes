"""Clean-database release proof for the lead-research product contract.

This module deliberately crosses repository, orchestration, HTTP, evidence,
and tenant boundaries.  Narrow unit tests live under ``lead_research/``; these
tests answer the release question: can two real tenants use the whole feature
without sharing decisions or private inputs?
"""
from __future__ import annotations

import json

import pytest

from server import compliance
from server.db import json_load
from server.lead_research.candidates import CandidateRepository
from server.lead_research.facts import FactRepository
from server.lead_research.labels import LabelRepository
from server.lead_research.metrics import zero_result_explanation
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from tests.server.lead_research.fakes import (
    ContractProvider,
    ContractRunExecutor,
    ContractTenant,
    contract_scenario,
    contract_definition,
    create_and_run_campaign,
    onboard_two_companies,
    wait_for_results,
)
from tests.server.test_api_mvp import make_client


def _weights(product: int, buyer: int) -> dict[str, int]:
    """A campaign that weights what its configured sources can actually reach.

    `market_coverage` and `commercial_scale` stay non-zero — the gap pass has to
    have something to go and look for — but small, because no source here can
    corroborate them and 30 points of weight on evidence nobody can strengthen
    held every candidate below the strong-fit floor on arithmetic rather than on
    anything about the company. `product + buyer` must total 90.
    """
    return {
        "product_sector_fit": product,
        "buyer_channel_fit": buyer,
        "buying_intent": 0,
        "market_coverage": 5,
        "commercial_scale": 5,
        "trade_activity": 0,
        "contactability": 0,
    }


@pytest.fixture()
def contract_app():
    app, client, admin_headers, _bootstrap_company = make_client()
    app.state.lead_research.shutdown()
    definition = contract_definition()
    registry = ProviderRegistry(
        [definition], {definition.source_id: ContractProvider(definition)},
    )
    app.state.runs.executor = ContractRunExecutor()
    app.state.lead_research = LeadResearchService(
        app.state.db,
        registry=registry,
        workers=1,
        verify_workers=1,
        agent_runs=app.state.runs,
    )
    yield app, client, admin_headers
    app.state.lead_research.shutdown()


def _candidate(
    source_record_id: str,
    company_name: str,
    domain: str,
    *,
    country: str = "DE",
    categories: list[str] | None = None,
    buyer_types: list[str] | None = None,
    **data,
) -> dict:
    return {
        "source_record_id": source_record_id,
        "company_name": company_name,
        "country": country,
        "domain": f"https://{domain}",
        "categories": categories or ["industrial valve"],
        "buyer_types": buyer_types or ["distributor"],
        **data,
    }


def _jsonl(*rows: dict) -> bytes:
    return "\n".join(json.dumps(row) for row in rows).encode()


def _seed_contract_candidates(app, client, a: ContractTenant, b: ContractTenant) -> None:
    CandidateRepository(app.state.db).import_file(
        "contract-public",
        "1",
        "public.jsonl",
        _jsonl(_candidate(
            "public-valve", "Public Valve GmbH", "public-valve.example.test",
        )),
    )
    for tenant, prefix in ((a, "A"), (b, "B")):
        response = client.post(
            "/api/v1/candidate-datasets",
            headers=tenant.headers,
            files={
                "file": (
                    f"{prefix.lower()}-private.jsonl",
                    _jsonl(_candidate(
                        f"{prefix.lower()}-private-valve",
                        f"{prefix} Private Valve GmbH",
                        f"{prefix.lower()}-private-valve.example.test",
                    )),
                    "application/x-ndjson",
                ),
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["visibility"] == "tenant_private"


def assert_contract_result(result: dict) -> None:
    required = {
        "profile_version_id",
        "scope",
        "fit_score",
        "evidence_confidence",
        "known_weight",
        "unknown_weight",
        "unknown_dimensions",
        "not_applicable_dimensions",
        "priority_band",
        "evidence",
    }
    missing = required - result.keys()
    assert not missing, f"research result missing contract fields: {sorted(missing)}"
    total = (
        result["known_weight"]
        + result["unknown_weight"]
        + sum(result["not_applicable_dimensions"].values())
    )
    assert total == 100


def _assert_exact_campaign_spans(app, company_id: str, campaign_id: str) -> None:
    checked = 0
    for row in app.state.db.all(
        "SELECT payload FROM evidence_records WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    ):
        payload = json_load(row["payload"], {})
        content = payload.get("snapshot_content") or ""
        for spans in (payload.get("fact_spans") or {}).values():
            for span in spans:
                assert content[span["start"]:span["end"]] == span["original"]
                checked += 1
    assert checked > 0


def _shared_fact_count(app, domain: str) -> int:
    return int(app.state.db.one(
        "SELECT COUNT(*) AS n FROM shared_facts f "
        "JOIN shared_organizations o ON o.id=f.organization_id WHERE o.domain=?",
        (domain,),
    )["n"])


def _public_result(results: list[dict]) -> dict:
    return next(row for row in results if row["company_name"] == "Public Valve GmbH")


def _campaign_payload(*, countries: list[str] | None = None, scoring: dict | None = None) -> dict:
    return {
        "name": "Zero outcome contract",
        "target_countries": countries or ["DE"],
        "product_terms": ["industrial valve"],
        "sector_ids": ["industrial-machinery"],
        "buyer_types": ["distributor"],
        "enabled_source_ids": ["fixture-directory"],
        "scoring": scoring or {},
        "enrichment": {"enabled": False},
    }


def _create_and_settle(app, client, tenant: ContractTenant, payload: dict) -> tuple[dict, dict]:
    created = client.post(
        "/api/v1/research-campaigns", headers=tenant.headers, json=payload,
    )
    assert created.status_code == 201, created.text
    campaign = created.json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start",
        headers=tenant.headers,
    )
    assert started.status_code == 202, started.text
    settled = app.state.lead_research.wait_until_settled(
        tenant.company_id, campaign["id"], timeout=30,
    )
    assert settled is not None
    return campaign, settled


def test_lead_research_contract_end_to_end(contract_app):
    app, client, admin_headers = contract_app
    a, b = onboard_two_companies(client, admin_headers)
    _seed_contract_candidates(app, client, a, b)

    campaign = create_and_run_campaign(
        app,
        client,
        a,
        product_terms=["industrial valve"],
        weights=_weights(60, 30),
    )
    issues = [
        {**dict(row), "data": json_load(row["data"], {})}
        for row in app.state.db.all(
            "SELECT * FROM research_issues WHERE campaign_id=?",
            (campaign["id"],),
        )
    ]
    assert campaign["settled"]["status"] == "succeeded", json.dumps(
        {"settled": campaign["settled"], "issues": issues},
        indent=2,
        sort_keys=True,
    )
    results = wait_for_results(app, client, a, campaign["id"])

    assert {row["company_name"] for row in results} == {
        "A Private Valve GmbH",
        "Public Valve GmbH",
    }
    for result in results:
        assert_contract_result(result)
        assert result["profile_version_id"] == campaign["profile_version_id"]
        assert result["evidence"]
        serialized = json.dumps(result)
        assert "hidden_label_ids" not in serialized
        assert "high_export_readiness" not in serialized
    _assert_exact_campaign_spans(app, a.company_id, campaign["id"])

    cross_tenant = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results",
        headers=b.headers,
    )
    assert cross_tenant.status_code == 404
    gap_runs = [
        run for run in app.state.runs.list(a.company_id)
        if run["run_type"] == "lead_research_gap"
    ]
    assert gap_runs and {run["status"] for run in gap_runs} == {"succeeded"}

    lead_id = _public_result(results)["lead_id"]
    contacts = []
    for email in (
        "a-primary.person@public-valve.example.test",
        "b-cc.person@public-valve.example.test",
        "yellow.person@public-valve.example.test",
        "info@public-valve.example.test",
        "red.person@public-valve.example.test",
    ):
        response = client.post(
            "/api/v1/contacts",
            headers=a.headers,
            json={
                "lead_id": lead_id,
                "email": email,
                "data": {"full_name": email.split("@", 1)[0]},
            },
        )
        assert response.status_code == 201, response.text
        contacts.append(response.json())
    app.state.db.execute(
        "UPDATE contacts SET verification_tier='yellow' WHERE id=?",
        (contacts[2]["id"],),
    )
    app.state.db.execute(
        "UPDATE contacts SET verification_tier='red' WHERE id=?",
        (contacts[4]["id"],),
    )
    outreach = client.post(
        f"/api/v1/leads/{lead_id}/generate-outreach", headers=a.headers,
    )
    assert outreach.status_code == 202, outreach.text
    assert outreach.json()["payload"]["contact_id"] == contacts[0]["id"]
    assert outreach.json()["payload"]["recipients"]["cc"] == [contacts[1]["email"]]


def test_two_tenants_reuse_public_fact_but_keep_decisions_private(contract_app):
    app, client, admin_headers = contract_app
    a, b = onboard_two_companies(client, admin_headers)
    _seed_contract_candidates(app, client, a, b)

    first = create_and_run_campaign(
        app,
        client,
        a,
        product_terms=["industrial valve"],
        weights=_weights(60, 30),
    )
    first_results = wait_for_results(app, client, a, first["id"])
    first_public = _public_result(first_results)
    shared_after_first = _shared_fact_count(app, "public-valve.example.test")
    assert shared_after_first > 0

    second = create_and_run_campaign(
        app,
        client,
        b,
        product_terms=["industrial valve"],
        # Both tenants weight the reachable dimensions heavily enough to
        # qualify — a review has no primary result to compare — while still
        # splitting product against buyer differently, which is the point.
        weights=_weights(70, 20),
    )
    second_results = wait_for_results(app, client, b, second["id"])
    second_public = _public_result(second_results)
    assert _shared_fact_count(app, "public-valve.example.test") == shared_after_first
    assert first_public["fit_score"] != second_public["fit_score"]

    labels = LabelRepository(app.state.db)
    labels.assign(
        a.company_id,
        first_public["id"],
        "high_export_readiness",
        "high",
        "result",
        "admin",
        "usr_contract_admin",
        "e2e outcome policy",
        first["profile_version_id"],
    )
    compliance.suppress(
        app.state.db,
        a.company_id,
        "buyer@public-valve.example.test",
        "contract opt-out",
    )
    assert labels.history(b.company_id, second_public["id"]) == []
    assert compliance.is_suppressed(
        app.state.db, b.company_id, "buyer@public-valve.example.test",
    ) is False
    assert "high_export_readiness" not in json.dumps(second_public)

    first_snapshot = app.state.db.one(
        "SELECT snapshot_json FROM research_score_snapshots "
        "WHERE result_id=? ORDER BY created_at LIMIT 1",
        (first_public["id"],),
    )["snapshot_json"]
    shared_fact_id = next(
        fact_id
        for fact_id in json_load(first_snapshot, {})["fact_ids"]
        if fact_id.startswith("sf_")
        and first_public["id"] in FactRepository(app.state.db).consumers(fact_id).result_ids
        and second_public["id"] in FactRepository(app.state.db).consumers(fact_id).result_ids
    )
    impact = FactRepository(app.state.db).correct(
        shared_fact_id,
        "corrected public value",
        "usr_contract_admin",
        "verified public correction",
        True,
    )
    assert {first_public["id"], second_public["id"]} <= set(
        impact.recomputed_result_ids
    )
    assert app.state.db.one(
        "SELECT snapshot_json FROM research_score_snapshots "
        "WHERE result_id=? ORDER BY created_at LIMIT 1",
        (first_public["id"],),
    )["snapshot_json"] == first_snapshot


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("no_runnable_source", "no_candidate_source_runnable"),
        ("missing_market_mapping", "product_terms_missing_local_mapping"),
        ("no_named_candidate", "sources_named_no_candidate"),
        ("excluded_range", "candidates_excluded_by_range"),
        ("eligibility_veto", "candidates_failed_eligibility"),
        ("below_threshold", "researched_below_threshold"),
        ("source_failure", "sources_failed"),
        ("cancelled", "campaign_cancelled"),
    ],
)
def test_real_zero_lead_outcome_has_named_explanation(contract_app, scenario, expected):
    app, client, admin_headers = contract_app
    tenant, _other = onboard_two_companies(client, admin_headers)

    if scenario == "no_runnable_source":
        app.state.lead_research.ensure_catalog(tenant.company_id)
        disabled = client.post(
            "/api/v1/data-sources/fixture-directory/disable",
            headers={**admin_headers, "X-Company-ID": tenant.company_id},
        )
        assert disabled.status_code == 200, disabled.text
        blocked = client.post(
            "/api/v1/research-campaigns",
            headers=tenant.headers,
            json=_campaign_payload(),
        )
        assert blocked.status_code == 409, blocked.text
        outcome = blocked.json()
    else:
        if scenario in {"excluded_range", "eligibility_veto", "below_threshold", "source_failure"}:
            row = _candidate(
                f"scenario-{scenario}",
                f"Scenario {scenario} GmbH",
                f"scenario-{scenario}.example.test",
            )
            if scenario == "excluded_range":
                row.update({
                    "categories": ["bakery equipment"],
                    "explicit_product_ranges": ["bakery equipment"],
                })
            elif scenario == "eligibility_veto":
                row["buyer_types"] = ["manufacturer"]
            elif scenario == "source_failure":
                row["contract_source_failure"] = True
            CandidateRepository(app.state.db).import_file(
                f"contract-{scenario}",
                "1",
                f"{scenario}.jsonl",
                _jsonl(row),
            )

        scoring = None
        if scenario == "below_threshold":
            scoring = {
                "bands": {
                    "A": {"min_fit": 100, "min_confidence": 0},
                    "B": {"min_fit": 99, "min_confidence": 0},
                    "C": {"min_fit": 98, "min_confidence": 0},
                },
            }
        # A market whose language no sector playbook covers. AE used to serve
        # here and no longer can: Arabic is mapped now, so the scenario has to
        # name a language we genuinely have no terms for or it stops testing
        # anything. Japanese is the same market the unit test uses.
        countries = ["JP"] if scenario == "missing_market_mapping" else ["DE"]
        if scenario == "cancelled":
            created = client.post(
                "/api/v1/research-campaigns",
                headers=tenant.headers,
                json=_campaign_payload(countries=countries, scoring=scoring),
            )
            assert created.status_code == 201, created.text
            campaign = created.json()
            app.state.db.execute(
                "UPDATE research_campaigns SET status='cancelled' WHERE id=? AND company_id=?",
                (campaign["id"], tenant.company_id),
            )
            outcome = app.state.lead_research.run(tenant.company_id, campaign["id"])
        else:
            _campaign, outcome = _create_and_settle(
                app,
                client,
                tenant,
                _campaign_payload(countries=countries, scoring=scoring),
            )

    normalized = contract_scenario(client, tenant, outcome)
    assert normalized.leads == []
    assert normalized.zero_result_explanation == expected


@pytest.mark.parametrize(
    ("status", "metrics", "failed_sources", "unmapped", "expected"),
    [
        ("cancelled", {}, [], [], "campaign_cancelled"),
        (
            "succeeded",
            {"qualified_leads": 0, "named_candidates": 0},
            [],
            ["DE"],
            "product_terms_missing_local_mapping",
        ),
        (
            "succeeded",
            {"qualified_leads": 0, "named_candidates": 0, "candidate_supply_supplied": 0},
            [],
            [],
            "sources_named_no_candidate",
        ),
        (
            "succeeded",
            {
                "qualified_leads": 0,
                "named_candidates": 0,
                "candidate_supply_supplied": 2,
                "candidate_supply_excluded_by_range": 2,
                "candidate_supply_passed_cheap_gate": 0,
            },
            [],
            [],
            "candidates_excluded_by_range",
        ),
        (
            "succeeded",
            {"qualified_leads": 0, "named_candidates": 2, "eligible_companies": 0},
            [],
            [],
            "candidates_failed_eligibility",
        ),
        (
            "succeeded",
            {"qualified_leads": 0, "named_candidates": 2, "eligible_companies": 2},
            [],
            [],
            "researched_below_threshold",
        ),
        (
            "failed",
            {"qualified_leads": 0, "named_candidates": 2, "eligible_companies": 0},
            ["fixture-directory"],
            [],
            "sources_failed",
        ),
    ],
)
def test_zero_lead_outcome_has_named_explanation(
    status, metrics, failed_sources, unmapped, expected,
):
    assert zero_result_explanation(
        status=status,
        metrics=metrics,
        failed_source_ids=failed_sources,
        unmapped_markets=unmapped,
    ) == expected


def test_nonempty_outcome_has_no_zero_result_explanation():
    assert zero_result_explanation(
        status="succeeded",
        metrics={"qualified_leads": 1},
        failed_source_ids=[],
        unmapped_markets=[],
    ) is None


def test_review_outcome_has_no_zero_result_explanation():
    assert zero_result_explanation(
        status="succeeded",
        metrics={
            "qualified_leads": 0,
            "review_leads": 1,
            "named_candidates": 1,
            "eligible_companies": 1,
        },
        failed_source_ids=[],
        unmapped_markets=[],
    ) is None
