from __future__ import annotations

import json

from server.lead_research.facts import FactRepository
from server.lead_research.labels import LabelRepository
from tests.server.lead_research.test_vertical_slice import (
    campaign_body,
    make_research_client,
    start_and_settle,
)


def _seed():
    app, client, admin_headers, company_id = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post(
        "/api/v1/research-campaigns", headers=admin_headers, json=body,
    ).json()
    start_and_settle(app, client, admin_headers, campaign["id"])
    result = app.state.db.one(
        "SELECT * FROM research_results WHERE company_id=? AND campaign_id=? LIMIT 1",
        (company_id, campaign["id"]),
    )
    return app, client, admin_headers, company_id, campaign, result


def _customer_headers(client, admin_headers, company_id):
    created = client.post("/api/v1/admin/users", headers=admin_headers, json={
        "email": "researcher@acme.test",
        "password": "another-secure-password",
        "role": "customer",
        "company_id": company_id,
    })
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={
        "email": "researcher@acme.test",
        "password": "another-secure-password",
    }).json()
    return {
        "Authorization": f"Bearer {login['access_token']}",
        "X-Company-ID": company_id,
    }


def test_hidden_label_history_is_admin_owned_and_never_serialized_to_customer():
    app, client, admin_headers, company_id, campaign, result = _seed()
    labels = LabelRepository(app.state.db)
    labels.assign(
        company_id, result["id"], "export_readiness", "high", "result",
        "admin", "usr_admin", "verified from outcome analysis",
        campaign["profile_version_id"],
    )
    labels.assign(
        company_id, result["id"], "export_readiness", "medium", "result",
        "outcome_analysis", "usr_admin", "reply outcome changed assessment",
        campaign["profile_version_id"],
    )

    history = labels.history(company_id, result["id"])
    assert [item.value for item in history] == ["high", "medium"]
    assert history[0].effective_until is not None
    customer_headers = _customer_headers(client, admin_headers, company_id)
    payload = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results",
        headers=customer_headers,
    ).json()
    serialized = json.dumps(payload)
    assert "hidden_labels" not in serialized
    assert "export_readiness" not in serialized


def test_correction_previews_consumers_and_appends_recomputed_snapshot():
    app, _, _, _, _, result = _seed()
    first_snapshot = app.state.db.one(
        "SELECT * FROM research_score_snapshots WHERE result_id=? ORDER BY created_at LIMIT 1",
        (result["id"],),
    )
    snapshot = json.loads(first_snapshot["snapshot_json"])
    fact_id = snapshot["fact_ids"][0]
    original_snapshot = first_snapshot["snapshot_json"]
    facts = FactRepository(app.state.db)

    preview = facts.correct(
        fact_id, "corrected value", "usr_admin", "registry correction", False,
    )
    applied = facts.correct(
        fact_id, "corrected value", "usr_admin", "registry correction", True,
    )

    assert result["id"] in preview.result_ids
    assert result["id"] in applied.recomputed_result_ids
    assert app.state.db.one(
        "SELECT snapshot_json FROM research_score_snapshots WHERE id=?",
        (first_snapshot["id"],),
    )["snapshot_json"] == original_snapshot
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_score_snapshots WHERE result_id=?",
        (result["id"],),
    )["n"] == 2


def test_scoring_fact_correction_recomputes_current_score_but_preserves_history():
    app, _, _, _, _, result = _seed()
    original_row = app.state.db.one(
        "SELECT id,snapshot_json FROM research_score_snapshots "
        "WHERE result_id=? ORDER BY created_at LIMIT 1",
        (result["id"],),
    )
    original = json.loads(original_row["snapshot_json"])
    product_fact_id = next(
        fact_id
        for fact_id in original["fact_ids"]
        if fact_id.startswith("sf_")
        and app.state.db.one(
            "SELECT field FROM shared_facts WHERE id=?", (fact_id,),
        )["field"] == "product_term"
    )

    impact = FactRepository(app.state.db).correct(
        product_fact_id, False, "usr_admin", "official product correction", True,
    )

    current = app.state.db.one(
        "SELECT fit_score,snapshot_json FROM research_results WHERE id=?",
        (result["id"],),
    )
    revised = json.loads(current["snapshot_json"])
    assert result["id"] in impact.recomputed_result_ids
    assert current["fit_score"] == revised["score"]["fit_score"]
    assert revised["score"]["dimensions"]["product_sector_fit"] \
        < original["score"]["dimensions"]["product_sector_fit"]
    assert revised["score"]["fit_score"] < original["score"]["fit_score"]
    assert app.state.db.one(
        "SELECT snapshot_json FROM research_score_snapshots WHERE id=?",
        (original_row["id"],),
    )["snapshot_json"] == original_row["snapshot_json"]
