"""Conservative compatibility backfill for pre-contract tenant data."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import Database, json_dump, now
from server.lead_research.backfill import backfill_contract


KEY = "KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4="


def _legacy_db(tmp_path) -> Database:
    db = Database(tmp_path / "legacy.db")
    stamp = now() - 10_000
    for suffix in ("a", "b"):
        company_id, user_id = f"cmp_{suffix}", f"usr_{suffix}"
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (company_id, f"Legacy {suffix.upper()}", "active", json_dump({"country": "TR"}), stamp, stamp),
        )
        db.execute(
            "INSERT INTO users(id,email,role,company_id,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, f"legacy-{suffix}@example.test", "customer", company_id, "active", "{}", stamp, stamp),
        )
        profile = {
            "name": f"Legacy {suffix.upper()}",
            "website": f"https://legacy-{suffix}.example",
            "seller_countries": ["TR"],
        }
        db.execute(
            "INSERT INTO company_sections(company_id,section,data,updated_at) VALUES(?,?,?,?)",
            (company_id, "profile", json_dump(profile), stamp),
        )
        db.execute(
            "INSERT INTO company_sections(company_id,section,data,updated_at) VALUES(?,?,?,?)",
            (company_id, "market_preferences", json_dump({"target_countries": ["DE"]}), stamp),
        )
        db.execute(
            "INSERT INTO products(id,company_id,name,normalized_name,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"prd_{suffix}", company_id, "Industrial valve", "industrial valve",
             json_dump({"english_name": "Industrial valve", "hs_codes": ["8481"]}), stamp, stamp),
        )
        db.execute(
            "INSERT INTO organizations("
            "id,company_id,display_name,normalized_name,domain,country,data,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (f"org_{suffix}", company_id, f"Buyer {suffix.upper()}", f"buyer {suffix}",
             f"buyer-{suffix}.example", "DE", "{}", stamp, stamp),
        )
        db.execute(
            "INSERT INTO research_campaigns(id,company_id,name,status,config,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"rc_{suffix}", company_id, "Legacy campaign", "succeeded",
             json_dump({"name": "Legacy campaign", "target_countries": ["DE"]}), stamp, stamp),
        )
        db.execute(
            "INSERT INTO feature_claims("
            "id,company_id,campaign_id,organization_id,field,status,value,confidence,method,"
            "evidence_ids,data,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"claim_{suffix}", company_id, f"rc_{suffix}", f"org_{suffix}",
             "buyer_role", "observed", json_dump("distributor"), .8, "legacy",
             json_dump([f"ev_{suffix}"]), "{}", stamp),
        )
        legacy_result = {"reasons": ["legacy decision"], "score_inputs": {"buyer_role": 80}}
        db.execute(
            "INSERT INTO research_results("
            "id,company_id,campaign_id,organization_id,verdict,fit_score,evidence_confidence,data,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"result_{suffix}", company_id, f"rc_{suffix}", f"org_{suffix}",
             "review", 64, .55, json_dump(legacy_result), stamp, stamp),
        )
        db.execute(
            "INSERT INTO leads(id,company_id,company_name,country,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"lead_{suffix}", company_id, f"Buyer {suffix.upper()}", "DE", stamp, stamp),
        )
        db.execute(
            "INSERT INTO contacts(id,company_id,lead_id,email,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"contact_{suffix}", company_id, f"lead_{suffix}", f"buyer@buyer-{suffix}.example",
             "active", "{}", stamp, stamp),
        )
    return db


def test_backfill_is_idempotent_and_never_promotes_legacy_claims_without_validation(tmp_path):
    db = _legacy_db(tmp_path)

    first = backfill_contract(db)
    second = backfill_contract(db)

    assert first.profile_versions_created == 2
    assert first.tenant_facts_created == 2
    assert first.results_snapshotted == 2
    assert first.contacts_classified == 2
    assert second.total_changes == 0
    assert db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM tenant_facts")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM feature_claims")["n"] == 2
    for company_id in ("cmp_a", "cmp_b"):
        row = db.one("SELECT * FROM tenant_facts WHERE company_id=?", (company_id,))
        assert row["visibility"] == "private"
        assert row["mechanically_validated"] == 0
        assert row["source_class"] == "legacy"


def test_backfill_preserves_result_payload_and_tenant_boundaries(tmp_path):
    db = _legacy_db(tmp_path)
    before = {
        row["id"]: row["data"] for row in db.all("SELECT id,data FROM research_results ORDER BY id")
    }

    backfill_contract(db)

    after = db.all("SELECT * FROM research_results ORDER BY id")
    assert {row["id"]: row["data"] for row in after} == before
    assert all(row["profile_version_id"] for row in after)
    assert all(row["snapshot_json"] for row in after)
    snapshots = db.all("SELECT * FROM research_score_snapshots ORDER BY company_id")
    assert len(snapshots) == 2
    assert snapshots[0]["company_id"] != snapshots[1]["company_id"]
    assert db.one(
        "SELECT COUNT(*) AS n FROM tenant_facts WHERE company_id='cmp_a' AND organization_id='org_b'"
    )["n"] == 0


def test_backfilled_campaign_and_contact_routes_remain_readable(tmp_path):
    db = _legacy_db(tmp_path)
    backfill_contract(db)
    settings = Settings(
        database_path=tmp_path / "legacy.db", upload_dir=tmp_path / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=KEY,
    )
    app = create_app(settings, db=db, run_executor=StubRunExecutor())
    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-Company-ID": "cmp_a",
    }

    campaign = client.get("/api/v1/research-campaigns/rc_a", headers=headers)
    contact = client.get("/api/v1/contacts/contact_a", headers=headers)

    assert campaign.status_code == 200, campaign.text
    assert campaign.json()["profile_version_id"]
    assert contact.status_code == 200, contact.text
    assert contact.json()["verification_tier"] == "red"
    assert contact.json()["verification_method"] == "legacy_unverified"
    assert client.get("/api/v1/research-campaigns/rc_b", headers=headers).status_code == 404
