"""Sector-aware feature planning and evidence-bound local-model validation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import Claim
from .sectors import REFERENCE_DIR


@dataclass(frozen=True)
class FeatureRequest:
    field: str
    applicability: str
    priority: int


# Playbooks name what a sector needs; verifiers emit what a page or a notice
# actually said. The two vocabularies were written apart and never met, which is
# why FeaturePlanner has been importable but unused. This is the bridge, and it
# is deliberately explicit: a guessed mapping would silently mark a gap filled.
PLAYBOOK_SATISFIED_BY = {
    "identity_scale": ("company_name", "domain", "employee_count", "store_count", "revenue"),
    "market_coverage": ("country", "locations", "countries_served", "market_coverage"),
    "product_fit": ("product_term", "product_fit", "hs_code", "brands_carried", "sector_ids"),
    "buying_intent": ("buying_intent", "procurement_intent", "tender", "buyer_role"),
    "procurement_intent": ("procurement_intent", "tender", "buyer_role"),
    "store_count": ("store_count",),
    "relevant_import_value": ("relevant_import_value",),
    "brands_carried": ("brands_carried",),
    "certifications": ("certifications",),
    "facilities": ("facilities",),
    "private_label_fit": ("private_label_fit",),
}


def satisfied_playbook_fields(fact_fields) -> set[str]:
    """Playbook fields already covered by the claim fields collected so far."""
    present = set(fact_fields)
    return {
        field for field, accepted in PLAYBOOK_SATISFIED_BY.items()
        if present.intersection(accepted)
    }


class FeaturePlanner:
    def __init__(self, path: Path = REFERENCE_DIR / "feature-playbooks.yaml"):
        self.playbooks = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def missing_claims(self, organization: dict, sector_ids: list[str]) -> list[FeatureRequest]:
        present = set(organization.get("claims", {}))
        requests: dict[str, FeatureRequest] = {}
        for sector_id in sector_ids:
            playbook = self.playbooks.get(sector_id, {})
            for field in playbook.get("required", []):
                if field not in present:
                    requests[field] = FeatureRequest(field, "required", 1)
            for field in playbook.get("useful", []):
                if field not in present and field not in playbook.get("not_applicable", []):
                    requests.setdefault(field, FeatureRequest(field, "useful", 2))
        return sorted(requests.values(), key=lambda item: (item.priority, item.field))


class EnrichmentService:
    def __init__(self, evidence_exists=None):
        self.evidence_exists = evidence_exists or (lambda _: True)

    def validate_claim(self, claim: Claim | dict) -> dict:
        try:
            value = claim if isinstance(claim, Claim) else Claim.model_validate(claim)
        except Exception as exc:
            message = str(exc)
            reason = "numeric_claim_requires_evidence" if "require evidence" in message else "invalid_claim"
            return {"accepted": False, "reason": reason}
        if any(not self.evidence_exists(item) for item in value.evidence_ids):
            return {"accepted": False, "reason": "unresolved_evidence"}
        return {"accepted": True, "claim": value}
