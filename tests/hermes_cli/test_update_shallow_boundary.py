"""Regression tests for #94477.

``hermes update`` on a Windows installer checkout (``git clone --depth 1``)
hard-resets instead of fast-forwarding. Root cause: the banner / ``update
--check`` paths fetch with ``--depth 1`` on shallow repos, which moves
``origin/<branch>`` onto a commit DISCONNECTED from HEAD (git never re-sends
history below a shallow boundary the client already has). The updater's
``merge --ff-only`` then fails with "unrelated histories" and falls into
``reset --hard`` even though the checkout has zero local commits.

These tests build tiny local bare remotes and exercise the real git binary
(no network), because the bug is a git object-graph phenomenon that mocks
cannot reproduce.
"""

import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

GIT_AVAILABLE = shutil.which("git") is not None


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _make_origin(root, commits=2):
    """Bare ``origin.git`` remote + ``seed`` repo with N commits on main."""
    origin = root / "origin.git"
    seed = root / "seed"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.name", "t"], check=True
    )
    for _ in range(commits):
        subprocess.run(
            ["git", "-C", str(seed), "commit", "--allow-empty", "-q", "-m", "c"],
            capture_output=True, check=True,
        )
    subprocess.run(
        ["git", "-C", str(seed), "push", "../origin.git", "main"],
        capture_output=True, check=True,
    )
    return seed, origin


def _add_upstream_commit(seed, origin):
    subprocess.run(
        ["git", "-C", str(seed), "commit", "--allow-empty", "-q", "-m", "up"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "../origin.git", "main"],
        capture_output=True, check=True,
    )


def _clone(root, origin, shallow):
    work = root / "work"
    url = "file://" + str(origin).replace("\\", "/")
    args = ["git", "clone"]
    if shallow:
        args += ["--depth", "1"]
    subprocess.run(
        args + [url, str(work)], capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(work), "config", "user.name", "t"], check=True
    )
    return work


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not available")
class TestRepairShallowBoundary:
    """The updater must heal a disconnected shallow boundary and fast-forward
    instead of concluding "history diverged" and resetting (#94477)."""

    def test_heals_disconnected_boundary_and_fast_forwards(self, tmp_path):
        from hermes_cli import update_cmd

        seed, origin = _make_origin(tmp_path)
        work = _clone(tmp_path, origin, shallow=True)
        _add_upstream_commit(seed, origin)

        # The poisoner: banner / --check style `fetch --depth 1`.
        assert _git(work, "fetch", "--depth", "1", "origin", "main").returncode == 0
        # Disconnected boundary: no common ancestor with origin/main.
        assert _git(work, "merge-base", "HEAD", "origin/main").returncode != 0

        repaired = update_cmd._repair_shallow_boundary(["git"], "main", work)

        assert repaired is True
        # Fast-forwarded: HEAD == origin/main tip, boundary fully gone.
        head = _git(work, "rev-parse", "HEAD").stdout.strip()
        remote = _git(work, "rev-parse", "origin/main").stdout.strip()
        seed_tip = _git(seed, "rev-parse", "HEAD").stdout.strip()
        assert head == remote == seed_tip
        assert not (work / ".git" / "shallow").exists()
        assert _git(work, "merge-base", "HEAD", "origin/main").returncode == 0

    def test_does_not_hide_real_divergence(self, tmp_path):
        """A genuine local commit must survive: the repair reports False and
        leaves HEAD untouched so the caller keeps its divergence handling."""
        from hermes_cli import update_cmd

        seed, origin = _make_origin(tmp_path)
        work = _clone(tmp_path, origin, shallow=True)
        _add_upstream_commit(seed, origin)
        _git(work, "fetch", "--depth", "1", "origin", "main")
        _git(work, "commit", "--allow-empty", "-q", "-m", "local")
        local_tip = _git(work, "rev-parse", "HEAD").stdout.strip()

        repaired = update_cmd._repair_shallow_boundary(["git"], "main", work)

        assert repaired is False
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == local_tip

    def test_full_clone_returns_false_without_unshallowing(self, tmp_path):
        """Non-shallow checkouts must never trigger an unshallow fetch."""
        from hermes_cli import update_cmd

        _, origin = _make_origin(tmp_path)
        work = _clone(tmp_path, origin, shallow=False)

        repaired = update_cmd._repair_shallow_boundary(["git"], "main", work)

        assert repaired is False
        assert not (work / ".git" / "shallow").exists()


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not available")
class TestBannerShallowPassiveProbe:
    """The banner's behind-count check must probe the remote tip via
    ls-remote on shallow checkouts instead of fetching with --depth 1
    (which is what poisons the boundary in the first place, #94477)."""

    def test_shallow_check_never_fetches_and_boundary_stays_intact(self, tmp_path):
        from hermes_cli import banner

        seed, origin = _make_origin(tmp_path)
        work = _clone(tmp_path, origin, shallow=True)
        _add_upstream_commit(seed, origin)

        boundary_before = sorted((work / ".git" / "shallow").read_text().split())
        origin_main_before = _git(work, "rev-parse", "origin/main").stdout.strip()

        real_run = subprocess.run

        def recording_run(cmd, **kwargs):
            if "fetch" in cmd:
                raise AssertionError(
                    f"shallow banner check must not fetch: {cmd}"
                )
            return real_run(cmd, **kwargs)

        with (
            patch.object(banner, "_github_compare_behind", return_value=1),
            patch.object(banner.subprocess, "run", side_effect=recording_run),
        ):
            behind = banner._check_via_local_git(work)

        assert behind == 1
        # No ref movement, no new boundary entries.
        assert sorted((work / ".git" / "shallow").read_text().split()) == boundary_before
        assert _git(work, "rev-parse", "origin/main").stdout.strip() == origin_main_before


class TestCheckUpdateShallowPassive:
    """`hermes update --check` on a shallow checkout must probe the remote
    tip via ls-remote (no local fetch) and keep the upstream preference."""

    def _fake_run(self, calls, upstream_url=None):
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            key = " ".join(cmd)
            if "--is-shallow-repository" in key:
                return MagicMock(stdout="true", returncode=0)
            if "get-url" in key and "upstream" in key:
                if upstream_url:
                    return MagicMock(stdout=upstream_url, returncode=0)
                return MagicMock(stdout="", returncode=1)
            if "get-url" in key and "origin" in key:
                return MagicMock(
                    stdout="https://github.com/NousResearch/hermes-agent.git",
                    returncode=0,
                )
            if "rev-parse" in key:
                return MagicMock(stdout="a" * 40, returncode=0)
            raise AssertionError(f"unexpected command: {cmd}")

        return fake_run

    def test_shallow_check_uses_ls_remote_not_fetch(self, tmp_path, capsys, monkeypatch):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        calls = []
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run(calls),
            ),
            patch("hermes_cli.banner._ls_remote_tip", return_value="b" * 40),
            patch("hermes_cli.banner._github_compare_behind", return_value=1),
            patch(
                "hermes_cli.config.recommended_update_command",
                return_value="hermes update",
            ),
        ):
            update_cmd._check_update_shallow(["git"], "main")

        joined = [" ".join(c) for c in calls]
        assert not any("fetch" in j for j in joined), f"must not fetch: {calls}"
        out = capsys.readouterr().out
        assert "1 commit behind origin/main" in out

    def test_shallow_check_prefers_upstream_remote(self, tmp_path, capsys, monkeypatch):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        calls = []
        upstream_url = "https://github.com/NousResearch/hermes-agent.git"
        ls_remote_urls = []

        def capture_ls_remote(url, branch):
            ls_remote_urls.append(url)
            return "b" * 40

        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run(calls, upstream_url=upstream_url),
            ),
            patch("hermes_cli.banner._ls_remote_tip", side_effect=capture_ls_remote),
            patch("hermes_cli.banner._github_compare_behind", return_value=1),
            patch(
                "hermes_cli.config.recommended_update_command",
                return_value="hermes update",
            ),
        ):
            update_cmd._check_update_shallow(["git"], "main")

        assert ls_remote_urls == [upstream_url]
        out = capsys.readouterr().out
        assert "1 commit behind upstream/main" in out
