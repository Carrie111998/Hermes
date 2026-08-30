import subprocess
from pathlib import Path

from agent.fusion.repo_guard import RepoMutationGuard


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )


def _init_repo(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "fusion@example.test")
    _git(repo, "config", "user.name", "Fusion Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def test_repo_guard_ignores_preexisting_untracked_noise(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "old.rej").write_text("noise\n", encoding="utf-8")
    guard = RepoMutationGuard(repo)

    before = guard.snapshot()
    after = guard.snapshot()
    result = guard.compare(before, after)

    assert result.available is True
    assert result.write_leak is False
    assert before.tracked_status == []


def test_repo_guard_detects_tracked_modification(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    guard = RepoMutationGuard(repo)

    before = guard.snapshot()
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    result = guard.run_after(before)

    assert result.write_leak is True
    assert any("tracked.txt" in line for line in result.diff_summary)


def test_repo_guard_detects_staged_addition_but_not_untracked(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    guard = RepoMutationGuard(repo)

    before = guard.snapshot()
    (repo / "ignored_untracked.txt").write_text("noise\n", encoding="utf-8")
    assert guard.run_after(before).write_leak is False

    (repo / "new_tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "new_tracked.txt")
    result = guard.run_after(before)
    assert result.write_leak is True
    assert any("new_tracked.txt" in line for line in result.diff_summary)
