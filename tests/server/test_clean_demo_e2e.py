"""Release gate for a genuinely empty tenant's first research run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import Database
from server.lead_research.candidates import CandidateRepository
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from server.provisioning import provision_demo_account
from tests.server.lead_research.fakes import deterministic_provider, fixture_definition
from tests.server.test_api_mvp import TEST_CREDENTIAL_KEY


TEST_EMAIL = "demo-release@example.test"
TEST_PASSWORD = "test-only-clean-demo-password"


@pytest.fixture()
def fake_verifier() -> ProviderRegistry:
    definition = fixture_definition()
    provider = deterministic_provider(definition)
    return ProviderRegistry([definition], {definition.source_id: provider})


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


def make_clean_demo(tmp_path: Path, fake_verifier: ProviderRegistry):
    db = Database(tmp_path / "clean-demo.db")
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
        database_path=tmp_path / "clean-demo.db",
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
    assert started.json()["status"] == "succeeded"

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
