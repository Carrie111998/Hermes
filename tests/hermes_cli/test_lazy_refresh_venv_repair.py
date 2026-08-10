"""Tests for lazy-backend refresh venv repair (#57828 / #58004)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_cli.main as m
import pytest






def test_package_only_install_detects_interpreter_flags_before_pip_marker(
    monkeypatch,
):
    floor_prefixes: list[list[str]] = []
    install_commands: list[list[str]] = []

    monkeypatch.setattr(
        m,
        "_ensure_direct_pip_floor",
        lambda prefix, *, env=None: floor_prefixes.append(prefix),
    )
    monkeypatch.setattr(
        m,
        "_run_install_with_heartbeat",
        lambda cmd, *, env=None: install_commands.append(cmd),
    )

    command = ["python", "-E", "-m", "pip", "install", "pkg==1.0"]
    m._run_package_only_install(command)

    assert floor_prefixes == [["python", "-E", "-m", "pip"]]
    assert install_commands == [command]


def test_direct_pip_prefix_marker_extracts_full_prefix():
    assert m._direct_pip_prefix(["python", "-E", "-m", "pip", "install"]) == [
        "python",
        "-E",
        "-m",
        "pip",
    ]
    assert m._direct_pip_prefix(["uv", "pip", "install"]) is None


def test_direct_pip_floor_preserves_interpreter_flags_for_ensurepip(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "missing"})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(
        m._pip_security,
        "ensure_pip_floor",
        lambda *args, **kwargs: (True, ""),
    )

    m._ensure_direct_pip_floor(["python", "-E", "-m", "pip"])

    assert calls[1] == [
        "python",
        "-E",
        "-m",
        "ensurepip",
        "--upgrade",
        "--default-pip",
    ]


def test_early_repair_bounds_ensurepip_and_force_reinstall(monkeypatch, tmp_path):
    import hermes_cli._early_recovery as early_recovery

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(early_recovery.subprocess, "run", fake_run)
    monkeypatch.setattr(
        early_recovery._pip_security,
        "ensure_pip_floor",
        lambda *args, **kwargs: (True, ""),
    )

    assert early_recovery._run_repair_install(["PyYAML==6.0.3"], tmp_path) is True

    ensure_call = next(call for call in calls if "ensurepip" in call[0])
    repair_call = next(call for call in calls if "--force-reinstall" in call[0])
    assert ensure_call[1]["timeout"] == 120
    assert repair_call[1]["timeout"] == 600


def test_detect_returns_none_when_probe_subprocess_fails(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        m, "_resolve_install_target_python", lambda *a, **k: python
    )
    monkeypatch.setattr(
        m.subprocess,
        "run",
        MagicMock(side_effect=OSError("exec failed")),
    )
    assert m._detect_broken_lazy_refresh_imports(["uv", "pip"]) is None




def test_repair_runs_force_reinstall_with_pyproject_pins(
    tmp_path, monkeypatch
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent(
            """\
            [project]
            name = "fake"
            version = "0.0.0"
            dependencies = [
              "pyyaml==6.0.3",
              "click==8.2.1",
            ]
        """
        )
    )
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)

    calls: list[list[str]] = []

    def fake_install(cmd, **kwargs):
        calls.append(cmd)

    detect_calls = {"count": 0}

    def fake_detect(prefix, *, env=None):
        detect_calls["count"] += 1
        return []

    monkeypatch.setattr(m, "_run_package_only_install", fake_install)
    monkeypatch.setattr(m, "_detect_broken_lazy_refresh_imports", fake_detect)

    ok = m._repair_broken_lazy_refresh_imports(
        ["uv", "pip"],
        ["PyYAML", "click"],
        env={"VIRTUAL_ENV": str(tmp_path)},
    )
    assert ok is True
    assert calls == [
        [
            "uv",
            "pip",
            "install",
            "--force-reinstall",
            "pyyaml==6.0.3",
            "click==8.2.1",
        ]
    ]
    assert detect_calls["count"] == 1


def test_repair_handles_pip_floor_failure_without_raising(monkeypatch):
    """A floor refusal is a failed repair, not an uncaught update traceback."""
    monkeypatch.setattr(
        m,
        "_run_package_only_install",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            m._pip_security.PipFloorError("pip floor unavailable")
        ),
    )
    monkeypatch.setattr(
        m,
        "_lazy_refresh_repair_specs",
        lambda _packages: ["pyyaml==6.0.3"],
    )

    assert m._repair_broken_lazy_refresh_imports(
        ["python", "-m", "pip"], ["PyYAML"]
    ) is False


def test_refresh_repairs_venv_after_lazy_failure(tmp_path, monkeypatch, capsys):
    import tools.lazy_deps as lazy_deps_mod

    monkeypatch.setattr(lazy_deps_mod, "active_features", lambda: ["platform.matrix"])
    monkeypatch.setattr(
        lazy_deps_mod,
        "refresh_active_features",
        lambda **kw: {"platform.matrix": "failed: pip install failed"},
    )

    repair_calls: list[list[str]] = []

    def fake_repair(prefix, packages, *, env=None):
        repair_calls.append(packages)
        return True

    monkeypatch.setattr(m, "_detect_broken_lazy_refresh_imports", lambda *a, **k: ["PyYAML"])
    monkeypatch.setattr(m, "_repair_broken_lazy_refresh_imports", fake_repair)

    ok = m._refresh_active_lazy_features(["uv", "pip"], env={"VIRTUAL_ENV": str(tmp_path)})
    out = capsys.readouterr().out

    assert ok is True
    assert repair_calls == [["PyYAML"]]
    assert "Venv repair succeeded" in out
    assert "import probes" in out
    assert "Backends keep their previously-installed version" not in out


def test_refresh_uses_pre_rebuild_snapshot_when_provided(monkeypatch):
    """Replacement runtimes must not re-detect features after packages vanish."""
    import tools.lazy_deps as lazy_deps_mod

    monkeypatch.setattr(
        lazy_deps_mod,
        "active_features",
        lambda: pytest.fail("post-rebuild detection must not run"),
    )
    restored = []
    monkeypatch.setattr(
        lazy_deps_mod,
        "restore_features",
        lambda features: restored.append(features) or {"platform.telegram": "restored"},
    )

    assert m._refresh_active_lazy_features(
        ["uv", "pip"], features=["platform.telegram"]
    ) is True
    assert restored == [["platform.telegram"]]


def test_capture_active_tool_dependencies_uses_tools_status_probes(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(
        tools_config,
        "_module_installed",
        lambda module: module in {"langfuse", "ddgs"},
    )

    assert m._capture_active_tool_dependencies() == ["ddgs", "langfuse"]


def test_restore_active_tool_dependencies_uses_static_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        m,
        "_run_package_only_install",
        lambda cmd, *, env=None: calls.append((cmd, env)),
    )

    env = {"VIRTUAL_ENV": "/tmp/venv"}
    m._restore_active_tool_dependencies(
        ["langfuse", "not-allowlisted"],
        ["uv", "pip"],
        env=env,
    )

    assert calls == [(["uv", "pip", "install", "langfuse", "--quiet"], env)]


def test_cmd_update_captures_and_propagates_pre_rebuild_snapshot(
    tmp_path, monkeypatch
):
    """The updater must carry pre-rebuild state into its repair refresh."""
    from hermes_cli import managed_uv, update_cmd

    (tmp_path / ".git").mkdir()
    snapshot = ["platform.telegram"]
    tool_snapshot = ["langfuse"]
    refresh_calls = []
    restore_calls = []

    class RestoreReached(Exception):
        pass

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_refresh(prefix, *, env=None, features=None):
        refresh_calls.append((prefix, env, features))
        return True

    def fake_restore(dependencies, prefix, *, env=None):
        restore_calls.append((dependencies, prefix, env))
        raise RestoreReached

    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(m, "_capture_active_lazy_features", lambda: snapshot.copy())
    monkeypatch.setattr(
        m, "_capture_active_tool_dependencies", lambda: tool_snapshot.copy()
    )
    monkeypatch.setattr(m, "_is_windows", lambda: False)
    monkeypatch.setattr(m, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(m, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(m, "_resume_windows_gateways_after_update", lambda state: None)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *args: None)
    monkeypatch.setattr(m, "_get_origin_url", lambda *args: "https://github.com/NousResearch/hermes-agent.git")
    monkeypatch.setattr(m, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(m, "_stash_local_changes_if_needed", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (False, "broken")
    )
    monkeypatch.setattr(update_cmd, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        m, "_install_python_dependencies_with_optional_fallback", lambda *a, **k: None
    )
    monkeypatch.setattr(m, "_refresh_active_lazy_features", fake_refresh)
    monkeypatch.setattr(m, "_restore_active_tool_dependencies", fake_restore)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(managed_uv, "update_managed_uv", lambda **kwargs: None)
    monkeypatch.setattr(managed_uv, "ensure_uv", lambda **kwargs: "uv")

    args = SimpleNamespace(
        yes=True,
        force=False,
        force_venv=False,
        no_backup=True,
        backup=False,
        branch=None,
    )
    with pytest.raises(RestoreReached):
        m._cmd_update_impl(args, gateway_mode=False)

    # The repair env is now built via managed_python_env (#83914): third-party
    # UV vars are stripped, managed pins set, then VIRTUAL_ENV re-pointed at
    # the install's venv. Assert the CONTRACT, not the raw environ copy.
    from hermes_cli.managed_uv import managed_python_env

    expected_env = managed_python_env()
    expected_env["VIRTUAL_ENV"] = str(tmp_path / "venv")
    assert refresh_calls == [
        (
            ["uv", "pip"],
            expected_env,
            snapshot,
        )
    ]
    assert restore_calls == [
        (
            tool_snapshot,
            ["uv", "pip"],
            expected_env,
        )
    ]






