#!/usr/bin/env python3
"""Kanban integrity steward — re-open coding tasks marked ``done`` whose
referenced branch has no commits ahead of ``main`` on origin.

This is the safety net behind ``kanban_complete``'s new merge-required
gate. Even with the gate in place, drift can happen: a force-push to
``main`` that squashes the branch's history, a human running
``hermes kanban complete --force`` outside the worker path, or a
gate-bypassing manual CLI invocation. Every 30 minutes this watchdog
re-scans all ``done`` tasks whose body carries a ``repo: <owner>/<name>``
directive and checks that the named branch has at least one commit ahead
of ``origin/main``. Any task that fails the check is moved back to
``ready`` and a ``merge_required_reopened`` event is recorded so the
next dispatcher tick re-spawns a worker to fix it.

Usage:
  python -m cron.scripts.kanban_integrity_steward

Cron wiring (LLM-driven job, every 30 minutes):
  hermes cron add \
    --name kanban-integrity-steward \
    --schedule "*/30 * * * *" \
    --prompt "Run `python -m hermes_cli.cron.scripts.kanban_integrity_steward`.
              If it prints any re-opened task ids, deliver them via Slack
              as a single line. Stay silent if the script prints nothing." \
    --deliver slack

Pure-script (no_agent=True) wiring — no LLM, deterministic:
  hermes cron add \
    --name kanban-integrity-steward \
    --schedule "*/30 * * * *" \
    --script hermes_cli.cron.scripts.kanban_integrity_steward \
    --no-agent \
    --deliver slack

Exit codes:
  0 — clean run (no re-opens, or all re-opens were reported)
  1 — script error (DB unreachable, etc.)
  2 — at least one task was re-opened (so the cron supervisor sees a
       non-zero exit and surfaces the delivery even when stdout is
       empty — see cron no_agent=True semantics)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Allow running both as a module (``python -m cron.scripts.kanban_integrity_steward``)
# and as a script. The cron subsystem is a top-level package — not under
# hermes_cli — so the path fallback only needs to anchor ``hermes_cli``.
try:
    from hermes_cli import kanban_db as kb
except Exception:  # pragma: no cover — module import must succeed
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hermes_cli import kanban_db as kb  # type: ignore[no-redef]


# Body pattern that opts a task into the gate (kept in sync with
# ``hermes_cli/kanban_db.py::_CODING_TASK_REPO_RE``).
_REPO_DIRECTIVE_RE = re.compile(
    r"(?m)^\s*repo:\s*([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s*$"
)


def _git_rev_local(repo_root: Path, branch: str) -> str | None:
    """Local HEAD SHA for ``branch`` in ``repo_root``, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", branch],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip().splitlines()[0] or None


def _git_rev_remote(repo_root: Path, branch: str) -> str | None:
    """origin/<branch> SHA, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "origin", branch],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    return (result.stdout or "").strip().splitlines()[0].split("\t", 1)[0].strip() or None


def _git_rev_origin_main(repo_root: Path) -> str | None:
    """origin/main SHA, falling back to origin/master, then local main/master."""
    for ref in ("origin/main", "origin/master"):
        sha = _git_rev_remote(repo_root, ref.split("/", 1)[-1])
        if sha:
            return sha
    for ref in ("main", "master"):
        sha = _git_rev_local(repo_root, ref)
        if sha:
            return sha
    return None


def _resolve_repo(conn, task_id: str, workspace_path: str | None,
                  project_id: str | None) -> Path | None:
    """Resolve a git repo for a task. Mirrors
    ``kanban_db._resolve_coding_repo`` but takes the row fields directly
    so the steward script doesn't need to duplicate the SQL.
    """
    candidates: list[Path] = []
    if workspace_path:
        candidates.append(Path(workspace_path).expanduser())
        wt = Path(workspace_path).expanduser()
        if wt.parent.name == ".worktrees":
            candidates.append(wt.parent.parent)
    if project_id:
        try:
            from hermes_cli import projects_db as _pdb
            with _pdb.connect_closing() as _pconn:
                proj = _pdb.get_project(_pconn, project_id)
                if proj and proj.primary_path:
                    candidates.append(Path(proj.primary_path).expanduser())
        except Exception:
            pass
    for cand in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(cand), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except Exception:
            continue
        if result.returncode == 0 and (result.stdout or "").strip():
            return Path((result.stdout or "").strip().splitlines()[0])
    return None


def audit_done_coding_tasks() -> list[dict]:
    """Find ``done`` tasks with a ``repo:`` directive whose branch
    isn't ahead of ``main`` on origin, and re-open them.

    Returns a list of {task_id, repo, branch, reason} dicts for any
    task that was re-opened. Empty list = clean run.
    """
    reopened: list[dict] = []
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT id, body, branch_name, workspace_path, project_id "
            "FROM tasks WHERE status = 'done'",
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        body = row["body"] or ""
        match = _REPO_DIRECTIVE_RE.search(body)
        if not match:
            continue  # Not a coding task; skip.
        branch = (row["branch_name"] or "").strip()
        if not branch:
            # Coding task marked done with no branch — already a gate
            # failure. Re-open so a worker can fix it.
            reopened.append({
                "task_id": row["id"],
                "repo": match.group(1),
                "branch": None,
                "reason": "done task missing branch_name",
            })
            _reopen(row["id"], match.group(1), None,
                    "done task missing branch_name")
            continue
        repo_root = _resolve_repo(
            None, row["id"], row["workspace_path"], row["project_id"],
        )
        if repo_root is None:
            # Repo not on disk; can't verify. Leave alone — the task may
            # have been legitimately completed on a worker machine we
            # no longer have access to. A human steward can re-open
            # manually if needed.
            continue
        local_head = _git_rev_local(repo_root, branch)
        remote_head = _git_rev_remote(repo_root, branch)
        main_sha = _git_rev_origin_main(repo_root)
        if local_head is None or remote_head is None or main_sha is None:
            continue  # Can't verify; leave alone.
        if local_head == main_sha or remote_head == main_sha:
            reason = (
                f"branch {branch!r} has no commits ahead of origin/main "
                f"(local={local_head[:7]}, remote={remote_head[:7]}, "
                f"main={main_sha[:7]})"
            )
            reopened.append({
                "task_id": row["id"],
                "repo": match.group(1),
                "branch": branch,
                "reason": reason,
            })
            _reopen(row["id"], match.group(1), branch, reason)

    return reopened


def _reopen(task_id: str, repo: str, branch: str | None, reason: str) -> None:
    """Move a task back to ``ready`` and record the audit event.

    Done in its own txn so a partial failure (e.g. event append fails)
    doesn't leave the status flip half-applied.
    """
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, block_kind=NULL, "
                "block_recurrences=0 WHERE id=?",
                (task_id,),
            )
            kb._append_event(
                conn, task_id, "merge_required_reopened",
                {
                    "repo": repo,
                    "branch": branch,
                    "reason": reason,
                    "steward_run_at": int(time.time()),
                },
            )
    finally:
        conn.close()


def main() -> int:
    try:
        reopened = audit_done_coding_tasks()
    except Exception as exc:
        print(f"kanban-integrity-steward: error during audit: {exc!r}",
              file=sys.stderr)
        return 1
    if reopened:
        # One JSON line per re-opened task so downstream consumers can
        # parse without depending on prose layout.
        for entry in reopened:
            print(json.dumps(entry, sort_keys=True))
        return 2
    print("")  # quiet run; no delivery
    return 0


if __name__ == "__main__":
    sys.exit(main())
