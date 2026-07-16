"""Explicit eligibility gates for named-company candidates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    gates: dict[str, str]
    reasons: list[str]


class EligibilityService:
    def evaluate(self, candidate: dict, config) -> EligibilityResult:
        target = set(config.target_countries)
        buyer_types = set(candidate.get("buyer_types") or [])
        requested_buyers = set(config.buyer_types)
        gates = {
            "resolved_identity": "pass" if candidate.get("organization_id") else "fail",
            "target_geography": "pass" if candidate.get("country") in target else "fail",
            "product_sector_relevance": "pass" if candidate.get("sector_ids") else "unknown",
            "buyer_role": "pass" if buyer_types & requested_buyers else "fail",
            "compliance": "pass" if not candidate.get("sanctioned") else "fail",
        }
        reasons = [key for key, value in gates.items() if value == "fail"]
        return EligibilityResult(not reasons, gates, reasons)
