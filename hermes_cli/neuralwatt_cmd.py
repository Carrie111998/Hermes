"""Shared ``/neuralwatt`` command logic for gateway (and agent-side use).

Surfaces NeuralWatt's account models/quota/sessions/usage analytics as a
single slash command with subcommands, and as an importable module the agent
itself can call (``python -m hermes_cli.neuralwatt_cmd <subcmd>``) to fetch
and analyze usage for cost/energy/routing optimizations.

Subcommands:

  /neuralwatt models            live model catalog (id, ctx, out, efforts)
  /neuralwatt quota             balance, plan, monthly usage, kWh allowed/used
  /neuralwatt sessions [N]      recent sessions (cost, energy, cache-hit, ttft)
  /neuralwatt families [N]      sessions grouped by routing fingerprint
  /neuralwatt session <id>      per-turn detail + optimization flags
  /neuralwatt requests [N]      latest per-request rows
  /neuralwatt energy [days]     daily energy series
  /neuralwatt analyze [id]      recommendations from flags + usage

Data comes live from the NeuralWatt API (authenticated with the same key the
agent uses for chat).  Output is compact Telegram-friendly text;
``analyze_*`` helpers return structured dicts for the agent to iterate on.

Security: the API key is resolved from the Hermes runtime (provider config /
NEURALWATT_API_KEY) and never echoed.  Keys are never logged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_QUALITY_SCORE_CACHE: dict[str, Any] = {}


def _runtime_profile_kwargs() -> Optional[dict[str, Any]]:
    """Resolve the NeuralWatt runtime (api_key + base_url) the agent uses."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        rt = resolve_runtime_provider(requested="neuralwatt")
    except Exception as exc:
        logger.debug("neuralwatt runtime resolution failed: %s", exc)
        return None
    api_key = rt.get("api_key") or ""
    if not api_key or api_key == "no-key-required":
        return None
    return {
        "api_key": api_key,
        "base_url": rt.get("base_url") or "https://api.neuralwatt.com/v1",
    }


def _profile() -> Any:
    from providers import get_provider_profile

    profile = get_provider_profile("neuralwatt")
    if profile is None or profile.name != "neuralwatt":
        return None
    return profile


def _fmt_usd(v: Any) -> str:
    return f"${float(v):.4f}" if v is not None else "–"


def _fmt_energy_kwh(v: Any) -> str:
    return f"{float(v) * 1000:.1f} Wh" if v is not None else "–"


def cmd_models(profile: Any, kwargs: dict[str, Any]) -> str:
    """Live model catalog as a compact table."""
    models = profile.fetch_models(**kwargs, timeout=12.0) or []
    lines = ["NeuralWatt models:"]
    for mid in sorted(models or []):
        contract = profile._contract_for(mid)
        if not contract:
            lines.append(f"• {mid}")
            continue
        eff = ",".join(contract.get("supported_efforts") or ()) or "—"
        ctx = contract.get("max_context_length") or 0
        out = contract.get("max_output_tokens") or "∞"
        preview = " ⚠️" if profile.is_preview(mid) else ""
        vision = "👁" if contract.get("vision") else ""
        json_ok = "📡" if contract.get("json_mode") else ""
        lines.append(
            f"• {mid}{preview}{vision}{json_ok} — ctx {ctx:,} | out {out} | eff {eff}"
        )
    lines.append(f"({len(models)} models — ⚠️=preview, 👁=vision, 📡=json)")
    return "\n".join(lines)


def cmd_quota(profile: Any, kwargs: dict[str, Any]) -> str:
    """Account balance + subscription."""
    data = profile.fetch_quota(**kwargs, timeout=12.0)
    if not data:
        return "Quota unavailable (API error or no key)."
    balance = data.get("balance", {}) or {}
    usage = data.get("usage", {}) or {}
    sub = data.get("subscription", {}) or {}
    month = usage.get("current_month", {}) or {}
    lifetime = usage.get("lifetime", {}) or {}
    lines = [
        "NeuralWatt quota:",
        f"• Balance: {_fmt_usd(balance.get('credits_remaining_usd'))} left of "
        f"{_fmt_usd(balance.get('total_credits_usd'))}",
        f"• Month: {_fmt_usd(month.get('cost_usd'))} · {month.get('requests', 0):,} req · "
        f"{_fmt_energy_kwh(month.get('energy_kwh'))}",
        f"• Lifetime: {_fmt_usd(lifetime.get('cost_usd'))} · {lifetime.get('requests', 0):,} req",
    ]
    if sub:
        kwh_in = float(sub.get("kwh_included") or 0)
        kwh_used = float(sub.get("kwh_used") or 0)
        kwh_left = float(sub.get("kwh_remaining") or 0)
        overage = "⚠️ OVERAGE" if sub.get("in_overage") else "ok"
        lines.append(
            f"• Plan {sub.get('plan', '?')}: {kwh_incl_str(kwh_in)} included, "
            f"{kwh_incl_str(kwh_used)} used, {kwh_incl_str(kwh_left)} left [{overage}] "
            f"reset {sub.get('kwh_reset_date', '?')[:10]}"
        )
    lines.append(f"• Snapshot: {data.get('snapshot_at', '?')[:19]}")
    return "\n".join(lines)


def kwh_incl_str(v: float) -> str:
    return f"{v:.3f} kWh" if abs(v) >= 1 else f"{v * 1000:.1f} Wh"


def _session_row(s: dict[str, Any]) -> str:
    models = ",".join(s.get("models") or [])
    cache = float(s.get("cache_hit_fraction") or 0)
    return (
        f"• {str(s.get('session_id'))[:12]}… {s.get('turns', 0):>3}t "
        f"{models:<28} {_fmt_usd(s.get('cost_usd'))} "
        f"{_fmt_energy_kwh(s.get('energy_kwh'))} "
        f"cache {cache * 100:.0f}% ttft {float(s.get('ttft_avg_seconds') or 0):.1f}s"
    )


def cmd_sessions(profile: Any, kwargs: dict[str, Any], n: int) -> str:
    """Recent sessions with cost/energy/cache health."""
    data = profile.fetch_sessions(limit=max(1, min(int(n), 50)), **kwargs, timeout=20.0)
    if not data:
        return "Sessions unavailable."
    sessions = data.get("sessions") or []
    if not sessions:
        return "No sessions in the current period."
    lines = ["Recent NeuralWatt sessions:"]
    for s in sessions:
        lines.append(_session_row(s))
    return "\n".join(lines)


def cmd_families(profile: Any, kwargs: dict[str, Any], n: int) -> str:
    """Conversation families grouped by routing fingerprint."""
    data = profile.fetch_session_families(
        limit=max(1, min(int(n), 50)), **kwargs, timeout=20.0
    )
    if not data:
        return "Families unavailable."
    families = data.get("families") or []
    if not families:
        return "No conversation families found."
    lines = ["NeuralWatt conversation families:"]
    for f in families:
        t = f.get("totals", {}) or {}
        lines.append(
            f"• {str(f.get('root_session_id'))[:12]}… branches={f.get('size', 0)} "
            f"turns={t.get('turns', 0)} {_fmt_usd(t.get('cost_usd'))} "
            f"{_fmt_energy_kwh(t.get('energy_kwh'))}"
            + (" ⚠️ truncated" if f.get("truncated") else "")
        )
    return "\n".join(lines)


def cmd_session_detail(profile: Any, kwargs: dict[str, Any], session_id: str) -> str:
    """Per-turn detail + optimization flags for one session."""
    data = profile.fetch_session_detail(session_id, **kwargs, timeout=20.0)
    if not data:
        return f"Session {session_id}: unavailable."
    totals = data.get("totals", {}) or {}
    lines = [
        f"Session {session_id}:",
        f"• {totals.get('turns', 0)} turns · {_fmt_usd(totals.get('cost_usd'))} · "
        f"{_fmt_energy_kwh(totals.get('energy_kwh'))} · "
        f"reasoning {totals.get('reasoning_tokens', 0):,} tok",
    ]
    for flag in data.get("flags") or []:
        sev = {"warn": "⚠️", "error": "🔴", "info": "ℹ️"}.get(flag.get("severity"), "•")
        lines.append(f"{sev} {flag.get('headline', flag.get('code', ''))}")
    turns = data.get("turns") or []
    for tr in turns[-5:]:
        cache = float(tr.get("cache_hit_fraction") or 0)
        lines.append(
            f"  #{tr.get('request_id', '')[:8]} {cache * 100:>3.0f}% cache "
            f"{_fmt_usd(tr.get('cost_usd'))} {float(tr.get('ttft_seconds') or 0):.1f}s"
        )
    if not turns:
        lines.append("  (no per-turn rows)")
    return "\n".join(lines)


def cmd_requests(profile: Any, kwargs: dict[str, Any], n: int) -> str:
    """Latest per-request rows."""
    data = profile.fetch_requests(limit=max(1, min(int(n), 50)), **kwargs, timeout=20.0)
    if not data:
        return "Requests unavailable."
    requests = data.get("requests") or []
    if not requests:
        return "No requests in the current period."
    lines = ["Latest NeuralWatt requests:"]
    for r in requests:
        lines.append(
            f"• {r.get('requested_model', '?')} {r.get('total_tokens', 0):,}tok "
            f"{_fmt_usd(r.get('cost_usd'))} {float(r.get('ttft') or 0):.2f}s "
            f"[{r.get('finish_reason', '?')}]"
        )
    if data.get("has_more"):
        lines.append("(more pages — cursor not shown)")
    return "\n".join(lines)


def cmd_energy(profile: Any, kwargs: dict[str, Any], days: int) -> str:
    """Daily energy series."""
    data = profile.fetch_usage(period_days=int(days), **kwargs, timeout=20.0)
    if not data:
        return "Energy unavailable."
    lines = ["NeuralWatt energy:"]
    daily = data.get("daily") or []
    for d in daily[-10:]:
        lines.append(
            f"• {d.get('date')} {d.get('requests', 0):,}req {_fmt_energy_kwh(d.get('energy_kwh'))}"
        )
    totals = data.get("totals", {}) or {}
    lines.append(
        f"• period: {totals.get('requests', 0):,}req {_fmt_energy_kwh(totals.get('energy_kwh'))}"
    )
    return "\n".join(lines)


def _collapse_caused_by_model_switch(
    turns: list[dict[str, Any]], at_turn: int
) -> tuple[bool, str, str]:
    """Detect whether a cache collapse at ``at_turn`` sits on a model switch.

    Turns are 1-indexed (``metrics.at_turn``); compares the ``requested_model``
    of the turn BEFORE the collapse turn against the collapse turn itself.
    A differing pair means the prefix cache was invalidated by the model
    change, not by prompt churn.  Returns ``(switched, before, after)``.
    """
    if not turns or at_turn < 2 or at_turn > len(turns):
        return False, "", ""
    before = str(turns[at_turn - 2].get("requested_model") or "").strip()
    after = str(turns[at_turn - 1].get("requested_model") or "").strip()
    if not before or not after:
        return False, before, after
    return before != after, before, after


def analyze_recent(profile: Any, kwargs: dict[str, Any], n: int = 10) -> dict[str, Any]:
    """Structured analysis of recent sessions — agent-consumable."""
    sessions = (
        profile.fetch_sessions(limit=max(1, min(int(n), 50)), **kwargs, timeout=20.0)
        or {}
    ).get("sessions", [])

    top_cost: list[dict[str, Any]] = []
    low_cache: list[dict[str, Any]] = []
    pro_usage_cost = 0.0
    pro_turns = 0
    total_cost = 0.0
    total_energy_kwh = 0.0
    flagged: list[str] = []
    last_session_id: Optional[str] = None
    flags: list[dict[str, Any]] = []
    model_switch_collapses: list[dict[str, Any]] = []

    for s in sessions:
        total_cost += float(s.get("cost_usd") or 0)
        total_energy_kwh += float(s.get("energy_kwh") or 0)
        if float(s.get("cache_hit_fraction") or 0) < 0.6:
            low_cache.append(s)
        if "deepseek-v4-pro" in (s.get("models") or []):
            pro_turns += int(s.get("turns") or 0)
            pro_usage_cost += float(s.get("cost_usd") or 0)
        if last_session_id is None:
            last_session_id = str(s.get("session_id", ""))
        top_cost.append(s)

    top_cost.sort(key=lambda s2: float(s2.get("cost_usd") or 0), reverse=True)

    # Look for optimization flags on the most expensive sessions (bounded IO).
    for s in top_cost[:3]:
        sid = str(s.get("session_id", ""))
        if not sid:
            continue
        detail = profile.fetch_session_detail(sid, **kwargs, timeout=20.0)
        if detail:
            for flag in detail.get("flags") or []:
                if str(flag.get("severity")) in {"warn", "error"}:
                    flags.append(flag)
                    flagged.append(f"{str(flag.get('headline', flag.get('code')))}")
            # ADDITIONAL signal (does not replace the above): attribute each
            # collapse flag to a mid-conversation model switch when the
            # turn models differ across the collapse boundary.
            turns = detail.get("turns") or []
            for flag in detail.get("flags") or []:
                metrics = flag.get("metrics") or {}
                at_turn = int(metrics.get("at_turn") or 0)
                switched, before, after = _collapse_caused_by_model_switch(
                    turns, at_turn
                )
                if switched and str(flag.get("severity")) in {"warn", "error"}:
                    model_switch_collapses.append({
                        "session_id": sid,
                        "at_turn": at_turn,
                        "before": before,
                        "after": after,
                        "headline": str(flag.get("headline", flag.get("code"))),
                        "energy_joules": float(flag.get("energy_joules") or 0),
                    })

    return {
        "sessions_seen": len(sessions),
        "total_cost_usd": total_cost,
        "total_energy_kwh": total_energy_kwh,
        "top_cost": [
            (str(s.get("session_id")), float(s.get("cost_usd") or 0))
            for s in top_cost[:3]
        ],
        "low_cache_sessions": [str(s.get("session_id")) for s in low_cache],
        "pro_turns": pro_turns,
        "pro_cost_usd": pro_usage_cost,
        "flags": flagged,
        "model_switch_collapses": model_switch_collapses,
        "last_session_id": last_session_id,
    }


def cmd_analyze(profile: Any, kwargs: dict[str, Any], target: Optional[str]) -> str:
    """Recommendations from flags + usage — Telegram-friendly."""
    a = analyze_recent(profile, kwargs)
    total = a["total_cost_usd"]
    lines = ["NeuralWatt analysis:"]
    lines.append(
        f"• {a['sessions_seen']} sessions · {_fmt_usd(total)} · "
        f"{_fmt_energy_kwh(a['total_energy_kwh'])}"
    )
    if a["flags"]:
        lines.append("⚠️ Flags:")
        for f in a["flags"][:4]:
            lines.append(f"  — {f}")
    if a["model_switch_collapses"]:
        lines.append("🔄 Collapses ON MODEL SWITCH (cache was warm — switch wiped it):")
        for msc in a["model_switch_collapses"][:4]:
            kj = float(msc["energy_joules"]) / 1000
            lines.append(
                f"  — {msc['before']}→{msc['after']} @ turn {msc['at_turn']} "
                f"({kj:.1f} kJ) — switch only at /new"
            )
    if a["low_cache_sessions"]:
        lines.append(
            f"ℹ️ {len(a['low_cache_sessions'])} session(s) with <60% cache-hit — "
            f"toolset/system-prompt churn likely"
        )
    if a["pro_cost_usd"] > 0:
        share = (a["pro_cost_usd"] / total * 100) if total else 0.0
        lines.append(
            f"ℹ️ {a['pro_turns']} pro turns = {_fmt_usd(a['pro_cost_usd'])} "
            f"({share:.0f}% of spend) — drop trivial ones to flash"
        )
    if not a["flags"] and not a["low_cache_sessions"] and a["pro_cost_usd"] == 0:
        lines.append("✅ Cache healthy, no flags, no pro on trivial traffic.")
    return "\n".join(lines)


def handle_neuralwatt_command(
    args: str,
    *,
    surface: str = "gateway",
) -> str:
    """Dispatch a ``/neuralwatt`` invocation. Returns text to show the user.

    ``args`` is everything after ``/neuralwatt`` (stripped of the command
    word).  Subcommands: models|quota|sessions [N]|families [N]|
    session <id>|requests [N]|energy [days]|analyze [id].
    """
    default_n = "10"
    default_days = "30"
    parts = (args or "").strip().split()
    subcmd = (parts[0] or "help").lower() if parts else "help"

    profile = _profile()
    if profile is None:
        return "NeuralWatt provider profile is not available."
    kwargs = _runtime_profile_kwargs()
    if kwargs is None:
        return "NeuralWatt API key is not configured (set NEURALWATT_API_KEY or providers.neuralwatt)."
    if not isinstance(kwargs, dict):
        return "NeuralWatt runtime resolution failed."

    try:
        if subcmd == "help" or subcmd == "":
            return (
                "NeuralWatt commands:\n"
                "• models — live model catalog\n"
                "• quota — balance + plan\n"
                "• sessions [N] — recent sessions\n"
                "• families [N] — conversation families\n"
                "• session <id> — per-turn detail + flags\n"
                "• requests [N] — latest requests\n"
                "• energy [days] — daily energy\n"
                "• analyze — optimization recommendations"
            )
        if subcmd == "models":
            return cmd_models(profile, kwargs)
        if subcmd == "quota":
            return cmd_quota(profile, kwargs)
        if subcmd == "sessions":
            return cmd_sessions(
                profile,
                kwargs,
                int(parts[1])
                if len(parts) > 1 and parts[1].isdigit()
                else int(default_n),
            )
        if subcmd == "families":
            return cmd_families(
                profile,
                kwargs,
                int(parts[1])
                if len(parts) > 1 and parts[1].isdigit()
                else int(default_n),
            )
        if subcmd == "session":
            if len(parts) < 2:
                return "Usage: /neuralwatt session <id>"
            return cmd_session_detail(profile, kwargs, parts[1])
        if subcmd == "requests":
            return cmd_requests(
                profile,
                kwargs,
                int(parts[1])
                if len(parts) > 1 and parts[1].isdigit()
                else int(default_n),
            )
        if subcmd == "energy":
            return cmd_energy(
                profile,
                kwargs,
                int(parts[1])
                if len(parts) > 1 and parts[1].isdigit()
                else int(default_days),
            )
        if subcmd == "analyze":
            return cmd_analyze(profile, kwargs, parts[1] if len(parts) > 1 else None)
        return f"Unknown subcommand '{parts[0]}'. Try /neuralwatt with no args."
    except Exception as exc:
        logger.warning("neuralwatt command %s failed: %s", subcmd, exc)
        return f"NeuralWatt command failed: {exc}"


if __name__ == "__main__":
    import sys

    print(handle_neuralwatt_command(" ".join(sys.argv[1:]), surface="agent"))
