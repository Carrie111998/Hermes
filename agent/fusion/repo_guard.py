"""Tracked-repo mutation guard for Fusion v2."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .models import FusionRepoGuardResult, FusionRepoSnapshot


def _digest_lines(lines: list[str]) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


class RepoMutationGuard:
    """Snapshot tracked git state before/after participant fan-out.

    The guard intentionally ignores untracked files by default, so existing
    unrelated `.orig` / `.rej` files and out-of-repo Fusion artifacts do not
    make a clean participant run look dirty. Staged additions are still caught
    by `git status --porcelain --untracked-files=no`.
    """

    def __init__(self, repo_root: str | Path | None):
        self.repo_root = str(Path(repo_root).resolve()) if repo_root else None

    def snapshot(self) -> FusionRepoSnapshot:
        if not self.repo_root:
            return FusionRepoSnapshot(
                repo_root=None,
                available=False,
                error="Repository root unavailable; cannot snapshot tracked state.",
            )
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=no"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - defensive around platform git failures
            return FusionRepoSnapshot(
                repo_root=self.repo_root,
                available=False,
                error=f"git status failed: {exc}",
            )
        if proc.returncode != 0:
            return FusionRepoSnapshot(
                repo_root=self.repo_root,
                available=False,
                error=(proc.stderr or proc.stdout or "git status failed").strip(),
            )
        lines = sorted(line for line in proc.stdout.splitlines() if line.strip())
        return FusionRepoSnapshot(
            repo_root=self.repo_root,
            available=True,
            tracked_status=lines,
            digest=_digest_lines(lines),
        )

    @staticmethod
    def compare(before: FusionRepoSnapshot, after: FusionRepoSnapshot) -> FusionRepoGuardResult:
        available = bool(before.available and after.available)
        if not available:
            return FusionRepoGuardResult(
                repo_root=before.repo_root or after.repo_root,
                available=False,
                before=before,
                after=after,
                write_leak=False,
                error=before.error or after.error,
            )

        before_set = set(before.tracked_status)
        after_set = set(after.tracked_status)
        added = sorted(after_set - before_set)
        removed = sorted(before_set - after_set)
        diff_summary = [f"+ {line}" for line in added] + [f"- {line}" for line in removed]
        return FusionRepoGuardResult(
            repo_root=before.repo_root,
            available=True,
            before=before,
            after=after,
            write_leak=before.digest != after.digest,
            diff_summary=diff_summary,
        )

    def run_after(self, before: FusionRepoSnapshot) -> FusionRepoGuardResult:
        return self.compare(before, self.snapshot())
