"""Evidence-led, reversible organization identity resolution."""
from __future__ import annotations

from ..db import json_dump, new_id, now
from ..quality import normalize_name


class IdentityResolver:
    def __init__(self, db, company_id: str):
        self.db = db
        self.company_id = company_id

    def resolve(self, payload: dict, source_id: str) -> dict:
        identifiers = []
        if payload.get("registry_id"):
            identifiers.append(("registry_id", f"{payload.get('country', '')}:{payload['registry_id']}"))
        if payload.get("domain"):
            identifiers.append(("domain", payload["domain"].lower().removeprefix("www.")))
        for kind, value in identifiers:
            row = self.db.one(
                "SELECT organization_id FROM organization_links WHERE company_id=? AND identifier_type=? AND identifier_value=?",
                (self.company_id, kind, value),
            )
            if row:
                return {"organization_id": row["organization_id"], "created": False, "matched_by": kind}
        organization_id, stamp = new_id("org"), now()
        display = payload.get("display_name") or payload.get("legal_name") or "Unknown organization"
        self.db.execute(
            "INSERT INTO organizations VALUES(?,?,?,?,?,?,?,?,?)",
            (organization_id, self.company_id, display, normalize_name(display), payload.get("domain"),
             payload.get("country"), json_dump(payload), stamp, stamp),
        )
        for kind, value in identifiers:
            self.db.execute(
                "INSERT INTO organization_links VALUES(?,?,?,?,?,?,?,?)",
                (new_id("link"), self.company_id, organization_id, kind, value, source_id, 1, stamp),
            )
        return {"organization_id": organization_id, "created": True, "matched_by": "new"}


    def detach_source(self, source_id: str) -> int:
        return self.db.execute(
            "DELETE FROM organization_links WHERE company_id=? AND source_id=? AND reversible=1",
            (self.company_id, source_id),
        )
