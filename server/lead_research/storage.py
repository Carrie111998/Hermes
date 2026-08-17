"""Immutable raw snapshots and normalized evidence repository."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Iterable

from ..db import json_dump, json_load, new_id, now
from .models import EvidenceEnvelope, RawPage, VerificationBundle
from .paths import tenant_research_root


class EvidenceRepository:
    def __init__(self, db, company_id: str):
        self.db = db
        self.company_id = company_id

    def for_company(self, company_id: str) -> "EvidenceRepository":
        return EvidenceRepository(self.db, company_id)

    def save_snapshot(self, page: RawPage, campaign_id: str | None = None) -> dict:
        payload = "\n".join(
            json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            for record in page.records
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        existing = self.db.one(
            "SELECT * FROM dataset_snapshots WHERE company_id=? AND source_id=? AND raw_hash=?",
            (self.company_id, page.snapshot.source_id, digest),
        )
        if existing:
            return dict(existing)
        snapshot_id = page.snapshot.snapshot_id or new_id("snap")
        root = tenant_research_root(self.company_id) / "raw" / page.snapshot.source_id / snapshot_id
        root.mkdir(parents=True, exist_ok=True)
        target = root / "page-000001.jsonl.gz"
        temporary = target.with_suffix(target.suffix + ".tmp")
        with gzip.open(temporary, "wb") as handle:
            handle.write(payload)
        temporary.replace(target)
        stamp = now()
        self.db.execute(
            "INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, self.company_id, page.snapshot.source_id, campaign_id, "valid", str(target),
             digest, len(page.records), json_dump({"source_reported_total": page.source_reported_total}), stamp),
        )
        return dict(self.db.one("SELECT * FROM dataset_snapshots WHERE id=?", (snapshot_id,)))

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM dataset_snapshots WHERE id=? AND company_id=?", (snapshot_id, self.company_id)
        )
        return dict(row) if row else None

    def save_evidence(
        self, items: Iterable[EvidenceEnvelope], campaign_id: str | None = None,
        organization_ids: dict[str, str] | None = None,
    ) -> int:
        organization_ids = organization_ids or {}
        saved = 0
        for item in items:
            exists = self.db.one(
                "SELECT id FROM evidence_records WHERE company_id=? AND source_id=? AND source_record_id=? "
                "AND snapshot_id=? AND raw_hash=?",
                (self.company_id, item.source_id, item.source_record_id, item.snapshot_id, item.raw_hash),
            )
            if exists:
                continue
            payload = item.payload
            organization_id = organization_ids.get(item.source_record_id) or payload.get("organization_id")
            saved += max(0, self.db.execute(
                "INSERT INTO evidence_records "
                "(id,company_id,campaign_id,organization_id,source_id,source_record_id,snapshot_id,record_type,"
                "payload,provenance_url,raw_hash,method,confidence,observed_at,retrieved_at,withdrawn_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (item.evidence_id, self.company_id, campaign_id, organization_id, item.source_id,
                 item.source_record_id, item.snapshot_id, item.record_type, json_dump(payload),
                 item.provenance_url, item.raw_hash, item.method, item.confidence,
                 item.observed_at.timestamp() if item.observed_at else None, item.retrieved_at.timestamp()),
            ))
        return saved

    def save_verification(
        self,
        bundle: VerificationBundle,
        source_id: str,
        campaign_id: str,
        organization_id: str,
    ) -> list[dict]:
        """Persist cited verification hashes as snapshots and normalized evidence."""
        stored: list[dict] = []
        stamp = now()
        for source in bundle.sources:
            snapshot = self.db.one(
                "SELECT id FROM dataset_snapshots WHERE company_id=? AND source_id=? AND raw_hash=?",
                (self.company_id, source_id, source.raw_hash),
            )
            if snapshot:
                snapshot_id = snapshot["id"]
            else:
                seed = f"{self.company_id}:{source_id}:{source.raw_hash}".encode()
                snapshot_id = f"snap_{hashlib.sha256(seed).hexdigest()[:20]}"
                self.db.execute(
                    "INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        self.company_id,
                        source_id,
                        campaign_id,
                        "valid",
                        None,
                        source.raw_hash,
                        1,
                        json_dump({
                            "provenance_url": source.provenance_url,
                            "retrieved_via": source.retrieved_via,
                            "classification": source.classification,
                        }),
                        stamp,
                    ),
                )
            evidence_seed = (
                f"{self.company_id}:{source_id}:{bundle.candidate_source_record_id}:"
                f"{source.provenance_url}:{source.raw_hash}"
            ).encode()
            evidence_id = f"ev_{hashlib.sha256(evidence_seed).hexdigest()[:20]}"
            record_seed = hashlib.sha256(source.provenance_url.encode()).hexdigest()[:16]
            envelope = EvidenceEnvelope(
                evidence_id=evidence_id,
                source_id=source_id,
                source_record_id=f"{bundle.candidate_source_record_id}:{record_seed}",
                snapshot_id=snapshot_id,
                record_type="company_signal",
                provenance_url=source.provenance_url,
                raw_hash=source.raw_hash,
                method="observed",
                confidence=.95 if source.classification == "official" else .85,
                payload={
                    "facts": source.facts,
                    "classification": source.classification,
                    "retrieved_via": source.retrieved_via,
                },
            )
            self.save_evidence([envelope], campaign_id, {
                envelope.source_record_id: organization_id,
            })
            stored.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "source": source,
                "confidence": envelope.confidence,
            })
        return stored

    def upsert_result(
        self,
        *,
        campaign_id: str,
        organization_id: str,
        lead_id: str | None,
        verdict: str,
        fit_score: int,
        evidence_confidence: float,
        data: dict,
    ) -> str:
        existing = self.db.one(
            "SELECT id,created_at FROM research_results "
            "WHERE company_id=? AND campaign_id=? AND organization_id=?",
            (self.company_id, campaign_id, organization_id),
        )
        result_id = existing["id"] if existing else new_id("result")
        stamp = now()
        self.db.execute(
            "INSERT INTO research_results("
            "id,company_id,campaign_id,organization_id,lead_id,verdict,fit_score,"
            "evidence_confidence,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id,campaign_id,organization_id) DO UPDATE SET "
            "lead_id=excluded.lead_id,verdict=excluded.verdict,fit_score=excluded.fit_score,"
            "evidence_confidence=excluded.evidence_confidence,data=excluded.data,updated_at=excluded.updated_at",
            (
                result_id,
                self.company_id,
                campaign_id,
                organization_id,
                lead_id,
                verdict,
                fit_score,
                evidence_confidence,
                json_dump(data),
                existing["created_at"] if existing else stamp,
                stamp,
            ),
        )
        return result_id

    def impact(self, source_id: str) -> dict:
        def count(table: str, where: str, params: tuple) -> int:
            return int(self.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"])
        evidence = count(
            "evidence_records", "company_id=? AND source_id=? AND withdrawn_at IS NULL",
            (self.company_id, source_id),
        )
        organizations = int(self.db.one(
            "SELECT COUNT(DISTINCT organization_id) AS n FROM evidence_records "
            "WHERE company_id=? AND source_id=? AND withdrawn_at IS NULL AND organization_id IS NOT NULL",
            (self.company_id, source_id),
        )["n"])
        campaigns = int(self.db.one(
            "SELECT COUNT(DISTINCT campaign_id) AS n FROM evidence_records "
            "WHERE company_id=? AND source_id=? AND withdrawn_at IS NULL",
            (self.company_id, source_id),
        )["n"])
        claims = sum(
            1 for row in self.db.all("SELECT data FROM feature_claims WHERE company_id=?", (self.company_id,))
            if source_id in json_load(row["data"], {}).get("source_ids", [])
        )
        bytes_used = 0
        for row in self.db.all(
            "SELECT path FROM dataset_snapshots WHERE company_id=? AND source_id=?", (self.company_id, source_id)
        ):
            path = Path(row["path"]) if row["path"] else None
            if path and path.exists():
                bytes_used += path.stat().st_size
        return {
            "source_id": source_id, "campaigns": campaigns, "organizations": organizations,
            "claims": claims, "evidence_records": evidence, "leads_may_lose_qualification": organizations,
            "storage_bytes": bytes_used,
        }

    def withdraw_source(self, source_id: str, purge: bool = False) -> dict:
        impact = self.impact(source_id)
        stamp = now()
        affected_organizations = {
            row["organization_id"] for row in self.db.all(
                "SELECT DISTINCT organization_id FROM evidence_records "
                "WHERE company_id=? AND source_id=? AND withdrawn_at IS NULL AND organization_id IS NOT NULL",
                (self.company_id, source_id),
            )
        }
        self.db.execute(
            "UPDATE evidence_records SET withdrawn_at=? WHERE company_id=? AND source_id=? AND withdrawn_at IS NULL",
            (stamp, self.company_id, source_id),
        )
        if purge:
            for row in self.db.all(
                "SELECT path FROM dataset_snapshots WHERE company_id=? AND source_id=?", (self.company_id, source_id)
            ):
                path = Path(row["path"]) if row["path"] else None
                if path and path.exists():
                    path.unlink(missing_ok=True)
            self.db.execute(
                "DELETE FROM dataset_snapshots WHERE company_id=? AND source_id=?", (self.company_id, source_id)
            )
        for row in self.db.all("SELECT id,data FROM feature_claims WHERE company_id=?", (self.company_id,)):
            if source_id in json_load(row["data"], {}).get("source_ids", []):
                self.db.execute("DELETE FROM feature_claims WHERE id=? AND company_id=?", (row["id"], self.company_id))
        for organization_id in affected_organizations:
            active = int(self.db.one(
                "SELECT COUNT(*) AS n FROM evidence_records WHERE company_id=? AND organization_id=? "
                "AND withdrawn_at IS NULL", (self.company_id, organization_id),
            )["n"])
            for lead in self.db.all("SELECT id,data FROM leads WHERE company_id=?", (self.company_id,)):
                data = json_load(lead["data"], {})
                if data.get("organization_id") != organization_id:
                    continue
                data["source_ids"] = [item for item in data.get("source_ids", []) if item != source_id]
                if not active:
                    data["qualification_state"] = "unqualified_after_source_removal"
                    data["evidence_confidence"] = 0
                    self.db.execute(
                        "UPDATE leads SET status='unqualified_after_source_removal',data=?,updated_at=? "
                        "WHERE id=? AND company_id=?",
                        (json_dump(data), stamp, lead["id"], self.company_id),
                    )
                else:
                    self.db.execute(
                        "UPDATE leads SET data=?,updated_at=? WHERE id=? AND company_id=?",
                        (json_dump(data), stamp, lead["id"], self.company_id),
                    )
        return impact
