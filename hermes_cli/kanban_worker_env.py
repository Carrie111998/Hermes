from __future__ import annotations

import os


def pin_worker_cwd_env(env: dict, workspace: str) -> dict:
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    return env
