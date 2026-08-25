"""Verifier for imported candidate corpora.

A corpus is candidate supply, not evidence: `import-candidates` writes names a
campaign may later evaluate, and treating those names as proof of themselves
would turn the verdict system into a rubber stamp.

Two things lift a row above that. A row that arrived with a citation --
`provenance_url` naming a TED award notice, a customer's own export -- is
ordinary evidence with an ordinary source. And a row belonging to a dataset
version whose operator filed an assertion manifest is covered by that
assertion: someone stated, immutably and in advance, that these companies exist
in these markets and buy in this sector. The manifest is the source, the row is
the record, and the locator is an internal `dataset:` reference because there
is no public page to link.

Everything else abstains. A corpus with no citation and no assertion still
needs a real verifier; this makes that visible rather than papering over it.

Contact columns are never read here. `candidate_records` is shared across
tenants and `data` keeps unknown columns verbatim, so a verifier that reached
for an email would publish one into evidence.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

from ..candidates import CandidateRecord
from ..models import (
    DatasetAssertionManifest,
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from ..quotes import spans_for_facts
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


# Which manifest purposes assert enough to speak for a row. A directory says
# "this company exists"; a curated buyer list says "this company buys this".
_ASSERTING_PURPOSES = frozenset({"curated_buyers", "curated_prospects"})

# What a curated-list assertion is worth on each dimension it covers.
_ASSERTED_SECTOR_FIT = 90
_ASSERTED_CHANNEL_FIT = 85
_ASSERTED_MARKET_COVERAGE = 80


class CorpusProvider(CatalogProvider):
    def __init__(self) -> None:
        super().__init__(corpus_definition())

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="Imported corpora need no credential")

    def verify(self, query: DiscoveryQuery, candidate: CandidateRecord) -> VerificationBundle:
        manifest = candidate.assertion_manifest
        if manifest is not None and manifest.purpose in _ASSERTING_PURPOSES:
            return self._asserted(candidate, manifest)
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

        snapshot_content = json.dumps(
            {"source_record_id": candidate.source_record_id, "facts": facts},
            sort_keys=True, ensure_ascii=False,
        )
        payload = snapshot_content.encode()
        source = VerificationSource(
            provenance_url=provenance,
            raw_hash=hashlib.sha256(payload).hexdigest(),
            classification="official" if official else "independent",
            retrieved_via=provenance,
            facts=facts,
            snapshot_content=snapshot_content,
            fact_spans=spans_for_facts(snapshot_content, facts),
        )
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[source],
            independent_source_count=0 if official else 1,
        )

    def research_fields(
        self,
        company: CandidateRecord,
        fields: frozenset[str],
        query: DiscoveryQuery,
    ) -> VerificationBundle:
        del fields
        return self.verify(query, company)

    @staticmethod
    def _asserted(
        candidate: CandidateRecord, manifest: DatasetAssertionManifest
    ) -> VerificationBundle:
        """Normalize one manifest-covered row into the same claims any provider emits.

        Each asserted field is checked explicitly against the row rather than
        assumed: a manifest that claims sector relevance does not make an
        out-of-sector row relevant, and a fact nobody asserted is never emitted.

        The dimension constants are the strength of a curated-list assertion,
        not of its publisher. They sit below what corroborated public evidence
        reaches, so a lead carried only by a curated list still has to clear the
        strong-fit floor on answered weight and confidence.
        """
        asserted = manifest.asserted_fields
        facts: dict[str, list[str | int | float]] = {}
        if "company_identity" in asserted:
            facts["company_name"] = [candidate.company_name]
            facts["country"] = [candidate.country.upper()]
            domain = _normalized_domain(candidate.domain)
            if domain:
                facts["domain"] = [domain]
        if "buyer_membership" in asserted:
            facts["buyer_role"] = ["sector_buyer"]
        row_sectors = {
            str(value).strip().casefold()
            for value in candidate.data.get("categories", [])
            if str(value).strip()
        }
        manifest_scope = {
            str(value).strip().casefold()
            for value in [*manifest.sector_ids, *manifest.product_terms]
            if str(value).strip()
        }
        if "product_sector_relevance" in asserted and row_sectors & manifest_scope:
            facts["product_sector_fit"] = [_ASSERTED_SECTOR_FIT]
        if "buyer_membership" in asserted:
            facts["buyer_channel_fit"] = [_ASSERTED_CHANNEL_FIT]
        if "target_presence" in asserted:
            facts["market_coverage"] = [_ASSERTED_MARKET_COVERAGE]
        if not facts:
            return VerificationBundle(candidate_source_record_id=candidate.source_record_id)

        reference = (
            f"dataset:{candidate.dataset_id}:{candidate.version}:{candidate.source_record_id}"
        )
        # The snapshot is the sanitized row plus the assertion it rests on, and
        # nothing else from `data`. Exact-span validation then holds for every
        # emitted value, so a manifest claim is checkable the same way a quoted
        # web page is.
        snapshot_content = json.dumps(
            {
                "source_reference": reference,
                "assertion": {
                    "purpose": manifest.purpose,
                    "publisher_label": manifest.publisher_label,
                    "asserted_fields": sorted(asserted),
                    "sector_ids": list(manifest.sector_ids),
                    "curated_at": manifest.curated_at,
                    "freshness_unknown": manifest.freshness_unknown,
                    "curation_note": manifest.curation_note,
                    "snapshot_hash": manifest.snapshot_hash,
                },
                "facts": facts,
            },
            sort_keys=True, ensure_ascii=False,
        )
        source = VerificationSource(
            source_reference=reference,
            raw_hash=hashlib.sha256(snapshot_content.encode()).hexdigest(),
            # A curated list is a third party speaking about the company, not
            # the company speaking about itself.
            classification="independent",
            retrieved_via=reference,
            publisher_label=manifest.publisher_label,
            facts=facts,
            snapshot_content=snapshot_content,
            fact_spans=spans_for_facts(snapshot_content, facts),
        )
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[source],
            independent_source_count=1,
        )
