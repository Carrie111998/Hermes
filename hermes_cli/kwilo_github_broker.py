"""Repository-scoped GitHub App execution for governed Kwilo workers."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli._subprocess_compat import windows_hide_flags


@dataclass(frozen=True)
class BrokerContext:
    persona: str
    repository: str
    broker_path: Path
    workspace: Path


def _hermes_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        root = Path(configured)
        if root.parent.name == "profiles":
            return root.parent.parent
        return root
    return Path.home() / ".hermes"


def resolve_current_task_broker() -> Optional[BrokerContext]:
    """Resolve broker authority only from the claimed card and live manifest."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kwilo_governance as governance

    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    db_value = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not task_id or not db_value:
        return None
    db_path = Path(db_value)
    with kb.connect_closing(db_path=db_path) as conn:
        if not governance.is_kwilo_board(conn):
            return None
        row = conn.execute(
            "SELECT assignee, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"governed GitHub broker task {task_id} not found")
        effective_profile = os.environ.get("HERMES_PROFILE", "").strip().lower()
        assignee = str(row["assignee"] or "").strip().lower()
        if effective_profile and effective_profile != assignee:
            raise RuntimeError(
                "claimed task assignee does not match the effective worker profile"
            )
        semantics = governance.get_task_semantics(conn, task_id)
        if semantics is None:
            raise RuntimeError(
                f"governed GitHub broker task {task_id} has no semantics"
            )
        binding = governance.github_broker_binding(
            assignee,
            semantics["contract"],
        )
        if binding is None:
            return None
        workspace_value = (
            os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip()
            or str(row["workspace_path"] or "").strip()
        )
    workspace = Path(workspace_value) if workspace_value else Path.cwd()
    broker_path = _hermes_root() / "scripts" / "github_app_broker.py"
    if not broker_path.is_file():
        raise RuntimeError(f"Kwilo GitHub App broker is unavailable: {broker_path}")
    if not workspace.is_dir():
        raise RuntimeError(f"claimed task workspace is unavailable: {workspace}")
    return BrokerContext(
        persona=binding["persona"],
        repository=binding["repository"],
        broker_path=broker_path,
        workspace=workspace,
    )


def _broker_env() -> dict[str, str]:
    env = os.environ.copy()
    # The broker mints its own short-lived installation token. Ambient human
    # or profile tokens are neither needed nor allowed to influence it.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def _run(
    context: BrokerContext,
    broker_args: Iterable[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(context.broker_path), *broker_args],
        cwd=str(context.workspace),
        env=_broker_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=windows_hide_flags(),
    )


def _validate_git_arguments(context: BrokerContext, argv: list[str]) -> None:
    """Keep the out-of-sandbox git capability inside the claimed worktree."""
    if not argv:
        raise ValueError("broker command arguments are required")
    escape_options = ("-C", "--git-dir", "--work-tree")
    if any(
        value == option or value.startswith(option + "=")
        for value in argv
        for option in escape_options
    ):
        raise ValueError("brokered git may not override its task worktree")
    command = argv[0]
    if command == "add":
        if len(argv) < 3 or argv[1] != "--":
            raise ValueError(
                "brokered git add requires explicit paths after '--'"
            )
        workspace = context.workspace.resolve()
        for value in argv[2:]:
            if not value or value.startswith("-") or any(
                marker in value for marker in ("*", "?", "[")
            ):
                raise ValueError(
                    "brokered git add accepts explicit path names only"
                )
            candidate = (workspace / value).resolve()
            if not candidate.is_relative_to(workspace):
                raise ValueError(
                    "brokered git add path escapes the task worktree"
                )
        return
    if command == "commit":
        if (
            len(argv) != 3
            or argv[1] not in {"-m", "--message"}
            or not argv[2].strip()
        ):
            raise ValueError(
                "brokered git commit requires exactly one explicit message"
            )
        return
    if command not in {"push", "fetch", "pull", "ls-remote"}:
        raise ValueError(
            "brokered git supports explicit add, commit, push, fetch, pull "
            "and ls-remote operations only"
        )


def _validate_gh_arguments(argv: list[str]) -> None:
    """Prevent a task from replacing the repository fixed by its contract."""
    for argument in argv:
        if (
            argument in {"-R", "--repo"}
            or argument.startswith("--repo=")
            or (argument.startswith("-R") and argument != "-R")
        ):
            raise ValueError(
                "brokered gh may not override its task repository"
            )


def verify_broker(context: BrokerContext) -> None:
    result = _run(
        context,
        [
            "verify",
            context.persona,
            "--repo",
            context.repository,
        ],
        timeout=90,
    )
    if result.returncode != 0:
        detail = redact_sensitive_text(
            (result.stderr or result.stdout or "broker verification failed").strip(),
            force=True,
        )
        raise RuntimeError(
            f"GitHub App broker verification failed for "
            f"{context.persona}/{context.repository}: {detail}"
        )


def run_broker_command(
    context: BrokerContext,
    mode: str,
    arguments: Iterable[str],
) -> dict[str, Any]:
    if mode not in {"gh", "git"}:
        raise ValueError("broker mode must be 'gh' or 'git'")
    argv = [str(value) for value in arguments]
    if not argv:
        raise ValueError("broker command arguments are required")
    if any("\x00" in value for value in argv):
        raise ValueError("broker command arguments may not contain NUL bytes")
    if mode == "git":
        _validate_git_arguments(context, argv)
    else:
        _validate_gh_arguments(argv)
    if mode == "git" and argv[0] in {"add", "commit"}:
        # Local linked-worktree metadata must be written outside the Codex
        # filesystem sandbox, but it neither needs nor should mint a network
        # credential. The validated task workspace remains the fixed cwd.
        local_env = _broker_env()
        local_env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            ["git", *argv],
            cwd=str(context.workspace),
            env=local_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            creationflags=windows_hide_flags(),
        )
    else:
        result = _run(
            context,
            [
                mode,
                context.persona,
                "--repo",
                context.repository,
                "--",
                *argv,
            ],
            timeout=180,
        )
    return {
        "ok": result.returncode == 0,
        "exit_code": int(result.returncode),
        "stdout": redact_sensitive_text(result.stdout or "", force=True),
        "stderr": redact_sensitive_text(result.stderr or "", force=True),
        "identity": context.persona,
        "repository": context.repository,
    }


def broker_execution_instruction(context: BrokerContext) -> str:
    return (
        "GitHub network identity is already preflighted as the repository-scoped "
        f"{context.persona!r} GitHub App for {context.repository}. For every "
        "authenticated git network operation, invoke the hermes-tools MCP "
        "`github_broker_git` tool with an argument array. Linked-worktree Git "
        "metadata may sit outside the Codex sandbox, so staging and committing "
        "must also use `github_broker_git`: stage only explicit paths as "
        "`[\"add\", \"--\", ...]` and commit as "
        "`[\"commit\", \"-m\", \"...\"]`. Read-only local git commands may use "
        "the shell. For every GitHub API, "
        "PR, issue, check or Actions operation, invoke `github_broker_gh`. "
        "Never run `gh auth status`, "
        "ambient `gh`, a direct networked `git push`, or request/persist a token; "
        "ambient CLI authentication is deliberately irrelevant."
    )
