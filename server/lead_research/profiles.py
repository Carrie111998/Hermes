"""Immutable, tenant-scoped company research profile versions."""
from __future__ import annotations

from .models import CompanyProfileVersion, CompanyResearchProfile
from ..db import Database, json_dump, json_load, new_id, now


class ProfileRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _decode(row) -> CompanyProfileVersion | None:
        if row is None:
            return None
        return CompanyProfileVersion(
            id=row["id"],
            company_id=row["company_id"],
            version=row["version"],
            status=row["status"],
            profile=CompanyResearchProfile.model_validate(json_load(row["profile_json"], {})),
            created_by=row["created_by"],
            confirmed_by=row["confirmed_by"],
            created_at=row["created_at"],
            confirmed_at=row["confirmed_at"],
            superseded_at=row["superseded_at"],
        )

    def create_version(
        self,
        company_id: str,
        actor_id: str,
        profile: CompanyResearchProfile,
    ) -> CompanyProfileVersion:
        profile = CompanyResearchProfile.model_validate(profile)
        stamp = now()
        profile_id = new_id("cpv")
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next_version "
                "FROM company_profile_versions WHERE company_id=?",
                (company_id,),
            ).fetchone()
            version = int(row["next_version"])
            tx.execute(
                "UPDATE company_profile_versions SET status='superseded', superseded_at=? "
                "WHERE company_id=? AND status='confirmed'",
                (stamp, company_id),
            )
            tx.execute(
                "INSERT INTO company_profile_versions("
                "id,company_id,version,status,profile_json,created_by,confirmed_by,"
                "created_at,confirmed_at,superseded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_id,
                    company_id,
                    version,
                    "confirmed",
                    json_dump(profile.model_dump(mode="json")),
                    actor_id,
                    actor_id,
                    stamp,
                    stamp,
                    None,
                ),
            )
        created = self.get(company_id, profile_id)
        if created is None:  # pragma: no cover - a committed insert must be readable
            raise RuntimeError("created company profile version could not be loaded")
        return created

    def get(self, company_id: str, profile_id: str) -> CompanyProfileVersion | None:
        return self._decode(
            self.db.one(
                "SELECT * FROM company_profile_versions WHERE company_id=? AND id=?",
                (company_id, profile_id),
            )
        )

    def current(self, company_id: str) -> CompanyProfileVersion | None:
        return self._decode(
            self.db.one(
                "SELECT * FROM company_profile_versions "
                "WHERE company_id=? AND status='confirmed' ORDER BY version DESC LIMIT 1",
                (company_id,),
            )
        )
