from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Optional

from ai_usage.contract import iso

MANUAL_PROVIDER_KEYS = frozenset({"gemini", "xai", "opencode-go"})
MANUAL_STALE_SECONDS = 86400

_PROVIDER_LABELS = {
    "gemini": "Gemini",
    "xai": "Grok",
    "opencode-go": "OpenCode Go",
}
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def read_manual_snapshot(path: str, now: datetime) -> dict[str, dict]:
    """Return valid manual provider rows keyed by provider; fail open on read errors."""
    try:
        with open(path, encoding="utf-8") as handle:
            store = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    if not isinstance(store, dict) or store.get("schema_version") != 1:
        return {}
    records = store.get("providers")
    if not isinstance(records, dict):
        return {}

    rows: dict[str, dict] = {}
    for key, record in records.items():
        if key not in MANUAL_PROVIDER_KEYS:
            continue
        row = _build_manual_row(key, record, now)
        if row is not None:
            rows[key] = row
    return rows


def _build_manual_row(key: str, record: Any, now: datetime) -> Optional[dict]:
    if not isinstance(record, dict):
        return None

    used_pct = record.get("used_pct")
    if not isinstance(used_pct, (int, float)) or isinstance(used_pct, bool):
        return None
    used_pct = float(used_pct)
    if not math.isfinite(used_pct) or not 0.0 <= used_pct <= 100.0:
        return None

    saved_at = _parse_iso(record.get("saved_at"))
    if saved_at is None:
        return None

    resets_at_raw = record.get("resets_at")
    if resets_at_raw is not None and not isinstance(resets_at_raw, str):
        return None
    resets_at = _parse_iso(resets_at_raw) if resets_at_raw is not None else None
    if resets_at_raw is not None and resets_at is None:
        return None

    age_seconds = max(0.0, (now - saved_at).total_seconds())
    state = "ok" if age_seconds <= MANUAL_STALE_SECONDS else "stale"
    window: dict = {
        "id": "subscription",
        "label": "Subscription",
        "used_pct": round(used_pct, 1),
    }
    if resets_at is not None:
        window["resets_at"] = iso(resets_at)

    detail = f"Manual · Subscription {used_pct:.0f}%"
    if resets_at is not None:
        detail += f" ({_format_reset_short(resets_at)})"

    return {
        "key": key,
        "label": _PROVIDER_LABELS[key],
        "mode": "budget",
        "source": "manual",
        "state": state,
        "fetched_at": iso(saved_at),
        "windows": [window],
        "detail": detail,
    }


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_reset_short(value: datetime) -> str:
    local = value.astimezone()
    ampm = "am" if local.hour < 12 else "pm"
    hour = local.hour % 12 or 12
    clock = f"{hour}{ampm}" if local.minute == 0 else f"{hour}:{local.minute:02d}{ampm}"
    return f"{_DOW[local.weekday()]} {local.month}/{local.day} {clock}"
