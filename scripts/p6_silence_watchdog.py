#!/usr/bin/env python3
"""Durable silence watchdog for the P6 fleet-controller enforce lane.

Session-independent: runs on its own Windows scheduled task
(Hermes-Claude-Fleet-Silence-Watchdog, every 10 min), so it keeps watching
after any agent session ends or the box reboots. It checks the event bus for
a recent CLAUDE_FLEET_RESULT (the controller emits one every 5-min pass, even
a no_action one), and if none has arrived within --max-age-seconds it emits a
WATCHDOG_SELF_DEGRADED event that the notification layer delivers to Telegram.

Why 20 min default: the controller fires every 5 min, so four consecutive
missed passes is a real stop, not IgnoreNew overlap or scheduler jitter.

Cooldown: a small state file suppresses re-alerts to at most one per
--re-alert-seconds during a sustained silence, and emits one recovery event
when passes resume. Mirrors the watchdog_sweep self-degraded cadence.

Exit codes: 0 healthy, 3 silent (alert emitted or in cooldown), 4 bus/runtime
error (cannot verify — surfaced via the task's LastTaskResult, not paged).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SOURCE = "claude-fleet-silence-watchdog"
DEFAULT_MAX_AGE_SECONDS = 1200.0     # 20 min = 4 missed 5-min passes
DEFAULT_RE_ALERT_SECONDS = 3600.0    # re-ping a sustained silence hourly


def _parse_iso(ts: str):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def newest_result_epoch(results) -> float | None:
    """Newest CLAUDE_FLEET_RESULT timestamp as epoch, or None if there are
    none / none parseable. Takes the MAX over timestamps rather than trusting
    row order."""
    epochs = [e for e in (_parse_iso(r.timestamp) for r in results) if e is not None]
    return max(epochs) if epochs else None


def evaluate(newest_epoch: float | None, now: float, max_age_seconds: float):
    """Pure: (is_silent, age_seconds). No events ever => silent with age None."""
    if newest_epoch is None:
        return True, None
    age = now - newest_epoch
    return age > max_age_seconds, age


def decide_emit(is_silent: bool, now: float, state: dict, re_alert_seconds: float):
    """Pure cooldown/edge logic. Returns (action, new_state) where action is
    one of 'alert' (rising edge or re-alert due), 'recovered' (silence just
    ended), or 'none'."""
    was_silent = bool(state.get("silent"))
    last_alert = state.get("last_alert_at")
    if is_silent:
        due = (
            not was_silent
            or not isinstance(last_alert, (int, float))
            or (now - last_alert) >= re_alert_seconds
        )
        if due:
            return "alert", {"silent": True, "last_alert_at": now}
        return "none", {"silent": True, "last_alert_at": last_alert}
    # not silent
    if was_silent:
        return "recovered", {"silent": False, "last_alert_at": None}
    return "none", {"silent": False, "last_alert_at": None}


def _load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    ap.add_argument("--re-alert-seconds", type=float, default=DEFAULT_RE_ALERT_SECONDS)
    ap.add_argument("--state", type=Path,
                    default=Path.home() / ".hermes" / "fleet_control" / "silence_state.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print the decision but never emit or persist state")
    args = ap.parse_args(argv)

    from events.bus import EventBus
    from events.schema import EventType, Priority

    now = time.time()
    try:
        bus = EventBus()
        since = datetime.fromtimestamp(now - 6 * 3600, tz=timezone.utc).isoformat()
        results = bus.query(event_type=EventType.CLAUDE_FLEET_RESULT, since=since)
    except Exception as exc:  # cannot verify — do not page, surface via exit code
        print(f"p6-silence-watchdog: bus error, cannot verify: {exc}")
        return 4

    newest = newest_result_epoch(results)
    is_silent, age = evaluate(newest, now, args.max_age_seconds)
    state = _load_state(args.state)
    action, new_state = decide_emit(is_silent, now, state, args.re_alert_seconds)
    age_txt = "never" if age is None else f"{age / 60.0:.1f} min"

    if args.dry_run:
        verdict = "SILENT" if is_silent else "healthy"
        print(f"p6-silence-watchdog[dry-run]: {verdict} (newest result {age_txt}); "
              f"would_action={action} (no emit, no state write)")
        return 3 if is_silent else 0

    if action in ("alert", "recovered"):
        try:
            if action == "alert":
                bus.emit(
                    event_type=EventType.WATCHDOG_SELF_DEGRADED,
                    source=SOURCE,
                    priority=Priority.HIGH,
                    payload={
                        "reason": "p6 enforce lane silent",
                        "detail": (
                            f"No claude_fleet_result in {age_txt}; the P6 fleet "
                            f"controller (Hermes-Claude-Fleet-Controller, 5-min) "
                            f"appears to have stopped. Enforce lane not observing."
                        ),
                        "last_result_age_min": None if age is None else round(age / 60.0, 1),
                        "max_age_min": round(args.max_age_seconds / 60.0, 1),
                        "status": "degraded",
                    },
                )
            else:  # recovered
                bus.emit(
                    event_type=EventType.WATCHDOG_SELF_DEGRADED,
                    source=SOURCE,
                    priority=Priority.NORMAL,
                    payload={
                        "reason": "p6 enforce lane recovered",
                        "detail": "claude_fleet_result events resumed.",
                        "status": "recovered",
                    },
                )
        except Exception as exc:
            print(f"p6-silence-watchdog: emit failed: {exc}")
            return 4

    try:
        _save_state(args.state, new_state)
    except OSError as exc:
        print(f"p6-silence-watchdog: state save failed: {exc}")

    verdict = "SILENT" if is_silent else "healthy"
    print(f"p6-silence-watchdog: {verdict} (newest result {age_txt}); action={action}")
    return 3 if is_silent else 0


if __name__ == "__main__":
    raise SystemExit(main())
