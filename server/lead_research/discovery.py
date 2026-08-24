"""Union named-company supply without confusing it with fact research."""
from __future__ import annotations

from urllib.parse import urlparse

from ..db import now
from ..quality import normalize_name
from .candidates import CandidateRecord, CandidateRepository, ISO_ALPHA_2
from .models import CandidateSupply, DiscoveryQuery
from .providers.base import CandidateSource


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.") or None


def _identity(candidate: CandidateRecord) -> tuple[str, str, str]:
    if candidate.domain:
        return ("domain", candidate.domain, "")
    return ("name_country", candidate.normalized_name, candidate.country.upper())


class CandidateDiscoveryService:
    def __init__(self, db, registry):
        self.db = db
        self.registry = registry
        self.repository = CandidateRepository(db)

    def supply(
        self,
        company_id: str,
        query: DiscoveryQuery,
        limit: int,
        *,
        exclude: set[tuple[str, str]] | None = None,
        repository: CandidateRepository | None = None,
    ) -> CandidateSupply:
        if limit < 1:
            return CandidateSupply(candidates=[], counts={"indexed_candidates": 0})
        self.registry.ensure_tenant(self.db, company_id, now())
        indexed = (repository or self.repository).select(
            company_id=company_id,
            countries=query.target_countries,
            product_terms=[
                *query.search_product_terms, *query.sector_ids, *query.hs_codes,
            ],
            limit=limit,
            exclude=exclude,
        )
        candidates: list[CandidateRecord] = []
        seen: set[tuple[str, str, str]] = set()
        counts: dict[str, int] = {"indexed_candidates": len(indexed), "duplicates_collapsed": 0}

        def add(candidate: CandidateRecord) -> None:
            key = _identity(candidate)
            if key in seen:
                counts["duplicates_collapsed"] += 1
                return
            seen.add(key)
            candidates.append(candidate)

        for candidate in indexed:
            add(candidate)

        enabled = {
            row["source_id"]
            for row in self.db.all(
                "SELECT source_id FROM dataset_definitions "
                "WHERE company_id=? AND installed=1 AND enabled=1",
                (company_id,),
            )
        }
        for source_id in sorted(enabled):
            if len(candidates) >= limit:
                break
            provider = self.registry.get(source_id)
            if not isinstance(provider, CandidateSource):
                continue
            health = provider.health()
            if health.status not in {"active", "degraded"}:
                continue
            page = provider.discover_candidates(
                query.model_copy(update={"max_records": limit - len(candidates)}),
                cursor=None,
            )
            supplied = 0
            for record in page.records:
                payload = record.payload
                name = str(
                    payload.get("company_name")
                    or payload.get("display_name")
                    or payload.get("legal_name")
                    or ""
                ).strip()
                country = str(payload.get("country") or "").strip().upper()
                if not name or country not in ISO_ALPHA_2:
                    continue
                before = len(candidates)
                add(CandidateRecord(
                    dataset_id=f"source:{source_id}",
                    version=page.snapshot.snapshot_id,
                    source_record_id=record.source_record_id,
                    company_name=name,
                    normalized_name=normalize_name(name),
                    country=country,
                    domain=_domain(payload.get("domain") or payload.get("website")),
                    data={**payload, "discovery_source_id": source_id},
                ))
                if len(candidates) > before:
                    supplied += 1
                if len(candidates) >= limit:
                    break
            counts[f"{source_id}_discovered"] = supplied
        return CandidateSupply(candidates=candidates[:limit], counts=counts)
