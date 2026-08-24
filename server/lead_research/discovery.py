"""Union named-company supply without confusing it with fact research."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from ..db import now
from ..quality import normalize_name
from .acquisition import CandidateMetadataCheapVerifier
from .candidates import (
    CandidateRecord, CandidateRepository, ISO_ALPHA_2, matches_term, search_text,
    searchable_term,
)
from .facts import FactRepository
from .metrics import count_cheap_gate
from .models import CandidateSupply, DiscoveryQuery
from .providers.base import CandidateSource


GateReason = Literal[
    "shared_relevance",
    "corpus_term",
    "cheap_verification",
    "excluded_by_range",
    "cheap_verification_no_scope_signal",
]


@dataclass(frozen=True)
class ResearchScope:
    product_terms: list[str]
    sector_ids: list[str] = field(default_factory=list)
    hs_codes: list[str] = field(default_factory=list)

    @property
    def all_terms(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in [*self.product_terms, *self.sector_ids, *self.hs_codes]:
            normalized = searchable_term(value)
            if normalized and normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result


@dataclass(frozen=True)
class CheapGateDecision:
    passed: bool
    reason: GateReason
    evidence_ids: list[str] = field(default_factory=list)
    requests: int = 0


def explicit_range_exclusion(candidate: CandidateRecord, scope: ResearchScope) -> bool:
    ranges = [
        searchable_term(value)
        for value in candidate.data.get("explicit_product_ranges", [])
        if str(value).strip()
    ]
    if not ranges:
        return False
    return not any(
        matches_term(term, product_range) or matches_term(product_range, term)
        for product_range in ranges
        for term in scope.all_terms
    )


class CheapGate:
    def __init__(self, facts, cheap_verifier):
        self.facts = facts
        self.cheap_verifier = cheap_verifier

    def evaluate(
        self, company_id: str, candidate: CandidateRecord, scope: ResearchScope,
    ) -> CheapGateDecision:
        shared = self.facts.relevance(
            company_id, candidate, scope.all_terms, at=now(),
        )
        if shared:
            return CheapGateDecision(True, "shared_relevance", list(shared))
        candidate_text = search_text(candidate.normalized_name, candidate.data)
        if any(matches_term(term, candidate_text) for term in scope.all_terms):
            return CheapGateDecision(True, "corpus_term")
        if explicit_range_exclusion(candidate, scope):
            return CheapGateDecision(False, "excluded_by_range")
        verified = self.cheap_verifier.verify(candidate, scope.all_terms)
        return CheapGateDecision(
            passed=verified.matched,
            reason=(
                "cheap_verification"
                if verified.matched
                else "cheap_verification_no_scope_signal"
            ),
            evidence_ids=verified.evidence_ids,
            requests=verified.requests,
        )


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
    def __init__(self, db, registry, *, gate: CheapGate | None = None):
        self.db = db
        self.registry = registry
        self.repository = CandidateRepository(db)
        self.gate = gate or CheapGate(
            FactRepository(db), CandidateMetadataCheapVerifier(),
        )

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
        scope = ResearchScope(
            product_terms=query.search_product_terms,
            sector_ids=query.sector_ids,
            hs_codes=query.hs_codes,
        )
        candidate_repository = repository or self.repository
        # Alternate repositories used for bounded refresh/import views may
        # implement the long-standing select() boundary only. They are already
        # term-filtered, so falling back preserves their semantics while the
        # built-in repository supplies an additional bounded gate fallback.
        selector = getattr(candidate_repository, "select_for_gate", None)
        if not callable(selector):
            selector = candidate_repository.select
        indexed = selector(
            company_id=company_id,
            countries=query.target_countries,
            product_terms=scope.all_terms,
            limit=limit,
            exclude=exclude,
        )
        candidates: list[CandidateRecord] = []
        seen: set[tuple[str, str, str]] = set()
        counts: dict[str, int] = {
            "indexed_candidates": len(indexed),
            "duplicates_collapsed": 0,
            "supplied": 0,
            "passed_cheap_gate": 0,
            "cheap_verification_requests": 0,
            "candidate_discovery_requests": 0,
        }

        def add(candidate: CandidateRecord) -> bool:
            key = _identity(candidate)
            if key in seen:
                counts["duplicates_collapsed"] += 1
                return False
            seen.add(key)
            counts["supplied"] += 1
            decision = self.gate.evaluate(company_id, candidate, scope)
            count_cheap_gate(counts, decision)
            if decision.passed:
                candidates.append(candidate)
            return True

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
            counts["candidate_discovery_requests"] += page.requests
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
                unique = add(CandidateRecord(
                    dataset_id=f"source:{source_id}",
                    version=page.snapshot.snapshot_id,
                    source_record_id=record.source_record_id,
                    company_name=name,
                    normalized_name=normalize_name(name),
                    country=country,
                    domain=_domain(payload.get("domain") or payload.get("website")),
                    data={
                        **payload,
                        "discovery_source_id": source_id,
                        "cheap_verification": payload.get("cheap_verification", True),
                        "cheap_verification_evidence_ids": payload.get(
                            "cheap_verification_evidence_ids",
                            [f"discovery:{source_id}:{record.source_record_id}"],
                        ),
                    },
                ))
                if unique:
                    supplied += 1
                if len(candidates) >= limit:
                    break
            counts[f"{source_id}_discovered"] = supplied
        return CandidateSupply(candidates=candidates[:limit], counts=counts)
