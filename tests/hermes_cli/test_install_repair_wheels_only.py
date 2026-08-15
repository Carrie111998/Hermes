"""The unattended install-repair path must never build a registry sdist.

`_install_repair` fires on plain module import when a previous `hermes update`
was interrupted, and on server installs it runs as root.  A source distribution
runs its PEP 517 backend (or legacy `setup.py`) at install time, so resolving
one sdist is arbitrary code execution from a maintainer account we do not
control.  `--only-binary=:all:` removes that step; pinning does not, because
pinning constrains which artifact is fetched, not whether its hooks run.

These tests pin the property at the chokepoint every install in that module
routes through, so a new call site cannot miss it.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import _install_repair as R

FLAG = "--only-binary=:all:"


@pytest.fixture
def seen(monkeypatch, tmp_path):
    """Capture the argv actually spawned, running nothing."""
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, check, env):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    monkeypatch.setattr(R, "_is_windows", lambda: False)
    return calls


def test_editable_install_is_forced_to_wheels_only(seen, tmp_path):
    R._run_install_cmd(["uv", "pip", "install", "-e", "."], env=None, root=tmp_path)
    assert FLAG in seen[0]


def test_extras_install_is_forced_to_wheels_only(seen, tmp_path):
    R._run_install_cmd(["uv", "pip", "install", "-e", ".[all]"], env=None, root=tmp_path)
    assert FLAG in seen[0]


def test_flag_lands_immediately_after_the_install_verb(seen, tmp_path):
    """Position matters: it must be an option to `install`, not a stray token."""
    R._run_install_cmd(["uv", "pip", "install", "-e", ".[all]"], env=None, root=tmp_path)
    cmd = seen[0]
    assert cmd[cmd.index("install") + 1] == FLAG


def test_not_applied_twice_when_caller_already_passed_it(seen, tmp_path):
    """Callers may harden their own argv; that must not produce a duplicate."""
    R._run_install_cmd(
        ["uv", "pip", "install", FLAG, "-e", "."], env=None, root=tmp_path
    )
    assert seen[0].count(FLAG) == 1


def test_non_install_commands_are_left_alone(seen, tmp_path):
    """The flag is meaningless outside `install` and must not be injected."""
    R._run_install_cmd(["uv", "pip", "list"], env=None, root=tmp_path)
    assert FLAG not in seen[0]


def test_caller_arguments_survive_injection(seen, tmp_path):
    original = ["uv", "pip", "install", "--no-build-isolation", "-e", ".[all]"]
    R._run_install_cmd(list(original), env=None, root=tmp_path)
    for token in original:
        assert token in seen[0]
