from __future__ import annotations

from typing import Any, Optional

from ai_usage.contract import iso


def balance_provider(key: str, label: str, snapshot: Optional[Any]) -> dict:
    """Map a pay-as-you-go AccountUsageSnapshot (or None) into a tray dict.

    Unlike ``budget_provider`` (rolling %-windows) and ``tokensum_provider``
    (log-summed token counts), balance mode carries a single outstanding-$
    figure — the money left on a prepaid, direct-billed key (DeepSeek). The
    tray colors the row by how low the balance has run; the numeric value is
    the whole story, so there are no per-window bars.
    """
    base = {"key": key, "label": label, "mode": "balance"}

    if snapshot is None:
        return {**base, "state": "error", "windows": [], "detail": "no data"}

    if not getattr(snapshot, "available", False):
        reason = getattr(snapshot, "unavailable_reason", None)
        state = "unconfigured" if reason else "error"
        return {**base, "state": state, "windows": [], "detail": "no data"}

    bal = getattr(snapshot, "balance_usd", None)
    if bal is None:
        return {**base, "state": "error", "windows": [], "detail": "no data"}

    balance = round(float(bal), 2)
    return {
        **base,
        "state": "ok",
        "fetched_at": iso(snapshot.fetched_at),
        "balance_usd": balance,
        "windows": [],
        "detail": f"${balance:.2f} left",
    }
