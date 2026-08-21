"""Release gate for a genuinely empty tenant's first research run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts.ci import interfaze_clean_demo_smoke as smoke
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
