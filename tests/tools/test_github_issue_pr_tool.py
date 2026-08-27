"""GitHub Issue→PR 工作流前置检查测试。"""

import json
from pathlib import Path

from tools import github_issue_pr_tool as tool


def test_preflight_rejects_non_git_workspace(tmp_path: Path):
    assert "Git 仓库" in tool.github_issue_pr_preflight(str(tmp_path))


def test_preflight_reports_auth_state_without_side_effects(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(tool.shutil, "which", lambda _: "/usr/bin/gh")

    class Result:
        def __init__(self, code, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = code, stdout, stderr

    replies = [Result(1, stderr="未登录"), Result(0, stdout="git@github.com:org/repo.git\n"), Result(0)]
    monkeypatch.setattr(tool, "_git", lambda *args: replies.pop(0))
    result = json.loads(tool.github_issue_pr_preflight(str(tmp_path)))
    assert result["github_authenticated"] is False
    assert result["origin"] == "git@github.com:org/repo.git"
    assert result["working_tree_clean"] is True
