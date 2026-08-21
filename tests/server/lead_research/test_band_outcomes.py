"""Whether a priority band actually converts is the only ground truth we have.

Fit and evidence confidence are our own opinion of a company — measured against
evidence we chose to collect, weighted by numbers the customer chose to set.
Nothing in that loop closes, so a scoring profile can be confidently,
consistently wrong and look healthy from the inside.

This report closes it. If band A replies no better than band C, or no better
than the leads the run rejected, the weights or the criteria are wrong and no
further evidence rigour will fix it. It carries a second load too: labels are
not shown to customers, so a customer cannot tell us a label is wrong —
conversion per band is the only channel through which a mislabelled profile ever
surfaces.
"""
from __future__ import annotations

from server.db import json_dump, new_id, now

from tests.server.lead_research.test_vertical_slice import make_research_client


def _lead(db, company_id, *, band, fit, confidence, name):
    lead_id = new_id("lead")
    stamp = now()
    db.execute(
        "INSERT INTO leads(id,company_id,company_name,country,status,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (lead_id, company_id, name, "DE", "qualified",
         json_dump({"priority_band": band, "fit_score": fit}), stamp, stamp),
    )
    return lead_id


def _result(db, company_id, campaign_id, lead_id, *, fit, confidence, verdict="strong_fit"):
    db.execute(
        "INSERT INTO research_results(id,company_id,campaign_id,organization_id,lead_id,"
        "verdict,fit_score,evidence_confidence,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("res"), company_id, campaign_id, new_id("org"), lead_id, verdict,
         fit, confidence, "{}", now(), now()),
    )


def _message(db, company_id, lead_id, *, sent=True, replied=False, bounced=False):
    stamp = now()
    db.execute(
        "INSERT INTO outreach_messages(id,company_id,lead_id,channel,status,content_hash,"
        "content,sent_at,replied_at,bounced_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("msg"), company_id, lead_id, "email", "sent", "h" * 8, "{}",
         stamp if sent else None, stamp if replied else None,
         stamp if bounced else None, stamp, stamp),
    )


def _campaign(db, company_id):
    campaign_id = new_id("rc")
    db.execute(
        "INSERT INTO research_campaigns(id,company_id,name,status,version,config,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, "DACH distributors", "succeeded", 1, "{}", now(), now()),
    )
    return campaign_id


def _fixture():
    app, client, headers, company_id = make_research_client()
    db = app.state.db
    campaign_id = _campaign(db, company_id)
    return app, client, headers, company_id, db, campaign_id


def _bands(payload):
    return {band["band"]: band for band in payload["bands"]}


def test_reply_rate_is_reported_for_each_band():
    app, client, headers, company_id, db, campaign_id = _fixture()
    # Band A: two contacted, one replied. Band C: two contacted, none replied.
    for name, band, replied in (
        ("Atlas", "A", True), ("Beacon", "A", False),
        ("Corner", "C", False), ("Delta", "C", False),
    ):
        lead_id = _lead(db, company_id, band=band, fit=90, confidence=.8, name=name)
        _result(db, company_id, campaign_id, lead_id, fit=90, confidence=.8)
        _message(db, company_id, lead_id, replied=replied)

    payload = client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json()

    bands = _bands(payload)
    assert bands["A"]["reply_rate"] == .5
    assert bands["C"]["reply_rate"] == 0
    assert payload["totals"]["leads_contacted"] == 4
    assert payload["totals"]["reply_rate"] == .25


def test_a_band_nobody_contacted_has_no_reply_rate_rather_than_zero():
    """0% reads as "we tried and failed"; those lead to opposite decisions."""
    app, client, headers, company_id, db, campaign_id = _fixture()
    lead_id = _lead(db, company_id, band="B", fit=70, confidence=.6, name="Untouched")
    _result(db, company_id, campaign_id, lead_id, fit=70, confidence=.6)

    payload = client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json()

    band = _bands(payload)["B"]
    assert band["leads"] == 1
    assert band["leads_contacted"] == 0
    assert band["reply_rate"] is None


def test_several_messages_to_one_company_are_one_lead():
    """Otherwise a persistent follow-up sequence looks like poor targeting."""
    app, client, headers, company_id, db, campaign_id = _fixture()
    lead_id = _lead(db, company_id, band="A", fit=88, confidence=.8, name="Atlas")
    _result(db, company_id, campaign_id, lead_id, fit=88, confidence=.8)
    for _ in range(4):
        _message(db, company_id, lead_id)
    _message(db, company_id, lead_id, replied=True)

    band = _bands(client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json())["A"]

    assert band["messages_sent"] == 5
    assert band["leads_contacted"] == 1
    assert band["leads_replied"] == 1
    assert band["reply_rate"] == 1


def test_rejected_leads_are_reported_as_the_control_group():
    """A band that converts no better than what we threw away is the finding."""
    app, client, headers, company_id, db, campaign_id = _fixture()
    strong = _lead(db, company_id, band="A", fit=90, confidence=.8, name="Atlas")
    _result(db, company_id, campaign_id, strong, fit=90, confidence=.8)
    _message(db, company_id, strong, replied=True)
    dropped = _lead(db, company_id, band="Rejected", fit=20, confidence=.3, name="Northstar")
    _result(db, company_id, campaign_id, dropped, fit=20, confidence=.3, verdict="reject")
    _message(db, company_id, dropped, replied=True)

    bands = _bands(client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json())

    assert bands["Rejected"]["reply_rate"] == 1
    assert bands["Rejected"]["mean_fit_score"] == 20
    assert bands["A"]["reply_rate"] == 1


def test_bounces_are_separated_from_silence():
    """A bounced address is a contact-data problem, not a scoring one."""
    app, client, headers, company_id, db, campaign_id = _fixture()
    lead_id = _lead(db, company_id, band="A", fit=90, confidence=.8, name="Atlas")
    _result(db, company_id, campaign_id, lead_id, fit=90, confidence=.8)
    _message(db, company_id, lead_id, bounced=True)

    band = _bands(client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json())["A"]

    assert band["bounce_rate"] == 1
    assert band["reply_rate"] == 0


def test_another_tenant_campaign_is_not_reported():
    app, client, headers, company_id, db, campaign_id = _fixture()
    payload = client.get(
        "/api/v1/research-campaigns/rc_missing/outcomes", headers=headers
    )

    assert payload.status_code == 404


def test_a_campaign_with_no_leads_reports_empty_rather_than_failing():
    app, client, headers, company_id, db, campaign_id = _fixture()

    payload = client.get(
        f"/api/v1/research-campaigns/{campaign_id}/outcomes", headers=headers
    ).json()

    assert payload["bands"] == []
    assert payload["totals"]["leads"] == 0
    assert payload["totals"]["reply_rate"] is None
