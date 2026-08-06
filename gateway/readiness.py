"""Bounded, non-destructive readiness probes for authenticated health surfaces."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home


_DISK_DEGRADED_PERCENT = 90.0

# Bounded scan of named-profile stores (<home>/profiles/*/state.db). The
# probe reports aggregate counts only — never profile names or paths.
# Bounded two ways: store count AND elapsed wall time — each sqlite connect
# carries a 1s busy timeout, so a count limit alone still risks a ~30s
# worst case inside a health poll.
_PROFILE_STORE_PROBE_LIMIT = 32
_PROFILE_STORE_PROBE_BUDGET_SECONDS = 1.0

# Rotating start offset for the capped profile scan. A fixed sorted()[:cap]
# window would leave every profile past the cap permanently uninspected —
# the same "silently unchecked store" blind spot OOF-76 fixed, just moved
# past index 32. Advancing the window each poll gives eventual coverage of
# the whole fleet. Plain int mutation under CPython's GIL; a lost race
# costs at most one repeated window.
_profile_probe_cursor = 0


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _inspect_state_store(path: Path) -> tuple[str, int]:
    """Read-only inspection of one session store.

    Returns ``(status, mismatch_count)`` where status is ``ok`` (readable
    and schema-converged), ``stale`` (readable but behind the SCHEMA_SQL
    contract — OOF-76: dashboard session routes 500 on such stores while
    naive readable-only probes stay green), or ``error`` (unreadable or
    corrupt). Never takes a write reservation.
    """
    # A readiness probe must never compete with normal state writers. A
    # read-only schema query still catches unreadable/corrupt databases
    # without taking a write reservation on every health poll.
    # ``closing(...)`` is required: sqlite3's connection context manager
    # only commits/rolls back — it never closes, so a bare ``with
    # sqlite3.connect(...)`` leaks one connection (and its fds) per
    # health poll in the long-running gateway (#69678/#69567 bug class).
    try:
        from hermes_state_common import state_schema_mismatches

        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            # Errors from the diff itself (locked, malformed mid-read)
            # propagate to the outer handler and report "error" — a store
            # whose schema cannot be read is not healthy. Only contract
            # construction fails open (inside state_schema_mismatches).
            mismatches = state_schema_mismatches(conn)
        if mismatches:
            return "stale", len(mismatches)
        return "ok", 0
    except Exception:
        return "error", 0


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    status, mismatch_count = _inspect_state_store(path)
    if status == "error":
        return _check("degraded", "unreadable")
    if status == "stale":
        return _check("degraded", "schema behind contract", mismatches=mismatch_count)
    return _check("ok")


def _probe_profile_state_dbs(home: Path) -> dict[str, Any]:
    """Aggregate schema/readability status of named-profile session stores.

    Named profiles whose gateway is not running never get a writable open,
    so their stores drift behind the dashboard's read surface silently
    (OOF-76) — the exact class of store this probe exists to surface.
    Reports counts only: never profile names or paths. Bounded to
    ``_PROFILE_STORE_PROBE_LIMIT`` stores per poll.
    """
    profiles_root = home / "profiles"
    try:
        # Name listing only — no per-entry stat yet. The count cap must bound
        # the stat traffic too, not just the sqlite opens: this runs inside a
        # health poll and a pathological profiles dir must not turn the
        # listing itself into the cost.
        names = sorted(os.listdir(profiles_root))
    except FileNotFoundError:
        return _check("ok", "no profiles")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)
    stores: list[Path] = []
    # Strict entry bound: cap the entries EXAMINED, not the stores found —
    # otherwise a profiles dir full of non-store entries still stats every
    # one of them inside the health poll. Entries past the cap are
    # unverified, so the scan is truncated regardless of what they hold.
    truncated = len(names) > _PROFILE_STORE_PROBE_LIMIT
    if truncated:
        # Rotate the capped window across polls so entries past the cap are
        # eventually inspected instead of staying invisible forever.
        global _profile_probe_cursor
        start = _profile_probe_cursor % len(names)
        _profile_probe_cursor = start + _PROFILE_STORE_PROBE_LIMIT
        window = (names[start:] + names[:start])[:_PROFILE_STORE_PROBE_LIMIT]
    else:
        window = names
    for name in window:
        candidate = profiles_root / name / "state.db"
        try:
            if candidate.is_file():
                stores.append(candidate)
        except OSError:
            continue
    if not stores:
        if truncated:
            return _check("ok", "profile store probe incomplete", truncated=True)
        return _check("ok", "no profiles")
    stale = 0
    errors = 0
    checked = 0
    deadline = time.monotonic() + _PROFILE_STORE_PROBE_BUDGET_SECONDS
    for store in stores:
        if checked and time.monotonic() >= deadline:
            truncated = True
            break
        status, _count = _inspect_state_store(store)
        checked += 1
        if status == "stale":
            stale += 1
        elif status == "error":
            errors += 1
    # A truncated scan (count cap or time budget) is incomplete evidence,
    # not evidence of failure: even on this advisory check, a fleet with
    # >_PROFILE_STORE_PROBE_LIMIT healthy profiles would otherwise sit
    # permanently "degraded" on every poll, training operators to ignore
    # the signal. Truncation is surfaced explicitly via detail/extra so the
    # verdict stays honest about its coverage.
    result_status = "degraded" if (stale or errors) else "ok"
    extra: dict[str, Any] = {"checked": checked, "stale": stale, "errors": errors}
    if truncated:
        extra["truncated"] = True
    detail = None
    if stale and errors:
        detail = "profile stores behind schema contract and unreadable"
    elif stale:
        detail = "profile stores behind schema contract"
    elif errors:
        detail = "profile stores unreadable"
    elif truncated:
        detail = "profile store probe incomplete"
    return _check(result_status, detail, **extra)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
        status = "degraded" if used_pct >= _DISK_DEGRADED_PERCENT else "ok"
        return _check(status, used_percent=used_pct, free_bytes=usage.free)
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_gateway(runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime_status.get("gateway_state") or "unknown")
    platforms = runtime_status.get("platforms")
    connected = 0
    configured = 0
    if isinstance(platforms, dict):
        configured = len(platforms)
        connected = sum(
            1
            for value in platforms.values()
            if isinstance(value, dict)
            and str(value.get("state") or value.get("status") or "").lower()
            in {"connected", "running", "ok"}
        )
    status = "ok" if state in {"running", "draining"} else "degraded"
    return _check(status, state=state, connected_platforms=connected, platforms=configured)


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return bounded readiness diagnostics without mutating runtime state.

    The detailed health endpoint is authenticated. Even there, probes expose
    status and counts only: never config values, credentials, paths, commands,
    queue payloads, or exception messages.
    """
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    checks = {
        "state_db": _probe_state_db(home),
        "profile_state_dbs": _probe_profile_state_dbs(home),
        "config": _probe_config(home),
        "model": _check("ok" if str(configured_model or "").strip() else "degraded"),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    # profile_state_dbs is advisory: named-profile schema drift is real user
    # impact (dashboard session routes 500) but restart-driven remediation
    # cannot heal an idle profile store — only the dashboard's heal-capable
    # read path can. Folding it into the restart-driving overall verdict
    # would invite an unhealable-signal restart loop (OOF-39 class), so it
    # is surfaced in checks but excluded from the rollup.
    advisory_checks = {"profile_state_dbs"}
    overall = (
        "ok"
        if all(
            item.get("status") == "ok"
            for name, item in checks.items()
            if name not in advisory_checks
        )
        else "degraded"
    )
    return {"status": overall, "checks": checks}


__all__ = ["collect_runtime_readiness"]
