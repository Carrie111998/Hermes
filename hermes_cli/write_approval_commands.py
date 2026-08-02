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
    from agent import learning_ledger

    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        candidate = learning_ledger.get_candidate(str(r.get("candidate_id") or r["id"]))
        risk = (candidate or {}).get("evidence", {}).get("risk", "unknown")
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}  risk={risk}")
        if candidate:
            evidence = candidate.get("evidence", {})
            excerpt = str(evidence.get("excerpt") or "")[:200]
            hypothesis = str(evidence.get("hypothesis") or "")[:200]
            if excerpt:
                lines.append(f"    Evidence: {excerpt}")
            if hypothesis:
                lines.append(f"    Hypothesis: {hypothesis}")
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(f"Apply: {where}   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


def _fmt_history(subsystem: str) -> str:
    from agent import learning_ledger

    candidates = [
        item for item in learning_ledger.list_candidates()
        if item.get("subsystem") == subsystem
    ]
    if not candidates:
        return f"No {subsystem} learning history."
    lines = [f"{subsystem.capitalize()} learning history ({len(candidates)}):"]
    for candidate in candidates:
        reason = ""
        events = learning_ledger.list_events(candidate_id=candidate["candidate_id"])
        for event in reversed(events):
            if event["event"] in {"candidate_rejected", "candidate_rolled_back"}:
                reason = str(event.get("detail", {}).get("reason") or "")
                break
        suffix = f" — {reason}" if reason else ""
        lines.append(
            f"  {candidate['candidate_id']}  {candidate['status']}  "
            f"{candidate.get('proposal', {}).get('summary', '')}{suffix}"
        )
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

    if sub in {"history", "ledger"}:
        return _fmt_history(subsystem)

    if sub == "audit":
        from agent.context_health import format_context_audit

        return format_context_audit()

    if sub in {"compile", "compilations"}:
        from agent.trace_compiler import format_compilation_proposals

        return format_compilation_proposals()

    if sub == "reconcile":
        return _reconcile(subsystem, rest)

    if sub in {"eval", "evaluate"}:
        return _evaluate_skill(rest) if subsystem == wa.SKILLS else _evaluate_memory(rest, memory_store)

    if sub == "rollback":
        return _rollback_skill(rest) if subsystem == wa.SKILLS else _rollback_memory(rest, memory_store)

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

    from agent import learning_ledger

    applied, failed = 0, []
    for rec in targets:
        if target.lower() == "all":
            candidate = learning_ledger.get_candidate(str(rec.get("candidate_id") or rec["id"]))
            if (candidate or {}).get("evidence", {}).get("risk") == "high":
                failed.append(f"{rec['id']}: high-risk candidate requires explicit approval by id")
                continue
        claim = wa.claim_pending(subsystem, rec["id"])
        if claim is None:
            failed.append(f"{rec['id']}: already claimed or no longer pending")
            continue
        candidate = wa.ensure_candidate_for_record(claim)
        if candidate is None:
            wa.release_claim(subsystem, claim, restore=True)
            failed.append(f"{rec['id']}: learning ledger unavailable")
            continue
        reviewed_ok, reviewed_error = wa.verify_reviewed_payload(
            subsystem, claim, candidate, memory_store=memory_store
        )
        if not reviewed_ok:
            wa.release_claim(subsystem, claim, restore=True)
            failed.append(f"{rec['id']}: stale review — {reviewed_error}")
            continue
        started = learning_ledger.transition_candidate(
            rec["id"],
            from_status="pending",
            to_status="applying",
            event="candidate_apply_started",
            detail={"claim_id": claim.get("_claim_id")},
        )
        if started is None:
            wa.release_claim(subsystem, claim, restore=True)
            failed.append(f"{rec['id']}: candidate is no longer pending")
            continue

        try:
            ok, msg = _apply_one(subsystem, claim, memory_store)
        except Exception:
            # The canonical mutation handler may have completed its durable
            # side effect before losing its result.  Replaying automatically
            # would violate exactly-once semantics, so preserve both the
            # applying lifecycle and claim for explicit reconciliation.
            failed.append(f"{rec['id']}: apply result is uncertain; needs reconciliation")
            continue
        if ok:
            terminal = learning_ledger.transition_candidate(
                rec["id"],
                from_status="applying",
                to_status="active",
                event="candidate_activated",
            )
            if terminal is not None and wa.release_claim(subsystem, claim, restore=False):
                applied += 1
            else:
                failed.append(f"{rec['id']}: applied but lifecycle finalization needs reconciliation")
        else:
            learning_ledger.transition_candidate(
                rec["id"],
                from_status="applying",
                to_status="pending",
                event="candidate_apply_failed",
                detail={"error": msg},
            )
            wa.release_claim(subsystem, claim, restore=True)
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    if subsystem == wa.MEMORY:
        if memory_store is None:
            return False, "memory store unavailable"
        from tools.memory_tool import apply_memory_pending
        result = apply_memory_pending(payload, memory_store)
        return bool(result.get("success")), result.get("error", "")
    from tools.skill_manager_tool import apply_skill_pending
    result = json.loads(apply_skill_pending(payload))
    return bool(result.get("success")), result.get("error", "")


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    from agent import learning_ledger

    reason = " ".join(rest[1:]).strip()
    records = wa.list_pending(subsystem)
    if target.lower() == "all":
        targets = records
    else:
        record = wa.get_pending(subsystem, target)
        if record is None:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [record]

    rejected = 0
    for record in targets:
        claim = wa.claim_pending(subsystem, record["id"])
        if claim is None:
            continue
        candidate = wa.ensure_candidate_for_record(claim)
        if candidate is None:
            # A crash may leave exact replay JSON durable before its ledger row.
            # Explicit human rejection must still be able to discard that
            # non-executable orphan instead of restoring it forever.
            if wa.release_claim(subsystem, claim, restore=False):
                rejected += 1
            continue
        transitioned = None
        transitioned = learning_ledger.transition_candidate(
            record["id"],
            from_status="pending",
            to_status="rejected",
            event="candidate_rejected",
            detail={"reason": reason},
        )
        if transitioned is not None and not learning_ledger.purge_candidate_evidence(
            record["id"], reason="candidate_rejected"
        ):
            transitioned = None
        if transitioned is not None and wa.release_claim(subsystem, claim, restore=False):
            rejected += 1
        else:
            wa.release_claim(subsystem, claim, restore=True)

    if target.lower() == "all":
        return f"Rejected {rejected} pending {subsystem} write(s)."
    if rejected:
        return f"Rejected pending {subsystem} write '{target}'."
    return f"Could not reject pending {subsystem} write '{target}'."


def _reconcile(subsystem: str, rest: List[str]) -> str:
    """Expose interrupted claims; resolution is always an explicit human choice."""
    from agent import learning_ledger

    claims = wa.list_claims(subsystem)
    if not rest:
        if not claims:
            return f"No interrupted {subsystem} claims."
        lines = [f"Interrupted {subsystem} claims ({len(claims)}):"]
        for claim in claims:
            candidate = learning_ledger.get_candidate(str(claim.get("candidate_id") or claim["id"]))
            lines.append(
                f"  {claim['id']}  status={(candidate or {}).get('status', 'unknown')}  "
                f"age={int(claim.get('_claim_age_seconds', 0))}s"
            )
        lines.append(
            f"Resolve explicitly: /{subsystem} reconcile <id> <restore|mark-applied|discard-claim>"
        )
        return "\n".join(lines)
    if len(rest) != 2 or rest[1] not in {"restore", "mark-applied", "discard-claim"}:
        return f"Usage: /{subsystem} reconcile <id> <restore|mark-applied|discard-claim>"
    pending_id, resolution = rest
    claim = next((item for item in claims if str(item.get("id")) == pending_id), None)
    if claim is None:
        return f"No interrupted {subsystem} claim with id '{pending_id}'."
    candidate = learning_ledger.get_candidate(str(claim.get("candidate_id") or pending_id))
    if candidate is None:
        return f"Claim '{pending_id}' has no matching candidate; inspect it manually."
    if resolution == "discard-claim":
        if candidate.get("status") not in {"active", "rejected", "rolled_back", "validated"}:
            return f"Claim '{pending_id}' is not terminal; choose restore or mark-applied instead."
        return (
            f"Discarded finalized claim '{pending_id}'."
            if wa.release_claim(subsystem, claim, restore=False)
            else f"Could not discard finalized claim '{pending_id}'."
        )
    if candidate.get("status") not in {"pending", "applying", "active"}:
        return f"Claim '{pending_id}' is not pending/applying/active; inspect it manually."
    if resolution == "restore":
        if candidate.get("status") == "active":
            return f"Claim '{pending_id}' is already active and cannot be restored to pending."
        transitioned = candidate
        if candidate.get("status") == "applying":
            transitioned = learning_ledger.transition_candidate(
                pending_id,
                from_status="applying",
                to_status="pending",
                event="candidate_reconciled_pending",
                detail={"resolution": "human_confirmed_not_applied"},
            )
        released = transitioned is not None and wa.release_claim(subsystem, claim, restore=True)
        return (
            f"Restored '{pending_id}' to pending review."
            if released else f"Could not restore interrupted claim '{pending_id}'."
        )
    transitioned = candidate
    if candidate.get("status") in {"pending", "applying"}:
        transitioned = learning_ledger.transition_candidate(
            pending_id,
            from_status=str(candidate["status"]),
            to_status="active",
            event="candidate_reconciled_active",
            detail={"resolution": "human_confirmed_applied"},
        )
    released = transitioned is not None and wa.release_claim(subsystem, claim, restore=False)
    return (
        f"Marked '{pending_id}' applied and removed its claim."
        if released else f"Could not finalize interrupted claim '{pending_id}'."
    )


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    diff = wa.skill_pending_diff(rec)
    header = f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n"
    return header + "\n" + diff


def _evaluate_skill(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills eval <id>"
    record = wa.get_pending(wa.SKILLS, rest[0])
    if record is None:
        return f"No pending skill write with id '{rest[0]}'."
    try:
        from agent import learning_ledger
        from agent.learning_evaluation import evaluate_pending_skill

        result = evaluate_pending_skill(record)
        verdict = result["verdict"]
        if verdict == "no_manifest":
            return f"Candidate {record['id']}: no evals/manifest.json; no evaluation recorded."
        outcome = "verification_succeeded" if verdict in {"improved", "passed"} else "verification_failed"
        learning_ledger.record_outcome(
            str(record.get("candidate_id") or record["id"]),
            outcome,
            detail={
                "verdict": verdict,
                "baseline_passed": result["baseline"]["passed"],
                "candidate_passed": result["candidate"]["passed"],
                "total": result["candidate"]["total"],
            },
        )
        return (
            f"Candidate {record['id']} evaluation: {verdict} "
            f"(baseline {result['baseline']['passed']}/{result['baseline']['total']}, "
            f"candidate {result['candidate']['passed']}/{result['candidate']['total']})."
        )
    except Exception as e:
        return f"Candidate {record['id']} evaluation failed: {e}"


def _evaluate_memory(rest: List[str], memory_store) -> str:
    if not rest:
        return "Usage: /memory eval <id>"
    record = wa.get_pending(wa.MEMORY, rest[0])
    if record is None:
        return f"No pending memory write with id '{rest[0]}'."
    if memory_store is None:
        return "Memory store unavailable."
    try:
        from agent import learning_ledger
        from agent.learning_evaluation import evaluate_pending_memory

        result = evaluate_pending_memory(record, memory_store)
        outcome = "verification_succeeded" if result["verdict"] == "passed" else "verification_failed"
        learning_ledger.record_outcome(
            str(record.get("candidate_id") or record["id"]),
            outcome,
            detail={"verdict": result["verdict"], "checks": result["candidate"]["checks"]},
            attempt_id=f"memory-eval:{record.get('payload_fingerprint', '')}",
        )
        return (
            f"Candidate {record['id']} evaluation: {result['verdict']} "
            f"({result['candidate']['passed']}/{result['candidate']['total']} invariants)."
        )
    except Exception as e:
        return f"Candidate {record['id']} evaluation failed: {e}"


def _rollback_skill(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills rollback <candidate-id>"
    candidate_id = rest[0]
    try:
        from agent import learning_ledger
        from agent.learning_evaluation import prepare_evaluated_skill_rollback
        from tools.skill_manager_tool import apply_skill_pending

        candidate = learning_ledger.get_candidate(candidate_id)
        if candidate is None or candidate.get("subsystem") != "skills":
            return f"Unknown skill candidate '{candidate_id}'."
        if candidate.get("status") == "rolling_back":
            if len(rest) == 2 and rest[1] == "mark-rolled-back":
                transitioned = learning_ledger.transition_candidate(
                    candidate_id,
                    from_status="rolling_back",
                    to_status="rolled_back",
                    event="rollback_reconciled",
                    detail={"resolution": "human_confirmed_rolled_back"},
                )
                if transitioned:
                    return f"Marked '{candidate_id}' rolled back."
                return f"Could not reconcile rollback for '{candidate_id}'."
            return (
                f"Candidate '{candidate_id}' is in rolling_back state. "
                "Inspect the skill file, then run: /skills rollback <id> mark-rolled-back"
            )
        if candidate.get("status") not in {"active", "validated"}:
            return f"Candidate '{candidate_id}' is not active or validated."

        transitioned = learning_ledger.transition_candidate(
            candidate_id,
            from_status=str(candidate["status"]),
            to_status="rolling_back",
            event="rollback_started",
            detail={"subsystem": "skills"},
        )
        if transitioned is None:
            return f"Candidate '{candidate_id}' could not enter rolling_back state."

        payload = prepare_evaluated_skill_rollback(candidate_id)
        result = json.loads(apply_skill_pending(payload))
        if not result.get("success"):
            learning_ledger.transition_candidate(
                candidate_id,
                from_status="rolling_back",
                to_status=str(candidate["status"]),
                event="rollback_failed",
                detail={"error": str(result.get("error", "unknown"))[:200]},
            )
            return f"Candidate {candidate_id} rollback failed: {result.get('error', 'unknown error')}"
        try:
            learning_ledger.record_outcome(
                candidate_id,
                "rolled_back",
                detail={"rollback": "evaluated_snapshot"},
                attempt_id="evaluated-snapshot-rollback",
            )
        except Exception as receipt_error:
            return (
                f"Candidate {candidate_id} rollback mutation succeeded but receipt failed: {receipt_error}. "
                f"Candidate needs reconciliation: /skills rollback {candidate_id} mark-rolled-back"
            )
        return f"Rolled back evaluated skill candidate '{candidate_id}'."
    except Exception as e:
        return f"Candidate {candidate_id} rollback failed: {e}"


def _rollback_memory(rest: List[str], memory_store) -> str:
    if not rest:
        return "Usage: /memory rollback <candidate-id>"
    if memory_store is None:
        return "Memory store unavailable for rollback."
    candidate_id = rest[0]
    try:
        from agent import learning_ledger
        from agent.learning_evaluation import prepare_evaluated_memory_rollback
        from tools.memory_tool import apply_memory_pending

        candidate = learning_ledger.get_candidate(candidate_id)
        if candidate is None or candidate.get("subsystem") != "memory":
            return f"Unknown memory candidate '{candidate_id}'."
        if candidate.get("status") == "rolling_back":
            if len(rest) == 2 and rest[1] == "mark-rolled-back":
                transitioned = learning_ledger.transition_candidate(
                    candidate_id,
                    from_status="rolling_back",
                    to_status="rolled_back",
                    event="rollback_reconciled",
                    detail={"resolution": "human_confirmed_rolled_back"},
                )
                if transitioned:
                    return f"Marked '{candidate_id}' rolled back."
                return f"Could not reconcile rollback for '{candidate_id}'."
            return (
                f"Candidate '{candidate_id}' is in rolling_back state. "
                "Inspect the memory file, then run: /memory rollback <id> mark-rolled-back"
            )
        if candidate.get("status") not in {"active", "validated"}:
            return f"Candidate '{candidate_id}' is not active or validated."

        transitioned = learning_ledger.transition_candidate(
            candidate_id,
            from_status=str(candidate["status"]),
            to_status="rolling_back",
            event="rollback_started",
            detail={"subsystem": "memory"},
        )
        if transitioned is None:
            return f"Candidate '{candidate_id}' could not enter rolling_back state."

        payload = prepare_evaluated_memory_rollback(candidate_id, memory_store)
        result = apply_memory_pending(payload, memory_store)
        if not result.get("success"):
            learning_ledger.transition_candidate(
                candidate_id,
                from_status="rolling_back",
                to_status=str(candidate["status"]),
                event="rollback_failed",
                detail={"error": str(result.get("error", "unknown"))[:200]},
            )
            return f"Candidate {candidate_id} rollback failed: {result.get('error', 'unknown error')}"
        try:
            learning_ledger.record_outcome(
                candidate_id,
                "rolled_back",
                detail={"rollback": "evaluated_memory_snapshot"},
                attempt_id="evaluated-memory-snapshot-rollback",
            )
        except Exception as receipt_error:
            return (
                f"Candidate {candidate_id} rollback mutation succeeded but receipt failed: {receipt_error}. "
                f"Candidate needs reconciliation: /memory rollback {candidate_id} mark-rolled-back"
            )
        return f"Rolled back evaluated memory candidate '{candidate_id}'."
    except Exception as e:
        return f"Candidate {candidate_id} rollback failed: {e}"


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
