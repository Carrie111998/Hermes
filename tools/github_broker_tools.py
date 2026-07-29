"""Task-scoped GitHub App tools for governed Codex workers."""
from __future__ import annotations

import json
import logging

from tools.registry import registry

logger = logging.getLogger(__name__)


def _check_broker_mode() -> bool:
    try:
        from hermes_cli.kwilo_github_broker import resolve_current_task_broker

        return resolve_current_task_broker() is not None
    except Exception:
        logger.debug("GitHub broker tool admission failed", exc_info=True)
        return False


def _handle(mode: str, args: dict) -> str:
    arguments = args.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        return json.dumps(
            {"error": "arguments must be a non-empty string array"},
            ensure_ascii=False,
        )
    try:
        import os
        from pathlib import Path
        from hermes_cli.kwilo_github_broker import resolve_current_task_broker
        from hermes_cli.kanban_codex_bridge import request_host_broker_command

        context = resolve_current_task_broker()
        if context is None:
            raise RuntimeError("current task has no GitHub App broker authority")
        task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", "0"))
        db_path = Path(os.environ.get("HERMES_KANBAN_DB", ""))
        return json.dumps(
            request_host_broker_command(
                db_path=db_path,
                task_id=task_id,
                run_id=run_id,
                tool=mode,
                arguments=arguments,
            ),
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.exception("GitHub broker %s command failed", mode)
        return json.dumps(
            {"error": str(exc), "mode": mode},
            ensure_ascii=False,
        )


def _handle_gh(args: dict, **_kw) -> str:
    return _handle("gh", args)


def _handle_git(args: dict, **_kw) -> str:
    return _handle("git", args)


_ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "arguments": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Exact argv after gh/git; never include a token.",
        },
    },
    "required": ["arguments"],
}

GITHUB_BROKER_GH_SCHEMA = {
    "name": "github_broker_gh",
    "description": (
        "Run a GitHub CLI operation through the current task's preflighted, "
        "repository-scoped GitHub App. Use for PRs, issues, checks, Actions "
        "and API calls. Ambient gh authentication is prohibited."
    ),
    "parameters": _ARGUMENTS_SCHEMA,
}

GITHUB_BROKER_GIT_SCHEMA = {
    "name": "github_broker_git",
    "description": (
        "Run a scoped git operation outside the Codex sandbox from the current "
        "task's registered worktree. Use explicit ['add', '--', <paths>] and "
        "['commit', '-m', <message>] for linked-worktree metadata, and use it "
        "for authenticated push/fetch/pull/ls-remote through the preflighted "
        "repository-scoped GitHub App. Read-only local git may use the shell."
    ),
    "parameters": _ARGUMENTS_SCHEMA,
}

registry.register(
    name="github_broker_gh",
    toolset="kanban",
    schema=GITHUB_BROKER_GH_SCHEMA,
    handler=_handle_gh,
    check_fn=_check_broker_mode,
    emoji="🔐",
)

registry.register(
    name="github_broker_git",
    toolset="kanban",
    schema=GITHUB_BROKER_GIT_SCHEMA,
    handler=_handle_git,
    check_fn=_check_broker_mode,
    emoji="🔐",
)
