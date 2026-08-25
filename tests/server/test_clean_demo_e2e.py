"""Release gate for a genuinely empty tenant's first research run.

The success proof here is the manifest-bearing corpus path, not a two-row
deterministic verifier. That matters because the deployed system's evidence
comes from a curated customer list with no public pages: a gate that only ever
passed on synthetic web citations proved nothing about the run an operator
actually makes.

The fixture is five markets of fictional companies. Twenty can clear the
strong-fit floor, which is more than the list holds, so the gate exercises the
cap and the country balance rather than asserting that everything found is
shown.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts.ci import interfaze_clean_demo_smoke as smoke
from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import Database, json_load
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import ProviderHealth, VerificationBundle
from server.lead_research.providers.corpus import CorpusProvider, corpus_definition
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from server.provisioning import provision_demo_account
from tests.server.lead_research.fakes import (
    cited_source, deterministic_provider, fixture_definition,
)
from tests.server.test_api_mvp import TEST_CREDENTIAL_KEY


TEST_EMAIL = "demo-release@example.test"
TEST_PASSWORD = "test-only-clean-demo-password"


@pytest.fixture()
def fake_verifier() -> ProviderRegistry:
    definition = fixture_definition()
    provider = deterministic_provider(definition)
    return ProviderRegistry([definition], {definition.source_id: provider})


class LifecycleFixture:
    """Speaks only about the two legacy rows the curated manifest does not cover.

    A curated buyer list cannot state that a company has closed or that it only
    manufactures, so the fixture that can is a separate source — which is also
    how the deployed system is shaped. It abstains on everything else so the
    curated rows' scores stay the corpus provider's own.
    """

    SELLER_ONLY = "legacy-seller_only-1"
    CLOSED = "legacy-closed-2"

    def __init__(self, definition):
        self.definition = definition

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        record = candidate.source_record_id
        if record not in {self.SELLER_ONLY, self.CLOSED}:
            return VerificationBundle(candidate_source_record_id=record)
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": ["manufacturer"],
            "product_term": ["household-appliances"],
        }
        if record == self.CLOSED:
            facts["lifecycle_status"] = ["closed"]
        return VerificationBundle(
            candidate_source_record_id=record,
            sources=[cited_source(
                provenance_url=f"https://registry.example.test/{record}",
                classification="independent",
                retrieved_via="https://search.example.test",
                facts=facts,
            )],
            independent_source_count=1,
            requests=1,
        )


@pytest.fixture()
def acceptance_registry() -> ProviderRegistry:
    """The real corpus verifier plus the one fixture the legacy rows need."""
    corpus = corpus_definition().model_copy(update={"default_enabled": True})
    lifecycle = fixture_definition().model_copy(update={
        "source_id": "legacy-lifecycle",
        "display_name": "Legacy lifecycle fixture",
        "emits": ["company_name", "country", "buyer_role", "product_term"],
    })
    provider = CorpusProvider()
    provider.definition = corpus
    return ProviderRegistry(
        [corpus, lifecycle],
        {corpus.source_id: provider, lifecycle.source_id: LifecycleFixture(lifecycle)},
    )


@pytest.fixture()
def product_catalog() -> bytes:
    return b"product_name,category,aliases\nBuilt-in oven,Ovens,oven;electric oven\n"


@pytest.fixture()
def candidate_csv() -> bytes:
    return (
        b"source_record_id,company_name,country,domain,categories,buyer_types\n"
        b"de-qualified,Atlas Appliances,DE,https://atlas.example.test,"
        b"household-appliances,distributor\n"
        b"de-rejected,Factory Direct,DE,https://factory.example.test,"
        b"household-appliances,manufacturer\n"
    )


def make_clean_demo(
    tmp_path: Path,
    fake_verifier: ProviderRegistry,
    *,
    target_countries: tuple[str, ...] = ("DE",),
    db_name: str = "clean-demo.db",
):
    db = Database(tmp_path / db_name)
    provisioned = provision_demo_account(
        db,
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
        company_profile={"name": "Release Gate Company", "website": "https://example.test"},
        onboarding_sources=[{
            "url": "https://example.test/about",
            "retrieved_at": 1.0,
        }],
    )
    settings = Settings(
        database_path=tmp_path / db_name,
        upload_dir=tmp_path / "uploads",
        credential_key=TEST_CREDENTIAL_KEY,
        chat_enabled=False,
    )
    app = create_app(settings, db=db, run_executor=StubRunExecutor())
    app.state.lead_research = LeadResearchService(db, registry=fake_verifier)
    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD,
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Provisioning creates the account; the user still explicitly confirms the
    # versioned research inputs before any campaign can exist.
    profile = client.put(
        "/api/v1/company/research-profile",
        headers=headers,
        json={
            "identity": {
                "name": "Release Gate Company",
                "website": "https://example.test",
            },
            "seller_countries": ["TR"],
            "products": [{
                "id": "prd_release_appliance",
                "name": "Ankastre fırın",
                "english_name": "Built-in oven",
                "hs_codes": ["8516"],
                "sector_ids": ["household-appliances"],
                "emphasis": 1,
            }],
            "market_preferences": {
                "target_countries": list(target_countries),
                "languages": ["de", "en"],
            },
            "playbook_versions": {"household-appliances": "1"},
        },
    )
    assert profile.status_code == 200, profile.text
    return db, client, headers, provisioned["company_id"]


def test_clean_demo_first_research_run(
    tmp_path: Path,
    fake_verifier: ProviderRegistry,
    product_catalog: bytes,
    candidate_csv: bytes,
):
    db, client, headers, company_id = make_clean_demo(tmp_path, fake_verifier)

    assert client.get("/api/v1/products", headers=headers).json() == []
    assert client.get("/api/v1/leads", headers=headers).json() == []
    assert client.get("/api/v1/contacts", headers=headers).json() == []
    assert client.get("/api/v1/lead-map/selected-countries", headers=headers).json() == []
    assert client.get("/api/v1/research-campaigns", headers=headers).json() == []
    assert client.get("/api/v1/outreach/campaigns", headers=headers).json() == []

    products = client.post(
        "/api/v1/products/import",
        headers=headers,
        files={"file": ("catalog.csv", product_catalog, "text/csv")},
    )
    assert products.status_code == 201, products.text
    assert products.json()["imported"] == 1

    imported = CandidateRepository(db).import_file(
        "kitchen-appliances", "test-v1", "candidates.csv", candidate_csv,
    )
    assert imported.record_count == 2
    # The shared backend corpus is not customer data until research verifies it.
    assert client.get("/api/v1/leads", headers=headers).json() == []
    assert client.get("/api/v1/contacts", headers=headers).json() == []
    assert client.get("/api/v1/lead-map/selected-countries", headers=headers).json() == []
    assert client.get("/api/v1/research", headers=headers).json() == []
    assert client.get("/api/v1/research-campaigns", headers=headers).json() == []
    assert client.get("/api/v1/outreach/campaigns", headers=headers).json() == []

    created = client.post("/api/v1/research-campaigns", headers=headers, json={
        "name": "Germany appliance buyers",
        "seller_countries": ["TR"],
        "target_countries": ["DE"],
        "sector_ids": ["household-appliances"],
        "buyer_types": ["distributor"],
        "enabled_source_ids": ["fixture-directory"],
    })
    assert created.status_code == 201, created.text
    campaign = created.json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"
    # `/start` queues; a real client polls. Wait the way one would.
    settled = client.app.state.lead_research.wait_until_settled(
        company_id, campaign["id"], timeout=60
    )
    assert settled is not None and settled["status"] == "succeeded", settled
    assert settled["zero_result_explanation"] is None

    active = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results", headers=headers,
    ).json()
    rejected = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results?view=rejected", headers=headers,
    ).json()
    assert active and rejected
    assert {row["verdict"] for row in active} <= {"strong_fit", "review"}
    assert {row["verdict"] for row in rejected} == {"reject"}
    assert not ({row["id"] for row in active} & {row["id"] for row in rejected})
    assert all(row["source_ids"] for row in active)
    for row in active:
        assert {
            "profile_version_id", "scope", "priority_band", "known_weight",
            "unknown_weight", "unknown_dimensions", "not_applicable_dimensions",
            "evidence",
        } <= row.keys()
        assert row["known_weight"] + row["unknown_weight"] + sum(
            row["not_applicable_dimensions"].values()
        ) == 100
        assert row["evidence"]
        claims = client.get(
            f"/api/v1/research/results/{row['id']}/claims", headers=headers,
        ).json()
        evidence = [item for claim in claims for item in claim["evidence"]]
        assert evidence
        assert all(item["provenance_url"].startswith("https://") for item in evidence)
        assert all(item["snapshot_id"] and item["raw_hash"] for item in evidence)

    assert db.one(
        "SELECT COUNT(*) AS n FROM products WHERE company_id=?", (company_id,),
    )["n"] == 1


def test_operational_smoke_cli_exposes_secret_file_contract():
    script = Path("scripts/ci/interfaze_clean_demo_smoke.py")
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--base-url" in completed.stdout
    assert "--email" in completed.stdout
    assert "--password-file" in completed.stdout
    assert "--mode" in completed.stdout
    assert "--confirm-disposable-tenant" in completed.stdout
    parsed = smoke.parser().parse_args([
        "--base-url", "https://example.test",
        "--email", TEST_EMAIL,
        "--password-file", "password-file",
    ])
    assert parsed.mode == "read-only"
    assert parsed.confirm_disposable_tenant is None


class InProcessApiClient:
    """Use the real FastAPI app while replacing only HTTP socket transport."""

    def __init__(self, client: TestClient, token: str | None = None):
        self.client = client
        self.token = token

    def request(self, method, path, *, payload=None, body=None, content_type="application/json"):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = content_type
        response = self.client.request(
            method,
            path,
            headers=headers,
            json=payload,
            content=body,
        )
        return response.status_code, response.json() if response.content else None


def _smoke_args(tmp_path: Path, *, mode: str, confirmation: str | None = None):
    password_file = tmp_path / "smoke-password"
    password_file.write_text(TEST_PASSWORD + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    return SimpleNamespace(
        base_url="http://in-process.test",
        email=TEST_EMAIL,
        password_file=password_file,
        source_id="fixture-directory",
        country="DE",
        mode=mode,
        confirm_disposable_tenant=confirmation,
    )


def _prepare_smoke(
    tmp_path: Path,
    fake_verifier: ProviderRegistry,
    candidate_csv: bytes,
) -> tuple[TestClient, dict[str, str]]:
    db, client, headers, _ = make_clean_demo(tmp_path, fake_verifier)
    CandidateRepository(db).import_file(
        "kitchen-appliances", "smoke-v1", "candidates.csv", candidate_csv,
    )
    return client, headers


def test_operational_smoke_defaults_to_read_only(
    tmp_path: Path,
    monkeypatch,
    fake_verifier: ProviderRegistry,
    candidate_csv: bytes,
):
    client, headers = _prepare_smoke(tmp_path, fake_verifier, candidate_csv)
    monkeypatch.setattr(smoke, "ApiClient", lambda _base_url, token=None: InProcessApiClient(client, token))

    smoke.run(_smoke_args(tmp_path, mode="read-only"))

    assert client.get("/api/v1/products", headers=headers).json() == []
    assert client.get("/api/v1/research-campaigns", headers=headers).json() == []


def test_operational_smoke_rejects_mutation_without_matching_disposable_confirmation(
    tmp_path: Path,
    monkeypatch,
    fake_verifier: ProviderRegistry,
    candidate_csv: bytes,
):
    client, headers = _prepare_smoke(tmp_path, fake_verifier, candidate_csv)
    monkeypatch.setattr(smoke, "ApiClient", lambda _base_url, token=None: InProcessApiClient(client, token))

    with pytest.raises(smoke.SmokeFailure, match="disposable tenant"):
        smoke.run(_smoke_args(tmp_path, mode="full", confirmation="another-smoke@example.test"))
    assert client.get("/api/v1/products", headers=headers).json() == []
    assert client.get("/api/v1/research-campaigns", headers=headers).json() == []


def test_operational_smoke_never_mutates_the_real_silverline_demo(
    tmp_path: Path,
    monkeypatch,
    fake_verifier: ProviderRegistry,
    candidate_csv: bytes,
):
    client, headers = _prepare_smoke(tmp_path, fake_verifier, candidate_csv)
    monkeypatch.setattr(smoke, "ApiClient", lambda _base_url, token=None: InProcessApiClient(client, token))
    args = _smoke_args(
        tmp_path,
        mode="full",
        confirmation="efe@anexa-arelvia.com",
    )
    args.email = "efe@anexa-arelvia.com"

    with pytest.raises(smoke.SmokeFailure, match="protected demo"):
        smoke.run(args)
    assert client.get("/api/v1/products", headers=headers).json() == []
    assert client.get("/api/v1/research-campaigns", headers=headers).json() == []


def test_operational_smoke_full_mode_runs_real_api_and_evidence_path(
    tmp_path: Path,
    monkeypatch,
    fake_verifier: ProviderRegistry,
    candidate_csv: bytes,
    capsys,
):
    client, headers = _prepare_smoke(tmp_path, fake_verifier, candidate_csv)
    monkeypatch.setattr(smoke, "ApiClient", lambda _base_url, token=None: InProcessApiClient(client, token))

    smoke.run(_smoke_args(tmp_path, mode="full", confirmation=TEST_EMAIL))

    assert "clean demo smoke passed" in capsys.readouterr().out
    campaigns = client.get("/api/v1/research-campaigns", headers=headers).json()
    assert len(campaigns) == 1
    active = client.get(
        f"/api/v1/research-campaigns/{campaigns[0]['id']}/results", headers=headers,
    ).json()
    rejected = client.get(
        f"/api/v1/research-campaigns/{campaigns[0]['id']}/results?view=rejected",
        headers=headers,
    ).json()
    assert active and rejected


def test_clean_demo_balanced_primary_list(
    tmp_path: Path,
    acceptance_registry: ProviderRegistry,
):
    """The release gate for the contract this change ships.

    Five markets, twenty candidates that can clear the floor, and a list that
    holds fifteen. What is asserted is the set of relationships a customer
    depends on: the list never exceeds its limit, it never takes a fourth from
    one market while another is short, reviews are visible and unmaterialized,
    the metrics agree with the rows, and the run left a durable trail.
    """
    curated, legacy = smoke.load_acceptance_corpus()
    db, client, headers, company_id = make_clean_demo(
        tmp_path, acceptance_registry,
        target_countries=smoke.ACCEPTANCE_MARKETS,
        db_name="balanced-demo.db",
    )
    repository = CandidateRepository(db)
    assert repository.import_file(
        "acceptance-appliance-buyers", "1", "curated.jsonl", curated,
        assertion_manifest=smoke.ACCEPTANCE_MANIFEST,
    ).record_count == 22
    # No manifest: these rows stay candidate supply, and only the lifecycle
    # source can say anything about them.
    assert repository.import_file(
        "acceptance-legacy-list", "1", "legacy.jsonl", legacy,
    ).record_count == 3
    assert client.get("/api/v1/leads", headers=headers).json() == []

    created = client.post("/api/v1/research-campaigns", headers=headers, json={
        "name": "Five-market appliance buyers",
        "seller_countries": ["TR"],
        "target_countries": list(smoke.ACCEPTANCE_MARKETS),
        "sector_ids": ["household-appliances"],
        "buyer_types": ["importer", "distributor", "retailer", "wholesaler"],
        "enabled_source_ids": ["customer-list-corpus", "legacy-lifecycle"],
        # The policy the deployed Demo campaign actually carries. It matters
        # here because curated-corpus evidence has no domain: a gate that
        # counted domains rejected every one of these candidates for lacking a
        # source it does have, and the run reported 42 researched with 0
        # eligible.
        "eligibility": {
            "require_resolved_identity": True,
            "require_official_domain": False,
            "require_target_presence": True,
            "require_buyer_role": True,
            "exclude_inactive": True,
            "minimum_independent_sources": 1,
        },
    })
    assert created.status_code == 201, created.text
    campaign = created.json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )
    assert started.status_code == 202, started.text
    settled = client.app.state.lead_research.wait_until_settled(
        company_id, campaign["id"], timeout=120,
    )
    assert settled is not None, "the campaign never reached a terminal state"
    assert settled["status"] in {"succeeded", "partial"}, settled

    def view(name):
        response = client.get(
            f"/api/v1/research-campaigns/{campaign['id']}/results?view={name}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    primary_results = view("active")
    review_results = view("review")
    overflow_results = view("outside_limit")
    rejected_results = view("rejected")
    metrics = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/metrics", headers=headers,
    ).json()[0]

    assert 5 <= metrics["qualified_leads"] <= 15
    assert metrics["qualified_leads"] == len(primary_results)
    assert metrics["strong_fit_pool"] >= len(primary_results)
    assert len({row["country"] for row in primary_results}) == 5
    assert max(Counter(row["country"] for row in primary_results).values()) <= 3
    assert all(row["evidence"] for row in primary_results)
    assert all(row["selection"]["displayed"] for row in primary_results)
    assert all(row["lead_id"] is None for row in review_results)
    assert all(row["lead_id"] is None for row in overflow_results)

    # The same relationships the operational smoke run holds a live deployment
    # to, so the gate and the tool cannot drift apart.
    smoke.assert_balanced_primary_list(
        active=primary_results, review=review_results,
        overflow=overflow_results, metrics=metrics,
    )

    # Evidence is an immutable dataset reference, not an invented URL.
    citations = [item for row in primary_results for item in row["evidence"]]
    assert citations
    assert any(item["source_reference"] for item in citations)
    assert all(
        item["provenance_url"] is None
        for item in citations if item["source_reference"]
    )
    assert all(
        item["publisher_label"] == "Acceptance appliance buyer list"
        for item in citations if item["source_reference"]
    )

    # The exclusion cases, each for its own named reason.
    rejected_names = {row["company_name"] for row in rejected_results}
    assert "Zaklad Dawny Sp z oo" in rejected_names, "a closed company is rejected"
    assert "Fabrica Solano SA" in rejected_names, "a seller-only company is rejected"
    assert "Anonim Vechi SRL" not in {
        row["company_name"] for row in
        primary_results + review_results + overflow_results + rejected_results
    }, "an unasserted legacy row nothing verified produces no result"
    assert not ({"Sudwerk Industrieventile GmbH", "Forges Ternay SAS"} & {
        row["company_name"] for row in
        primary_results + review_results + overflow_results + rejected_results
    }), "a row whose own product range is out of scope is never researched"

    # /leads is the primary list and nothing else.
    leads = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()
    assert [row["id"] for row in leads] == [row["lead_id"] for row in primary_results]
    assert db.one(
        "SELECT COUNT(*) AS n FROM leads WHERE company_id=?", (company_id,),
    )["n"] == len(primary_results)

    durable_events = [
        dict(row) for row in db.all(
            "SELECT kind,data FROM run_events WHERE run_id=? ORDER BY id",
            (settled["run_id"],),
        )
    ]
    assert durable_events[0]["kind"] == "lead_research_started"
    assert durable_events[-1]["kind"] == "lead_research_completed"
    ranked = next(
        json_load(row["data"], {}) for row in durable_events
        if row["kind"] == "lead_research_ranked"
    )
    assert ranked["qualified_leads"] == metrics["qualified_leads"]
    trail = "\n".join(row["data"] for row in durable_events)
    assert "Nordwind" not in trail and "@" not in trail


def test_the_acceptance_fixture_carries_no_person_and_no_real_domain():
    """A fixture is committed forever. Nothing personal may be in it.

    The real corpus is built from a customer's contact list, and a fixture
    derived from one by hand is exactly where a name or an address survives
    without anybody noticing.
    """
    entries = [
        json.loads(line)
        for line in smoke.FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(entries) == 25
    assert {entry["dataset"] for entry in entries} == {"curated", "legacy"}
    permitted = {
        "source_record_id", "company_name", "country", "categories",
        "buyer_types", "explicit_product_ranges",
    }
    for entry in entries:
        row = entry["row"]
        assert set(row) <= permitted, sorted(set(row) - permitted)
        assert row["country"] in smoke.ACCEPTANCE_MARKETS
    text = smoke.FIXTURE_PATH.read_text(encoding="utf-8")
    for forbidden in ("@", "+", "http", "www.", ".com", ".net", "tel:", "fax"):
        assert forbidden not in text, forbidden
    identities = {(row["row"]["company_name"], row["row"]["country"]) for row in entries}
    assert len(identities) == len(entries), "every fixture company is distinct"
