#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.
"""

from __future__ import annotations

import json
from typing import List, Optional

from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        candidate = r.get("refinement_candidate")
        if isinstance(candidate, dict) and candidate.get("id"):
            tag += f" [candidate {str(candidate['id'])[:8]}]"
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(f"Apply: {where}   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, f"Usage: /{subsystem} approve|reject <id>  (or 'all')"
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} approve <id>"

    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed, details = 0, [], []
    for rec in targets:
        candidate = rec.get("refinement_candidate")
        try:
            if subsystem == wa.SKILLS and isinstance(candidate, dict):
                with wa.refinement_candidate_transaction(rec) as latest:
                    ok, msg = _apply_one(subsystem, latest, memory_store)
                    if ok and not wa.discard_pending(subsystem, latest["id"]):
                        ok, msg = False, "Applied candidate could not be removed from pending state."
            else:
                ok, msg = _apply_one(subsystem, rec, memory_store)
                if ok:
                    wa.discard_pending(subsystem, rec["id"])
        except Exception as exc:
            ok, msg = False, str(exc)
        if ok:
            applied += 1
            if msg:
                details.append(f"{rec['id']}: {msg}")
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if details:
        out.append("Applied:")
        out.extend(f"  {detail}" for detail in details)
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    candidate = rec.get("refinement_candidate") if subsystem == wa.SKILLS else None

    def _record_failure(message: str) -> str:
        if isinstance(candidate, dict):
            stored, guard_error = wa.record_refinement_candidate_outcome(
                rec, "failed"
            )
            if not stored:
                return f"{message} ({guard_error})"
        return message

    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", "")
        else:
            snapshot = None
            if isinstance(candidate, dict):
                ok, message = wa.can_attempt_refinement_apply(rec)
                if not ok:
                    return False, message
                ok, message = wa.validate_refinement_candidate_base(rec)
                if not ok:
                    return False, _record_failure(message)
                from agent.curator_backup import snapshot_skills

                snapshot = snapshot_skills(
                    reason=f"pre-refinement {candidate.get('id', '')}"
                )
                if snapshot is None:
                    return False, _record_failure(
                        "Required Curator snapshot failed or is disabled; "
                        "the refinement was not applied."
                    )

            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(
                apply_skill_pending(payload, refinement_candidate=candidate)
            )
            if not result.get("success"):
                message = result.get("error", "")
                if snapshot is not None:
                    message = f"{message} (recovery snapshot: {snapshot.name})"
                return False, _record_failure(message)

            if isinstance(candidate, dict):
                ok, message = wa.validate_refinement_candidate_result(rec)
                if not ok:
                    return False, _record_failure(
                        f"{message} (recovery snapshot: {snapshot.name})"
                    )
                stored, guard_error = wa.record_refinement_candidate_outcome(
                    rec, "applied"
                )
                if not stored:
                    retained = dict(rec)
                    retained["refinement_apply_state"] = "applied_guard_error"
                    retained["refinement_apply_error"] = guard_error
                    wa.replace_pending_record(wa.SKILLS, retained)
                    return False, (
                        "Refinement write was applied, but the loop-guard outcome "
                        f"failed and the pending record was retained: {guard_error}"
                    )
                detail = f"refinement applied; recovery snapshot: {snapshot.name}"
                return True, detail
            return True, ""
    except Exception as e:
        return False, _record_failure(str(e))


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all":
        n, failed = 0, []
        for rec in wa.list_pending(subsystem):
            if isinstance(rec.get("refinement_candidate"), dict):
                try:
                    with wa.refinement_candidate_transaction(rec) as latest:
                        stored, message = wa.record_refinement_candidate_outcome(
                            latest, "rejected"
                        )
                        if not stored:
                            failed.append(f"{rec['id']}: {message}")
                            continue
                        if wa.discard_pending(subsystem, latest["id"]):
                            n += 1
                except Exception as exc:
                    failed.append(f"{rec['id']}: {exc}")
                    continue
            elif wa.discard_pending(subsystem, rec["id"]):
                n += 1
        out = [f"Rejected {n} pending {subsystem} write(s)."]
        if failed:
            out.append("Failed:")
            out.extend(f"  {item}" for item in failed)
        return "\n".join(out)

    rec = wa.get_pending(subsystem, target)
    if not rec:
        return f"No pending {subsystem} write with id '{target}'."
    if isinstance(rec.get("refinement_candidate"), dict):
        try:
            with wa.refinement_candidate_transaction(rec) as latest:
                stored, message = wa.record_refinement_candidate_outcome(
                    latest, "rejected"
                )
                if not stored:
                    return f"Could not reject refinement candidate '{target}': {message}"
                if wa.discard_pending(subsystem, target):
                    return f"Rejected pending {subsystem} write '{target}'."
        except Exception as exc:
            return f"Could not reject refinement candidate '{target}': {exc}"
        return f"No pending {subsystem} write with id '{target}'."
    if wa.discard_pending(subsystem, target):
        return f"Rejected pending {subsystem} write '{target}'."
    return f"No pending {subsystem} write with id '{target}'."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    diff = wa.skill_pending_diff(rec)
    header = f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n"
    candidate = rec.get("refinement_candidate")
    if isinstance(candidate, dict):
        evidence = candidate.get("evidence", {})
        base = candidate.get("base", {})
        proposed = candidate.get("proposed", {})
        header += (
            f"Candidate: {candidate.get('id', '')}\n"
            f"Evidence: origin={evidence.get('origin', '')} "
            f"session={evidence.get('session_id', '')} "
            f"task={evidence.get('task_id', '')}\n"
            f"Base: {base.get('state', '')} {base.get('sha256', '')}\n"
            f"Proposed: {proposed.get('state', '')} {proposed.get('sha256', '')}\n"
        )
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
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
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."
