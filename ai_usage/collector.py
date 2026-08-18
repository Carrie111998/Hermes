from __future__ import annotations

import inspect
import json
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
from ai_usage.manual_snapshot import MANUAL_PROVIDER_KEYS, read_manual_snapshot
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
                resets_at=finding.get("resets_at") or "",
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


def collect(
    *,
    db_path: str,
    prev: Optional[dict],
    fetch_usage: Callable[..., object],
    now: Optional[datetime] = None,
    manual_store_path: Optional[str] = None,
    deadline_seconds: float = 90.0,
    _monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    now = now or datetime.now(timezone.utc)
    started = _monotonic()
    manual = read_manual_snapshot(manual_store_path, now) if manual_store_path else {}
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
    try:
        for key, label, mode in PROVIDERS:
            attempt_started = _monotonic()
            remaining = max(0.0, deadline - attempt_started)

            if remaining <= 0:
                if key in MANUAL_PROVIDER_KEYS and key in manual:
                    providers.append(dict(manual[key]))
                elif mode in ("budget", "balance"):
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
                attempts.append(_diagnostic(key, "deadline_exhausted", 0.0, remaining))
                continue

            if key in MANUAL_PROVIDER_KEYS and key in manual:
                providers.append(dict(manual[key]))
                attempts.append(_diagnostic(key, "ok", _monotonic() - attempt_started, remaining))
                continue

            if mode in ("budget", "balance"):
                make = budget_provider if mode == "budget" else balance_provider
                outcome = "unavailable"
                try:
                    if accepts_budget:
                        snapshot = fetch_usage(key, budget_seconds=remaining)
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
                attempts.append(_diagnostic(key, outcome, finished - attempt_started, remaining))
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
