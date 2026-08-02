"""Deterministic context-health and workflow-compilation CLI."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import subprocess


def build_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "context",
        help="Audit durable context and inspect deterministic workflow proposals",
    )
    actions = parser.add_subparsers(dest="context_action")
    actions.add_parser("audit", help="Run the deterministic Context Health Audit")
    actions.add_parser(
        "compile-proposals",
        aliases=["compile"],
        help="Show human-reviewed stable trace-to-script proposals",
    )
    verify = actions.add_parser(
        "verify",
        help="Run an allowlisted deterministic check linked to one learning candidate",
    )
    verify.add_argument("candidate_id")
    verify.add_argument("verify_argv", nargs=argparse.REMAINDER)
    return parser


def cmd_context(args) -> int:
    action = getattr(args, "context_action", None)
    if action == "audit":
        from agent.context_health import format_context_audit

        print(format_context_audit())
        return 0
    if action in {"compile-proposals", "compile"}:
        from agent.trace_compiler import format_compilation_proposals

        print(format_compilation_proposals())
        return 0
    if action == "verify":
        from agent import learning_ledger
        from agent.trace_compiler import _safe_argv
        from agent.verification_evidence import record_terminal_result

        candidate_id = str(args.candidate_id)
        candidate = learning_ledger.get_candidate(candidate_id)
        if candidate is None or candidate.get("status") != "active":
            print("Verification requires a known active learning candidate.")
            return 2
        raw = list(args.verify_argv or [])
        if raw and raw[0] == "--":
            raw = raw[1:]
        command = shlex.join(raw)
        argv = _safe_argv(command)
        if argv is None:
            print("Verification command is not an allowlisted deterministic check.")
            return 2
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        try:
            receipt = record_terminal_result(
                command=command,
                cwd=Path.cwd(),
                session_id=f"context-verify:{candidate_id}",
                exit_code=completed.returncode,
                output=output,
                candidate_id=candidate_id,
            )
        except Exception:
            if output:
                print(output[-4000:].rstrip())
            print("Verification ran, but its candidate outcome could not be persisted.")
            return 2
        if output:
            print(output[-4000:].rstrip())
        if receipt is None:
            print("Command ran but is not a recognized project verification command; no learning outcome was recorded.")
            return 2
        if not receipt.get("candidate_outcome_recorded"):
            print("Verification ran, but its candidate outcome could not be persisted.")
            return 2
        print(
            f"Candidate {candidate_id} verification "
            f"{'passed' if completed.returncode == 0 else 'failed'} (exit {completed.returncode})."
        )
        return completed.returncode
    print("Usage: hermes context <audit|compile-proposals|verify>")
    return 2
