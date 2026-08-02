"""Human-reviewed proposals for compiling stable deterministic traces.

The compiler never writes a script or creates a cron job.  It only produces a
bounded proposal after repeated successful evidence across distinct sessions.
"""

from __future__ import annotations

import hashlib
import shlex
from collections import defaultdict
from typing import Any, Iterable, Mapping

_CONTROL = set(";&|><`$\n\r")
_SECRET_FLAGS = {
    "--api-key",
    "--apikey",
    "--password",
    "--token",
    "--secret",
    "-p",
}
_SAFE_KINDS = {"test", "lint", "typecheck", "build"}
_SAFE_PACKAGE_TASKS = {"test", "check", "lint", "typecheck", "build"}


def _safe_argv(command: str) -> list[str] | None:
    if not command or any(char in command for char in _CONTROL):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or len(argv) > 40:
        return None
    lowered = [token.lower() for token in argv]
    if any(token in _SECRET_FLAGS for token in lowered):
        return None
    if any(any(marker in token for marker in ("api_key=", "token=", "password=", "secret=")) for token in lowered):
        return None
    executable = lowered[0]
    if executable in {"npm", "pnpm", "yarn"}:
        task = next((token for token in lowered[1:] if not token.startswith("-")), "")
        if task == "run":
            index = lowered.index("run") + 1
            task = lowered[index] if index < len(lowered) else ""
        if task not in _SAFE_PACKAGE_TASKS:
            return None
    elif executable in {"pytest", "py.test", "scripts/run_tests.sh"}:
        pass
    elif executable in {"python", "python3"}:
        if len(lowered) < 3 or lowered[1:3] not in (["-m", "pytest"], ["-m", "ruff"]):
            return None
    elif executable == "uv":
        if "pytest" not in lowered and "ruff" not in lowered:
            return None
    else:
        return None
    return argv


def _script_preview(argv: list[str]) -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import subprocess\n\n"
        f"raise SystemExit(subprocess.run({argv!r}, check=False).returncode)\n"
    )


def propose_compilations(
    events: Iterable[Mapping[str, Any]],
    *,
    min_successes: int = 3,
    min_sessions: int = 3,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        root = str(event.get("root") or "")
        command = str(event.get("canonical_command") or "")
        if root and command:
            groups[(root, command)].append(event)

    proposals: list[dict[str, Any]] = []
    for (root, command), records in sorted(groups.items()):
        argv = _safe_argv(command)
        if argv is None:
            continue
        if any(str(record.get("status")) != "passed" for record in records):
            continue
        successes = len(records)
        sessions = {str(record.get("session_id") or "") for record in records}
        if successes < min_successes or len(sessions) < min_sessions:
            continue
        digest = hashlib.sha256(f"{root}\0{command}".encode("utf-8")).hexdigest()[:16]
        summaries = {str(record.get("output_summary") or "") for record in records}
        kinds = {str(record.get("kind") or "") for record in records}
        stable_output = len(summaries) == 1
        proposals.append(
            {
                "id": f"trace-{digest}",
                "root": root,
                "canonical_command": command,
                "argv": argv,
                "successes": successes,
                "distinct_sessions": len(sessions),
                "stable_output": stable_output,
                "script_preview": _script_preview(argv),
                "user_review_required": True,
                "no_agent_eligible": stable_output and len(kinds) == 1 and kinds <= _SAFE_KINDS,
                "schedule": None,
                "next_action": "Review the script, choose a destination and schedule, then create it through the existing script/cron approval path.",
            }
        )
    return proposals


def discover_compilation_proposals() -> list[dict[str, Any]]:
    """Project stable proposals from the profile's verification evidence DB."""
    try:
        from agent.verification_evidence import _connect

        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT session_id, root, canonical_command, command, kind, status, output_summary
                FROM verification_events
                ORDER BY id DESC
                LIMIT 1000
                """
            ).fetchall()
            events = [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception:
        return []
    return propose_compilations(events)


def format_compilation_proposals() -> str:
    proposals = discover_compilation_proposals()
    if not proposals:
        return "Trace compilation: no workflow has enough stable successful receipts yet."
    lines = [f"Trace compilation: {len(proposals)} human-review proposal(s)"]
    for proposal in proposals:
        lines.append(
            f"- {proposal['id']}: {proposal['canonical_command']} "
            f"({proposal['successes']} successes across {proposal['distinct_sessions']} sessions; no schedule inferred)"
        )
    return "\n".join(lines)
