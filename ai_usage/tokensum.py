from __future__ import annotations

import sqlite3
from datetime import datetime

from ai_usage.contract import BILLING_PROVIDER_ALIASES, TOKEN_WINDOWS, iso


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def tokensum_provider(
    key: str, label: str, conn: sqlite3.Connection, now: datetime
) -> dict:
    """Rolling 5h/24h/7d token+cost sums from session_model_usage for `key`."""
    aliases = BILLING_PROVIDER_ALIASES.get(key, [key])
    like_clause = " OR ".join(["lower(billing_provider) LIKE ?"] * len(aliases))
    like_params = [f"%{a.lower()}%" for a in aliases]
    now_epoch = now.timestamp()

    windows: list[dict] = []
    for wid, wlabel, secs in TOKEN_WINDOWS:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0), "
            "COALESCE(SUM(estimated_cost_usd), 0) "
            "FROM session_model_usage "
            f"WHERE ({like_clause}) AND last_seen >= ?",
            like_params + [now_epoch - secs],
        ).fetchone()
        windows.append(
            {
                "id": wid,
                "label": wlabel,
                "tokens": int(row[0]),
                "cost_usd": round(float(row[1]), 4),
            }
        )

    by_id = {w["id"]: w for w in windows}
    any_data = any(w["tokens"] > 0 for w in windows)
    detail = (
        f"5h {_fmt(by_id['5h']['tokens'])} · wk {_fmt(by_id['7d']['tokens'])}"
        if any_data
        else "no usage yet"
    )
    return {
        "key": key,
        "label": label,
        "mode": "tokens",
        "state": "ok" if any_data else "unconfigured",
        "fetched_at": iso(now),
        "windows": windows,
        "detail": detail,
    }
