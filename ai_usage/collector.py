from __future__ import annotations

import inspect
import json
import math
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ai_usage.balance import balance_provider
from ai_usage.budget import budget_provider
from ai_usage.contract import PROVIDERS, iso
from ai_usage.spend import spend_provider
from ai_usage.tokensum import tokensum_provider


def _default_source(key: str) -> str:
    for provider_key, _label, mode in PROVIDERS:
        if provider_key == key:
            return "official" if mode in ("budget", "balance") else "hermes"
    return "hermes"


def _carry_forward(prev: Optional[dict], key: str) -> Optional[dict]:
    if not prev:
        return None
    for p in prev.get("providers", []):
        if p.get("key") == key:
            if p.get("state") not in ("ok", "stale"):
                return None
            carried = dict(p)
            carried["state"] = "stale"
            carried.setdefault("source", _default_source(key))
            return carried
    return None


def _hermes_error_row(key: str, label: str, mode: str) -> dict:
    return {
        "key": key,
        "label": label,
        "mode": mode,
        "source": "hermes",
        "state": "error",
        "windows": [],
        "detail": "db error",
    }


def _state_db_row(
    key: str,
    label: str,
    mode: str,
    conn: Optional[sqlite3.Connection],
    now: datetime,
    prev: Optional[dict],
) -> dict:
    """Build a fresh Hermes row, or preserve a prior row when state.db is unavailable."""
    try:
        if conn is None:
            raise sqlite3.OperationalError("db unavailable")
        make = spend_provider if mode == "spend" else tokensum_provider
        row = make(key, label, conn, now)
    except sqlite3.Error:
        return _carry_forward(prev, key) or _hermes_error_row(key, label, mode)
    row["source"] = "hermes"
    return row


def _supports_budget(fetch_usage: Callable[..., object]) -> bool:
    """Return whether the callable explicitly accepts the cooperative budget."""
    try:
        signature = inspect.signature(fetch_usage)
    except (TypeError, ValueError):
        return False
    return "budget_seconds" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )



# ---------------------------------------------------------------------------
# Deadline baseline
#
# collect()'s budget was a flat 90s measured from ITS OWN entry, which cannot
# bound what the scheduler actually limits. AIUsageCollector's
# ExecutionTimeLimit (PT6M = 360s) runs from the moment Task Scheduler starts
# the wrapper, and on this box interpreter startup plus imports is where nearly
# all of that goes -- ~229s at BelowNormal. A budget blind to that prefix is
# respected in full while the task is killed anyway, and a kill produces
# NOTHING: no snapshot, no diagnostics, just a stale ai-tokens.json. The 90s
# only ever bounded the tail of an unbounded prefix.
#
# So bin/ai_usage_collector_run.ps1 publishes the ABSOLUTE instant the run must
# finish by. An instant, not a duration: the remaining time is computed here,
# AFTER the imports have been paid, so the prefix subtracts itself. When the
# variable is absent -- a hand-run ``python -m ai_usage``, or an older wrapper
# -- the historical constant stands, so nothing regresses.
#
# This is strictly a TIGHTENING mechanism. The result is capped at the
# historical constant, so a fast import cannot buy a longer run than collect()
# was ever trusted with, and a garbage-large published value cannot buy an
# unbounded one.
DEADLINE_EPOCH_ENV = "HERMES_AI_USAGE_DEADLINE_EPOCH"
FALLBACK_DEADLINE_SECONDS = 90.0
#: Slack, ON TOP of a full deadline, that must remain before the httpx warm-up is
#: worth paying. The warm-up measured ~48s at the task's BelowNormal priority
#: (import prefix 96.35s alone vs 144.17s with it); 90s leaves room for that plus
#: the variance that BelowNormal contention produces on this box.
WARMUP_HEADROOM_SECONDS = 90.0


def _derive_deadline_seconds(
    now_epoch: Optional[Callable[[], float]] = None,
) -> float:
    """Seconds collect() may spend, measured against the TASK's clock.

    ``now_epoch`` is resolved at CALL time, not bound as a default: a default of
    ``time.time`` captures the function object at import and would sail straight
    past ``monkeypatch.setattr(collector.time, "time", ...)``, which is how the
    wiring test for this was silently reading the real clock.
    """
    clock = now_epoch or time.time
    raw = (os.environ.get(DEADLINE_EPOCH_ENV) or "").strip()
    if not raw:
        return FALLBACK_DEADLINE_SECONDS
    try:
        finish_by = float(raw)
    except (TypeError, ValueError):
        return FALLBACK_DEADLINE_SECONDS
    if not math.isfinite(finish_by):
        return FALLBACK_DEADLINE_SECONDS
    remaining = finish_by - clock()
    return max(0.0, min(remaining, FALLBACK_DEADLINE_SECONDS))


def _raw_remaining_seconds(
    now_epoch: Optional[Callable[[], float]] = None,
) -> Optional[float]:
    """Seconds left before the TASK must finish -- UNCAPPED, unlike the deadline.

    ``_derive_deadline_seconds`` clamps to FALLBACK_DEADLINE_SECONDS, which is
    right for budgeting providers but destroys exactly the signal needed here:
    a clamped 90.0 cannot distinguish "240s of slack" from "91s of slack".
    ``None`` means the runner published no finish instant (a hand-run
    ``python -m ai_usage``, or an older wrapper), i.e. there is no
    ExecutionTimeLimit to overrun.
    """
    clock = now_epoch or time.time
    raw = (os.environ.get(DEADLINE_EPOCH_ENV) or "").strip()
    if not raw:
        return None
    try:
        finish_by = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(finish_by):
        return None
    return finish_by - clock()


def _warmup_is_affordable(
    now_epoch: Optional[Callable[[], float]] = None,
) -> bool:
    """Whether paying the httpx warm-up still leaves the task able to finish.

    Warming does NOT shorten the run -- and the first version of this shipped
    on the mistaken belief that it was wall-clock neutral. It is not. The 90s
    budget is a CAP, so moving ~48s of import cost OUT of it and into the
    uncapped prefix does not shrink the budget by 48s; it simply adds 48s to the
    process. Measured effect on AIUsageCollector's PT6M ExecutionTimeLimit:
    terminations went from 3.3% of launches (16/491 over the 48h before) to
    19.3% (17/88 after), and a terminated run writes NOTHING -- no snapshot, no
    diagnostics -- which is strictly worse than the ordered starvation the
    warm-up was introduced to cure.

    So warm only out of genuine slack. Below that, skip it and let the first
    provider pay the import inside its budget (the pre-warm-up behaviour): some
    rows go stale, but the run COMPLETES and writes a snapshot instead of being
    killed. Note this is a different question from whether a thinly-budgeted
    provider can survive the import -- it cannot either way, since a Python
    import is not interruptible by an httpx timeout. What is being protected
    here is the task, not the provider.
    """
    remaining = _raw_remaining_seconds(now_epoch)
    if remaining is None:
        return True
    return remaining >= FALLBACK_DEADLINE_SECONDS + WARMUP_HEADROOM_SECONDS


def _episode_model_for(finding: dict) -> str:
    """Synthesize collect()'s (provider, model) episode key for a finding.

    Deliberately NOT a routable model slug -- same shape as detector B's
    "{provider}:pool" -- which is what keeps Phase 2's reroute buttons off
    these alerts (events.override_buttons.buttons_for gates on
    detector == "runtime").
    """
    if finding.get("kind") == "balance":
        return "balance"
    return f"{finding.get('window_id')}-window"


def _future_resets_at(raw: object) -> str:
    """Pass `resets_at` through ONLY when it is genuinely in the future.

    Phase 1's episode reaper forgets any episode whose `resets_at` has passed
    (events/rate_limit_signal.py, _episode_expired) -- correct for a real rate
    limit, where a passed reset means the limit lifted.

    It is WRONG for this detector's data. Observed live 2026-08-18: anthropic's
    weekly window reported used_pct 100.0 with resets_at 2026-08-17 -- a day in
    the PAST on a window that was still fully capped. Handing that value to
    record() got the episode reaped on the very next read, so every 5-minute
    poll looked like a brand-new episode and re-alerted. Caught in production
    within two cycles: 00:20:13 and 00:25:14, same window, same outcome.

    The Phase 3 design rule was already "resets_at is display-only, never
    branch on it" -- but that was enforced in quota_signal.evaluate(), one layer
    too shallow. The value still reached a consumer that DOES branch on it.
    A stale timestamp is dropped here rather than poisoning the episode.
    """
    if not raw:
        return ""
    try:
        from datetime import datetime, timezone
        text = str(raw).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(raw) if parsed > datetime.now(timezone.utc) else ""
    except Exception:
        return ""


def _emit_quota_findings(snapshot: dict) -> None:
    """Report ai_usage.quota_signal findings as MODEL_RATE_LIMITED alerts.

    REPORT-ONLY (the defining constraint of Phase 3): Claude Code and the
    Codex CLI are separate processes with their own model selection, so
    Hermes cannot reroute them from here. This calls record() only -- never
    clear() -- so a window that goes missing from one snapshot to the next
    (Codex nulls its 5h window exactly when the weekly is capped) is never
    misread as a recovery that closes an open episode. evaluate() already
    treats an absent/None window as "no finding"; the absence of a finding
    here is therefore silently dropped, not translated into a clear().

    Each finding is emitted independently so one detector/record failure
    (e.g. a raising record()) cannot suppress the rest of this snapshot's
    findings.
    """
    from ai_usage.quota_signal import evaluate
    from events.rate_limit_signal import record

    for finding in evaluate(snapshot):
        try:
            record(
                provider=finding["provider"],
                model=_episode_model_for(finding),
                reason="quota_window",
                detector="usage_poller",
                outcome=finding["outcome"],
                resets_at=_future_resets_at(finding.get("resets_at")),
                # Without this the alert's `source` falls back through
                # HERMES_CRON_JOB_NAME / HERMES_AGENT_SOURCE to the generic
                # "agent-loop", which reads as if the agent runtime raised it.
                # These come from the 5-minute usage poller, not a model call.
                source_hint="usage-poller",
            )
        except Exception:
            continue


def _diagnostic(key: str, outcome: str, elapsed: float, budget: float) -> dict:
    return {
        "key": key,
        "outcome": outcome,
        "elapsed_ms": max(0, round(elapsed * 1000)),
        "budget_seconds": max(0.0, round(budget, 3)),
    }


def _flag_duplicate_accounts(providers: list[dict]) -> None:
    """Mark rows whose credentials resolve to the SAME upstream account.

    Two provider rows exist to track two DISTINCT subscriptions. Two tokens
    minted against ONE account produce two rows with identical numbers and no
    visible symptom -- exactly the 2026-08-23 defect where the isolated
    ``~/.claude-anthropic2`` login landed on the already-signed-in browser
    account, so "Claude" and "Claude 2" both reported diegodearagaous@gmail.com.

    The first row of a colliding group keeps its data (it is the one whose
    numbers are genuinely its own); every later row is flagged so the tray shows
    the collision instead of a plausible duplicate. Mutates in place.
    """
    first_by_account: dict[str, dict] = {}
    for row in providers:
        account = str(row.get("account_uuid") or "").strip()
        if not account or row.get("state") != "ok":
            continue
        incumbent = first_by_account.get(account)
        if incumbent is None:
            first_by_account[account] = row
            continue
        email = row.get("account_email") or account
        row["state"] = "error"
        row["duplicate_of"] = incumbent.get("key")
        row["detail"] = f"same account as {incumbent.get('label')} ({email})"


#: Provider modes that spend the deadline on a network fetch. The state.db modes
#: read a local sqlite file in ~0s, so they neither need a budget nor should they
#: dilute the fair share computed for the ones that do.
_BUDGETED_MODES = ("budget", "balance")


def _default_warmup() -> None:
    """Pay the process-wide lazy-httpx cost BEFORE the deadline clock starts.

    ``agent.account_usage`` imports httpx on FIRST USE (commit fea63d0d16, which
    stopped a module-scope ``import httpx`` from blowing the PT6M ETL). That fix
    is right, but it relocated the cost rather than removing it: whichever
    provider ran first paid ``import httpx`` plus the first ``httpx.Client()``
    -- measured on this box at 13.28s and 4.22s respectively, against ~0.03s for
    every client after -- and paid it INSIDE ``collect()``'s cooperative budget.

    Since PROVIDERS is ordered, "first" is always ``anthropic``, which is how one
    provider came to spend 87s of a 90s budget and starve the other seven into
    ``deadline_exhausted`` (measured 2026-08-25: anthropic 27.97s vs anthropic2
    1.17s vs openai-codex 0.38s in one process, same code path, same endpoints).

    Doing it here makes the cost a process-startup expense again, billed to no
    provider. It does NOT make the run shorter -- the same work happens either
    way, and the task is still bounded by its ExecutionTimeLimit. That is safe
    because the runner exports HERMES_AI_USAGE_DEADLINE_EPOCH as an ABSOLUTE
    finish time, so time spent here shrinks the derived deadline by exactly as
    much and the process still lands inside the ETL.

    Best-effort by construction: warming is an optimisation, and a failure here
    must never cost a collection that would otherwise have succeeded.
    """
    from agent.account_usage import _ensure_httpx

    httpx = _ensure_httpx()
    # Constructing (and discarding) one client is what materialises the default
    # SSL context / certifi bundle that every later client then reuses.
    httpx.Client().close()


def collect(
    *,
    db_path: str,
    prev: Optional[dict],
    fetch_usage: Callable[..., object],
    now: Optional[datetime] = None,
    deadline_seconds: Optional[float] = None,
    warmup: Optional[Callable[[], None]] = None,
    _monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    now = now or datetime.now(timezone.utc)
    # MUST precede `started`: see _default_warmup. Swallowing the exception is
    # deliberate -- a warm-up is an optimisation and may not cost a collection.
    if warmup is not None and _warmup_is_affordable():
        try:
            warmup()
        except Exception:  # noqa: BLE001 - advisory; never fatal to collection
            pass
    started = _monotonic()
    if deadline_seconds is None:
        deadline_seconds = _derive_deadline_seconds()
    deadline_seconds = max(0.0, float(deadline_seconds))
    deadline = started + deadline_seconds
    accepts_budget = _supports_budget(fetch_usage)

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        conn = None

    providers: list[dict] = []
    attempts: list[dict] = []
    # Fair-share denominator: how many budgeted providers are still AHEAD of us,
    # including the current one. Decremented as each is attempted, so time a fast
    # provider leaves unspent is redistributed to the ones behind it instead of
    # being hoarded by whoever happens to be next.
    budgeted_left = sum(1 for _k, _l, m in PROVIDERS if m in _BUDGETED_MODES)
    try:
        for key, label, mode in PROVIDERS:
            attempt_started = _monotonic()
            remaining = max(0.0, deadline - attempt_started)
            # A single provider may spend at most its equal share of what is
            # LEFT, never the whole pot. Before this, the budget was simply
            # `remaining`, so the list order decided who got data: provider #1
            # could drain all 90s and every provider behind it recorded
            # deadline_exhausted with a carried-forward, hours-old row.
            # `remaining` still governs whether we attempt at all; `share` governs
            # how long the attempt may take.
            if mode in _BUDGETED_MODES:
                share = remaining / max(1, budgeted_left)
                budgeted_left -= 1
            else:
                share = remaining

            if remaining <= 0:
                if mode in ("budget", "balance"):
                    make = budget_provider if mode == "budget" else balance_provider
                    providers.append(
                        _carry_forward(prev, key)
                        or {**make(key, label, None), "source": "official"}
                    )
                else:
                    providers.append(
                        _carry_forward(prev, key)
                        or _hermes_error_row(key, label, mode)
                    )
                attempts.append(_diagnostic(key, "deadline_exhausted", 0.0, share))
                continue

            if mode in ("budget", "balance"):
                make = budget_provider if mode == "budget" else balance_provider
                outcome = "unavailable"
                try:
                    if accepts_budget:
                        snapshot = fetch_usage(key, budget_seconds=share)
                    else:
                        snapshot = fetch_usage(key)
                except Exception:
                    snapshot = None
                    outcome = "exception"
                finished = _monotonic()
                if finished >= deadline:
                    outcome = "deadline_exhausted"
                elif snapshot is not None and getattr(snapshot, "available", False):
                    outcome = "ok"

                if snapshot is None or not getattr(snapshot, "available", False):
                    row = _carry_forward(prev, key)
                    if row is None:
                        row = make(key, label, snapshot)
                        row["source"] = "official"
                else:
                    row = make(key, label, snapshot)
                    row["source"] = "official"
                providers.append(row)
                attempts.append(_diagnostic(key, outcome, finished - attempt_started, share))
                continue

            row = _state_db_row(key, label, mode, conn, now, prev)
            providers.append(row)
            outcome = "ok"
            if row.get("state") == "stale":
                outcome = "stale"
            elif row.get("state") == "error":
                outcome = "exception"
            attempts.append(
                _diagnostic(key, outcome, _monotonic() - attempt_started, remaining)
            )
    finally:
        if conn is not None:
            conn.close()

    _flag_duplicate_accounts(providers)

    elapsed = max(0.0, _monotonic() - started)
    result = {
        "generated_at": iso(now),
        "providers": providers,
        "diagnostics": {
            "elapsed_ms": round(elapsed * 1000),
            "deadline_seconds": deadline_seconds,
            "providers": attempts,
        },
    }

    # Phase 3: report quota-threshold findings as MODEL_RATE_LIMITED alerts.
    # This must never affect the snapshot collect() returns -- it runs last,
    # over the already-built result, behind a blanket try/except. The
    # collector feeds the tray; a detector defect must never break usage
    # collection or corrupt the snapshot it returns.
    try:
        _emit_quota_findings(result)
    except Exception:
        pass

    return result


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
