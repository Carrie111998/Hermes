"""Tenders Electronic Daily candidate verifier.

TED publishes EU public-procurement notices. The eForms-era ones carry a
structured award block — winner name, country, city, website — which is exactly
the citation shape :class:`VerificationSource` wants, and the API answers
without a key.

What this proves about a candidate is narrow and worth stating: that it won, or
bid for, an EU public contract. That is a strong buying signal where it exists
and simply absent where it does not, so this verifier is a complement to a web
verifier, never a replacement.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

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
from .bright_data import _clean_terms, _normalized_domain


SEARCH_ENDPOINT = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{}"
# One search per candidate. TED rate-limits hard (nginx 429) and a campaign
# walks thousands of candidates, so this stays deliberately small.
MAX_NOTICES = 5
RETRY_PAUSE_SECONDS = 5.0

FIELDS = [
    "publication-number",
    "notice-title",
    "winner-name",
    "winner-country",
    "winner-city",
    "winner-internet-address",
    "buyer-name",
    "buyer-country",
    "publication-date",
]

# TED reports countries as ISO alpha-3; candidates carry alpha-2. Only the
# codes TED actually emits are listed — EU, EEA, and the neighbours that show
# up as winners. An unmapped code yields no country fact rather than a wrong
# one, which is the whole reason this is a lookup and not a prefix rule (SK is
# SVK, SE is SWE, AE is ARE — no prefix rule survives contact with those).
ALPHA3_TO_ALPHA2 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY",
    "CZE": "CZ", "DNK": "DK", "EST": "EE", "FIN": "FI", "FRA": "FR",
    "DEU": "DE", "GRC": "GR", "HUN": "HU", "IRL": "IE", "ITA": "IT",
    "LVA": "LV", "LTU": "LT", "LUX": "LU", "MLT": "MT", "NLD": "NL",
    "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK", "SVN": "SI",
    "ESP": "ES", "SWE": "SE", "ISL": "IS", "LIE": "LI", "NOR": "NO",
    "CHE": "CH", "GBR": "GB", "TUR": "TR", "SRB": "RS", "MNE": "ME",
    "MKD": "MK", "ALB": "AL", "BIH": "BA", "UKR": "UA", "MDA": "MD",
    "USA": "US", "CHN": "CN", "IND": "IN", "JPN": "JP", "KOR": "KR",
    "ARE": "AE", "SAU": "SA", "ISR": "IL", "CAN": "CA", "AUS": "AU",
}

# The expert query language treats these as syntax. A candidate called
# `ISP / Asia 4 Y` or `BEGA, s.r.o.` would otherwise produce a 400, not a miss.
QUERY_UNSAFE = re.compile(r'["()\[\]{}~!=<>&|*?:/\\,]+')


def ted_definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="ted",
        display_name="Tenders Electronic Daily",
        publisher="Publications Office of the European Union",
        jurisdiction=["EU"],
        categories=["procurement"],
        homepage="https://ted.europa.eu/",
        access_tier="public",
        entity_levels=["named_company", "opportunity"],
        capabilities=[
            "procurement", "organizations", "candidate_verification",
            # The publisher is the EU's Publications Office; see the catalog entry.
            "authoritative_registry",
        ],
        emits=["company_name", "country", "domain", "buyer_role", "product_term"],
        freshness_days=7,
        adapter_mode="live",
        default_enabled=False,
        health="active",
    )


def _search_term(company_name: str) -> str:
    """A quoted term TED's parser accepts, or empty if nothing usable is left."""
    cleaned = QUERY_UNSAFE.sub(" ", company_name)
    return " ".join(cleaned.split())[:80].strip()


def _names(value) -> list[str]:
    """TED returns names as {lang: [name, ...]}, repeated once per lot."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, dict):
        out: list[str] = []
        for entry in value.values():
            out.extend(_names(entry))
        return list(dict.fromkeys(out))
    return []


def _title(value) -> str:
    if isinstance(value, dict):
        return str(value.get("eng") or next(iter(value.values()), "") or "")
    return str(value or "")


# Legal-form and filing noise. Stripped from both sides before comparing, so
# "BEGA, s.r.o." still matches "BEGA" without the suffix padding the score.
# Passed through normalize_name so the list can be written the way these words
# are actually spelled. It half-folds diacritics — SPÓŁKA becomes "społka", not
# "spolka" — and hand-guessing that is how "spółka" silently stopped matching.
LEGAL_TOKENS = frozenset(normalize_name(token) for token in """
sp spol spolka spółka spolecnost společnost sro akciova akcyjna komandytowa
ograniczona ograniczoną odpowiedzialnoscia odpowiedzialnością doo dooel
gmbh mbh kg ohg ag eg ug ltd limited plc llc lllp inc incorporated corp
corporation company cie cia sarl sas sasu sa srl spa sl slu sau sociedad
limitada anonima anonyme bv nv ab asa aps oyj kft zrt nyrt ead ood eood
sh shpk tic
""".split())

TOKEN = re.compile(r"[^0-9a-z\u00c0-\u024f]+")


def _core_tokens(value: str) -> frozenset[str]:
    """Distinctive tokens of a company name.

    Tokens under three characters go too: they are initials and legal-form
    fragments ("p", "m", "z", "o", "s") that carry no identity but would
    otherwise weigh as much as the real name.
    """
    tokens = TOKEN.split(normalize_name(value))
    return frozenset(
        token for token in tokens if len(token) >= 3 and token not in LEGAL_TOKENS
    )


def _identity_match(candidate: CandidateRecord, names: list[str]) -> str | None:
    """The winner name that is really this candidate, or None.

    TED's ``~`` is a contains-match, so it returns notices whose winner merely
    shares a word. A bare substring test is not enough to filter those: the
    corpus row `DUKAT` matches `CADXPERT P. GURGA M. DUKAT SPÓŁKA KOMANDYTOWA`,
    where Dukat is a partner's surname and the company is somebody else
    entirely. That is a wrong lead, not a weak one.

    So the candidate's distinctive tokens must all appear in the winner's, and
    must account for at least half of them. `DUKAT` covers one of five and is
    rejected; `Leroy Merlin` covers two of two and is kept.
    """
    aliases = candidate.data.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    identities = [candidate.company_name, *[str(alias) for alias in aliases]]

    for name in names:
        winner_core = _core_tokens(name)
        winner_normalized = normalize_name(name)
        for identity in identities:
            core = _core_tokens(identity)
            if not core or not core <= winner_core:
                continue
            # A one-word candidate has to *be* the winner, not merely appear in
            # it. Half-coverage is fine for `Leroy Merlin` but waves through the
            # generic ones: `SPS` is not `Maurer SPS GmbH`, and `Distrib` is not
            # `POM DISTRIB`. Both were real corpus rows and both were wrong.
            required = len(winner_core) if len(core) == 1 else len(winner_core) * 0.5
            if len(core) >= required:
                return name
            # Nothing distinctive survived stripping (initialisms, single short
            # words). Only an exact name is trustworthy at that point.
            if not core and normalize_name(identity) == winner_normalized:
                return name
    return None


class TedVerifier(CatalogProvider):
    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(ted_definition())
        self.client = client or httpx.Client()

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="active", message="TED search API needs no credential")

    def _search(self, term: str, country: str) -> tuple[list[dict], int]:
        """Notices for a candidate, and how many HTTP calls that took.

        The 429 backoff makes a second call, and it is a real request against
        TED's rate limit, so it is counted rather than assumed away.
        """
        query = f'winner-name ~ ("{term}")'
        alpha3 = [a3 for a3, a2 in ALPHA3_TO_ALPHA2.items() if a2 == country.upper()]
        if alpha3:
            query += f' AND winner-country IN ("{alpha3[0]}")'
        payload = {"query": query, "limit": MAX_NOTICES, "fields": FIELDS}
        requests = 0
        for attempt in (0, 1):
            response = self.client.post(SEARCH_ENDPOINT, json=payload, timeout=45.0)
            requests += 1
            # 429 is routine here, not exceptional. One backoff, then give up and
            # let the campaign record a verification error for this candidate.
            if response.status_code == 429 and attempt == 0:
                time.sleep(RETRY_PAUSE_SECONDS)
                continue
            response.raise_for_status()
            return response.json().get("notices") or [], requests
        return [], requests

    def _source(
        self,
        notice: dict,
        matched_name: str,
        candidate: CandidateRecord,
        buyer_terms: list[str],
        product_terms: list[str],
    ) -> VerificationSource:
        publication = str(notice.get("publication-number") or "")
        raw = json.dumps(notice, sort_keys=True, ensure_ascii=False).encode()
        facts: dict[str, list[str]] = {"company_name": [candidate.company_name]}

        for code in notice.get("winner-country") or []:
            if ALPHA3_TO_ALPHA2.get(str(code).upper()) == candidate.country.upper():
                facts["country"] = [candidate.country.upper()]
                break

        candidate_domain = _normalized_domain(candidate.domain)
        for address in notice.get("winner-internet-address") or []:
            if candidate_domain and _normalized_domain(address) == candidate_domain:
                facts["domain"] = [candidate_domain]
                break

        # Winning or bidding for a public contract is itself a buying signal, and
        # it is the one role TED can state without inference.
        facts["buyer_role"] = ["public procurement supplier"]

        # Hyphens are folded on both sides: a sector id is written
        # "kitchen-appliances" and a notice title says "kitchen appliances".
        haystack = " ".join(normalize_name(" ".join([
            _title(notice.get("notice-title")),
            *_names(notice.get("buyer-name")),
            matched_name,
        ])).replace("-", " ").split())
        matched_products = [
            term for term in product_terms
            if " ".join(term.casefold().replace("-", " ").split()) in haystack
        ]
        if matched_products:
            facts["product_term"] = matched_products
        matched_buyers = [
            term for term in buyer_terms
            if " ".join(term.casefold().replace("-", " ").split()) in haystack
        ]
        if matched_buyers:
            facts["buyer_role"] = [*facts["buyer_role"], *matched_buyers]

        return VerificationSource(
            provenance_url=NOTICE_URL.format(publication),
            raw_hash=hashlib.sha256(raw).hexdigest(),
            # Never "official": TED is a third-party register, not the
            # candidate's own site, whatever it says about them.
            classification="independent",
            retrieved_via=SEARCH_ENDPOINT,
            facts=facts,
        )

    def verify(self, query: DiscoveryQuery, candidate: CandidateRecord) -> VerificationBundle:
        term = _search_term(candidate.company_name)
        if not term:
            return VerificationBundle(candidate_source_record_id=candidate.source_record_id)

        buyer_terms = _clean_terms([*query.buyer_types, *candidate.data.get("buyer_types", [])])
        product_terms = _clean_terms([
            *query.sector_ids, *query.hs_codes, *candidate.data.get("categories", []),
        ])

        sources: list[VerificationSource] = []
        seen: set[str] = set()
        notices, requests = self._search(term, candidate.country)
        for notice in notices:
            names = _names(notice.get("winner-name"))
            matched = _identity_match(candidate, names)
            if not matched:
                continue
            publication = str(notice.get("publication-number") or "")
            if not publication or publication in seen:
                continue
            seen.add(publication)
            sources.append(self._source(notice, matched, candidate, buyer_terms, product_terms))

        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=sources,
            independent_source_count=len(sources),
            requests=requests,
        )
