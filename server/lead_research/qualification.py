"""Explicit eligibility gates for named-company candidates."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


def _normalize_role(value: str) -> str:
    return " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())


# A company that resells what it does not make. Every one of these is a channel
# a seller can sell *through*, which is why they are interchangeable as far as
# the gate is concerned.
CHANNEL_ROLES = frozenset({
    "distributor", "importer", "retailer", "wholesaler", "procurement organization",
})

# Evidence and campaigns do not share one vocabulary. A verifier states the role
# it can actually prove; a campaign asks in its sector's terms, and
# `EligibilityService` intersects the two sets. Where those vocabularies differ
# the intersection is empty and every candidate is rejected for a reason that
# has nothing to do with the company.
#
# Written out per term rather than inferred, because a wrong entry qualifies
# companies nobody asked for:
#
# - "public procurement supplier" is what TED can state: this company won a
#   public contract to supply these goods. That proves a channel role and
#   nothing more, so it answers a request for any role in CHANNEL_ROLES and
#   deliberately not for "brand" or "manufacturer" — those claim ownership or
#   production that winning a supply contract does not evidence.
ROLE_EQUIVALENTS: dict[str, frozenset[str]] = {
    "public procurement supplier": CHANNEL_ROLES,
}


def satisfies_buyer_role(observed: set[str], requested: set[str]) -> bool:
    """Whether any observed role answers any requested one."""
    observed_terms = {_normalize_role(value) for value in observed}
    requested_terms = {_normalize_role(value) for value in requested}
    if observed_terms & requested_terms:
        return True
    return any(
        ROLE_EQUIVALENTS.get(term, frozenset()) & requested_terms
        for term in observed_terms
    )


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    gates: dict[str, str]
    reasons: list[str]


class EligibilityService:
    """The campaign's own eligibility policy, applied.

    Every gate here is a switch the campaign editor already renders. They used
    to be collected, stored, and ignored: this service hardcoded five gates and
    never read `config.eligibility`, so turning "Require official domain" on
    changed nothing and turning "Require buyer role" off still rejected on it. A
    control that does nothing is worse than a missing one, because someone
    tunes it and trusts the result.

    A gate the policy switches off reports `not_required` rather than vanishing:
    the stored gate map is the record of why a company qualified, and a silently
    absent gate reads as one that passed.
    """

    def evaluate(self, candidate: dict, config) -> EligibilityResult:
        policy = config.eligibility or {}
        exclusions = config.exclusions or {}
        target = set(config.target_countries)
        buyer_types = set(candidate.get("buyer_types") or [])
        requested_buyers = set(config.buyer_types)
        minimum_independent = int(policy.get("minimum_independent_sources", 0) or 0)
        lifecycle = candidate.get("lifecycle_status")
        gates = {
            "resolved_identity": _gate(
                policy.get("require_resolved_identity", True),
                bool(candidate.get("organization_id")),
            ),
            "target_geography": _gate(
                policy.get("require_target_presence", True),
                candidate.get("country") in target,
            ),
            "product_sector_relevance": "pass" if candidate.get("sector_ids") else "unknown",
            "buyer_role": _gate(
                policy.get("require_buyer_role", True),
                satisfies_buyer_role(buyer_types, requested_buyers),
            ),
            "official_domain": _gate(
                policy.get("require_official_domain", False),
                bool(candidate.get("official_domains")),
            ),
            "independent_sources": _gate(
                minimum_independent > 0,
                int(candidate.get("independent_domain_count") or 0) >= minimum_independent,
            ),
            # Closure is evidence, so it can reject here as well as skip a
            # candidate before selection: a company that closed since the last
            # run is still selected, and this is where that gets caught.
            "lifecycle": _gate(
                policy.get("exclude_inactive", True),
                lifecycle != "closed",
            ),
            "exclusion_list": _gate(
                True,
                not _excluded(candidate, exclusions),
            ),
            # No sanctions screening source is configured, so this cannot be a
            # "pass": that would claim a check nobody ran. It reports unknown
            # and does not block. Wire a screening list and it becomes a gate.
            "compliance": "fail" if candidate.get("sanctioned") else "unknown",
        }
        reasons = [key for key, value in gates.items() if value == "fail"]
        return EligibilityResult(not reasons, gates, reasons)


def _gate(required: bool, satisfied: bool) -> str:
    if not required:
        return "not_required"
    return "pass" if satisfied else "fail"


def _excluded(candidate: dict, exclusions: dict) -> bool:
    """Whether the tenant's own exclusion list names this company.

    Domains are compared on the normalized host, so `https://www.Foo.com/x` in
    the list still excludes a candidate recorded as `foo.com`.
    """
    if candidate.get("organization_id") in set(exclusions.get("company_ids") or []):
        return True
    excluded_domains = {
        _normalize_domain(value) for value in (exclusions.get("domains") or [])
    } - {None}
    if not excluded_domains:
        return False
    candidate_domains = {
        _normalize_domain(value)
        for value in [candidate.get("domain"), *(candidate.get("official_domains") or [])]
    } - {None}
    return bool(candidate_domains & excluded_domains)


def _normalize_domain(value) -> str | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().casefold()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or raw.split("/", 1)[0]).rstrip(".").removeprefix("www.")
    return host or None
