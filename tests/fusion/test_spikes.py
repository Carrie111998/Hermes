import subprocess
from pathlib import Path

from agent.fusion.spikes import capture_spike_diff, cleanup_spike_worktree, create_spike_worktree


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
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


def test_spike_worktree_captures_diff_and_cleans_up(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)

    spike = create_spike_worktree(str(repo), str(run_dir), 1, "glm-max")
    assert spike.available is True
    assert spike.worktree_path
    worktree = Path(spike.worktree_path)
    assert worktree.exists()

    (worktree / "tracked.txt").write_text("two\n", encoding="utf-8")
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")
    spike = capture_spike_diff(spike)
    assert "tracked.txt" in spike.diff_stat
    assert "new.txt" in spike.diff_stat
    assert "-one" in spike.diff
    assert "+two" in spike.diff
    assert "+new" in spike.diff

    spike = cleanup_spike_worktree(str(repo), spike)
    assert spike.cleanup_ok is True
    assert not worktree.exists()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "one\n"


def test_spike_worktree_reports_unavailable_for_non_git_repo(tmp_path):
    non_git = tmp_path / "plain"
    non_git.mkdir()
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)

    spike = create_spike_worktree(str(non_git), str(run_dir), 1, "glm-max")
    assert spike.available is False
    assert "not a git" in (spike.error or "")
