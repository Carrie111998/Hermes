"""Tests for _is_write_denied() — verifies deny list blocks sensitive paths on all platforms."""

import os

from pathlib import Path
from unittest.mock import patch

import pytest

from tools.file_operations import _is_write_denied


class TestWriteDenyExactPaths:
    def test_etc_shadow(self):
        assert _is_write_denied("/etc/shadow") is True


    def test_ssh_authorized_keys(self):
        assert _is_write_denied("~/.ssh/authorized_keys") is True


    def test_ssh_id_ed25519(self):
        path = os.path.join(str(Path.home()), ".ssh", "id_ed25519")
        assert _is_write_denied(path) is True


    def test_hermes_root_env_when_running_under_profile(self, tmp_path, monkeypatch):
        """Top-level ``<root>/.env`` stays write-denied even when running under
        a profile (#15981).

        Before the fix, ``build_write_denied_paths`` only added
        ``<active_profile>/.env`` to the deny list, so the global
        ``~/.hermes/.env`` (whose credentials are inherited by every profile)
        could be silently overwritten by ``write_file`` while a profile was
        active.
        """
        root = tmp_path / "hermes_root"
        profile_home = root / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        global_env = root / ".env"
        global_env.write_text("OPENAI_API_KEY=sk-real\n")

        monkeypatch.setenv("HERMES_HOME", str(profile_home))

        # Sanity check: HERMES_HOME does point to the profile dir, not the root.
        from hermes_constants import get_hermes_home, get_default_hermes_root
        assert get_hermes_home() == profile_home
        assert get_default_hermes_root() == root

        assert _is_write_denied(str(global_env)) is True

    def test_shell_profiles_are_writable(self):
        home = str(Path.home())
        for name in [".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"]:
            assert _is_write_denied(os.path.join(home, name)) is False, f"{name} should be writable"

    def test_credential_config_files_denied(self):
        home = str(Path.home())
        for name in [".netrc", ".pgpass", ".npmrc", ".pypirc"]:
            assert _is_write_denied(os.path.join(home, name)) is True, f"{name} should be denied"


class TestWriteDenyPrefixes:
    def test_ssh_prefix(self):
        path = os.path.join(str(Path.home()), ".ssh", "some_key")
        assert _is_write_denied(path) is True


    def test_systemd_prefix(self, tmp_path):
        # On NixOS, /etc/systemd is a symlink into /nix/store, so
        # realpath() resolves it to a store path that doesn't match
        # the /etc/systemd/ prefix.  Build a real directory tree so
        # realpath is a no-op and prefix matching works.
        fake_etc = tmp_path / "etc" / "systemd" / "system"
        fake_etc.mkdir(parents=True)
        target = str(fake_etc / "evil.service")
        # Patch the prefix builder to include our tmp_path prefix
        import agent.file_safety as _fs
        _orig = _fs.build_write_denied_prefixes
        _extra_prefix = str(tmp_path / "etc" / "systemd") + os.sep
        def _patched(home):
            return _orig(home) + [_extra_prefix]
        with patch.object(_fs, "build_write_denied_prefixes", _patched):
            assert _is_write_denied(target) is True


class TestWriteAllowed:
    def test_tmp_file(self):
        assert _is_write_denied("/tmp/safe_file.txt") is False


    def test_hermes_control_files_requested_writable(self):
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        for name in ["auth.json", "config.yaml", "webhook_subscriptions.json"]:
            assert _is_write_denied(str(home / name)) is False, f"{name} should be writable"


class TestGitWorktreePointerFileGuard:
    """Writes that touch a git worktree ``.git`` pointer FILE are refused.

    A linked worktree stores its ``gitdir: <path>`` link in a plain FILE
    named ``.git``.  write_file's temp-file + ``mv -f`` (and delete/move)
    would replace or sever that pointer, making the worktree vanish from
    ``git worktree list`` (#78565).  Normal repositories whose ``.git`` is
    a DIRECTORY must stay writable.
    """

    @pytest.fixture
    def ops(self, tmp_path: Path):
        from tools.environments.local import LocalEnvironment
        from tools.file_operations import ShellFileOperations
        env = LocalEnvironment(cwd=str(tmp_path))
        return ShellFileOperations(env, cwd=str(tmp_path))

    @pytest.fixture
    def worktree(self, tmp_path: Path) -> Path:
        """A fake linked worktree: <wt>/.git is a FILE pointing at a bare repo."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /srv/git/bare.git\n")
        return wt

    @pytest.fixture
    def normal_repo(self, tmp_path: Path) -> Path:
        """A normal repository: <repo>/.git is a DIRECTORY."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        return repo

    def test_denied_when_target_is_pointer_file(self, worktree: Path):
        from agent.file_safety import get_write_denied_error

        assert _is_write_denied(str(worktree / ".git")) is True
        err = get_write_denied_error(str(worktree / ".git"))
        assert err is not None
        assert "git worktree .git pointer file" in err
        assert "severs" in err
        assert "terminal or git" in err

    def test_denied_when_pointer_file_is_intermediate_component(self, worktree: Path):
        from agent.file_safety import get_write_denied_error

        target = worktree / ".git" / "refs" / "heads" / "main"
        assert _is_write_denied(str(target)) is True
        err = get_write_denied_error(str(target), verb="Patch")
        assert err is not None
        assert "git worktree .git pointer file" in err
        assert "severs" in err

    def test_denied_for_all_mutating_verbs(self, ops, worktree: Path):
        from agent.file_safety import get_write_denied_error

        pointer = worktree / ".git"
        # Write
        res = ops.write_file(str(pointer), "gitdir: /evil.git\n")
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error
        # The pointer file must be untouched.
        assert (worktree / ".git").read_text() == "gitdir: /srv/git/bare.git\n"
        # Patch
        res = ops.patch_replace(str(pointer), "gitdir", "gitdir")
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error
        # Delete
        res = ops.delete_file(str(pointer))
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error
        assert (worktree / ".git").exists()
        # Move (source side)
        res = ops.move_file(str(pointer), str(worktree / "moved"))
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error
        # Move (destination side)
        res = ops.move_file(str(worktree / "x.txt"), str(pointer))
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error
        # Verb-aware messages name the verb.
        err = get_write_denied_error(str(pointer), verb="Delete")
        assert err is not None and err.startswith("Delete denied")

    def test_denied_for_paths_under_pointer_file_via_tools(self, ops, worktree: Path):
        target = worktree / ".git" / "refs" / "heads" / "main"
        res = ops.write_file(str(target), "deadbeef\n")
        assert res.error is not None
        assert "git worktree .git pointer file" in res.error

    def test_normal_repo_with_git_directory_still_writable(self, ops, normal_repo: Path):
        from agent.file_safety import get_write_denied_error

        assert _is_write_denied(str(normal_repo / ".git")) is False
        assert _is_write_denied(str(normal_repo / ".git" / "config")) is False
        assert get_write_denied_error(str(normal_repo / ".git")) is None
        sibling = normal_repo / "notes.txt"
        res = ops.write_file(str(sibling), "hello\n")
        assert res.error is None
        assert sibling.read_text() == "hello\n"

    def test_sibling_file_in_worktree_still_writable(self, ops, worktree: Path):
        sibling = worktree / "notes.txt"
        res = ops.write_file(str(sibling), "hello\n")
        assert res.error is None
        assert sibling.read_text() == "hello\n"
