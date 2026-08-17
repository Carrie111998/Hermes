"""usage-meter plugin — provider-agnostic per-call accounting.

Captures one privacy-safe ledger event per completed model API call through
the existing ``post_api_request`` hook. MoA reference/aggregator calls,
subagents, and auxiliary tasks each produce their own event. Desktop and TUI
read aggregates through ``usage.meter.*`` RPC methods.

Completion scope originates from the #77221 design by @muhammadshess-10xe
(issue comment 5256969393). This first-party bundled plugin is the in-tree
integration path for that design.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import capture, ledger

logger = logging.getLogger(__name__)


def _local_tz():
    try:
        return datetime.now().astimezone().tzinfo or timezone.utc
    except Exception:
        return timezone.utc


def month_window(
    *,
    now: Optional[float] = None,
    tz_name: Optional[str] = None,
) -> Tuple[float, float, str]:
    """Return (start_ts, end_ts, month_label) for the current local month."""
    tz = _local_tz()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            tz = _local_tz()
    ts = float(now if now is not None else time.time())
    dt = datetime.fromtimestamp(ts, tz=tz)
    start = datetime(dt.year, dt.month, 1, tzinfo=tz)
    if dt.month == 12:
        end = datetime(dt.year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(dt.year, dt.month + 1, 1, tzinfo=tz)
    label = f"{dt.year:04d}-{dt.month:02d}"
    return start.timestamp(), end.timestamp(), label


def meter_summary(*, tz_name: Optional[str] = None) -> Dict[str, Any]:
    month_start, month_end, month_label = month_window(tz_name=tz_name)
    month = ledger.summarize(since_ts=month_start, until_ts=month_end)
    all_time = ledger.summarize()
    return {
        "month_label": month_label,
        "month_start_ts": month_start,
        "month_end_ts": month_end,
        "month": month,
        "all_time": all_time,
        "db_path": str(ledger.default_db_path()),
        "caveat": (
            "Estimated cost only — not an invoice. Unpriced routes never display "
            "as $0.00. Provider cache semantics can differ from billing."
        ),
    }


def meter_details(*, scope: str = "month", tz_name: Optional[str] = None) -> Dict[str, Any]:
    scope = (scope or "month").strip().lower()
    if scope == "all":
        data = ledger.summarize()
        label = "all-time"
        start = end = None
    else:
        start, end, label = month_window(tz_name=tz_name)
        data = ledger.summarize(since_ts=start, until_ts=end)
    return {
        "scope": "all" if scope == "all" else "month",
        "label": label,
        "start_ts": start,
        "end_ts": end,
        **data,
        "caveat": (
            "Estimated cost only — not an invoice. Unpriced routes never display "
            "as $0.00."
        ),
    }


def meter_recent(*, limit: int = 50) -> Dict[str, Any]:
    return {"events": ledger.recent_events(limit=limit)}


def register(ctx) -> None:
    """Register the fail-open capture hook. No tools, no overrides."""
    ctx.register_hook("post_api_request", capture.on_post_api_request)
    logger.info(
        "usage-meter: capturing post_api_request events into %s",
        ledger.default_db_path(),
    )
