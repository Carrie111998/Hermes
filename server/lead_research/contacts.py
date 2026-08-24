"""Mechanical contact evidence tiers and deterministic outreach ranking."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ..db import now
from ..quality import normalize_name, validate_email
from .models import ApiModel


GENERIC_LOCAL_PARTS = frozenset({
    "admin", "contact", "hello", "info", "office", "sales", "support",
    "team", "export", "purchasing", "procurement", "orders", "enquiries",
})
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "yahoo.com", "icloud.com", "proton.me", "protonmail.com", "aol.com",
})


class ContactVerification(ApiModel):
    tier: Literal["green", "yellow", "red"]
    contact_kind: Literal["person", "generic"]
    method: str
    evidence_ids: list[str] = Field(default_factory=list)
    checked_at: float


def _contact_value(contact: dict, *keys: str):
    data = contact.get("data") if isinstance(contact.get("data"), dict) else {}
    for key in keys:
        value = contact.get(key)
        if value not in (None, ""):
            return value
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _email_parts(contact: dict) -> tuple[str, str]:
    email = str(_contact_value(contact, "email") or "").strip().casefold()
    if not validate_email(email):
        return "", ""
    return tuple(email.rsplit("@", 1))  # type: ignore[return-value]


def _kind(contact: dict) -> Literal["person", "generic"]:
    local, _ = _email_parts(contact)
    explicit = str(_contact_value(contact, "contact_kind") or "").casefold()
    if explicit == "generic" or local in GENERIC_LOCAL_PARTS:
        return "generic"
    return "person"


def _evidence_ids(evidence: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        str(item.get("evidence_id") or item.get("id") or "").strip()
        for item in evidence
        if str(item.get("evidence_id") or item.get("id") or "").strip()
    ))


def _same_name(left, right) -> bool:
    return bool(left and right and normalize_name(str(left)) == normalize_name(str(right)))


def _published_binding(contact: dict, item: dict, kind: str) -> bool:
    source_class = str(item.get("source_class") or item.get("classification") or "").casefold()
    if source_class not in {"official", "registry"}:
        return False
    email = str(_contact_value(contact, "email") or "").strip().casefold()
    published_email = str(item.get("published_email") or item.get("email") or "").strip().casefold()
    channel_matches = bool(email and published_email and email == published_email)
    if not email:
        channel_matches = any(
            _contact_value(contact, key) and _contact_value(contact, key) == item.get(f"published_{key}")
            for key in ("phone", "linkedin_url")
        )
    if not channel_matches:
        return False
    if kind == "generic":
        return True
    contact_name = _contact_value(contact, "name", "full_name")
    evidence_name = item.get("person_name") or item.get("name")
    return _same_name(contact_name, evidence_name)


def verify_contact(contact: dict, evidence: list[dict]) -> ContactVerification:
    """Classify one address without sending mail or trusting model confidence."""
    evidence = [item for item in (evidence or []) if isinstance(item, dict)]
    kind = _kind(contact)
    evidence_ids = _evidence_ids(evidence)
    local, domain = _email_parts(contact)

    if domain in FREE_MAIL_DOMAINS:
        return ContactVerification(
            tier="red", contact_kind=kind, method="free_mail_domain",
            evidence_ids=evidence_ids, checked_at=now(),
        )
    if any(item.get("conflicting") or item.get("address_conflict") for item in evidence):
        return ContactVerification(
            tier="red", contact_kind=kind, method="conflicting_evidence",
            evidence_ids=evidence_ids, checked_at=now(),
        )
    if any(item.get("catch_all") for item in evidence):
        return ContactVerification(
            tier="red", contact_kind=kind, method="catch_all_domain",
            evidence_ids=evidence_ids, checked_at=now(),
        )
    if any(
        item.get("tenant_supplied")
        or str(item.get("source_class") or "").casefold() == "customer"
        for item in evidence
    ):
        return ContactVerification(
            tier="green", contact_kind=kind, method="tenant_supplied",
            evidence_ids=evidence_ids, checked_at=now(),
        )
    if any(_published_binding(contact, item, kind) for item in evidence):
        return ContactVerification(
            tier="green", contact_kind=kind, method="published_official_address",
            evidence_ids=evidence_ids, checked_at=now(),
        )

    name = _contact_value(contact, "name", "full_name")
    title = _contact_value(contact, "title", "job_title")
    person_confirmed = any(item.get("person_confirmed") for item in evidence)
    title_confirmed = any(item.get("title_confirmed") for item in evidence)
    pattern_evidence = [item for item in evidence if item.get("observed_email_pattern")]
    pattern_domain = next((str(item.get("company_domain") or "").casefold() for item in pattern_evidence), "")
    accepts_mail = any(item.get("mail_domain_accepts") is True for item in evidence)
    if (
        kind == "person" and name and title and person_confirmed and title_confirmed
        and pattern_evidence and domain and domain == pattern_domain and accepts_mail
    ):
        return ContactVerification(
            tier="yellow", contact_kind="person", method="derived_observed_pattern",
            evidence_ids=evidence_ids, checked_at=now(),
        )

    if kind == "generic":
        method = "unpublished_generic_address"
    elif not evidence:
        method = "uncorroborated_address"
    elif not person_confirmed or not title_confirmed:
        method = "unconfirmed_person_or_role"
    elif not pattern_evidence:
        method = "unobserved_address_pattern"
    elif not accepts_mail:
        method = "mail_domain_not_accepted"
    elif not domain or domain != pattern_domain:
        method = "company_domain_mismatch"
    else:
        method = "insufficient_evidence"
    return ContactVerification(
        tier="red", contact_kind=kind, method=method,
        evidence_ids=evidence_ids, checked_at=now(),
    )


def outreach_rank(contact: dict) -> tuple[int, int, int, str]:
    data = contact.get("data") if isinstance(contact.get("data"), dict) else {}
    kind = str(contact.get("contact_kind") or data.get("contact_kind") or "person")
    tier = str(contact.get("verification_tier") or data.get("verification_tier") or "red")
    role_match = bool(contact.get("buyer_role_match") or data.get("buyer_role_match"))
    stable = str(contact.get("email") or contact.get("id") or "").casefold()
    return (
        1 if kind == "generic" else 0,
        {"green": 0, "yellow": 1, "red": 2}.get(tier, 2),
        0 if role_match else 1,
        stable,
    )


def eligible_primary_contact(contact: dict) -> bool:
    """Whether an email contact may be selected as an outreach primary."""
    tier = str(contact.get("verification_tier") or "").casefold()
    status = str(contact.get("status") or "").casefold()
    return bool(
        tier in {"green", "yellow"}
        and not contact.get("do_not_contact")
        and status not in {"blocked", "invalid"}
        and validate_email(str(contact.get("email") or ""))
    )


def eligible_cc_contact(contact: dict) -> bool:
    """CC is stricter than primary: published/customer-supplied people only."""
    return bool(
        eligible_primary_contact(contact)
        and str(contact.get("verification_tier") or "").casefold() == "green"
        and str(contact.get("contact_kind") or "").casefold() == "person"
    )


def rank_contacts(contacts: list[dict]) -> list[dict]:
    return sorted((dict(contact) for contact in contacts), key=outreach_rank)
