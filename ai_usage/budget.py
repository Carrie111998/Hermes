from __future__ import annotations

from typing import Any, Optional

from ai_usage.contract import WINDOW_LABEL_TO_ID, iso


def budget_provider(key: str, label: str, snapshot: Optional[Any]) -> dict:
    """Map an AccountUsageSnapshot (or None) into a tray provider dict."""
    base = {"key": key, "label": label, "mode": "budget"}

    if snapshot is None:
        return {**base, "state": "error", "windows": [], "detail": "no data"}

    if not getattr(snapshot, "available", False):
        reason = getattr(snapshot, "unavailable_reason", None)
        state = "unconfigured" if reason else "error"
        return {**base, "state": state, "windows": [], "detail": "no data"}

    windows: list[dict] = []
    for w in snapshot.windows:
        mapped = WINDOW_LABEL_TO_ID.get(w.label)
        if not mapped or w.used_percent is None:
            continue
        wid, wlabel = mapped
        entry: dict = {"id": wid, "label": wlabel, "used_pct": round(float(w.used_percent), 1)}
        if w.reset_at is not None:
            entry["resets_at"] = iso(w.reset_at)
        windows.append(entry)

    by_id = {w["id"]: w for w in windows}
    bits: list[str] = []
    if "5h" in by_id:
        bits.append(f"5h {by_id['5h']['used_pct']:.0f}%")
    if "wk" in by_id:
        bits.append(f"wk {by_id['wk']['used_pct']:.0f}%")

    return {
        **base,
        "state": "ok",
        "fetched_at": iso(snapshot.fetched_at),
        "windows": windows,
        "detail": " · ".join(bits) or "ok",
    }
