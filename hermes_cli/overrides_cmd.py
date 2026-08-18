"""
hermes overrides — see and revoke active model reroute overrides.

Phase 2 of the rate-limit reroute feature (events/model_override.py) lets a
Telegram tap divert traffic off a rate-limited model onto a replacement for
a bounded window. Per spec Sec:Containment, that has to be "visible and
reversible" — a forgotten override is the main way this feature could hurt,
so it must be hard to forget. This module is that safety valve: a CLI path
to see and clear an override without needing the phone/Telegram flow that
set it.

Subcommands:
  hermes overrides [list]                 Show every active override
  hermes overrides clear <provider> <model>   Revoke one override
  hermes overrides clear --all            Revoke every active override

Storage/logic lives entirely in events/model_override.py — this module only
formats ``list_overrides()`` output and calls ``clear_override()`` with a
CLI-identifying ``cleared_by`` so the audit trail can tell a CLI revoke
apart from a Telegram one.
"""
from __future__ import annotations

import getpass
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from events.model_override import clear_override, list_overrides, store_status


def _cli_actor() -> str:
    """Identify the CLI actor for ``cleared_by`` (e.g. ``cli:diego``).

    Falls back to the bare string ``cli`` when the OS username can't be
    read (locked-down environments, some CI sandboxes) — still enough to
    distinguish a CLI revoke from a Telegram one in the audit trail.
    """
    try:
        user = getpass.getuser().strip()
    except Exception:
        user = ""
    return f"cli:{user}" if user else "cli"


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (as written by model_override.py) to an
    aware UTC datetime, or None if unparseable. Mirrors
    ``events.model_override._parse_iso`` without importing that private
    helper across the module boundary.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_remaining(expires_at: str) -> str:
    """Render remaining time until ``expires_at`` (e.g. ``expires in 4h12m``).

    ``list_overrides()`` only ever returns non-expired records (expired
    ones are reaped on load by events/model_override.py's ``_load_store``),
    so a non-positive remainder here would mean that invariant broke —
    flagged rather than silently displayed as a plausible-looking duration.
    """
    dt = _parse_iso(expires_at)
    if dt is None:
        return "expiry unknown"
    remaining = dt - datetime.now(timezone.utc)
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "INVARIANT VIOLATION: list_overrides returned an expired record"
    hours, rem = divmod(total_seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"expires in {hours}h{minutes:02d}m"
    if minutes:
        return f"expires in {minutes}m"
    return "expires in <1m"


def _format_entry(index: int, record: Dict[str, Any]) -> str:
    provider = record.get("provider", "?")
    model = record.get("model", "?")
    replacement_provider = record.get("replacement_provider", "?")
    replacement_model = record.get("replacement_model", "?")
    set_by = record.get("set_by") or "unknown"
    expires_at = record.get("expires_at", "")
    remaining = _format_remaining(expires_at)
    return (
        f"  {index}. {provider}/{model}  ->  {replacement_provider}/{replacement_model}\n"
        f"       {remaining}  (expires_at={expires_at or '?'} UTC)  set by {set_by}"
    )


def _sorted_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        records,
        key=lambda r: (str(r.get("provider", "")), str(r.get("model", ""))),
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _print_store_unreadable(status: Dict[str, Any]) -> None:
    """Say so when the store is dark, instead of "No active model overrides."

    ``list_overrides()`` fails open to [] for a corrupt/unreadable file, which
    renders identically to a genuinely empty store — so the CLI safety valve
    would cheerfully report "nothing to worry about" for a feature that is in
    fact dead (reads blank, writes skipped). This is the one caller that
    reports state rather than routing on it, so it is the one caller that has
    to tell the two apart.
    """
    path = status.get("path") or "the override store"
    print()
    print("  WARNING: the model-override store could not be read.")
    print(f"    {path}")
    print("    Overrides cannot be listed, set, or cleared while this")
    print("    persists — every read returns empty and every write is")
    print("    skipped. Inspect/repair or delete that file, then retry.")
    print()


def cmd_overrides_list(args) -> int:  # noqa: ARG001
    """Show every active override: what's avoided, what it routes to, expiry, who set it."""
    records = list_overrides()
    if not records:
        status = store_status()
        if not status.get("readable", True):
            _print_store_unreadable(status)
            return 1
        print()
        print("  No active model overrides.")
        print()
        return 0

    print()
    print(f"  Active model overrides ({len(records)}):")
    for i, record in enumerate(_sorted_records(records), 1):
        print(_format_entry(i, record))
    print()
    return 0


def cmd_overrides_clear(args) -> int:
    """Revoke one override (provider + model) or every override (--all)."""
    clear_all = bool(getattr(args, "all", False))
    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    cleared_by = _cli_actor()

    if clear_all:
        if provider or model:
            print(
                "  usage: hermes overrides clear <provider> <model>  |  "
                "hermes overrides clear --all (not both)",
                file=sys.stderr,
            )
            return 2

        records = list_overrides()
        if not records:
            status = store_status()
            if not status.get("readable", True):
                _print_store_unreadable(status)
                return 1
            print()
            print("  No active overrides — nothing to clear.")
            print()
            return 0

        cleared: List[tuple] = []
        for record in records:
            p, m = record.get("provider"), record.get("model")
            if not p or not m:
                continue
            if clear_override(provider=p, model=m, cleared_by=cleared_by):
                cleared.append((p, m))

        print()
        if cleared:
            print(f"  Cleared {len(cleared)} override(s):")
            for p, m in cleared:
                print(f"    - {p}/{m}")
            print()
            return 0
        print("  Nothing matched — no overrides were cleared.")
        print()
        return 1

    if not provider or not model:
        print(
            "  usage: hermes overrides clear <provider> <model>  |  "
            "hermes overrides clear --all",
            file=sys.stderr,
        )
        return 2

    ok = clear_override(provider=provider, model=model, cleared_by=cleared_by)
    if ok:
        print()
        print(f"  Cleared override: {provider}/{model}")
        print()
        return 0
    status = store_status()
    if not status.get("readable", True):
        _print_store_unreadable(status)
        return 1
    print()
    print(f"  Nothing matched — no active override found for {provider}/{model}.")
    print()
    return 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def cmd_overrides(args) -> int:
    """Top-level dispatcher for ``hermes overrides [subcommand]``."""
    sub = getattr(args, "overrides_command", None)
    if sub in {None, "", "list", "ls"}:
        return cmd_overrides_list(args)
    if sub == "clear":
        return cmd_overrides_clear(args)
    print(f"Unknown overrides subcommand: {sub}", file=sys.stderr)
    print("Use one of: list, clear", file=sys.stderr)
    return 2
