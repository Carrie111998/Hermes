"""Bright Data Web Unlocker candidate verifier.

This is an application service adapter, not a model tool.  It retrieves a
small, deterministic set of pages through the existing ``httpx`` dependency
and returns only citations and facts actually present in those pages.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ...quality import normalize_name
from ..candidates import CandidateRecord
from ..models import (
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from .base import CatalogProvider


UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
MAX_SEARCH_PAGES = 3
MAX_RESULTS_PER_PAGE = 5
MAX_TERM_LENGTH = 80
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https://[^\s)]+)\)")


def bright_data_definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="brightdata-web",
        display_name="Bright Data Web Unlocker",
        publisher="Bright Data",
        jurisdiction=["global"],
        categories=["web", "verification"],
        homepage="https://brightdata.com/products/web-unlocker",
        access_tier="credentialed_public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification", "web_evidence"],
        emits=[
            "company_name", "country", "domain", "buyer_role", "product_term",
            "lifecycle_status",
        ],
        freshness_days=30,
        adapter_mode="credential_required",
        default_enabled=False,
        health="active",
    )


def _clean_terms(values) -> list[str]:
    terms: list[str] = []
    normalized_terms: set[str] = set()
    for value in values:
        text = str(value).strip()[:MAX_TERM_LENGTH]
        normalized = text.casefold()
        if text and normalized not in normalized_terms:
            terms.append(text)
            normalized_terms.add(normalized)
    return terms[:4]


def _normalized_domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if not parsed.hostname:
        return None
    return parsed.hostname.casefold().rstrip(".").removeprefix("www.")


def _is_official(provenance_url: str, candidate_domain: str | None) -> bool:
    provenance_domain = _normalized_domain(provenance_url)
    return bool(
        candidate_domain
        and provenance_domain
        and (
            provenance_domain == candidate_domain
            or provenance_domain.endswith(f".{candidate_domain}")
        )
    )


# Phrases that mean the business itself has stopped, not that a page, branch or
# job posting closed. Every one is multi-word on purpose: bare "closed" appears
# on ordinary pages ("closed on Sundays", "closed a funding round") and a false
# positive here removes a live company from every future run.
CLOSURE_PHRASES = (
    "permanently closed",
    "closed permanently",
    "no longer in business",
    "no longer operating",
    "no longer trading",
    "out of business",
    "ceased operations",
    "ceased trading",
    "ceased its operations",
    "went out of business",
    "in liquidation",
    "under liquidation",
    "has been dissolved",
    "was dissolved",
    "company is dissolved",
    "declared bankruptcy",
    "filed for bankruptcy",
)


def _closure_signal(normalized: str) -> bool:
    return any(phrase in normalized for phrase in CLOSURE_PHRASES)


def _fact_matches(
    text: str,
    candidate: CandidateRecord,
    buyer_terms: list[str],
    product_terms: list[str],
    classification: str,
) -> dict[str, list[str]]:
    normalized = normalize_name(text)
    aliases = candidate.data.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    identity_terms = [
        candidate.normalized_name,
        *[normalize_name(str(alias)) for alias in aliases],
    ]
    identity_matched = any(term and term in normalized for term in identity_terms)
    if classification == "independent" and not identity_matched:
        return {}
    facts: dict[str, list[str]] = {}
    if identity_matched:
        facts["company_name"] = [candidate.company_name]
    country = candidate.country.strip().upper()
    if re.search(rf"(?<![a-z]){re.escape(country.casefold())}(?![a-z])", normalized):
        facts["country"] = [country]
    matched_buyers = [
        term for term in buyer_terms
        if " ".join(term.casefold().replace("-", " ").split()) in normalized
    ]
    if matched_buyers:
        facts["buyer_role"] = matched_buyers
    matched_products = [
        term for term in product_terms
        if " ".join(term.casefold().replace("-", " ").split()) in normalized
    ]
    if matched_products:
        facts["product_term"] = matched_products
    if classification == "official" and candidate.domain:
        facts["domain"] = [candidate.domain]
    # Gated on identity: a closure phrase about some other company in the same
    # snippet must never retire this candidate.
    if identity_matched and _closure_signal(normalized):
        facts["lifecycle_status"] = ["closed"]
    return facts


def _result_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    domain = parsed.hostname.casefold().removeprefix("www.")
    if domain == "google.com" or domain.endswith(".google.com"):
        redirect = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
        if not redirect:
            return None
        return _result_url(redirect[0])
    return url


class BrightDataVerifier(CatalogProvider):
    def __init__(
        self,
        api_key: str,
        zone: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Bright Data API key is required")
        if not zone.strip():
            raise ValueError("Bright Data Web Unlocker zone is required")
        super().__init__(bright_data_definition())
        self._api_key = api_key
        self.zone = zone
        self.client = client or httpx.Client()

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="Candidate verifier is configured")

    def _fetch_markdown(self, url: str) -> str:
        response = self.client.post(
            UNLOCKER_ENDPOINT,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "url": url,
                "zone": self.zone,
                "format": "raw",
                "data_format": "markdown",
            },
            timeout=45.0,
        )
        response.raise_for_status()
        return response.text

    def _search_urls(
        self,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
        buyer_terms: list[str],
        product_terms: list[str],
    ) -> list[str]:
        country = candidate.country or (query.target_countries[0] if query.target_countries else "")
        groups = [
            [candidate.company_name, country],
            [candidate.company_name, *buyer_terms],
            [candidate.company_name, *product_terms],
        ]
        urls: list[str] = []
        seen_searches: set[str] = set()
        for terms in groups:
            search = " ".join(_clean_terms(terms))
            normalized = search.casefold()
            if not search or normalized in seen_searches:
                continue
            urls.append(f"https://www.google.com/search?{urlencode({'q': search})}")
            seen_searches.add(normalized)
        return urls[:MAX_SEARCH_PAGES]

    def verify(
        self,
        query: DiscoveryQuery,
        candidate: CandidateRecord,
    ) -> VerificationBundle:
        candidate_domain = _normalized_domain(candidate.domain)
        buyer_terms = _clean_terms([
            *query.buyer_types,
            *candidate.data.get("buyer_types", []),
        ])
        product_terms = _clean_terms([
            *query.sector_ids,
            *query.hs_codes,
            *candidate.data.get("categories", []),
        ])
        sources: list[VerificationSource] = []
        seen_urls: set[str] = set()

        if candidate_domain:
            official_url = f"https://{candidate_domain}"
            markdown = self._fetch_markdown(official_url)
            if markdown.strip():
                sources.append(self._source(
                    official_url,
                    official_url,
                    markdown,
                    markdown,
                    candidate,
                    buyer_terms,
                    product_terms,
                ))
                seen_urls.add(official_url)

        for search_url in self._search_urls(query, candidate, buyer_terms, product_terms):
            markdown = self._fetch_markdown(search_url)
            digest = hashlib.sha256(markdown.encode()).hexdigest()
            matches = list(MARKDOWN_LINK.finditer(markdown))
            for index, match in enumerate(matches[:MAX_RESULTS_PER_PAGE]):
                provenance_url = _result_url(match.group(2))
                if not provenance_url or provenance_url in seen_urls:
                    continue
                entry_end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(markdown)
                )
                evidence_text = markdown[match.start():entry_end]
                classification = (
                    "official" if _is_official(provenance_url, candidate_domain) else "independent"
                )
                facts = _fact_matches(
                    evidence_text,
                    candidate,
                    buyer_terms,
                    product_terms,
                    classification,
                )
                if not facts:
                    continue
                sources.append(VerificationSource(
                    provenance_url=provenance_url,
                    raw_hash=digest,
                    classification=classification,
                    retrieved_via=search_url,
                    facts=facts,
                ))
                seen_urls.add(provenance_url)

        independent_domains = {
            _normalized_domain(source.provenance_url)
            for source in sources
            if source.classification == "independent"
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=sources,
            independent_source_count=len(independent_domains - {None}),
        )

    @staticmethod
    def _source(
        provenance_url: str,
        retrieved_via: str,
        markdown: str,
        evidence_text: str,
        candidate: CandidateRecord,
        buyer_terms: list[str],
        product_terms: list[str],
    ) -> VerificationSource:
        classification = (
            "official"
            if _is_official(provenance_url, _normalized_domain(candidate.domain))
            else "independent"
        )
        return VerificationSource(
            provenance_url=provenance_url,
            raw_hash=hashlib.sha256(markdown.encode()).hexdigest(),
            classification=classification,
            retrieved_via=retrieved_via,
            facts=_fact_matches(
                evidence_text,
                candidate,
                buyer_terms,
                product_terms,
                classification,
            ),
        )
