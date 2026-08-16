"""The secret-helper child envs must keep the Windows OS path vars.

Both ``run_secret_cli`` (shared by every subprocess-driven backend) and
``onepassword._op_child_env`` build the child environment from an
**allowlist** rather than by scrubbing a copy of ``os.environ``.  That is
the right posture for credentials, but an allowlist that omits
``SYSTEMDRIVE`` / ``PROGRAMDATA`` leaves a Windows child unable to expand
the ``REG_EXPAND_SZ`` known-folder template ``%SystemDrive%\\ProgramData``
— at which point it creates a *literal* ``%SystemDrive%`` directory tree
under whatever CWD it happened to inherit.

These tests are written to pass on POSIX too: the vars are injected with
``monkeypatch.setenv`` rather than assumed present, so what is under test
is the allowlist, not the host OS.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import base as sb  # noqa: E402
from agent.secret_sources import onepassword as op  # noqa: E402


# The vars whose absence produces the literal-%SystemDrive% failure mode,
# plus the two the CRT needs to resolve a command at all.
_REQUIRED = ("SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE", "COMSPEC", "PATHEXT")

_SAMPLE = {
    "SYSTEMDRIVE": "C:",
    "PROGRAMDATA": r"C:\ProgramData",
    "ALLUSERSPROFILE": r"C:\ProgramData",
    "COMSPEC": r"C:\Windows\system32\cmd.exe",
    "PATHEXT": ".COM;.EXE;.BAT",
}


@pytest.fixture
def _windows_env(monkeypatch):
    for key, value in _SAMPLE.items():
        monkeypatch.setenv(key, value)
    return _SAMPLE


def test_run_secret_cli_child_keeps_windows_path_vars(monkeypatch, _windows_env):
    """``run_secret_cli`` must not strip the OS path/identity vars."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.run_secret_cli(["helper", "read", "--", "ref"])

    env = captured["env"]
    missing = [k for k in _REQUIRED if k not in env]
    assert not missing, f"run_secret_cli dropped {missing} from the child env"
    for key in _REQUIRED:
        assert env[key] == _SAMPLE[key]


def test_run_secret_cli_still_withholds_unrelated_credentials(
    monkeypatch, _windows_env
):
    """Widening the allowlist must not turn it into an os.environ copy."""
    monkeypatch.setenv("OPENAI_API_KEY", "leak-me")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "nope")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.run_secret_cli(["helper"])

    env = captured["env"]
    assert "OPENAI_API_KEY" not in env
    assert "SOME_UNRELATED_VAR" not in env


def test_op_child_env_keeps_windows_path_vars(_windows_env):
    """``op``'s allowlisted child env must keep the OS path/identity vars."""
    env = op._op_child_env("tok")
    missing = [k for k in _REQUIRED if k not in env]
    assert not missing, f"_op_child_env dropped {missing} from the child env"
    for key in _REQUIRED:
        assert env[key] == _SAMPLE[key]


def test_op_child_env_still_withholds_unrelated_credentials(
    monkeypatch, _windows_env
):
    monkeypatch.setenv("OPENAI_API_KEY", "leak-me")
    env = op._op_child_env("tok")
    assert "OPENAI_API_KEY" not in env
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "tok"
