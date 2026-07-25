"""Deterministic validation and outreach safety rules.

Derived from ``docs/prototype-reference/qa`` but independent of its legacy
Sheets/EWS transports.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Unanchored variant for pulling a failed recipient out of a bounce subject.
EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
PLACEHOLDER_RE = re.compile(r"{{[^{}]+}}|\[[A-Z][^\]]*(?:HERE|SENTENCE|INSERT|TODO)[^\]]*\]", re.I)
INTERNAL_MARKER_RE = re.compile(r"\[(?:SKIP|EXCEPTION|HEADER ROW|NOT RELEVANT)\]", re.I)
UNKNOWN_RE = re.compile(r"\bunknown\b", re.I)
# Turkish-distinctive letters only. ö/ü/ç are shared with German/French/etc.
# and would fail every legitimate "Frau Müller" email; ı/İ/ğ/Ğ/ş/Ş appear in
# essentially all leaked Turkish operator text without false-positives.
TURKISH_CHARS_RE = re.compile(r"[ıİğĞşŞ]")
BOUNCE_RE = re.compile(
    r"mailer-daemon|postmaster|undeliverable|delivery (?:failed|failure|status)|"
    r"returned mail|failure notice|spam quarantine", re.I,
)


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(content: dict) -> str:
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def normalize_name(value: str) -> str:
    """Aggressive dedup key: casefold + strip diacritics.

    Handles Turkish input correctly — plain casefold() maps İ→i̇ (combining
    dot) and leaves ı distinct, so "İSTANBUL"/"istanbul"/"ISTANBUL" would get
    three different keys. Folding to ASCII-ish also matches Müller≈Muller.
    Keys are internal only, never displayed.
    """
    value = value.replace("İ", "i").replace("I", "i").replace("ı", "i")
    folded = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def validate_email(value: str | None) -> bool:
    return bool(value and EMAIL_RE.fullmatch(value.strip()))


def validate_phone(value: str | None) -> bool:
    return bool(value and E164_RE.fullmatch(value.strip()))


def canonical_linkedin_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "linkedin.com", "www.linkedin.com",
    }:
        return None
    path = parsed.path.rstrip("/")
    if not path.startswith("/in/") or len(path.split("/")) < 3:
        return None
    return f"https://www.linkedin.com{path}"


def validate_contact_record(record: dict) -> list[str]:
    failures: list[str] = []
    if record.get("email") and not validate_email(record["email"]):
        failures.append("invalid_email")
    if record.get("phone") and not validate_phone(record["phone"]):
        failures.append("invalid_e164_phone")
    if record.get("linkedin_url") and not canonical_linkedin_url(record["linkedin_url"]):
        failures.append("invalid_linkedin_profile_url")
    if not any(record.get(key) for key in ("email", "phone", "linkedin_url")):
        failures.append("no_contact_channel")
    return failures


@dataclass
class PreflightResult:
    passed: bool
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"pass": self.passed, "failures": self.failures}


def preflight_message(
    content: dict,
    *,
    fixed_subject: str | None = None,
    allowed_asset_hosts: set[str] | None = None,
) -> PreflightResult:
    subject = str(content.get("subject") or "")
    body = str(content.get("body") or "")
    language = str(content.get("language") or "en").lower()
    combined = f"{subject}\n{body}"
    failures: list[str] = []
    if not content.get("to") or not validate_email(str(content.get("to"))):
        failures.append("invalid_or_missing_recipient")
    invalid_cc = [address for address in (content.get("cc") or []) if not validate_email(str(address))]
    if invalid_cc:
        failures.append("invalid_cc_recipient")
    if INTERNAL_MARKER_RE.search(combined):
        failures.append("internal_marker")
    if UNKNOWN_RE.search(combined):
        failures.append("unknown_placeholder")
    if PLACEHOLDER_RE.search(combined):
        failures.append("unresolved_placeholder")
    if "--" in combined:
        failures.append("double_dash")
    if fixed_subject is not None and subject.strip() != fixed_subject.strip():
        failures.append("subject_mismatch")
    # The prototype's highest-impact contamination was Turkish operator text
    # leaking into non-Turkish messages. Full language classification remains
    # a model/evaluation concern; this deterministic guard catches that class.
    if language != "tr" and TURKISH_CHARS_RE.search(combined):
        failures.append("operator_language_contamination")
    if allowed_asset_hosts is not None:
        links = re.findall(r"https?://[^\s<>'\"]+", body)
        if any((urlparse(link).hostname or "").lower() not in allowed_asset_hosts for link in links):
            failures.append("unapproved_link")
    return PreflightResult(not failures, failures)


def is_bounce(sender: str, subject: str) -> bool:
    return bool(BOUNCE_RE.search(f"{sender} {subject}"))

