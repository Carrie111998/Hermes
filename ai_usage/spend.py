from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ai_usage.contract import BILLING_PROVIDER_ALIASES, iso
from ai_usage.pricing import cost_usd


def _month_start_epoch(now: datetime) -> float:
    """UTC-midnight of the 1st of `now`'s month, as a POSIX timestamp."""
    start = now.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return start.timestamp()


def spend_provider(
    key: str, label: str, conn: sqlite3.Connection, now: datetime
) -> dict:
    """Month-to-date ESTIMATED $ spend for a log-billed provider (Gemini).

    Sums per-model input/output tokens from ``session_model_usage`` since the
    start of the current UTC month and prices each model with its rate card
    (``ai_usage.pricing``). Distinct from ``tokensum_provider`` (which reports
    rolling 5h/24h/7d token COUNTS): here the money figure is the whole story,
    so there are no per-window bars — the tray shows one "$X.XX this month" line.

    Gemini exposes no balance/usage endpoint (postpaid Google Cloud billing),
    and the stored ``estimated_cost_usd`` is unpopulated, so this is the only
    way to surface a live $ figure — hence "estimate". Empty (no traffic this
    month) is reported as ``unconfigured`` so the tile reads "no usage yet"
    rather than a falsely-precise "$0.00".
    """
    aliases = BILLING_PROVIDER_ALIASES.get(key, [key])
    like_clause = " OR ".join(["lower(billing_provider) LIKE ?"] * len(aliases))
    like_params = [f"%{a.lower()}%" for a in aliases]
    since = _month_start_epoch(now)

    rows = conn.execute(
        "SELECT model, "
        "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
        "FROM session_model_usage "
        f"WHERE ({like_clause}) AND last_seen >= ? "
        "GROUP BY model",
        like_params + [since],
    ).fetchall()

    spend = 0.0
    total_tokens = 0
    for model, in_tok, out_tok in rows:
        in_tok = int(in_tok or 0)
        out_tok = int(out_tok or 0)
        total_tokens += in_tok + out_tok
        spend += cost_usd(key, model or "", in_tok, out_tok)

    spend = round(spend, 2)
    any_data = total_tokens > 0
    return {
        "key": key,
        "label": label,
        "mode": "spend",
        "state": "ok" if any_data else "unconfigured",
        "fetched_at": iso(now),
        "spend_usd": spend,
        "windows": [],
        "detail": f"${spend:.2f} this month" if any_data else "no usage yet",
    }
