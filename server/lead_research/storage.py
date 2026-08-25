"""Immutable raw snapshots and normalized evidence repository."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Iterable

from ..db import json_dump, json_load, new_id, now
from .models import EvidenceEnvelope, EvidenceSpan, RawPage, VerificationBundle, VerificationSource
from .paths import tenant_research_root
from .quotes import validate_span


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

    @staticmethod
    def query_fingerprint(query) -> str:
        """Identity of the question a bundle answered.

        Evidence facts are not a property of the page alone. A web verifier
        emits `product_term` and `buyer_role` by matching *the campaign's own
        terms* against what it fetched, so the same page yields different facts
        under different terms — and since fit is scored on how many terms
        matched, reusing evidence gathered under other terms would silently
        ignore a config change. Editing a campaign and rerunning it is the
        normal way this system gets tuned, so that cannot be allowed to look
        like a cache hit.

        Target countries are deliberately excluded: extraction keys off the
        candidate's own country, which is fixed by the immutable corpus row, so
        including them would fragment the cache per country for no gain.
        """
        material = json.dumps(
            {
                "sector_ids": sorted(query.sector_ids),
                "hs_codes": sorted(query.hs_codes),
                "buyer_types": sorted(query.buyer_types),
                "search_product_terms": sorted(query.search_product_terms),
            },
            sort_keys=True,
        ).encode()
        return hashlib.sha256(material).hexdigest()[:16]

    def reusable_bundles(
        self, cutoff_by_source: dict[str, float], query_fingerprint: str | set[str]
    ) -> dict[tuple[str, str], VerificationBundle]:
        """Evidence already held for a candidate, still inside its freshness window.

        Verifying one candidate costs three Web Unlocker fetches, and a rerun
        re-fetched every page it had already paid for. Evidence is immutable and
        content-addressed, so the stored rows rebuild the same bundle the
        provider would have returned — same provenance, same hash, same facts,
        therefore the same claims and the same verdict.

        Read once per run rather than once per candidate: this is one query
        against what would otherwise be a query, or a scan, per candidate per
        source.

        `retrieved_at` is deliberately not touched when a bundle is reused —
        `save_evidence` skips a row it already has — so the window expires on
        the age of the evidence rather than on the age of the last run that
        looked at it. Cached evidence that refreshed its own timestamp would
        never be re-fetched again.
        """
        if not cutoff_by_source:
            return {}
        placeholders = ",".join("?" for _ in cutoff_by_source)
        rows = self.db.all(
            "SELECT source_id,source_record_id,provenance_url,raw_hash,payload,retrieved_at "
            f"FROM evidence_records WHERE company_id=? AND withdrawn_at IS NULL "
            f"AND source_id IN ({placeholders}) AND retrieved_at>=? "
            "ORDER BY retrieved_at",
            (self.company_id, *sorted(cutoff_by_source), min(cutoff_by_source.values())),
        )
        # The locator is the identity of a source within a bundle, so a later
        # row for the same URL or dataset reference supersedes an earlier one.
        accepted_fingerprints = (
            {query_fingerprint} if isinstance(query_fingerprint, str) else query_fingerprint
        )
        collected: dict[tuple[str, str], dict[str, VerificationSource]] = {}
        for row in rows:
            if (row["retrieved_at"] or 0.0) < cutoff_by_source[row["source_id"]]:
                continue
            payload = json_load(row["payload"], {})
            if payload.get("query_fingerprint") not in accepted_fingerprints:
                # A different question, or evidence written before fingerprints
                # existed. Either way it cannot stand in for this run's answer.
                continue
            # `source_record_id` is "<candidate>:<provenance hash>"; rsplit so a
            # candidate id containing a colon still round-trips.
            candidate_id = str(row["source_record_id"]).rsplit(":", 1)[0]
            try:
                reference = payload.get("source_reference") or None
                source = VerificationSource(
                    provenance_url=row["provenance_url"] or None,
                    source_reference=reference,
                    raw_hash=row["raw_hash"],
                    classification=payload.get("classification", "independent"),
                    retrieved_via=(
                        payload.get("retrieved_via") or row["provenance_url"] or reference or ""
                    ),
                    facts=payload.get("facts") or {},
                    retrieved_at=row["retrieved_at"],
                    snapshot_content=payload.get("snapshot_content") or "",
                    source_language=payload.get("source_language") or "en",
                    archive_snapshot_at=payload.get("archive_snapshot_at"),
                    fact_spans={
                        field: [EvidenceSpan.model_validate(span) for span in spans]
                        for field, spans in (payload.get("fact_spans") or {}).items()
                    },
                )
            except Exception:
                # One unreadable row must not cost a run its whole cache, and it
                # must not be silently treated as evidence either.
                continue
            collected.setdefault((row["source_id"], candidate_id), {})[
                source.locator
            ] = source
        bundles: dict[tuple[str, str], VerificationBundle] = {}
        for (source_id, candidate_id), sources in collected.items():
            ordered = list(sources.values())
            independent = {
                source.locator for source in ordered
                if source.classification == "independent"
            }
            bundles[(source_id, candidate_id)] = VerificationBundle(
                candidate_source_record_id=candidate_id,
                sources=ordered,
                independent_source_count=len(independent),
            )
        return bundles

    def prepare_verification(
        self,
        bundle: VerificationBundle,
        source_id: str,
        query_fingerprint: str | None = None,
    ) -> list[dict]:
        """Derive immutable evidence identities without writing tenant state."""
        prepared: list[dict] = []
        for source in bundle.sources:
            if (
                not source.snapshot_content
                or hashlib.sha256(source.snapshot_content.encode()).hexdigest() != source.raw_hash
            ):
                continue
            accepted_facts: dict[str, list[str]] = {}
            accepted_spans: dict[str, list[EvidenceSpan]] = {}
            for field, values in source.facts.items():
                spans = source.fact_spans.get(field, [])
                for value in values:
                    for span in spans:
                        validation = validate_span(source.snapshot_content, span)
                        if not validation.valid:
                            continue
                        literal = str(value).strip()
                        if literal.casefold() not in span.original.casefold():
                            continue
                        if field in {"company_name", "registry_id", "domain"}:
                            if literal not in span.original:
                                continue
                        accepted_facts.setdefault(field, []).append(value)
                        accepted_spans.setdefault(field, []).append(span)
                        break
            if not accepted_facts:
                continue
            source = source.model_copy(update={
                "facts": accepted_facts,
                "fact_spans": accepted_spans,
            })
            seed = f"{self.company_id}:{source_id}:{source.raw_hash}".encode()
            snapshot_id = f"snap_{hashlib.sha256(seed).hexdigest()[:20]}"
            evidence_seed = (
                f"{self.company_id}:{source_id}:{bundle.candidate_source_record_id}:"
                f"{source.locator}:{source.raw_hash}"
            ).encode()
            evidence_id = f"ev_{hashlib.sha256(evidence_seed).hexdigest()[:20]}"
            record_seed = hashlib.sha256(source.locator.encode()).hexdigest()[:16]
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
                snapshot_content=source.snapshot_content,
                source_language=source.source_language,
                archive_snapshot_at=source.archive_snapshot_at,
                payload={
                    "facts": source.facts,
                    "classification": source.classification,
                    "retrieved_via": source.retrieved_via,
                    # Kept in the payload rather than in `provenance_url`: that
                    # column is the public link a customer may follow, and an
                    # internal reference is not one.
                    "source_reference": source.source_reference,
                    # What question this answered. Reuse requires a match; see
                    # `query_fingerprint`.
                    "query_fingerprint": query_fingerprint,
                    "fact_spans": {
                        field: [span.model_dump(mode="json") for span in spans]
                        for field, spans in source.fact_spans.items()
                    },
                    "snapshot_content": source.snapshot_content,
                    "source_language": source.source_language,
                    "archive_snapshot_at": (
                        source.archive_snapshot_at.isoformat()
                        if source.archive_snapshot_at else None
                    ),
                },
            )
            prepared.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "source": source,
                "confidence": envelope.confidence,
                "envelope": envelope,
            })
        return prepared

    def save_verification(
        self,
        prepared: list[dict],
        campaign_id: str,
        organization_id: str,
    ) -> list[dict]:
        """Persist an already-derived verification plan after identity resolution."""
        stamp = now()
        for stored in prepared:
            source = stored["source"]
            envelope = stored["envelope"]
            snapshot = self.db.one(
                "SELECT id FROM dataset_snapshots WHERE company_id=? AND source_id=? AND raw_hash=?",
                (self.company_id, stored["source_id"], source.raw_hash),
            )
            if not snapshot:
                self.db.execute(
                    "INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        envelope.snapshot_id,
                        self.company_id,
                        stored["source_id"],
                        campaign_id,
                        "valid",
                        None,
                        source.raw_hash,
                        1,
                        json_dump({
                            "provenance_url": source.provenance_url,
                            "source_reference": source.source_reference,
                            "retrieved_via": source.retrieved_via,
                            "classification": source.classification,
                            "snapshot_content": source.snapshot_content,
                            "source_language": source.source_language,
                            "archive_snapshot_at": (
                                source.archive_snapshot_at.isoformat()
                                if source.archive_snapshot_at else None
                            ),
                        }),
                        stamp,
                    ),
                )
            self.save_evidence([envelope], campaign_id, {
                envelope.source_record_id: organization_id,
            })
        return prepared

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
        result_id: str | None = None,
        created_at: float | None = None,
    ) -> str:
        existing = self.db.one(
            "SELECT id,created_at FROM research_results "
            "WHERE company_id=? AND campaign_id=? AND organization_id=?",
            (self.company_id, campaign_id, organization_id),
        )
        result_id = existing["id"] if existing else result_id or new_id("result")
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
                existing["created_at"] if existing else created_at or stamp,
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
