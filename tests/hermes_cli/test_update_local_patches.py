"""Tests for Atomic/local patch reapplication during ``hermes update``.

Local Hermes source customizations can be overwritten by ``git pull`` or the
hard-reset fallback in ``hermes update``.  A patch stack outside the repo keeps
those customizations durable across upstream version upgrades.
"""

from __future__ import annotations

import shlex
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main


def test_local_update_patches_dir_uses_profile_root(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes-root"
    profile_home = hermes_root / "profiles" / "worker"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert hermes_main._local_update_patches_dir() == (
        hermes_root / "local-patches" / "hermes-agent"
    )


def test_local_update_patches_are_applied_in_sorted_order(monkeypatch, tmp_path, capsys):
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)
    second = patches_dir / "002-second.patch"
    first = patches_dir / "001-first.patch"
    ignored = patches_dir / "README.md"
    second.write_text("second patch\n", encoding="utf-8")
    first.write_text("first patch\n", encoding="utf-8")
    ignored.write_text("ignore me\n", encoding="utf-8")

    calls = []

    def fake_dir():
        return patches_dir

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "--reverse" in cmd:
            return SimpleNamespace(stdout="", stderr="not applied\n", returncode=1)
        if "--diff-filter=U" in cmd:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", fake_dir)
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    applied = hermes_main._apply_local_update_patches(["git"], tmp_path)

    assert applied is True
    apply_calls = [
        call for call in calls
        if "--3way" in call[0] and "--check" not in call[0]
    ]
    assert [call[0][-1] for call in apply_calls] == [str(first), str(second)]
    assert all(call[0][:2] == ["git", "apply"] for call in apply_calls)
    assert "Applying local Hermes patches" in capsys.readouterr().out


def test_local_update_patches_skip_when_patch_already_applied(monkeypatch, tmp_path, capsys):
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)
    patch = patches_dir / "001-already.patch"
    patch.write_text("patch\n", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", lambda: patches_dir)
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    applied = hermes_main._apply_local_update_patches(["git"], tmp_path)

    assert applied is True
    assert any(
        call[0][1:5] == ["apply", "--cached", "--reverse", "--check"]
        and call[0][-1] == str(patch)
        for call in calls
    )
    assert not any("--3way" in call[0] for call in calls)
    assert "already applied" in capsys.readouterr().out


def test_overlapping_applied_stack_is_detected_in_reverse_order(
    monkeypatch, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    git("init", "-b", "main")
    git("config", "user.name", "Patch Test")
    git("config", "user.email", "patch-test@example.invalid")
    shared = repo / "shared.txt"
    added = repo / "added.txt"
    shared.write_text("base\n", encoding="utf-8")
    git("add", "shared.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD").stdout.strip()

    shared.write_text("one\n", encoding="utf-8")
    added.write_text("one\n", encoding="utf-8")
    git("add", "-A")
    first = patches_dir / "001-first.patch"
    first.write_text(
        git("diff", "--cached", "--binary", "--full-index").stdout,
        encoding="utf-8",
    )
    git("commit", "-m", "first")

    shared.write_text("two\n", encoding="utf-8")
    added.write_text("two\n", encoding="utf-8")
    git("add", "-A")
    second = patches_dir / "002-second.patch"
    second.write_text(
        git("diff", "--cached", "--binary", "--full-index").stdout,
        encoding="utf-8",
    )
    git("commit", "-m", "second")

    git("reset", "--hard", base)
    git("apply", str(first))
    git("apply", str(second))
    assert git("apply", "--reverse", "--check", str(first), check=False).returncode != 0
    before = git("status", "--short").stdout

    monkeypatch.setattr(
        hermes_main, "_local_update_patches_dir", lambda: patches_dir
    )
    assert hermes_main._apply_local_update_patches(["git"], repo) is True

    assert git("status", "--short").stdout == before
    assert shared.read_text(encoding="utf-8") == "two\n"
    assert added.read_text(encoding="utf-8") == "two\n"
    assert capsys.readouterr().out.count("already applied") == 2


def test_local_patch_apply_preserves_existing_git_index(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    git("init", "-b", "main")
    git("config", "user.name", "Patch Test")
    git("config", "user.email", "patch-test@example.invalid")
    patched = repo / "patched.txt"
    staged = repo / "staged.txt"
    patched.write_text("base\n", encoding="utf-8")
    staged.write_text("base\n", encoding="utf-8")
    git("add", "patched.txt", "staged.txt")
    git("commit", "-m", "base")

    patched.write_text("patched\n", encoding="utf-8")
    patch = patches_dir / "001-patched.patch"
    patch.write_text(
        git("diff", "--binary", "--full-index", "--", "patched.txt").stdout,
        encoding="utf-8",
    )
    git("restore", "patched.txt")

    staged.write_text("staged user change\n", encoding="utf-8")
    git("add", "staged.txt")
    cached_before = git("diff", "--cached", "--binary").stdout

    monkeypatch.setattr(
        hermes_main, "_local_update_patches_dir", lambda: patches_dir
    )
    assert hermes_main._apply_local_update_patches(["git"], repo) is True

    assert patched.read_text(encoding="utf-8") == "patched\n"
    assert git("diff", "--cached", "--binary").stdout == cached_before
    assert git("diff", "--name-only").stdout.splitlines() == ["patched.txt"]


def test_local_update_patches_noops_when_directory_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        hermes_main,
        "_local_update_patches_dir",
        lambda: tmp_path / "local-patches" / "hermes-agent",
    )

    applied = hermes_main._apply_local_update_patches(["git"], tmp_path)

    assert applied is False
    assert capsys.readouterr().out == ""


def test_local_update_patches_fail_closed_when_directory_unreadable(monkeypatch, tmp_path):
    class UnreadablePatchDir:
        def iterdir(self):
            raise PermissionError("permission denied")

    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", lambda: UnreadablePatchDir())

    with pytest.raises(hermes_main._LocalPatchApplyError):
        hermes_main._apply_local_update_patches(["git"], tmp_path)


def test_local_update_patches_can_be_disabled_by_env(monkeypatch, tmp_path, capsys):
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)
    (patches_dir / "001.patch").write_text("patch\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_SKIP_LOCAL_PATCHES", "1")
    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", lambda: patches_dir)

    applied = hermes_main._apply_local_update_patches(["git"], tmp_path)

    assert applied is False
    assert "skipped" in capsys.readouterr().out


def test_local_update_patch_failure_reports_manual_reapply(monkeypatch, tmp_path, capsys):
    repo_dir = tmp_path / "repo with space"
    repo_dir.mkdir()
    patches_dir = tmp_path / "local patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)
    patch = patches_dir / "001-conflict.patch"
    patch.write_text("patch\n", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "rev-parse" in cmd or "read-tree" in cmd:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "--reverse" in cmd:
            return SimpleNamespace(stdout="", stderr="not applied\n", returncode=1)
        if "--check" in cmd and "--3way" in cmd:
            return SimpleNamespace(stdout="", stderr="conflict\n", returncode=1)
        return SimpleNamespace(stdout="", stderr="unexpected apply\n", returncode=1)

    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", lambda: patches_dir)
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    with pytest.raises(hermes_main._LocalPatchApplyError):
        hermes_main._apply_local_update_patches(["git"], repo_dir)

    out = capsys.readouterr().out
    assert "failed" in out
    assert str(patch) in out
    assert "git apply --3way" in out
    assert f"cd {shlex.quote(str(repo_dir))}" in out
    assert shlex.quote(str(patch)) in out
    assert not any("--3way" in cmd and "--check" not in cmd for cmd in calls)


def test_local_update_patch_unmerged_state_aborts(monkeypatch, tmp_path):
    patches_dir = tmp_path / "local-patches" / "hermes-agent"
    patches_dir.mkdir(parents=True)
    patch = patches_dir / "001-conflict.patch"
    patch.write_text("patch\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if "--reverse" in cmd:
            return SimpleNamespace(stdout="", stderr="not applied\n", returncode=1)
        if "--3way" in cmd:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "--diff-filter=U" in cmd:
            return SimpleNamespace(stdout="agent/prompt_builder.py\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(hermes_main, "_local_update_patches_dir", lambda: patches_dir)
    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    with pytest.raises(hermes_main._LocalPatchApplyError):
        hermes_main._apply_local_update_patches(["git"], tmp_path)


def test_cmd_update_invokes_local_patch_reapply(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        calls.append(joined)
        if "rev-parse --abbrev-ref HEAD" in joined:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if "rev-list HEAD..origin/main --count" in joined:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if "rev-parse HEAD" in joined:
            return SimpleNamespace(stdout="deadbeef\n", stderr="", returncode=0)
        if "status --porcelain" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    invoked = []

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes_main.shutil, "which", lambda _name: None)
    monkeypatch.setattr(hermes_main, "_install_python_dependencies_with_optional_fallback", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *_a, **_k: True)
    monkeypatch.setattr(hermes_main, "_update_node_dependencies", lambda: None)
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *_a, **_k: None)
    monkeypatch.setattr(hermes_main, "_validate_critical_files_syntax", lambda _root: (True, None, None))
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda _root: 0)
    monkeypatch.setattr(hermes_main, "_apply_local_update_patches", lambda git_cmd, cwd: invoked.append((git_cmd, cwd)) or True)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: {"copied": [], "updated": []})
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: [])
    monkeypatch.setattr("hermes_cli.config.get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr("hermes_cli.config.get_missing_config_fields", lambda: [])
    monkeypatch.setattr("hermes_cli.config.check_config_version", lambda: (1, 1))

    hermes_main.cmd_update(SimpleNamespace(yes=True, check=False, gateway=False))

    assert invoked == [(["git"], hermes_main.PROJECT_ROOT)]


def test_cmd_update_stops_when_local_patch_reapply_fails(monkeypatch):
    def fake_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse --abbrev-ref HEAD" in joined:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if "rev-list HEAD..origin/main --count" in joined:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if "rev-parse HEAD" in joined:
            return SimpleNamespace(stdout="deadbeef\n", stderr="", returncode=0)
        if "status --porcelain" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    install_calls = []
    error_cls = getattr(hermes_main, "_LocalPatchApplyError", RuntimeError)

    def fail_local_patches(_git_cmd, _cwd):
        raise error_cls("local patch failed")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes_main.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        hermes_main,
        "_install_python_dependencies_with_optional_fallback",
        lambda *a, **k: install_calls.append((a, k)),
    )
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *_a, **_k: True)
    monkeypatch.setattr(hermes_main, "_update_node_dependencies", lambda: None)
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *_a, **_k: None)
    monkeypatch.setattr(hermes_main, "_validate_critical_files_syntax", lambda _root: (True, None, None))
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda _root: 0)
    monkeypatch.setattr(hermes_main, "_apply_local_update_patches", fail_local_patches)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(SimpleNamespace(yes=True, check=False, gateway=False))

    assert excinfo.value.code == 1
    assert install_calls == []
