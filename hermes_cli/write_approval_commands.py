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
import re
from pathlib import Path
from typing import List, Optional

from tools import write_approval as wa


GOVERNANCE_VERDICTS = frozenset({"APPROVE", "REJECT", "CONSOLIDATE", "REVISE", "NOT_EVALUATED"})
NATIVE_DISPOSITIONS = frozenset({"NOT_ATTEMPTED", "APPROVED", "REJECTED", "CONSOLIDATED", "FAILED"})
EFFECTIVE_STATUSES = frozenset({
    "APPROVE",
    "REJECT",
    "CONSOLIDATE",
    "REVISE",
    "BLOCKED_RESOLVER",
    "BLOCKED_SECURITY_SCAN_PREEXISTING",
    "REVISE_DEPENDENCY_BLOCKED",
    "BLOCKED_MANIFEST",
    "FAILED",
})
EXECUTION_STATUSES = frozenset({"NOT_STARTED", "COMPLETED", "PARTIAL", "FAILED", "BLOCKED"})
DELIVERY_STATUSES = frozenset({"NOT_ATTEMPTED", "DELIVERED", "FAILED", "ACTION_REQUIRED"})
_PENDING_GOVERNANCE_REQUIRED_FIELDS = (
    "governance_verdict",
    "native_disposition",
    "effective_status",
    "execution_status",
    "delivery_status",
    "pending_ids_before",
    "pending_ids_after",
    "native_results",
    "target_read_back",
)


def validate_pending_governance_result(result: dict) -> dict:
    """Validate one unambiguous pending-governance result contract.

    Status dimensions remain separate so scheduler execution, semantic review,
    native mutation, and channel delivery cannot be collapsed into a misleading
    combined verdict. The input is returned unchanged after validation.
    """
    missing = [field for field in _PENDING_GOVERNANCE_REQUIRED_FIELDS if field not in result]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    enum_fields = {
        "governance_verdict": GOVERNANCE_VERDICTS,
        "native_disposition": NATIVE_DISPOSITIONS,
        "effective_status": EFFECTIVE_STATUSES,
        "execution_status": EXECUTION_STATUSES,
        "delivery_status": DELIVERY_STATUSES,
    }
    for field, allowed in enum_fields.items():
        value = result[field]
        if value not in allowed:
            raise ValueError(
                f"invalid {field}: {value!r}; expected one of {sorted(allowed)}"
            )

    for field in ("pending_ids_before", "pending_ids_after", "native_results", "target_read_back"):
        if not isinstance(result[field], list):
            raise ValueError(f"{field} must be a list")
    return result


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

    applied, failed = 0, []
    for rec in targets:
        result = approve_pending_native(subsystem, rec["id"], memory_store)
        if result["success"]:
            applied += 1
        else:
            failed.append(
                f"{rec['id']}: {result.get('error', 'native replay failed')}"
            )

    out = [f"Approved {applied} {subsystem} write(s)."]
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", "")
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
            return bool(result.get("success")), result.get("error", "")
    except Exception as e:
        return False, str(e)


def _new_pending_result(subsystem: str, pending_id: str) -> dict:
    return {
        "success": False,
        "subsystem": subsystem,
        "pending_id": pending_id,
        "replayed": False,
        "discarded": False,
    }


def _dispose_pending_native(
    subsystem: str,
    pending_id: str,
    *,
    memory_store=None,
    replay: bool,
) -> dict:
    """Apply or discard one pending record with structured evidence."""
    result = _new_pending_result(subsystem, pending_id)
    if subsystem not in {wa.MEMORY, wa.SKILLS}:
        result["error"] = f"unsupported subsystem: {subsystem}"
        return result
    record = wa.get_pending(subsystem, pending_id)
    if not record:
        result["error"] = f"pending record not found: {pending_id}"
        return result
    payload = record.get("payload") or {}
    result["target"] = payload.get("name") or payload.get("target")
    if replay:
        ok, message = _apply_one(subsystem, record, memory_store)
        if not ok:
            result["error"] = message or "native replay failed"
            return result
        result["replayed"] = True
    result["discarded"] = wa.discard_pending(subsystem, pending_id)
    if not result["discarded"]:
        result["error"] = (
            "replay succeeded but pending record could not be discarded"
            if replay
            else "pending record could not be discarded"
        )
        return result
    result["success"] = True
    return result


def approve_pending_native(subsystem: str, pending_id: str, memory_store=None) -> dict:
    """Apply one pending write through the native replay path."""
    return _dispose_pending_native(
        subsystem, pending_id, memory_store=memory_store, replay=True
    )


def reject_pending_native(subsystem: str, pending_id: str) -> dict:
    """Reject one pending record with structured discard evidence."""
    return _dispose_pending_native(subsystem, pending_id, replay=False)


def consolidate_pending_native(
    subsystem: str,
    pending_ids: List[str],
    *,
    memory_store=None,
) -> dict:
    """Replay a related batch through the shared native disposition seam.

    Each record preserves the same replay-before-discard and retention-on-failure
    behavior as :func:`approve_pending_native`. A failed record does not prevent
    independent records in the batch from producing structured evidence.
    """
    results = [
        approve_pending_native(subsystem, pending_id, memory_store)
        for pending_id in pending_ids
    ]
    success = all(item.get("success") is True for item in results)
    return {
        "success": success,
        "subsystem": subsystem,
        "pending_ids": list(pending_ids),
        "native_disposition": "CONSOLIDATED" if success else "FAILED",
        "results": results,
    }


_SCAN_FINDING_RE = re.compile(
    r"(?P<file>[^\s:]+):(?P<line>\d+)\s+\"(?P<match>[^\"]+)\""
)


def classify_pending_skill_scan_block(
    native_error: str,
    skill_dir: Path,
    *,
    governance_verdict: str,
    seen_scan_fingerprints=None,
) -> dict:
    """Classify a native scanner block using the rolled-back target baseline.

    The scanner currently returns formatted text rather than a structured
    result. A finding is only classified as pre-existing when its reported
    match can be read from the retained target at the reported relative path.
    Proven pre-existing findings retain the caller's semantic verdict. All
    other findings fail closed as ``REVISE`` without claiming provenance.
    """
    findings = []
    root = skill_dir.resolve()
    for found in _SCAN_FINDING_RE.finditer(native_error or ""):
        relative_path = Path(found.group("file"))
        candidate = (root / relative_path).resolve()
        pre_existing = False
        try:
            if candidate.is_relative_to(root) and candidate.is_file():
                pre_existing = found.group("match") in candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        findings.append({
            "file": found.group("file"),
            "line": int(found.group("line")),
            "match": found.group("match"),
            "pre_existing": pre_existing,
        })

    import hashlib

    is_pre_existing = bool(findings) and all(item["pre_existing"] for item in findings)
    finding_fingerprint = hashlib.sha256(
        json.dumps(
            findings,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    retry_suppressed = False
    if seen_scan_fingerprints is not None:
        retry_suppressed = finding_fingerprint in seen_scan_fingerprints
        seen_scan_fingerprints.add(finding_fingerprint)
    return {
        "effective_status": (
            "BLOCKED_SECURITY_SCAN_PREEXISTING" if is_pre_existing else "REVISE"
        ),
        "governance_verdict": governance_verdict if is_pre_existing else "REVISE",
        "findings": findings,
        "finding_fingerprint": finding_fingerprint,
        "retry_suppressed": retry_suppressed,
    }


def _canonical_skill_identity(target: str, resolved: dict) -> str:
    """Return a path-qualified identity without collapsing to a basename."""
    from tools.skill_manager_tool import _skills_dir

    path = Path(resolved["path"]).resolve()
    try:
        return path.relative_to(_skills_dir().resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _find_canonical_skill_matches(name: str) -> List[Path]:
    """Return every active skill directory whose basename matches ``name``.

    Governance preflight uses native ``_find_skill`` resolver to match
    disposition replay behavior.
    """
    from tools.skill_manager_tool import _find_skill

    resolved = _find_skill(name)
    if resolved and resolved.get("path"):
        return [Path(resolved["path"]).resolve()]

    from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path

    matches = []
    seen = set()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md) or skill_md.parent.name != name:
                continue
            path = skill_md.parent.resolve()
            key = str(path)
            if key not in seen:
                seen.add(key)
                matches.append(path)
    return matches


def _pending_payload_fingerprints(records: List[dict]) -> dict:
    """Create stable evidence that read-only preflight did not rewrite payloads."""
    import hashlib

    return {
        record["id"]: hashlib.sha256(
            json.dumps(
                record.get("payload") or {},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        for record in records
    }


def preflight_pending_skill_review() -> dict:
    """Inventory pending skills with canonical resolver evidence, read-only.

    This is intentionally separate from the native disposition helper: cron
    review needs machine-readable resolver and dependency blockers before a
    human or policy verdict authorizes replay or discard.
    """
    records = wa.list_pending(wa.SKILLS)
    pending_ids = [record["id"] for record in records]
    fingerprints_before = _pending_payload_fingerprints(records)

    # Pass 1: Build parent map (first primary mutation record per target)
    parents = {}
    for record in records:
        payload = record.get("payload") or {}
        target = payload.get("name")
        action = payload.get("action")
        if target and target not in parents and action != "write_file":
            parents[target] = record["id"]

    results = []
    for record in records:
        payload = record.get("payload") or {}
        target = payload.get("name")
        matches = _find_canonical_skill_matches(target) if target else []
        parent_id = parents.get(target)
        is_dependent = payload.get("action") == "write_file" and parent_id is not None and parent_id != record["id"]

        if len(matches) == 1:
            result = {
                "pending_id": record["id"],
                "target": target,
                "canonical_target": _canonical_skill_identity(
                    target, {"path": matches[0]}
                ),
                "governance_verdict": "REVISE",
                "native_disposition": "NOT_ATTEMPTED",
                "effective_status": "REVISE",
                "dependency_on": None,
            }
        elif is_dependent:
            result = {
                "pending_id": record["id"],
                "target": target,
                "canonical_target": None,
                "governance_verdict": "REVISE",
                "native_disposition": "NOT_ATTEMPTED",
                "effective_status": "REVISE_DEPENDENCY_BLOCKED",
                "dependency_on": parent_id,
            }
        else:
            result = {
                "pending_id": record["id"],
                "target": target,
                "canonical_target": None,
                "governance_verdict": "REVISE",
                "native_disposition": "NOT_ATTEMPTED",
                "effective_status": "BLOCKED_RESOLVER",
                "dependency_on": None,
            }
            if len(matches) > 1:
                result["resolver_candidates"] = [
                    str(path.resolve()) for path in matches
                ]
        results.append(result)

    final_records = wa.list_pending(wa.SKILLS)
    final_ids = [record["id"] for record in final_records]
    fingerprints_after = _pending_payload_fingerprints(final_records)
    return {
        "execution_status": "COMPLETED",
        "governance_status": "COMPLETED" if not final_ids else "PARTIAL",
        "delivery_status": "NOT_ATTEMPTED",
        "pending_ids_before": pending_ids,
        "pending_ids_after": final_ids,
        "new_pending_ids": [pending_id for pending_id in final_ids if pending_id not in pending_ids],
        "queue_drained": not final_ids,
        "payload_fingerprints_before": fingerprints_before,
        "payload_fingerprints_after": fingerprints_after,
        "records_unchanged": fingerprints_before == fingerprints_after,
        "records": results,
    }


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all":
        n = 0
        for rec in wa.list_pending(subsystem):
            if reject_pending_native(subsystem, rec["id"])["success"]:
                n += 1
        return f"Rejected {n} pending {subsystem} write(s)."
    result = reject_pending_native(subsystem, target)
    if result["success"]:
        return f"Rejected pending {subsystem} write '{target}'."
    if result.get("error") == f"pending record not found: {target}":
        return f"No pending {subsystem} write with id '{target}'."
    return (
        f"Failed to reject pending {subsystem} write '{target}': "
        f"{result.get('error', '')}"
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


def finalize_pending_skill_manifest(initial_manifest: dict) -> dict:
    """Read back final native pending queue and compute exact queue deltas."""
    final_records = wa.list_pending(wa.SKILLS)
    final_ids = [r["id"] for r in final_records]
    initial_ids = initial_manifest.get("pending_ids_before", [])
    
    return {
        "pending_ids_before": initial_ids,
        "pending_ids_after": final_ids,
        "new_pending_ids": [pid for pid in final_ids if pid not in initial_ids],
        "queue_drained": len(final_ids) == 0,
        "payload_fingerprints_after": _pending_payload_fingerprints(final_records),
    }


def format_executive_summary_digest(governance_result: dict) -> str:
    """Format human-facing executive summary digest (excludes raw payload/diff/stack trace)."""
    exec_status = governance_result.get("execution_status", "UNKNOWN")
    gov_status = governance_result.get("governance_status", "UNKNOWN")
    records = governance_result.get("records", [])
    
    lines = [
        f"📋 **Pending Skills Governance Digest**",
        f"• Status: {exec_status} (Governance: {gov_status})",
        f"• Total Records Processed: {len(records)}",
    ]
    
    if records:
        lines.append("\n**Summary Gists:**")
        for rec in records:
            pid = rec.get("pending_id", "N/A")[:8]
            target = rec.get("target", "unknown")
            eff = rec.get("effective_status", "UNKNOWN")
            lines.append(f" - [{pid}] {target}: {eff}")
            
    return "\n".join(lines)


def write_internal_governance_artifact(governance_result: dict, output_dir: Optional[Path] = None) -> Path:
    """Persist full machine-readable governance artifact to disk independently of channel delivery."""
    import time
    if output_dir is None:
        from tools.skill_manager_tool import _skills_dir
        output_dir = _skills_dir().parent / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    artifact_path = output_dir / f"pending_governance_{timestamp}.json"
    artifact_path.write_text(
        json.dumps(governance_result, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return artifact_path

