from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any, Optional

from ai_usage.contract import WINDOW_LABEL_TO_ID, iso

# Locale-independent weekday abbreviations (Mon=0..Sun=6), so the label reads the
# same regardless of the host's locale — mirrors the C# FormatReset helper.
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _format_reset_short(dt: datetime, tz: Optional[tzinfo]) -> str:
    """Short local-time reset label like "Thu 8/6 10am" / "Wed 8/5 8:59am"."""
    local = dt.astimezone(tz)  # tz=None → host local time
    ampm = "am" if local.hour < 12 else "pm"
    h12 = local.hour % 12 or 12
    time = f"{h12}{ampm}" if local.minute == 0 else f"{h12}:{local.minute:02d}{ampm}"
    return f"{_DOW[local.weekday()]} {local.month}/{local.day} {time}"


def budget_provider(
    key: str, label: str, snapshot: Optional[Any], *, tz: Optional[tzinfo] = None
) -> dict:
    """Map an AccountUsageSnapshot (or None) into a tray provider dict.

    ``tz`` controls the timezone of the reset label in ``detail`` (None → host
    local time; the collector runs on the same machine as the tray).
    """
    base = {"key": key, "label": label, "mode": "budget"}

    if snapshot is None:
        return {**base, "state": "error", "windows": [], "detail": "no data"}

    if not getattr(snapshot, "available", False):
        reason = getattr(snapshot, "unavailable_reason", None)
        state = "unconfigured" if reason else "error"
        return {**base, "state": state, "windows": [], "detail": "no data"}

    windows: list[dict] = []
    reset_by_id: dict[str, datetime] = {}
    for w in snapshot.windows:
        mapped = WINDOW_LABEL_TO_ID.get(w.label)
        if not mapped or w.used_percent is None:
            continue
        wid, wlabel = mapped
        entry: dict = {"id": wid, "label": wlabel, "used_pct": round(float(w.used_percent), 1)}
        if w.reset_at is not None:
            entry["resets_at"] = iso(w.reset_at)
            reset_by_id[wid] = w.reset_at
        windows.append(entry)

    by_id = {w["id"]: w for w in windows}

    def _bit(prefix: str, wid: str) -> str:
        text = f"{prefix} {by_id[wid]['used_pct']:.0f}%"
        reset = reset_by_id.get(wid)
        if reset is not None:
            text += f" ({_format_reset_short(reset, tz)})"
        return text

    bits: list[str] = []
    if "5h" in by_id:
        bits.append(_bit("5h", "5h"))
    if "wk" in by_id:
        bits.append(_bit("wk", "wk"))

    return {
        **base,
        "state": "ok",
        "fetched_at": iso(snapshot.fetched_at),
        "windows": windows,
        "detail": " · ".join(bits) or "ok",
    }
