"""Outreach recipient and language safety contract.

The contact evidence tier is a mechanical safety boundary, not UI metadata.
These tests exercise the HTTP path so campaign generation, approval, and send
cannot quietly diverge from the contact rules used by lead research.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import compliance  # noqa: E402
from server.db import json_dump, new_id, now  # noqa: E402
from server.quality import validate_outreach_text  # noqa: E402
from server.routes.outreach import UnsupportedTemplateLanguage  # noqa: E402

from test_api_mvp import make_client, seed_lead_and_contact, wait_for_run  # noqa: E402


def _contact(client, headers, lead_id: str, email: str) -> dict:
    response = client.post(
        "/api/v1/contacts", headers=headers,
        json={"lead_id": lead_id, "email": email, "data": {"full_name": email.split("@", 1)[0]}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _tier(app, contact_id: str, tier: str, kind: str = "person", *, dnc: bool = False) -> None:
    app.state.db.execute(
        "UPDATE contacts SET verification_tier=?,contact_kind=?,do_not_contact=?,status=? WHERE id=?",
        (tier, kind, int(dnc), "blocked" if dnc else "active", contact_id),
    )


def _save_templates(client, headers, templates: dict) -> None:
    response = client.patch(
        "/api/v1/company/email-templates", headers=headers,
        json={"data": {"templates": templates}},
    )
    assert response.status_code == 200, response.text


def test_cc_contains_only_unsuppressed_green_person_contacts():
    app, client, headers, company_id = make_client()
    lead, primary = seed_lead_and_contact(client, headers)
    green = _contact(client, headers, lead["id"], "green.person@buyer.example")
    yellow = _contact(client, headers, lead["id"], "yellow.person@buyer.example")
    red = _contact(client, headers, lead["id"], "red.person@buyer.example")
    generic = _contact(client, headers, lead["id"], "info@buyer.example")
    suppressed = _contact(client, headers, lead["id"], "suppressed.person@buyer.example")
    dnc = _contact(client, headers, lead["id"], "dnc.person@buyer.example")
    _tier(app, yellow["id"], "yellow")
    _tier(app, red["id"], "red")
    _tier(app, generic["id"], "green", "generic")
    _tier(app, dnc["id"], "green", dnc=True)
    compliance.suppress(app.state.db, company_id, suppressed["email"], "unsubscribe")

    run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["payload"]["contact_id"] == primary["id"]
    assert run.json()["payload"]["recipients"]["cc"] == [green["email"]]


def test_cc_rules_cannot_inject_an_unverified_address():
    _app, client, headers, _company_id = make_client()
    lead, _primary = seed_lead_and_contact(client, headers)
    rule = client.post("/api/v1/cc-rules", headers=headers, json={
        "name": "unsafe default", "cc_emails": ["stranger@elsewhere.example"], "is_default": True,
    })
    assert rule.status_code == 201, rule.text

    run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["payload"]["recipients"]["cc"] == []


@pytest.mark.parametrize("tier", ["red", None])
def test_red_or_unclassified_contact_is_never_auto_primary(tier):
    app, client, headers, _company_id = make_client()
    lead = client.post(
        "/api/v1/leads", headers=headers,
        json={"company_name": "Safety Buyer", "country": "DE"},
    ).json()
    unsafe = _contact(client, headers, lead["id"], "named.person@safety.example")
    generic = _contact(client, headers, lead["id"], "info@safety.example")
    app.state.db.execute(
        "UPDATE contacts SET verification_tier=?,contact_kind='person' WHERE id=?",
        (tier, unsafe["id"]),
    )
    _tier(app, generic["id"], "green", "generic")

    run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["payload"]["contact_id"] == generic["id"]


def test_yellow_person_is_primary_before_green_generic_but_never_cc():
    app, client, headers, _company_id = make_client()
    lead = client.post(
        "/api/v1/leads", headers=headers,
        json={"company_name": "Ranked Buyer", "country": "DE"},
    ).json()
    generic = _contact(client, headers, lead["id"], "info@ranked.example")
    yellow = _contact(client, headers, lead["id"], "buyer.person@ranked.example")
    second_yellow = _contact(client, headers, lead["id"], "other.person@ranked.example")
    _tier(app, generic["id"], "green", "generic")
    _tier(app, yellow["id"], "yellow")
    _tier(app, second_yellow["id"], "yellow")

    run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["payload"]["contact_id"] == yellow["id"]
    assert run.json()["payload"]["recipients"]["cc"] == []


def test_suppression_is_authoritative_during_auto_selection():
    app, client, headers, company_id = make_client()
    lead, contact = seed_lead_and_contact(client, headers)
    compliance.suppress(app.state.db, company_id, contact["email"], "unsubscribe")

    response = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "Lead has no eligible email contact"


def test_turkish_character_guard_rejects_ascii_substitution():
    failures = validate_outreach_text("tr", "Sirketiniz icin cozum", "")
    assert "turkish_character_quality" in failures


def test_unsupported_language_is_reported_instead_of_falling_back_to_english():
    _app, client, headers, _company_id = make_client()
    lead, _contact_row = seed_lead_and_contact(client, headers)
    _save_templates(client, headers, {
        "en": {"subject": "Partnership", "body": "Hello {{contact_name}}."},
    })
    campaign = client.post("/api/v1/outreach/campaigns", headers=headers, json={
        "name": "Arabic", "lead_ids": [lead["id"]], "channel": "email",
    }).json()

    response = client.post(
        f"/api/v1/outreach/campaigns/{campaign['id']}/generate-messages",
        headers=headers, json={"language": "ar"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_template_language"
    assert "no approved ar template" in response.json()["detail"]["message"]
    assert issubclass(UnsupportedTemplateLanguage, ValueError)


def test_generation_records_selected_language_and_template_version():
    _app, client, headers, _company_id = make_client()
    lead, _contact_row = seed_lead_and_contact(client, headers)
    _save_templates(client, headers, {
        "de": {
            "version": "sales-de-v7",
            "subject": "Partnerschaft mit {{company_name}}",
            "body": "Guten Tag {{contact_name}}.",
        },
    })
    campaign = client.post("/api/v1/outreach/campaigns", headers=headers, json={
        "name": "German", "lead_ids": [lead["id"]], "channel": "email",
    }).json()

    response = client.post(
        f"/api/v1/outreach/campaigns/{campaign['id']}/generate-messages",
        headers=headers, json={"language": "de"},
    )
    assert response.status_code == 202, response.text
    run = response.json()[0]
    assert run["payload"]["language"] == "de"
    assert run["payload"]["template_version"] == "sales-de-v7"
    completed = wait_for_run(client, headers, run["id"])
    message = client.get(f"/api/v1/outreach/messages/{completed['output_ref']}", headers=headers).json()
    assert message["content"]["language"] == "de"
    assert message["data"]["generation"]["template_version"] == "sales-de-v7"


def test_contact_downgrade_blocks_approval_and_send():
    app, client, headers, company_id = make_client()
    stamp = now()
    app.state.db.execute("INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)", (
        new_id("int"), company_id, "email", "stub", "connected", None, json_dump({}), stamp, stamp,
    ))
    lead, contact = seed_lead_and_contact(client, headers)
    first_run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers).json()
    first = wait_for_run(client, headers, first_run["id"])
    _tier(app, contact["id"], "red")
    rejected = client.post(
        f"/api/v1/outreach/messages/{first['output_ref']}/approve", headers=headers,
    )
    assert rejected.status_code == 422
    assert "unsafe_primary_contact" in rejected.json()["detail"]["failures"]

    _tier(app, contact["id"], "green")
    second_run = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers).json()
    second = wait_for_run(client, headers, second_run["id"])
    approved = client.post(
        f"/api/v1/outreach/messages/{second['output_ref']}/approve", headers=headers,
    )
    assert approved.status_code == 200, approved.text
    _tier(app, contact["id"], "red")
    blocked = client.post(
        f"/api/v1/outreach/messages/{second['output_ref']}/send", headers=headers,
    )
    assert blocked.status_code == 409
    assert "no longer eligible" in blocked.json()["detail"]
