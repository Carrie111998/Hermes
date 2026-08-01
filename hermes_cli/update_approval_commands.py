#!/usr/bin/env python3
"""Shared handlers for update-approval review commands.

Used by the interactive CLI's ``/update`` slash command and by the
``hermes update <pending|reject|approval ...>`` command-line dispatch.
Approve is handled by the caller because the execution surface differs:
classic CLI relaunches into ``hermes update approve <id>``, whereas the
command-line path applies immediately in-process.
"""

from __future__ import annotations

from typing import List, Optional

from tools import update_approval as ua



def _fmt_state() -> str:
    return f"updates.apply_approval = {'on' if ua.apply_approval_enabled() else 'off'}"



def _fmt_pending_list() -> str:
    records = ua.list_pending()
    if not records:
        return "No pending updates."
    lines = [f"Pending updates ({len(records)}):"]
    for r in records:
        lines.append(f"  {r['id']}  {r.get('summary', '')}")
    lines.append("")
    lines.append("Apply: /update approve <id>   Reject: /update reject <id>")
    return "\n".join(lines)



def handle_pending_subcommand(args: List[str], *, set_mode_fn=None) -> Optional[str]:
    if not args:
        return f"{_fmt_state()}\n\n" + _fmt_pending_list()

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list()

    if sub in {"reject", "deny", "drop"}:
        return _reject(rest)

    if sub in {"approval", "mode"}:
        return _set_approval(rest, set_mode_fn)

    return None



def resolve_approve_target(args: List[str]) -> tuple[Optional[str], Optional[str]]:
    if not args:
        return None, "Usage: /update approve <id>"
    return args[0], None



def _reject(rest: List[str]) -> str:
    target, err = resolve_approve_target(rest)
    if err or target is None:
        return err or "Usage: /update reject <id>"
    if target.lower() == "all":
        n = 0
        for rec in ua.list_pending():
            if ua.discard_pending(rec["id"]):
                n += 1
        return f"Rejected {n} pending update(s)."
    if ua.discard_pending(target):
        return f"Rejected pending update '{target}'."
    return f"No pending update with id '{target}'."



def _set_approval(rest: List[str], set_mode_fn) -> str:
    if not rest:
        return f"{_fmt_state()}\nSet with: /update approval <on|off>"

    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return f"Invalid value '{arg}'. Use: on or off."

    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (
            "To change the update approval gate, run:\n"
            f"  hermes config set updates.apply_approval {val}"
        )
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set updates.apply_approval: {e}"
    return f"updates.apply_approval set to '{'on' if enabled else 'off'}'."
