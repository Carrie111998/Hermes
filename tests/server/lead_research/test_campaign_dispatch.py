"""Starting a campaign must not hold the request, and cancelling must stop it.

A campaign is hundreds of blocking Web Unlocker fetches. Running it inside the
request handler meant `/start` held a connection open for the whole campaign, so
any proxy or worker timeout killed it mid-run and left the campaign `running`
for good — and `/cancel` wrote a status nothing read, so a run that was told to
stop kept spending until the corpus ran out.
"""
from __future__ import annotations

import threading

import pytest

from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from tests.server.lead_research.fakes import deterministic_provider, fixture_definition
from tests.server.lead_research.test_vertical_slice import campaign_body, make_research_client


class BlockingVerifier:
    """Holds inside the first candidate until the test releases it."""

    def __init__(self, provider):
        self.provider = provider
        self.definition = provider.definition
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def discover(self, query):
        return self.provider.discover(query)

    def health(self):
        return self.provider.health()

    def verify(self, query, candidate):
        with self._lock:
            self.seen.append(candidate.source_record_id)
        self.entered.set()
        assert self.release.wait(10), "test never released the verifier"
        return self.provider.verify(query, candidate)


def _blocking_client():
    app, client, headers, company_id = make_research_client()
    definition = fixture_definition()
    verifier = BlockingVerifier(deterministic_provider(definition))
    app.state.lead_research = LeadResearchService(
        app.state.db, registry=ProviderRegistry([definition], {definition.source_id: verifier})
    )
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()
    return app, client, headers, company_id, campaign, verifier


def test_start_returns_before_the_campaign_has_run():
    """The regression this file exists for.

    The response used to carry the finished campaign's status, which is only
    possible if the request waited for every fetch.
    """
    app, client, headers, _, campaign, verifier = _blocking_client()

    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert started.status_code == 202
    assert started.json()["status"] == "queued"
    assert verifier.entered.wait(10), "campaign never reached the worker"
    # Still in flight while the request has already been answered.
    assert app.state.db.one(
        "SELECT status FROM research_campaigns WHERE id=?", (campaign["id"],)
    )["status"] == "running"

    verifier.release.set()
    app.state.lead_research.wait_until_settled(campaign["company_id"], campaign["id"])


def test_a_second_start_while_in_flight_is_refused():
    """Both runs would rebuild the same campaign's results, interleaved."""
    app, client, headers, _, campaign, verifier = _blocking_client()
    client.post(f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers)
    assert verifier.entered.wait(10)

    second = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert second.status_code == 409
    verifier.release.set()
    app.state.lead_research.wait_until_settled(campaign["company_id"], campaign["id"])


def test_cancelling_stops_before_the_next_candidate_is_verified():
    app, client, headers, _, campaign, verifier = _blocking_client()
    client.post(f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers)
    assert verifier.entered.wait(10), "campaign never reached the worker"

    cancelled = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/cancel", headers=headers,
    )
    assert cancelled.status_code == 200
    verifier.release.set()
    settled = app.state.lead_research.wait_until_settled(
        campaign["company_id"], campaign["id"], timeout=30
    )

    assert settled is not None and settled["status"] == "cancelled"
    # Two candidates exist in DE; the second was never paid for.
    assert verifier.seen == ["buyer-de-1"]
    assert app.state.db.one(
        "SELECT status FROM research_campaigns WHERE id=?", (campaign["id"],)
    )["status"] == "cancelled"
    assert app.state.db.one(
        "SELECT status FROM agent_runs WHERE id=?", (settled["run_id"],)
    )["status"] == "cancelled"


def test_a_campaign_cancelled_before_pickup_never_researches_anything():
    """Claiming `running` first would lose the cancellation and run the corpus."""
    app, client, headers, company_id, campaign, verifier = _blocking_client()
    app.state.db.execute(
        "UPDATE research_campaigns SET status='cancelled' WHERE id=?", (campaign["id"],)
    )

    result = app.state.lead_research.run(company_id, campaign["id"])

    assert result["status"] == "cancelled"
    assert verifier.seen == []
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )["n"] == 0


def test_start_after_shutdown_refuses_instead_of_silently_dropping_the_run():
    app, _client, _headers, company_id, campaign, _ = _blocking_client()
    app.state.lead_research.shutdown()

    with pytest.raises(RuntimeError):
        app.state.lead_research.start(company_id, campaign["id"])
