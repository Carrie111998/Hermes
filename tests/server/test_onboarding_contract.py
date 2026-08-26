"""The §6 onboarding contract: PRODUCT.md field lists vs. what storage accepts.

PRODUCT.md §7 is checked against the OpenAPI schema and §8.2 against main.js.
§6 had no such check, so this file is the third one: the field names in the
document and the keys `_put_section` accepts cannot drift apart without a
failure here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest

from server.provisioning import provision_demo_account
from server.routes.company import router as company_router
from server.routes.onboarding import STEPS
from server.schemas import SECTION_FIELDS, unknown_section_fields

from .test_webui import make_client


def _spec_fields(heading: str) -> list[str]:
    """The field names in one §6 subsection's first fenced block."""
    doc = (ROOT / "PRODUCT.md").read_text(encoding="utf-8")
    body = doc[doc.index(f"## {heading}"):]
    body = body[:body.index("\n## ")]
    fence = re.search(r"```text\n(.*?)```", body, re.S)
    assert fence, f"§{heading} has no field block"
    return [line.strip() for line in fence.group(1).strip().splitlines() if line.strip()]


@pytest.mark.parametrize(("heading", "section"), [
    ("6.1 Company identity", "profile"),
    ("6.2 Company positioning", "positioning"),
])
def test_every_documented_field_is_accepted_by_its_section(heading, section):
    fields = _spec_fields(heading)
    assert fields, f"§{heading} listed no fields"
    missing = sorted(set(fields) - SECTION_FIELDS[section])
    assert not missing, f"PRODUCT.md §{heading} names fields {section} rejects: {missing}"


def test_every_writable_section_has_an_allowlist():
    """A new step or company section must bring its field list with it.

    Without this, `unknown_section_fields` raises KeyError at request time
    instead of here.
    """
    reachable = set(STEPS.values())
    for route in company_router.routes:
        match = re.fullmatch(r"/company/([a-z-]+)", route.path)
        if match and "PATCH" in (route.methods or ()):
            reachable.add(match.group(1).replace("-", "_"))
    assert reachable <= set(SECTION_FIELDS), sorted(reachable - set(SECTION_FIELDS))


def test_a_misspelled_field_is_rejected_and_named():
    _, client = make_client()
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Acme"})
    headers["X-Company-ID"] = company.json()["id"]

    rejected = client.patch("/api/v1/onboarding/company-identity", headers=headers,
                            json={"data": {"industry": "Kitchen appliances", "founded_yr": 1994}})
    assert rejected.status_code == 422, rejected.text
    detail = rejected.json()["detail"]
    assert detail["code"] == "unknown_section_fields"
    assert detail["fields"] == ["founded_yr"]

    # The valid half of the patch must not have landed either, or a client that
    # ignores the 422 ends up with a half-saved section.
    assert client.get("/api/v1/company/profile", headers=headers).json()["data"] == {}


def test_the_whole_documented_identity_and_positioning_round_trips():
    _, client = make_client()
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Acme"})
    headers["X-Company-ID"] = company.json()["id"]

    for heading, path, section in (
        ("6.1 Company identity", "/api/v1/onboarding/company-identity", "profile"),
        ("6.2 Company positioning", "/api/v1/onboarding/positioning", "positioning"),
    ):
        patch = {field: f"value for {field}" for field in _spec_fields(heading)}
        saved = client.patch(path, headers=headers, json={"data": patch})
        assert saved.status_code == 200, saved.text
        stored = client.get(f"/api/v1/company/{section.replace('_', '-')}",
                            headers=headers).json()["data"]
        assert stored == patch


def test_provisioning_rejects_a_misspelled_profile_field(tmp_path):
    from server.db import Database

    db = Database(tmp_path / "provision.db")
    with pytest.raises(ValueError, match="headquarters_ctry"):
        provision_demo_account(
            db, email="demo@example.test", password="correct-horse-battery",
            company_profile={"name": "Silverline", "headquarters_ctry": "TR"},
            onboarding_sources=[],
        )


def test_unknown_section_fails_loudly_rather_than_skipping_validation():
    with pytest.raises(KeyError):
        unknown_section_fields("section_nobody_declared", {"anything": 1})
