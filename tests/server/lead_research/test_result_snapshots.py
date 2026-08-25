from __future__ import annotations

import json
import sqlite3

import pytest

from tests.server.lead_research.test_vertical_slice import (
    campaign_body,
    make_research_client,
    start_and_settle,
)


def _run(app, client, headers, name):
    body = campaign_body(name)
    body["target_countries"] = ["DE"]
    campaign = client.post(
        "/api/v1/research-campaigns", headers=headers, json=body,
    ).json()
    _, settled = start_and_settle(app, client, headers, campaign["id"])
    assert settled["status"] == "succeeded"
    return campaign


def test_repeat_campaign_reuses_leads_but_keeps_distinct_result_snapshots():
    app, client, headers, company_id = make_research_client()
    first = _run(app, client, headers, "First decision")
    second = _run(app, client, headers, "Second decision")

    first_rows = app.state.db.all(
        "SELECT id,organization_id,lead_id FROM research_results "
        "WHERE company_id=? AND campaign_id=? ORDER BY organization_id",
        (company_id, first["id"]),
    )
    second_rows = app.state.db.all(
        "SELECT id,organization_id,lead_id FROM research_results "
        "WHERE company_id=? AND campaign_id=? ORDER BY organization_id",
        (company_id, second["id"]),
    )

    assert [row["lead_id"] for row in first_rows] == [row["lead_id"] for row in second_rows]
    assert {row["id"] for row in first_rows}.isdisjoint({row["id"] for row in second_rows})
    snapshots = app.state.db.all(
        "SELECT result_id,campaign_id,snapshot_json FROM research_score_snapshots "
        "WHERE company_id=? ORDER BY created_at",
        (company_id,),
    )
    assert len(snapshots) == len(first_rows) + len(second_rows)
    assert {json.loads(row["snapshot_json"])["campaign_id"] for row in snapshots} == {
        first["id"], second["id"],
    }
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM leads WHERE company_id=?",
        (company_id,),
    )["n"] == len(first_rows)


def test_score_snapshot_exposes_weights_and_is_immutable():
    app, client, headers, company_id = make_research_client()
    campaign = _run(app, client, headers, "Frozen decision")
    row = app.state.db.one(
        "SELECT id,snapshot_json FROM research_score_snapshots "
        "WHERE company_id=? AND campaign_id=? LIMIT 1",
        (company_id, campaign["id"]),
    )
    snapshot = json.loads(row["snapshot_json"])

    assert snapshot["profile_version_id"] == campaign["profile_version_id"]
    assert snapshot["score"]["known_weight"] + snapshot["score"]["unknown_weight"] + sum(
        snapshot["score"]["not_applicable_dimensions"].values()
    ) == 100
    assert snapshot["score"]["dimension_evidence_ids"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        app.state.db.execute(
            "UPDATE research_score_snapshots SET snapshot_json='{}' WHERE id=?",
            (row["id"],),
        )


def test_customer_result_and_claim_payload_explains_uncertainty_and_exact_evidence():
    app, client, headers, _ = make_research_client()
    campaign = _run(app, client, headers, "Explain this decision")

    response = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results", headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()[0]
    assert {
        "priority_band", "known_weight", "unknown_weight", "unknown_dimensions",
        "not_applicable_dimensions", "dimension_evidence_ids",
    } <= result.keys()
    assert result["known_weight"] + result["unknown_weight"] + sum(
        result["not_applicable_dimensions"].values()
    ) == 100
    assert "hidden_labels" not in result and "label_assignments" not in result

    response = client.get(
        f"/api/v1/research/results/{result['id']}/claims", headers=headers,
    )
    assert response.status_code == 200, response.text
    citations = [item for claim in response.json() for item in claim["evidence"]]
    assert citations
    citation = next(item for item in citations if item.get("original_text"))
    assert citation["value_en"] is not None
    assert citation["source_language"]
    assert citation["retrieved_at"]
    assert citation["span_start"] < citation["span_end"]
    assert isinstance(citation["mechanically_validated"], bool)
    assert citation["criteria"]
    assert all(set(criterion) == {"dimension", "weight"} for criterion in citation["criteria"])
    assert "shared_fact_id" not in citation and "hidden_labels" not in citation


# ── the selection is recorded everywhere the score is ────────────────────────

def test_a_displayed_lead_says_the_same_rank_in_every_record():
    """Three places store this decision, and a disagreement is unauditable.

    The result row's `data`, the result's frozen `snapshot_json`, and the
    append-only score snapshot all have to name the same rank, because the
    customer reads the first and an audit reads the last.
    """
    app, client, headers, company_id = make_research_client()
    campaign = _run(app, client, headers, "Ranked decision")

    rows = app.state.db.all(
        "SELECT id,verdict,lead_id,data,snapshot_json FROM research_results "
        "WHERE company_id=? AND campaign_id=?",
        (company_id, campaign["id"]),
    )
    assert rows
    for row in rows:
        selection = json.loads(row["data"])["selection"]
        assert selection["displayed"] is True
        assert selection["display_rank"] >= 1
        assert selection["country_round"] >= 1
        assert row["lead_id"]
        assert json.loads(row["snapshot_json"])["selection"] == selection
        latest = app.state.db.one(
            "SELECT snapshot_json FROM research_score_snapshots "
            "WHERE company_id=? AND result_id=? ORDER BY created_at DESC",
            (company_id, row["id"]),
        )
        assert json.loads(latest["snapshot_json"])["selection"] == selection

    ranks = sorted(
        json.loads(row["data"])["selection"]["display_rank"] for row in rows
    )
    assert ranks == list(range(1, len(rows) + 1))
