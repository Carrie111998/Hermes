"""Verified-completion helpers for kanban tasks (#70806).

Policy + execution for the opt-in completion gate: a task created with
``--verify-cmd <command>`` (or ``--verify auto``) must pass verification
before ``kanban_complete`` is allowed to flip it ``done``. This module holds
the two verification strategies and their shared plumbing; it performs **no
DB access** — recording rejections is ``kanban_db.record_verify_failure``'s
job, and the gate itself lives in ``tools/kanban_tools.py``.

Philosophy, inherited from the verification evidence ledger
(``agent/verification_evidence.py``): strictly opt-in, the ledger stays
passive and is only ever *read* here, and targeted evidence is never
upgraded into a repo-green completion claim.

Infra failures (missing workspace, spawn error, timeout, ledger
``not_applicable``) fail **closed** — for a correctness gate, fail-open
contradicts "never silently done", and the bounded retry budget prevents
infinite infra-red loops (they end in ``blocked`` with the evidence saying
why).

Memory note (MVP-accepted): ``communicate()`` buffers the verify command's
full merged output before capping; a pathological command emitting gigabytes
balloons worker RSS. Incremental bounded reads are a follow-up.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.redact import redact_sensitive_text
from agent.verification_evidence import verification_status

# No per-task timeout flag in MVP. 600 s is safe against the claim
# machinery: host-local workers get TTL-expired claims *extended* while
# their PID is alive, and a non-host-local mid-verify reclaim is converted
# into a harmless audited no-op by the expected_run_id CAS on both the
# green (complete_task) and red (record_verify_failure) paths.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 600
# Matches the ledger's output_summary cap.
MAX_VERIFY_OUTPUT_CHARS = 2000
# Post-SIGKILL communicate() bound — a grandchild that escaped the process
# group via its own setsid keeps the inherited pipe open, and an unbounded
# reap would reintroduce the very hang the timeout exists to bound.
REAP_TIMEOUT_SECONDS = 10


@dataclass
class VerifyOutcome:
    """Result of one verification check, ready to persist or surface.

    ``detail`` is always redacted + capped (safe for events, comments, run
    error, tool_error) — either the command output excerpt or an actionable
    explanation of why the check could not accept.
    """

    ok: bool
    gate: str                  # "verify_cmd" | "verify_auto"
    command: Optional[str]     # verify_cmd, or the ledger's canonical_command
    exit_code: Optional[int]   # None on timeout / spawn error / no-execution
    detail: str
    timed_out: bool = False
    # Auto mode only: ledger row facts worth copying onto the completion
    # metadata (the ledger prunes, so reference-only would rot).
    evidence: Optional[dict] = None


def _cap_output(text: str, limit: int = MAX_VERIFY_OUTPUT_CHARS) -> str:
    """Head 1/3 + tail 2/3 with an omission marker.

    Deliberate local copy of the shape of the ledger's private
    ``_summarize_output`` (agent/verification_evidence.py) — exporting that
    helper would touch a module this feature is explicitly keeping
    byte-untouched.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return (
        text[:head]
        + f"\n... [{len(text) - limit} chars omitted] ...\n"
        + text[-tail:]
    )


def _workspace_error(cwd: Optional[str]) -> Optional[str]:
    """Shared fail-closed cwd guard for BOTH modes.

    A falsy or missing workspace must never fall through: in cmd mode the
    subprocess would run in the calling process's CWD, and in auto mode
    ``verification_status`` resolves ``Path(cwd or ".")`` the same way —
    letting evidence for whatever repo the process happens to sit in vouch
    for this task. Fail closed instead.
    """
    if not cwd or not os.path.isdir(cwd):
        return f"verify workspace unavailable: {cwd!r}"
    return None


def run_verify_command(
    command: str,
    cwd: Optional[str],
    timeout: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> VerifyOutcome:
    """Run ``command`` via ``/bin/sh -c`` in ``cwd`` and classify the result.

    ``start_new_session=True`` + a process-group SIGKILL on timeout matters:
    test runners fork grandchildren that a plain ``subprocess.run(timeout=)``
    would leak into the workspace. Output is merged (stdout+stderr), redacted
    BEFORE capping (so the head/tail split cannot leave a recognizable secret
    fragment straddling the omission marker), then capped to the ledger's
    2000-char bound.

    Callers must never invoke this while holding a ``write_txn`` — a
    multi-minute suite inside BEGIN IMMEDIATE starves every board writer.
    """
    ws_err = _workspace_error(cwd)
    if ws_err:
        return VerifyOutcome(
            ok=False, gate="verify_cmd", command=command,
            exit_code=None, detail=ws_err,
        )

    timed_out = False
    output = ""
    proc = None
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                output, _ = proc.communicate(timeout=REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # An escaped-setsid grandchild is holding the pipe open.
                # Close it and proceed with whatever was captured — the
                # reap must itself be bounded.
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
                output = ""
    except Exception as exc:
        # Spawn error (missing /bin/sh, EPERM, …) or an unexpected failure
        # mid-run: kill anything we started, then fail closed.
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return VerifyOutcome(
            ok=False, gate="verify_cmd", command=command,
            exit_code=None, detail=f"verify command failed to run: {exc}",
        )

    detail = _cap_output(redact_sensitive_text(output or "", force=True))
    if timed_out:
        prefix = f"verify command timed out after {timeout}s"
        detail = f"{prefix}\n{detail}" if detail else prefix
        return VerifyOutcome(
            ok=False, gate="verify_cmd", command=command,
            exit_code=None, detail=detail, timed_out=True,
        )
    return VerifyOutcome(
        ok=(proc.returncode == 0), gate="verify_cmd", command=command,
        exit_code=proc.returncode, detail=detail,
    )


def _project_verify_hint(cwd: Optional[str]) -> str:
    """Best-effort ": run one of <commands>" suffix naming the exact fix."""
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
        commands = list((facts or {}).get("verifyCommands") or [])
        if commands:
            return " Project verify commands: " + ", ".join(
                str(c) for c in commands[:3]
            )
    except Exception:
        pass
    return ""


def check_auto_evidence(
    session_ids: list,
    cwd: Optional[str],
) -> VerifyOutcome:
    """Consult the verification ledger (read-only) for accept-worthy evidence.

    ``session_ids`` is the ordered candidate-key chain mirroring how the
    recorder keys evidence (``session_id or task_id or … or "default"`` in
    tools/terminal_tool.py): dispatch session id first, env session id,
    then the dispatch task id. The shared ``"default"`` bucket is NEVER
    consulted — a gateway chat session must not vouch for a task it never
    worked on — and an empty chain fails closed.

    Accepts iff ``status == "passed"`` and ``scope == "full"`` on the first
    candidate that satisfies both: ``passed`` is already edit-aware (edits
    after the run flip it to ``stale``), and requiring full scope honors the
    ledger's own contract that targeted/ad-hoc checks are never upgraded
    into a repo-green claim — which a completion gate IS.

    Rejection details are built from the FIRST candidate (the primary
    identity), each naming the corrective action.
    """
    ws_err = _workspace_error(cwd)
    if ws_err:
        return VerifyOutcome(
            ok=False, gate="verify_auto", command=None,
            exit_code=None, detail=ws_err,
        )

    candidates: list[str] = []
    for cand in session_ids or []:
        s = str(cand).strip() if cand else ""
        if not s or s == "default" or s in candidates:
            continue
        candidates.append(s)
    if not candidates:
        return VerifyOutcome(
            ok=False, gate="verify_auto", command=None, exit_code=None,
            detail=(
                "no worker session identity; cannot consult the "
                "verification ledger"
            ),
        )

    primary: Optional[dict] = None
    for cand in candidates:
        state = verification_status(session_id=cand, cwd=cwd)
        if primary is None:
            primary = state
        evidence = state.get("evidence") or {}
        if state.get("status") == "passed" and evidence.get("scope") == "full":
            command = evidence.get("canonical_command") or evidence.get("command")
            return VerifyOutcome(
                ok=True, gate="verify_auto", command=command,
                exit_code=evidence.get("exit_code"),
                detail=f"fresh full-scope green evidence: {command}",
                evidence={
                    "evidence_id": evidence.get("id"),
                    "created_at": evidence.get("created_at"),
                    "canonical_command": evidence.get("canonical_command"),
                },
            )

    status = (primary or {}).get("status")
    evidence = (primary or {}).get("evidence") or {}
    command = evidence.get("canonical_command") or evidence.get("command")
    if status == "failed":
        summary = evidence.get("output_summary") or ""
        detail = (
            f"latest verification run failed (exit "
            f"{evidence.get('exit_code')}): {command}\n{summary}\n"
            f"Fix the failures and re-run it to green before completing."
        )
    elif status == "stale":
        detail = (
            "you edited files after the last verification run — re-run it "
            "to green before completing."
        )
    elif status == "passed":
        detail = (
            f"latest green evidence is targeted ({command}); run the bare "
            f"full-suite command — targeted checks are never upgraded to a "
            f"completion claim."
        )
    elif status == "not_applicable":
        detail = (
            "the verification ledger does not recognize this workspace as a "
            "project root — no evidence can vouch for it."
        )
    else:
        detail = (
            "no verification evidence recorded this session — run the "
            "project's verify command to completion via the terminal (in "
            "this session, not via a delegated subagent), then retry "
            "kanban_complete." + _project_verify_hint(cwd)
        )
    return VerifyOutcome(
        ok=False, gate="verify_auto", command=command,
        exit_code=evidence.get("exit_code") if evidence else None,
        detail=detail,
    )


def evaluate_task_verification(
    task,
    *,
    session_ids: list,
    runner=None,
    evidence_fn=None,
) -> Optional[VerifyOutcome]:
    """Run the task's verification policy, or ``None`` when it has none.

    The ``None`` short-circuit is the ONLY code non-verify tasks execute —
    the opt-in contract. ``runner`` / ``evidence_fn`` are the DI seams for
    tests; the ``or``-fallback (rather than a def-time default) keeps
    module-level monkeypatching effective.
    """
    mode = getattr(task, "verify_mode", None)
    if not mode:
        return None
    if mode == "cmd":
        return (runner or run_verify_command)(
            task.verify_cmd, cwd=task.workspace_path
        )
    return (evidence_fn or check_auto_evidence)(
        session_ids, task.workspace_path
    )
