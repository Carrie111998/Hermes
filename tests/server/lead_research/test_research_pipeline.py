from __future__ import annotations

import json

from server.lead_research.acquisition import CANDIDATE_STAGES
from tests.server.lead_research.test_vertical_slice import (
    campaign_body,
    make_research_client,
    start_and_settle,
)


def test_campaign_persists_the_full_candidate_stage_state_machine():
    app, client, headers, _ = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post(
        "/api/v1/research-campaigns", headers=headers, json=body,
    ).json()

    _, result = start_and_settle(app, client, headers, campaign["id"])

    assert result["status"] == "succeeded"
    row = app.state.db.one(
        "SELECT checkpoint FROM campaign_partitions WHERE campaign_id=?",
        (campaign["id"],),
    )
    candidates = json.loads(row["checkpoint"])["candidates"]
    assert candidates
    assert {entry["stage"] for entry in candidates.values()} == {"materialized"}
    metrics = result["metrics"]
    for stage in CANDIDATE_STAGES:
        assert metrics[f"stage_{stage}"] == len(candidates)


def test_retry_reuses_accepted_facts_before_deep_research():
    app, client, headers, company_id = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post(
        "/api/v1/research-campaigns", headers=headers, json=body,
    ).json()
    start_and_settle(app, client, headers, campaign["id"])
    before = app.state.db.one(
        "SELECT COUNT(*) AS n FROM shared_facts",
    )["n"]

    retried = app.state.lead_research.run(company_id, campaign["id"])

    assert retried["status"] == "succeeded"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"] == before
    assert retried["metrics"]["stage_reused"] > 0
