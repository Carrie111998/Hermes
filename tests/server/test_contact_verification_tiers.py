from __future__ import annotations

import pytest

from server.agent_service import AgentRunService, StubRunExecutor
from server.db import new_id, now
from server.lead_research.contacts import rank_contacts, verify_contact
from tests.server.test_api_mvp import make_client, seed_lead_and_contact


def official_staff_evidence():
    return {
        "evidence_id": "ev_staff", "source_class": "official",
        "published_email": "ayse@acme.test", "person_name": "Ayşe",
        "person_title": "Purchasing Manager",
    }


def tenant_supplied_evidence():
    return {"evidence_id": "tenant_row_1", "source_class": "customer", "tenant_supplied": True}


def pattern_and_mail_domain_evidence():
    return {
        "evidence_id": "ev_pattern", "person_confirmed": True, "title_confirmed": True,
        "observed_email_pattern": "{first}@acme.test", "company_domain": "acme.test",
        "mail_domain_accepts": True, "catch_all": False,
    }


def official_contact_page_evidence():
    return {
        "evidence_id": "ev_contact", "source_class": "official",
        "published_email": "info@acme.test",
    }


@pytest.mark.parametrize(("contact", "evidence", "tier", "kind"), [
    ({"email": "ayse@acme.test", "name": "Ayşe", "title": "Purchasing Manager"},
     [official_staff_evidence()], "green", "person"),
    ({"email": "ayse@acme.test", "name": "Ayşe"},
     [tenant_supplied_evidence()], "green", "person"),
    ({"email": "ayse@acme.test", "name": "Ayşe", "title": "Purchasing Manager"},
     [pattern_and_mail_domain_evidence()], "yellow", "person"),
    ({"email": "ayse@gmail.com", "name": "Ayşe"}, [], "red", "person"),
    ({"email": "info@acme.test"}, [official_contact_page_evidence()], "green", "generic"),
])
def test_contact_tiers_are_mechanical(contact, evidence, tier, kind):
    result = verify_contact(contact, evidence)

    assert (result.tier, result.contact_kind) == (tier, kind)
    assert result.checked_at > 0
    assert result.method


def test_catch_all_or_unconfirmed_person_is_red_even_with_a_plausible_pattern():
    evidence = pattern_and_mail_domain_evidence()
    evidence["catch_all"] = True

    result = verify_contact(
        {"email": "ayse@acme.test", "name": "Ayşe", "title": "Purchasing Manager"},
        [evidence],
    )

    assert result.tier == "red"
    assert result.method == "catch_all_domain"


def test_generic_address_ranks_after_people_even_when_green():
    ranked = rank_contacts([
        {"id": "generic", "verification_tier": "green", "contact_kind": "generic"},
        {"id": "yellow-person", "verification_tier": "yellow", "contact_kind": "person"},
        {"id": "green-person", "verification_tier": "green", "contact_kind": "person"},
    ])

    assert [row["id"] for row in ranked] == ["green-person", "yellow-person", "generic"]


def test_manual_contact_is_tenant_supplied_green_and_verify_returns_evidence_tier():
    _, client, headers, _ = make_client()
    lead = client.post(
        "/api/v1/leads", headers=headers, json={"company_name": "Buyer GmbH", "country": "DE"},
    ).json()
    created = client.post(
        "/api/v1/contacts", headers=headers,
        json={
            "lead_id": lead["id"], "email": "ayse@buyer.example",
            "data": {"name": "Ayşe", "title": "Purchasing Manager"},
        },
    )
    assert created.status_code == 201, created.text
    contact = created.json()

    assert contact["verification_tier"] == "green"
    assert contact["contact_kind"] == "person"
    assert contact["verification_method"] == "tenant_supplied"
    verified = client.post(f"/api/v1/contacts/{contact['id']}/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["verification_tier"] == "green"
    assert verified.json()["verification"] == "mechanical_evidence"


def test_contact_discovery_output_is_tiered_before_persistence():
    app, client, headers, company_id = make_client()
    lead, _ = seed_lead_and_contact(client, headers)
    service = AgentRunService(app.state.db, StubRunExecutor())
    stamp = now()
    run = {
        "id": new_id("run"), "company_id": company_id, "run_type": "contact_discovery",
        "payload": {"lead_ids": [lead["id"]], "max_contacts_per_company": 5},
    }
    output = {
        "contacts": [{
            "lead_id": lead["id"], "email": "buyer@buyer.example",
            "name": "Buyer", "title": "Purchasing Manager",
            "evidence": [pattern_and_mail_domain_evidence() | {
                "company_domain": "buyer.example",
                "observed_email_pattern": "{first}@buyer.example",
            }],
        }],
    }
    app.state.db.execute(
        "INSERT INTO agent_runs(id,company_id,run_type,status,payload,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (run["id"], company_id, run["run_type"], "running", "{}", stamp, stamp),
    )

    service._validate_output("contact_discovery", output)
    service.apply_output(run, output)
    stored = app.state.db.one(
        "SELECT * FROM contacts WHERE company_id=? AND email=?",
        (company_id, "buyer@buyer.example"),
    )

    assert stored["verification_tier"] == "yellow"
    assert stored["contact_kind"] == "person"
    assert stored["verification_method"] == "derived_observed_pattern"
