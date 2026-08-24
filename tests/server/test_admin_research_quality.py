from __future__ import annotations

from server.db import json_dump, new_id, now
from tests.server.test_api_mvp import make_client


def _customer_headers(client, admin_headers: dict, company_id: str) -> dict:
    email = f"quality-{new_id('user')}@example.test"
    created = client.post(
        "/api/v1/admin/users",
        headers=admin_headers,
        json={
            "email": email,
            "password": "customer-quality-password",
            "role": "customer",
            "company_id": company_id,
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "customer-quality-password"},
    )
    return {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-Company-ID": company_id,
    }


def _seed_quality(app, client, headers, company_id):
    db, stamp = app.state.db, now()
    other = client.post(
        "/api/v1/admin/companies", headers=headers, json={"name": "Reuse customer"},
    ).json()["id"]
    actor_id = db.one("SELECT id FROM users WHERE role='admin' LIMIT 1")["id"]
    profile_id = new_id("profile")
    db.execute(
        "INSERT INTO company_profile_versions(id,company_id,version,status,profile_json,"
        "created_by,confirmed_by,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            profile_id, company_id, 1, "confirmed",
            json_dump({"identity": {"name": "Acme"}, "products": [], "playbook_versions": {}}),
            actor_id, actor_id, stamp, stamp,
        ),
    )
    campaign_id = new_id("rc")
    db.execute(
        "INSERT INTO research_campaigns(id,company_id,name,status,version,config,profile_version_id,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, "Quality run", "partial", 1, "{}", profile_id, stamp, stamp),
    )
    db.execute(
        "INSERT INTO campaign_metrics VALUES(?,?,?,?,?,?)",
        (
            company_id, campaign_id, "overall", "all",
            json_dump({
                "candidate_supply_excluded_by_range": 7,
                "candidate_supply_duplicates_collapsed": 3,
                "provider_requests": 12,
                "reused_bundles": 4,
                "stage_agentic": 2,
                "qualified_leads": 1,
            }),
            stamp,
        ),
    )
    db.execute(
        "INSERT INTO dataset_definitions(company_id,source_id,installed,enabled,definition,health,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (company_id, "bright-data", 1, 1, "{}", "degraded", stamp),
    )
    db.execute(
        "INSERT INTO campaign_partitions(id,company_id,campaign_id,source_id,target_country,status,"
        "metrics,error_category,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            new_id("part"), company_id, campaign_id, "bright-data", "DE", "partial",
            json_dump({"provider_requests": 12, "errors": [{"stage": "verification"}]}),
            "verification_error", stamp,
        ),
    )
    for status, reason in (("succeeded", None), ("empty", "no_result"), ("failed", "timeout")):
        db.execute(
            "INSERT INTO research_search_attempts(id,company_id,shareable,organization_id,field,"
            "query_hash,source_id,status,reason,request_count,attempted_at,retry_after,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("attempt"), company_id, 0, "org_quality", "buyer_role",
                new_id("hash"), "bright-data", status, reason, 1, stamp, stamp + 60, stamp, stamp,
            ),
        )

    shared_org = new_id("sorg")
    shared_evidence = new_id("sev")
    shared_fact = new_id("sf")
    db.execute(
        "INSERT INTO shared_organizations VALUES(?,?,?,?,?,?,?,?)",
        (shared_org, "Buyer", "buyer", "DE", "buyer.example", None, stamp, stamp),
    )
    db.execute(
        "INSERT INTO shared_evidence_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            shared_evidence, "bright-data", "https://buyer.example/about", "a" * 64,
            "official", "public", "de", "Vertriebspartner", 0, 16,
            new_id("content"), stamp, stamp,
        ),
    )
    db.execute(
        "INSERT INTO shared_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            shared_fact, shared_org, "buyer_role", json_dump("distributor"), "b" * 64,
            shared_evidence, "translated", None, None, None, "observed", .9,
            "exact source span", "official", "public", 1, stamp, stamp,
            stamp + 86400, stamp, stamp,
        ),
    )
    db.execute("INSERT INTO shared_fact_evidence VALUES(?,?)", (shared_fact, shared_evidence))
    for consumer in (company_id, other):
        db.execute(
            "INSERT INTO research_fact_consumers VALUES(?,?,?,?)",
            (consumer, shared_fact, stamp, stamp),
        )

    db.execute(
        "INSERT INTO agent_runs(id,company_id,run_type,status,payload,output,cost,created_at,started_at,"
        "completed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id("run"), company_id, "lead_research_gap", "succeeded", "{}",
            json_dump({"pages": [{"url": "https://buyer.example"}], "tokens_used": 1450,
                       "requests_started": 3, "stop_reason": "token_limit"}),
            1.25, stamp, stamp, stamp + 4, stamp + 4,
        ),
    )
    return {"campaign_id": campaign_id, "shared_fact": shared_fact}


def test_quality_endpoint_is_admin_only_and_reports_actionable_warnings():
    app, client, headers, company_id = make_client()
    customer_headers = _customer_headers(client, headers, company_id)
    seeded = _seed_quality(app, client, headers, company_id)

    assert client.get("/api/v1/admin/research/quality", headers=customer_headers).status_code == 403
    response = client.get("/api/v1/admin/research/quality", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert {warning["code"] for warning in payload["warnings"]} >= {
        "thin_profile", "high_fact_reuse", "source_change",
    }
    assert payload["exclusions"]["excluded_by_range"] == 7
    assert payload["candidates"]["collapsed_rows"] == 3
    assert payload["facts"]["reused_facts"] == 1
    assert payload["facts"]["max_consumers"] == 2
    assert payload["costs"]["requests"] >= payload["costs"]["fresh_cache_hits"]
    assert payload["costs"]["tokens"] == 1450
    assert payload["costs"]["cost"] == 1.25
    assert payload["agentic"]["budget_stops"] == 1
    assert payload["sources"][0]["source_id"] == "bright-data"
    assert seeded["shared_fact"] in {warning.get("fact_id") for warning in payload["warnings"]}


def test_quality_report_includes_profile_label_and_correction_history_without_gating_runs():
    app, client, headers, company_id = make_client()
    seeded = _seed_quality(app, client, headers, company_id)
    db, stamp = app.state.db, now()
    db.execute(
        "INSERT INTO research_fact_corrections(id,company_id,fact_id,corrected_value_en,actor_id,"
        "reason,applied,impact,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            new_id("corr"), None, seeded["shared_fact"], json_dump("wholesaler"),
            "admin", "source correction", 0, "{}", stamp,
        ),
    )

    payload = client.get("/api/v1/admin/research/quality", headers=headers).json()

    assert payload["profiles"]["versions"] == 1
    assert payload["profiles"]["confirmed"] == 1
    assert payload["corrections"]["previews"] == 1
    assert payload["labels"]["history"] == 0
    assert app.state.db.one(
        "SELECT status FROM research_campaigns WHERE id=?", (seeded["campaign_id"],),
    )["status"] == "partial"
