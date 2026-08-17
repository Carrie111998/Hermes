"""Evidence-led, reversible organization identity resolution."""
from __future__ import annotations

from urllib.parse import urlparse

from ..db import json_dump, json_load, new_id, now
from ..quality import normalize_name


def _nonblank(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _normalize_domain(value) -> str | None:
    if not _nonblank(value):
        return None
    raw = str(value).strip().casefold()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or raw.split("/", 1)[0]).rstrip(".").removeprefix("www.")


class IdentityResolver:
    def __init__(self, db, company_id: str):
        self.db = db
        self.company_id = company_id

    @staticmethod
    def _identifiers(payload: dict, fallback_country: str | None = None) -> list[tuple[str, str]]:
        identifiers = []
        if _nonblank(payload.get("registry_id")):
            country = payload.get("country") or fallback_country or ""
            identifiers.append(("registry_id", f"{country}:{payload['registry_id']}"))
        domain = _normalize_domain(payload.get("domain"))
        if domain:
            identifiers.append(("domain", domain))
        return identifiers

    def _upsert_verified_links(
        self,
        organization_id: str,
        identifiers: list[tuple[str, str]],
        source_id: str,
        stamp: float,
    ) -> None:
        for kind, value in identifiers:
            linked = self.db.one(
                "SELECT organization_id FROM organization_links "
                "WHERE company_id=? AND identifier_type=? AND identifier_value=?",
                (self.company_id, kind, value),
            )
            if linked and linked["organization_id"] != organization_id:
                continue
            if linked:
                self.db.execute(
                    "UPDATE organization_links SET source_id=?,reversible=1 "
                    "WHERE company_id=? AND identifier_type=? AND identifier_value=?",
                    (source_id, self.company_id, kind, value),
                )
            else:
                self.db.execute(
                    "INSERT INTO organization_links VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("link"), self.company_id, organization_id, kind,
                        value, source_id, 1, stamp,
                    ),
                )

    def _refresh_match(
        self,
        organization_id: str,
        payload: dict,
        source_id: str,
        matched_by: str,
    ) -> dict:
        row = self.db.one(
            "SELECT * FROM organizations WHERE id=? AND company_id=?",
            (organization_id, self.company_id),
        )
        if not row:
            raise RuntimeError("matched organization is missing")
        verified = {key: value for key, value in payload.items() if _nonblank(value)}
        verified_domain = _normalize_domain(verified.get("domain"))
        if verified_domain:
            verified["domain"] = verified_domain
        display = (
            verified.get("display_name") or verified.get("legal_name") or row["display_name"]
        )
        domain = verified.get("domain") or row["domain"]
        country = verified.get("country") or row["country"]
        data = {**json_load(row["data"], {}), **verified}
        stamp = now()
        self.db.execute(
            "UPDATE organizations SET display_name=?,normalized_name=?,domain=?,country=?,data=?,updated_at=? "
            "WHERE id=? AND company_id=?",
            (
                display, normalize_name(display), domain, country, json_dump(data), stamp,
                organization_id, self.company_id,
            ),
        )
        self._upsert_verified_links(
            organization_id,
            self._identifiers(verified, fallback_country=country),
            source_id,
            stamp,
        )
        return {"organization_id": organization_id, "created": False, "matched_by": matched_by}

    def resolve(self, payload: dict, source_id: str, matching_hints: dict | None = None) -> dict:
        verified_payload = dict(payload)
        verified_domain = _normalize_domain(verified_payload.get("domain"))
        if verified_domain:
            verified_payload["domain"] = verified_domain
        identifiers = self._identifiers(verified_payload)
        for kind, value in identifiers:
            row = self.db.one(
                "SELECT organization_id FROM organization_links WHERE company_id=? AND identifier_type=? AND identifier_value=?",
                (self.company_id, kind, value),
            )
            if row:
                return self._refresh_match(
                    row["organization_id"], verified_payload, source_id, kind,
                )
        # Candidate-corpus fields may locate an identity already established by
        # evidence, but never create a new organization link or stored fact.
        matching_hints = matching_hints or {}
        hint_domain = _normalize_domain(matching_hints.get("domain"))
        if hint_domain:
            row = self.db.one(
                "SELECT organization_id FROM organization_links "
                "WHERE company_id=? AND identifier_type='domain' AND identifier_value=?",
                (self.company_id, hint_domain),
            )
            if row:
                return self._refresh_match(
                    row["organization_id"], verified_payload, source_id, "domain_hint",
                )
        organization_id, stamp = new_id("org"), now()
        display = (
            verified_payload.get("display_name")
            or verified_payload.get("legal_name")
            or "Unknown organization"
        )
        self.db.execute(
            "INSERT INTO organizations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                organization_id, self.company_id, display, normalize_name(display),
                verified_payload.get("domain"), verified_payload.get("country"),
                json_dump(verified_payload), stamp, stamp,
            ),
        )
        self._upsert_verified_links(organization_id, identifiers, source_id, stamp)
        return {"organization_id": organization_id, "created": True, "matched_by": "new"}


    def detach_source(self, source_id: str) -> int:
        return self.db.execute(
            "DELETE FROM organization_links WHERE company_id=? AND source_id=? AND reversible=1",
            (self.company_id, source_id),
        )
