"""Isolated write-enabled spike worktrees for Fusion."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import FusionSpikeRun


def _run_git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _is_git_repo(repo_root: str | None) -> bool:
    if not repo_root:
        return False
    repo = Path(repo_root)
    if not repo.exists():
        return False
    proc = _run_git(repo, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def create_spike_worktree(repo_root: str | None, run_dir: str, round_index: int, participant_slug: str | None = None) -> FusionSpikeRun:
    """Create a detached throwaway worktree for one participant spike."""
    phase = f"spike-{round_index}"
    if not _is_git_repo(repo_root):
        return FusionSpikeRun(
            round_index=round_index,
            phase=phase,
            available=False,
            error="Fusion spike skipped: target repo is not a git worktree.",
        )

    repo = Path(str(repo_root)).resolve()
    suffix = participant_slug or "shared"
    spike_root = Path(run_dir).resolve() / "spikes" / f"round-{round_index}" / suffix
    worktree = spike_root / "worktree"
    try:
        if worktree.exists():
            shutil.rmtree(worktree)
        spike_root.mkdir(parents=True, exist_ok=True)
        proc = _run_git(repo, "worktree", "add", "--detach", str(worktree), "HEAD", timeout=60)
        if proc.returncode != 0:
            return FusionSpikeRun(
                round_index=round_index,
                phase=phase,
                worktree_path=str(worktree),
                available=False,
                error=(proc.stderr or proc.stdout or "git worktree add failed").strip(),
            )
        return FusionSpikeRun(
            round_index=round_index,
            phase=phase,
            worktree_path=str(worktree),
            available=True,
        )
    except Exception as exc:
        return FusionSpikeRun(
            round_index=round_index,
            phase=phase,
            worktree_path=str(worktree),
            available=False,
            error=f"Fusion spike setup failed: {exc}",
        )


def capture_spike_diff(spike: FusionSpikeRun, *, max_chars: int = 20000) -> FusionSpikeRun:
    """Capture the worktree diff, including untracked files as intent-to-add."""
    if not spike.available or not spike.worktree_path:
        return spike
    worktree = Path(spike.worktree_path)
    if not worktree.exists():
        spike.error = spike.error or "Fusion spike diff unavailable: worktree missing."
        return spike
    try:
        # Make untracked files visible to `git diff` without staging real content.
        _run_git(worktree, "add", "-N", ".", timeout=30)
        stat_proc = _run_git(worktree, "diff", "--stat", timeout=30)
        diff_proc = _run_git(worktree, "diff", "--", timeout=30)
        spike.diff_stat = (stat_proc.stdout or stat_proc.stderr or "").strip()
        diff = (diff_proc.stdout or diff_proc.stderr or "").strip()
        if len(diff) > max_chars:
            diff = diff[:max_chars].rstrip() + "\n...[diff truncated]"
        spike.diff = diff
        return spike
    except Exception as exc:
        spike.error = f"Fusion spike diff capture failed: {exc}"
        return spike


def cleanup_spike_worktree(repo_root: str | None, spike: FusionSpikeRun) -> FusionSpikeRun:
    """Remove a throwaway spike worktree and record cleanup status."""
    if not spike.worktree_path:
        spike.cleanup_ok = True
        return spike
    worktree = Path(spike.worktree_path)
    repo = Path(str(repo_root)).resolve() if repo_root else None
    try:
        if repo is not None and repo.exists():
            proc = _run_git(repo, "worktree", "remove", "--force", str(worktree), timeout=60)
            if proc.returncode == 0:
                spike.cleanup_ok = True
                return spike
            spike.error = (spike.error or "") + ("; " if spike.error else "") + (proc.stderr or proc.stdout or "git worktree remove failed").strip()
        if worktree.exists():
            shutil.rmtree(worktree)
        if repo is not None and repo.exists():
            _run_git(repo, "worktree", "prune", timeout=30)
        spike.cleanup_ok = not worktree.exists()
        return spike
    except Exception as exc:
        spike.cleanup_ok = False
        spike.error = (spike.error or "") + ("; " if spike.error else "") + f"Fusion spike cleanup failed: {exc}"
        return spike
