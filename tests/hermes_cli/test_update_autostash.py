from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import config as hermes_config
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv(**kwargs):
        return shutil.which("uv")

    def _fake_ensure_uv(**kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**kwargs):
        return None  # never actually self-update in tests

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield

def _setup_update_mocks(monkeypatch, tmp_path):
    """Common setup for cmd_update tests."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_config, "get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr(hermes_config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(hermes_config, "check_config_version", lambda: (5, 5))
    monkeypatch.setattr(hermes_config, "migrate_config", lambda **kw: {"env_added": [], "config_added": []})
    monkeypatch.setattr(hermes_main, "_upgrade_pip_before_lazy_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *a, **kw: True)


def test_cmd_update_retries_optional_extras_individually_when_all_fails(monkeypatch, tmp_path, capsys):
    """When .[all] fails, update should keep base deps and retry extras individually."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(hermes_main, "_is_termux_env", lambda env=None: False)
    monkeypatch.setattr(hermes_main, "_load_installable_optional_extras", lambda group="all": ["matrix", "mcp"])

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd == [
            "git",
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "HEAD..origin/main", "--count"]:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if cmd == ["git", "pull", "--ff-only", "origin", "main"]:
            return SimpleNamespace(stdout="Updating\n", stderr="", returncode=0)
        if cmd == ["/usr/bin/uv", "pip", "install", "-e", ".[all]"]:
            raise CalledProcessError(returncode=1, cmd=cmd)
        if cmd == ["/usr/bin/uv", "pip", "install", "-e", "."]:
            return SimpleNamespace(returncode=0)
        if cmd == ["/usr/bin/uv", "pip", "install", "-e", ".[matrix]"]:
            raise CalledProcessError(returncode=1, cmd=cmd)
        if cmd == ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"]:
            return SimpleNamespace(returncode=0)
        # Catch-all must include stdout/stderr so consumers that parse
        # output (e.g. the dashboard-restart `ps -A` scan added in the
        # updater) don't crash on AttributeError.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main.cmd_update(SimpleNamespace())

    install_cmds = [c for c in recorded if "pip" in c and "install" in c]
    assert install_cmds == [
        ["/usr/bin/uv", "pip", "install", "-e", ".[all]"],
        ["/usr/bin/uv", "pip", "install", "-e", "."],
        ["/usr/bin/uv", "pip", "install", "-e", ".[matrix]"],
        ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"],
    ]

    out = capsys.readouterr().out
    assert "retrying extras individually" in out
    assert "Reinstalled optional extras individually: mcp" in out
    assert "Skipped optional extras that still failed: matrix" in out


def test_cmd_update_succeeds_with_extras(monkeypatch, tmp_path):
    """When .[all] succeeds, no fallback should be attempted."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(hermes_main, "_is_termux_env", lambda env=None: False)

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd == [
            "git",
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(stdout="main\n", stderr="", returncode=0)
        if cmd == ["git", "rev-list", "HEAD..origin/main", "--count"]:
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)
        if cmd == ["git", "pull", "--ff-only", "origin", "main"]:
            return SimpleNamespace(stdout="Updating\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main.cmd_update(SimpleNamespace())

    install_cmds = [c for c in recorded if "pip" in c and "install" in c]
    assert len(install_cmds) == 1
    assert ".[all]" in install_cmds[0]


def test_install_with_optional_fallback_honors_custom_group(monkeypatch):
    """Termux update path should target .[termux-all] when requested."""
    calls = []
    monkeypatch.setattr(
        hermes_main,
        "_load_installable_optional_extras",
        lambda group="all": ["termux", "mcp"] if group == "termux-all" else [],
    )

    def fake_run_with_heartbeat(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-1] == ".[termux-all]":
            raise CalledProcessError(returncode=1, cmd=cmd)
        return None

    monkeypatch.setattr(hermes_main, "_run_install_with_heartbeat", fake_run_with_heartbeat)

    hermes_main._install_python_dependencies_with_optional_fallback(
        ["/usr/bin/uv", "pip"],
        group="termux-all",
    )

    assert calls == [
        ["/usr/bin/uv", "pip", "install", "-e", ".[termux-all]"],
        ["/usr/bin/uv", "pip", "install", "-e", "."],
        ["/usr/bin/uv", "pip", "install", "-e", ".[termux]"],
        ["/usr/bin/uv", "pip", "install", "-e", ".[mcp]"],
    ]


def test_install_heartbeat_prints_when_dependency_install_is_silent(monkeypatch, capsys):
    """Long quiet installs should emit periodic heartbeat lines."""

    def fake_run(cmd, **kwargs):
        hermes_main._time.sleep(1.2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    hermes_main._run_install_with_heartbeat(
        ["uv", "pip", "install", "-e", "."],
        heartbeat_interval_seconds=1,
    )

    out = capsys.readouterr().out
    assert "still installing dependencies" in out


# ---------------------------------------------------------------------------
# ff-only fallback to reset --hard on diverged history
# ---------------------------------------------------------------------------

def _make_update_side_effect(
    current_branch="main",
    commit_count="3",
    ff_only_fails=False,
    reset_fails=False,
    fetch_fails=False,
    fetch_stderr="",
):
    """Build a subprocess.run side_effect for cmd_update tests."""
    recorded = []

    def side_effect(cmd, **kwargs):
        recorded.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "fetch" in joined and "origin" in joined:
            if fetch_fails:
                return SimpleNamespace(stdout="", stderr=fetch_stderr, returncode=128)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(stdout=f"{current_branch}\n", stderr="", returncode=0)
        if cmd[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(
                stdout="0123456789abcdef0123456789abcdef01234567\n",
                stderr="",
                returncode=0,
            )
        if "checkout" in joined and "main" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-list" in joined:
            return SimpleNamespace(stdout=f"{commit_count}\n", stderr="", returncode=0)
        if "--ff-only" in joined:
            if ff_only_fails:
                return SimpleNamespace(
                    stdout="",
                    stderr="fatal: Not possible to fast-forward, aborting.\n",
                    returncode=128,
                )
            return SimpleNamespace(stdout="Updating abc..def\n", stderr="", returncode=0)
        if "reset" in joined and "--hard" in joined:
            if reset_fails:
                return SimpleNamespace(stdout="", stderr="error: unable to write\n", returncode=1)
            return SimpleNamespace(stdout="HEAD is now at abc123\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect, recorded


def test_cmd_update_does_not_reset_when_ff_only_fails(monkeypatch, tmp_path, capsys):
    """A failed fast-forward preserves the checkout instead of hard-resetting it."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect(ff_only_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    reset_calls = [c for c in recorded if "reset" in c]
    assert reset_calls == []

    out = capsys.readouterr().out
    assert "did not reset the checkout" in out


def test_cmd_update_no_reset_when_ff_only_succeeds(monkeypatch, tmp_path):
    """When --ff-only succeeds, no reset is attempted."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    reset_calls = [c for c in recorded if "reset" in c]
    assert reset_calls == []


# ---------------------------------------------------------------------------
# Non-main branch → auto-checkout main
# ---------------------------------------------------------------------------

def test_cmd_update_switches_to_main_from_feature_branch(monkeypatch, tmp_path, capsys):
    """When on a feature branch, update checks out main before pulling."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect(current_branch="fix/something")
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    checkout_calls = [c for c in recorded if "checkout" in c and "main" in c]
    assert len(checkout_calls) == 1

    out = capsys.readouterr().out
    assert "fix/something" in out
    assert "switching to main" in out


def test_cmd_update_switches_to_main_from_detached_head(monkeypatch, tmp_path, capsys):
    """When in detached HEAD state, update checks out main before pulling."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect(current_branch="HEAD")
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    checkout_calls = [c for c in recorded if "checkout" in c and "main" in c]
    assert len(checkout_calls) == 1

    out = capsys.readouterr().out
    assert "detached HEAD" in out


def test_cmd_update_restores_branch_when_already_up_to_date(monkeypatch, tmp_path, capsys):
    """A clean feature branch is restored after checking an up-to-date main."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)


    side_effect, recorded = _make_update_side_effect(
        current_branch="fix/something", commit_count="0",
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    # Should have checked out back to the original branch
    checkout_back = [c for c in recorded if "checkout" in c and "fix/something" in c]
    assert len(checkout_back) == 1

    out = capsys.readouterr().out
    assert "Already up to date" in out


def test_cmd_update_no_checkout_when_already_on_main(monkeypatch, tmp_path):
    """When already on main, no checkout is needed."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    checkout_calls = [c for c in recorded if "checkout" in c]
    assert len(checkout_calls) == 0


def test_cmd_update_fetch_is_scoped_to_target_branch(monkeypatch, tmp_path):
    """The update fetch must name the target branch. A bare `git fetch origin`
    pulls every ref, and this repo has thousands of auto-generated branches, so
    an unscoped fetch can stall for minutes on a non-single-branch checkout."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace())

    fetch_calls = [c for c in recorded if "fetch" in c]
    assert fetch_calls == [
        [
            "git",
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ]
    ]
    assert ["git", "fetch", "origin"] not in recorded


# ---------------------------------------------------------------------------
# Fetch failure — friendly error messages
# ---------------------------------------------------------------------------

def test_cmd_update_network_error_shows_friendly_message(monkeypatch, tmp_path, capsys):
    """Network failures during fetch show a user-friendly message."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, _ = _make_update_side_effect(
        fetch_fails=True,
        fetch_stderr="fatal: unable to access 'https://...': Could not resolve host: github.com",
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    out = capsys.readouterr().out
    assert "Network error" in out


def test_cmd_update_auth_error_shows_friendly_message(monkeypatch, tmp_path, capsys):
    """Auth failures during fetch show a user-friendly message."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, _ = _make_update_side_effect(
        fetch_fails=True,
        fetch_stderr="fatal: Authentication failed for 'https://...'",
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    out = capsys.readouterr().out
    assert "Authentication failed" in out
