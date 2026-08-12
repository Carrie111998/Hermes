"""Normalize ``post_api_request`` kwargs into a privacy-safe ledger event."""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from . import ledger

logger = logging.getLogger(__name__)

# Pricing statuses exposed to UI. Hermes core uses "unknown"; the meter
# surface renames that to "unpriced" so $0.00 is never ambiguous.
_STATUS_MAP = {
    "actual": "estimated",  # still an estimate until invoice reconciliation
    "estimated": "estimated",
    "included": "included",
    "unknown": "unpriced",
}


def _profile_label() -> str:
    env = (os.environ.get("HERMES_PROFILE") or "").strip()
    if env:
        return env
    try:
        home = Path(__import__("hermes_constants", fromlist=["get_hermes_home"]).get_hermes_home())
        if home.parent.name == "profiles":
            return home.name
    except Exception:
        pass
    return "default"


def _usage_dict(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    usage = kwargs.get("usage")
    if isinstance(usage, dict):
        return usage
    response = kwargs.get("response")
    if isinstance(response, dict):
        nested = response.get("usage")
        if isinstance(nested, dict):
            return nested
    return {}


def _int_bucket(usage: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key not in usage:
            continue
        raw = usage[key]
        if isinstance(raw, bool):
            return None
        if not isinstance(raw, int):
            return None
        value = raw
        return value if value >= 0 else None
    return None


def _price_event(
    *,
    model: str,
    provider: str,
    base_url: str,
    usage_buckets: Dict[str, int],
) -> Dict[str, Any]:
    """Estimate cost via Hermes pricing resolver. Fail open to unpriced."""
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        cu = CanonicalUsage(
            input_tokens=usage_buckets["input_tokens"],
            output_tokens=usage_buckets["output_tokens"],
            cache_read_tokens=usage_buckets["cache_read_tokens"],
            cache_write_tokens=usage_buckets["cache_write_tokens"],
            reasoning_tokens=usage_buckets["reasoning_tokens"],
            request_count=1,
        )
        result = estimate_usage_cost(
            model or "",
            cu,
            provider=provider or None,
            base_url=base_url or None,
        )
        status = _STATUS_MAP.get(result.status, "unpriced")
        amount: Optional[float]
        if result.amount_usd is None:
            amount = None
            if status != "included":
                status = "unpriced"
        else:
            amount = float(Decimal(result.amount_usd))
        return {
            "estimated_cost_usd": amount,
            "pricing_status": status,
            "pricing_source": str(result.source or "none"),
        }
    except Exception:
        logger.debug("usage-meter pricing failed; recording unpriced", exc_info=True)
        return {
            "estimated_cost_usd": None,
            "pricing_status": "unpriced",
            "pricing_source": "none",
        }


def build_event_from_hook(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a ledger event dict, or None when there is nothing to record."""
    usage = _usage_dict(kwargs)
    parsed_buckets = {
        "input_tokens": _int_bucket(usage, "input_tokens"),
        "output_tokens": _int_bucket(usage, "output_tokens"),
        "cache_read_tokens": _int_bucket(usage, "cache_read_tokens", "cached_tokens"),
        "cache_write_tokens": _int_bucket(usage, "cache_write_tokens"),
        "reasoning_tokens": _int_bucket(usage, "reasoning_tokens"),
    }
    # A malformed bucket invalidates the event: pricing a partially invented
    # vector would turn unavailable evidence into a false zero-token call.
    if any(value is None for value in parsed_buckets.values()):
        return None
    known_keys = {
        "input_tokens", "output_tokens", "cache_read_tokens", "cached_tokens",
        "cache_write_tokens", "reasoning_tokens",
    }
    if not any(key in usage for key in known_keys):
        return None
    # Explicit zeroes are valid producer evidence; absent/null/malformed fields
    # have already failed closed above.
    buckets = {key: int(value) for key, value in parsed_buckets.items()}

    model = str(kwargs.get("model") or kwargs.get("response_model") or "")
    provider = str(kwargs.get("provider") or "")
    base_url = str(kwargs.get("base_url") or "")
    priced = _price_event(
        model=model,
        provider=provider,
        base_url=base_url,
        usage_buckets=buckets,
    )

    ended = kwargs.get("ended_at")
    try:
        ts = float(ended) if ended is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()

    return {
        "ts": ts,
        "profile": _profile_label(),
        "provider": provider,
        "model": model,
        "api_mode": str(kwargs.get("api_mode") or ""),
        "platform": str(kwargs.get("platform") or ""),
        "session_id": str(kwargs.get("session_id") or ""),
        "task_id": str(kwargs.get("task_id") or ""),
        "api_request_id": str(kwargs.get("api_request_id") or ""),
        **buckets,
        **priced,
        "request_count": 1,
    }


def on_post_api_request(**kwargs: Any) -> None:
    """Fail-open hook: ledger errors must never break a model call."""
    try:
        event = build_event_from_hook(**kwargs)
        if event is None:
            return
        ledger.append_event(event)
    except Exception:
        logger.debug("usage-meter post_api_request handler failed open", exc_info=True)
