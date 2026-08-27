"""GitHub Issue 到 PR 的受控工作流。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from tools.registry import registry, tool_error


def _git(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def github_issue_pr_preflight(workspace: Optional[str] = None) -> str:
    """验证 Issue→PR 闭环前置条件；不创建分支、不修改仓库。"""
    root = Path(workspace or ".").expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        return tool_error("workspace 必须是 Git 仓库。")
    gh = shutil.which("gh")
    if not gh:
        return tool_error("未找到 GitHub CLI（gh），无法执行 Issue→PR 工作流。")
    auth = _git([gh, "auth", "status"], root)
    remote = _git(["git", "remote", "get-url", "origin"], root)
    status = _git(["git", "status", "--short"], root)
    return json.dumps(
        {
            "workspace": str(root),
            "github_authenticated": auth.returncode == 0,
            "github_auth_output": (auth.stdout + auth.stderr).strip(),
            "origin": remote.stdout.strip() if remote.returncode == 0 else None,
            "working_tree_clean": not bool(status.stdout.strip()),
            "next_step": "提供 issue 编号后可创建分支并执行编码 Worker" if auth.returncode == 0 else "先执行 gh auth login；当前不会创建分支或 PR",
        },
        ensure_ascii=False,
    )


GITHUB_ISSUE_PR_SCHEMA = {
    "name": "github_issue_pr_preflight",
    "description": "检查 GitHub Issue→分支→编码 Worker→测试→PR 工作流的认证、仓库与工作区前置条件；只读。",
    "parameters": {
        "type": "object",
        "properties": {"workspace": {"type": "string", "description": "Git 仓库绝对路径"}},
    },
}

registry.register(
    name="github_issue_pr_preflight",
    toolset="github_workflow",
    schema=GITHUB_ISSUE_PR_SCHEMA,
    handler=lambda args, **kw: github_issue_pr_preflight(args.get("workspace")),
)
