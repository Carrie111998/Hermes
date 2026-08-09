"""Security-focused integration tests for CLI worktree setup."""

import subprocess
from pathlib import Path

import pytest


def _can_symlink():
    """Check if we can create symlinks (needs admin/dev-mode on Windows)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            src.write_text("x")
            lnk = Path(d) / "lnk"
            lnk.symlink_to(src)
            return True
    except OSError:
        return False


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing real cli._setup_worktree behavior."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


def _force_remove_worktree(info: dict | None) -> None:
    if not info:
        return
    subprocess.run(
        ["git", "worktree", "remove", info["path"], "--force"],
        cwd=info["repo_root"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "branch", "-D", info["branch"]],
        cwd=info["repo_root"],
        capture_output=True,
        check=False,
    )


class TestWorktreeIncludeSecurity:
    def test_rejects_parent_directory_file_traversal(self, git_repo):
        import cli as cli_mod

        outside_file = git_repo.parent / "sensitive.txt"
        outside_file.write_text("SENSITIVE DATA")
        (git_repo / ".worktreeinclude").write_text("../sensitive.txt\n")

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None

            wt_path = Path(info["path"])
            assert not (wt_path.parent / "sensitive.txt").exists()
            assert not (wt_path / "../sensitive.txt").resolve().exists()
        finally:
            _force_remove_worktree(info)

    def test_rejects_parent_directory_directory_traversal(self, git_repo):
        import cli as cli_mod

        outside_dir = git_repo.parent / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("SENSITIVE DIR DATA")
        (git_repo / ".worktreeinclude").write_text("../outside-dir\n")

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None

            wt_path = Path(info["path"])
            escaped_dir = wt_path.parent / "outside-dir"
            assert not escaped_dir.exists()
            assert not escaped_dir.is_symlink()
        finally:
            _force_remove_worktree(info)

    @pytest.mark.skipif(not _can_symlink(), reason="Symlinks need elevated privileges")
    def test_rejects_symlink_that_resolves_outside_repo(self, git_repo):
        import cli as cli_mod

        outside_file = git_repo.parent / "linked-secret.txt"
        outside_file.write_text("LINKED SECRET")
        (git_repo / "leak.txt").symlink_to(outside_file)
        (git_repo / ".worktreeinclude").write_text("leak.txt\n")

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None

            assert not (Path(info["path"]) / "leak.txt").exists()
        finally:
            _force_remove_worktree(info)

    def test_allows_valid_file_include(self, git_repo):
        import cli as cli_mod

        (git_repo / ".env").write_text("SECRET=***\n")
        (git_repo / ".worktreeinclude").write_text(".env\n")

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None

            copied = Path(info["path"]) / ".env"
            assert copied.exists()
            assert copied.read_text() == "SECRET=***\n"
        finally:
            _force_remove_worktree(info)

    @pytest.mark.skipif(not _can_symlink(), reason="Symlinks need elevated privileges")
    def test_allows_valid_directory_include(self, git_repo):
        import cli as cli_mod

        assets_dir = git_repo / ".venv" / "lib"
        assets_dir.mkdir(parents=True)
        (assets_dir / "marker.txt").write_text("venv marker")
        (git_repo / ".worktreeinclude").write_text(".venv\n")

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None

            linked_dir = Path(info["path"]) / ".venv"
            assert linked_dir.is_symlink()
            assert (linked_dir / "lib" / "marker.txt").read_text() == "venv marker"
        finally:
            _force_remove_worktree(info)


class TestWorktreeIncludeEncoding:
    """The include list and .gitignore are UTF-8 files; reading them with the
    locale default breaks Windows (cp1251/GBK mojibake or UnicodeDecodeError,
    swallowed at DEBUG so no include is copied), and a Notepad BOM glues to
    the first line on every platform."""


    def test_non_ascii_worktreeinclude_entry_copied(self, git_repo):
        import cli as cli_mod

        secret = git_repo / "секреты.env"
        secret.write_text("SECRET=***\n", encoding="utf-8")
        (git_repo / ".worktreeinclude").write_bytes(
            "# ключи агента\nсекреты.env\n".encode("utf-8")
        )

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo))
            assert info is not None
            assert (Path(info["path"]) / "секреты.env").exists()
        finally:
            _force_remove_worktree(info)


class TestUvWorktreeProvisioning:
    def test_syncs_declared_dev_and_test_extras_and_groups(self, tmp_path, monkeypatch):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "demo"
version = "0.1.0"
[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]
test = ["pytest-cov"]
docs = ["mkdocs"]
[dependency-groups]
dev = ["mypy", "pydantic"]
test = ["coverage"]
docs = ["sphinx"]
[tool.uv]
default-groups = ["docs"]
"""
        )
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "external-venv"))
        readiness = iter([False, True])
        monkeypatch.setattr(
            cli_mod,
            "_worktree_test_environment_ready",
            lambda _path: next(readiness),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is True
        assert captured["command"] == [
            "/usr/bin/uv",
            "sync",
            "--locked",
            "--no-default-groups",
            "--extra",
            "dev",
            "--extra",
            "test",
            "--group",
            "dev",
            "--group",
            "test",
        ]
        assert captured["kwargs"]["cwd"] == tmp_path
        assert captured["kwargs"]["env"]["UV_PROJECT_ENVIRONMENT"] == str(
            tmp_path / ".venv"
        )

    @pytest.mark.parametrize("existing", [".venv", "venv"])
    def test_preserves_existing_environment(self, tmp_path, monkeypatch, existing):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        environment = tmp_path / existing
        (environment / "bin").mkdir(parents=True)
        python = environment / "bin" / "python"
        python.touch()
        calls = []
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **_kwargs: calls.append(command)
            or subprocess.CompletedProcess(command, 0, "", ""),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert calls == [[str(python), "-c", "import pytest"]]

    def test_preserves_ready_windows_environment(self, tmp_path, monkeypatch):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        python = tmp_path / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        calls = []
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **_kwargs: calls.append(command)
            or subprocess.CompletedProcess(command, 0, "", ""),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert calls == [[str(python), "-c", "import pytest"]]

    def test_partial_venv_does_not_suppress_retry(self, tmp_path, monkeypatch):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        (tmp_path / ".venv").mkdir()
        calls = []
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        readiness = iter([False, True])
        monkeypatch.setattr(
            cli_mod,
            "_worktree_test_environment_ready",
            lambda _path: next(readiness),
        )
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **_kwargs: calls.append(command)
            or subprocess.CompletedProcess(command, 0, "", ""),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is True
        assert calls == [["/usr/bin/uv", "sync", "--locked", "--no-default-groups"]]

    def test_unusable_launcher_files_do_not_suppress_retry(self, tmp_path, monkeypatch):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        bin_dir = tmp_path / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").touch()
        (bin_dir / "pytest").touch()
        sync_calls = []
        synced = False

        def fake_run(command, **_kwargs):
            nonlocal synced
            if command[0] == str(bin_dir / "python"):
                if not synced:
                    raise OSError("incomplete launcher")
                return subprocess.CompletedProcess(command, 0, "", "")
            sync_calls.append(command)
            synced = True
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)

        assert cli_mod._provision_uv_worktree(tmp_path) is True
        assert sync_calls == [
            ["/usr/bin/uv", "sync", "--locked", "--no-default-groups"]
        ]

    def test_successful_sync_without_importable_pytest_is_not_success(
        self, tmp_path, monkeypatch, caplog
    ):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert "cannot import pytest" in caplog.text

    def test_failed_sync_with_unusable_launchers_retries_next_call(self, tmp_path, monkeypatch):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        python = tmp_path / ".venv" / "bin" / "python"
        sync_calls = []

        def fake_run(command, **_kwargs):
            if command[0] == str(python):
                raise OSError("incomplete launcher")
            sync_calls.append(command)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            (python.parent / "pytest").touch()
            return subprocess.CompletedProcess(command, 1, "", "offline")

        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert sync_calls == [
            ["/usr/bin/uv", "sync", "--locked", "--no-default-groups"],
            ["/usr/bin/uv", "sync", "--locked", "--no-default-groups"],
        ]

    def test_missing_uv_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert "uv is unavailable" in caplog.text

    @pytest.mark.parametrize(
        "pyproject",
        ["[project", 'project = "not-a-table"\n'],
    )
    def test_malformed_pyproject_is_non_fatal(self, tmp_path, monkeypatch, caplog, pyproject):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text(pyproject)
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert "uv worktree provisioning failed" in caplog.text

    def test_sync_failure_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "offline"),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert "uv worktree provisioning failed" in caplog.text

    def test_sync_timeout_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        import cli as cli_mod

        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n")
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(
            cli_mod.subprocess,
            "run",
            lambda command, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(command, kwargs["timeout"])
            ),
        )

        assert cli_mod._provision_uv_worktree(tmp_path) is False
        assert "uv worktree provisioning failed" in caplog.text

    def test_setup_calls_provisioner_with_new_worktree(self, git_repo, monkeypatch):
        import cli as cli_mod

        provisioned = []
        monkeypatch.setattr(
            cli_mod,
            "_provision_uv_worktree",
            lambda path: provisioned.append(path) or True,
        )

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo), sync_base=False)
            assert info is not None
            assert provisioned == [Path(info["path"])]
        finally:
            _force_remove_worktree(info)

    def test_setup_contains_unexpected_provisioner_exception(
        self, git_repo, monkeypatch, caplog
    ):
        import cli as cli_mod

        monkeypatch.setattr(
            cli_mod,
            "_provision_uv_worktree",
            lambda _path: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )

        info = None
        try:
            info = cli_mod._setup_worktree(str(git_repo), sync_base=False)
            assert info is not None
            assert "Unexpected uv worktree provisioning failure" in caplog.text
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=git_repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            section = listing.split(f"worktree {info['path']}", 1)[1]
            assert "\nlocked hermes pid=" in section
            subprocess.run(
                ["git", "worktree", "unlock", info["path"]],
                cwd=git_repo,
                check=True,
                capture_output=True,
            )
        finally:
            _force_remove_worktree(info)

