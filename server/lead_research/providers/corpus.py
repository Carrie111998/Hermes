"""Verifier for imported candidate corpora.

A corpus is candidate supply, not evidence: `import-candidates` writes names a
campaign may later evaluate, and treating those names as proof of themselves
would turn the verdict system into a rubber stamp.

The one exception is a row that arrived with a citation. `provenance_url` names
the record the row was taken from — a TED award notice, a customer's own export
— and citing that is ordinary evidence with an ordinary source. So this
verifier speaks only for rows that carry one, and abstains on the rest. A
corpus with no provenance still needs a real verifier; this makes that visible
rather than papering over it.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

from ..candidates import CandidateRecord
from ..models import (
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from .base import CatalogProvider
from .bright_data import _normalized_domain


def corpus_definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="customer-list-corpus",
        display_name="Customer list corpus",
        publisher="Customer-supplied company list",
        jurisdiction=["global"],
        categories=["matchmaking"],
        access_tier="customer_upload",
        entity_levels=["named_company"],
        capabilities=["organizations", "candidate_verification"],
        emits=["company_name", "country", "domain", "buyer_role", "product_term"],
        freshness_days=365,
        adapter_mode="live",
        default_enabled=False,
        health="active",
    )


class CorpusProvider(CatalogProvider):
    def __init__(self) -> None:
        super().__init__(corpus_definition())

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="Imported corpora need no credential")

    def verify(self, query: DiscoveryQuery, candidate: CandidateRecord) -> VerificationBundle:
        provenance = str(candidate.data.get("provenance_url") or "")
        parsed = urlparse(provenance)
        if parsed.scheme != "https" or not parsed.hostname:
            # No citation, nothing to say. Deliberately not an error: most rows
            # in a plain contact-list import look like this.
            return VerificationBundle(candidate_source_record_id=candidate.source_record_id)

        candidate_domain = _normalized_domain(candidate.domain)
        provenance_domain = _normalized_domain(provenance)
        official = bool(candidate_domain and provenance_domain == candidate_domain)

        facts: dict[str, list[str]] = {
            "company_name": [candidate.company_name],
            "country": [candidate.country.upper()],
        }
        if candidate_domain:
            facts["domain"] = [candidate_domain]
        buyer_types = [str(v) for v in candidate.data.get("buyer_types", []) if str(v).strip()]
        if buyer_types:
            facts["buyer_role"] = buyer_types
        categories = [str(v) for v in candidate.data.get("categories", []) if str(v).strip()]
        if categories:
            facts["product_term"] = categories

        payload = json.dumps(
            {"source_record_id": candidate.source_record_id, "facts": facts},
            sort_keys=True, ensure_ascii=False,
        ).encode()
        source = VerificationSource(
            provenance_url=provenance,
            raw_hash=hashlib.sha256(payload).hexdigest(),
            classification="official" if official else "independent",
            retrieved_via=provenance,
            facts=facts,
        )
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[source],
            independent_source_count=0 if official else 1,
        )
