"""Frozen registry for every cost-attributed vendor and programme lane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

VendorKind = Literal[
    "llm_self_reporting",
    "voice_metered",
    "search_metered",
    "free_tier_attributed",
    "subscription_bridge",
]

VALID_VENDOR_KINDS = frozenset(
    {
        "llm_self_reporting",
        "voice_metered",
        "search_metered",
        "free_tier_attributed",
        "subscription_bridge",
    }
)


@dataclass(frozen=True)
class VendorSpec:
    slug: str
    kind: VendorKind
    surcharge_pct: float = 0.0
    default_currency: str = "USD"
    billed_currency_override: str | None = None
    notes: str = ""


VENDORS: dict[str, VendorSpec] = {
    "openrouter": VendorSpec(
        "openrouter", "llm_self_reporting", surcharge_pct=5.5
    ),
    "openai": VendorSpec("openai", "llm_self_reporting"),
    "anthropic": VendorSpec("anthropic", "llm_self_reporting"),
    "openai-codex": VendorSpec(
        "openai-codex",
        "subscription_bridge",
        notes="Pro bridge; CS-02d tracks subscription turns.",
    ),
    "retell": VendorSpec(
        "retell",
        "voice_metered",
        notes="Per-minute; see ratecards.py.",
    ),
    "perplexity": VendorSpec(
        "perplexity",
        "search_metered",
        notes="Token-tiered search pricing; see ratecards.py.",
    ),
    "apple": VendorSpec(
        "apple",
        "free_tier_attributed",
        notes="App Store Connect attribution.",
    ),
    "meta": VendorSpec(
        "meta",
        "free_tier_attributed",
        notes="Meta Graph attribution.",
    ),
    "github": VendorSpec(
        "github",
        "free_tier_attributed",
        notes="GitHub API attribution.",
    ),
}

ALLOWED_LANES = (
    "green_captains",
    "dayroute",
    "tihna",
    "platform",
    "reserve",
    "escalation",
)


def get_vendor(slug: str) -> VendorSpec:
    normalized = str(slug or "").strip().lower()
    try:
        return VENDORS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unknown vendor: {slug!r}. Register it in "
            "hermes_cli/cost/vendors.py first."
        ) from exc


def validate_lane(lane: str) -> None:
    normalized = str(lane or "").strip().lower()
    if normalized not in ALLOWED_LANES:
        raise ValueError(
            f"Unknown lane: {lane!r}. Must be one of {ALLOWED_LANES}."
        )


__all__ = [
    "ALLOWED_LANES",
    "VALID_VENDOR_KINDS",
    "VENDORS",
    "VendorKind",
    "VendorSpec",
    "get_vendor",
    "validate_lane",
]
