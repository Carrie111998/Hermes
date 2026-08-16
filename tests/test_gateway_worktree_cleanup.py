import subprocess
from pathlib import Path

from gateway.worktree import cleanup_session_worktree


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "config", "user.email", "test@example.com")
    _git("-C", str(repo), "config", "user.name", "Test")
    (repo / "README").write_text("test\n")
    _git("-C", str(repo), "add", ".")
    _git("-C", str(repo), "commit", "-qm", "initial")
    _git("-C", str(repo), "branch", "-M", "main")
    _git("-C", str(repo), "remote", "add", "origin", str(repo))
    _git("-C", str(repo), "fetch", "-q", "origin")
    return repo


def _worktree(repo: Path, tmp_path: Path, branch: str) -> dict[str, str]:
    path = tmp_path / branch.replace("/", "-")
    _git("-C", str(repo), "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
    return {"path": str(path), "branch": branch, "repo_root": str(repo)}


def test_clean_session_worktree_is_released(tmp_path: Path):
    repo = _repo(tmp_path)
    info = _worktree(repo, tmp_path, "hermes/clean")

    result = cleanup_session_worktree(info)

    assert result == {"state": "released", "reason": "session_end", "branch_deleted": True}
    assert not Path(info["path"]).exists()


def test_dirty_session_worktree_is_retained(tmp_path: Path):
    repo = _repo(tmp_path)
    info = _worktree(repo, tmp_path, "hermes/dirty")
    Path(info["path"], "uncommitted").write_text("keep\n")

    result = cleanup_session_worktree(info)

    assert result["state"] == "retained"
    assert result["reason"] == "dirty"
    assert Path(info["path"]).exists()


def test_unpushed_session_worktree_is_retained(tmp_path: Path):
    repo = _repo(tmp_path)
    info = _worktree(repo, tmp_path, "hermes/unpushed")
    Path(info["path"], "committed").write_text("keep\n")
    _git("-C", info["path"], "add", ".")
    _git("-C", info["path"], "commit", "-qm", "retain work")

    result = cleanup_session_worktree(info)

    assert result["state"] == "retained"
    assert result["reason"] == "unpushed"
    assert Path(info["path"]).exists()


def test_missing_session_worktree_is_idempotently_released(tmp_path: Path):
    repo = _repo(tmp_path)
    info = _worktree(repo, tmp_path, "hermes/missing")
    _git("-C", str(repo), "worktree", "remove", info["path"])

    result = cleanup_session_worktree(info)

    assert result == {"state": "released", "reason": "already_removed"}
