"""外部编码 Worker 工具测试。"""

import json
from pathlib import Path

from tools import coding_worker_tool as worker


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_workspace_requires_git_repo(tmp_path: Path):
    try:
        worker._workspace(str(tmp_path))
        assert False, "非 Git 目录必须拒绝"
    except ValueError as exc:
        assert "Git 仓库" in str(exc)


def test_coding_worker_runs_implementation_and_review(tmp_path: Path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(
        worker,
        "_load_worker_config",
        lambda: {"enabled": True, "worker": "codex", "review_worker": "claude"},
    )
    seen = []

    def fake_run(name, prompt, cwd, timeout):
        seen.append((name, prompt, cwd, timeout))
        return {"status": "ok", "exit_code": 0, "output": "APPROVED"}

    monkeypatch.setattr(worker, "_run", fake_run)
    result = json.loads(worker.coding_worker("修复测试", workspace=str(root)))
    assert result["implementation"]["status"] == "ok"
    assert result["review"]["output"] == "APPROVED"
    assert [call[0] for call in seen] == ["codex", "claude"]
    assert all(call[2] == root for call in seen)


def test_coding_worker_does_not_review_failed_implementation(tmp_path: Path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(worker, "_load_worker_config", lambda: {"enabled": True})
    monkeypatch.setattr(worker, "_run", lambda *args: {"status": "error", "exit_code": 1, "output": "failed"})
    result = json.loads(worker.coding_worker("修复测试", workspace=str(root)))
    assert result["review"] is None
