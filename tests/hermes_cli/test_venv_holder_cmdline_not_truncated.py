"""Regression: venv-holder detection must not truncate cmdline before classification.

A gateway launched through the long ``.hermes-runtime`` interpreter path has
``gateway run`` sitting past character 120.  While
``_detect_venv_python_processes`` truncated to 120 chars at capture time, the
pausable-gateway matcher saw no subcommand, so the Desktop update preflight
reported that gateway as an *unpausable* blocker and aborted every update with
"another Hermes process is using this installation".
"""

from __future__ import annotations

import pytest

from hermes_cli import update_cmd
from hermes_cli._scan_venv_blockers import _is_pausable_gateway
from hermes_cli.update_cmd import _format_venv_python_holders_message

LONG_RUNTIME_GATEWAY = (
    r"C:\Users\someone\AppData\Local\hermes\hermes-agent\.hermes-runtime\python"
    r"\generation-1785635064-16968-cb2f7f1f\cpython-3.11-windows-x86_64-none"
    r"\python.exe -m hermes_cli.main gateway run"
)


def test_long_gateway_cmdline_is_pausable_only_when_untruncated():
    assert len(LONG_RUNTIME_GATEWAY) > 120, "fixture must exceed the old cut"
    assert _is_pausable_gateway(LONG_RUNTIME_GATEWAY) is True
    # The bug: the 120-char prefix loses `gateway run`.
    assert _is_pausable_gateway(LONG_RUNTIME_GATEWAY[:120]) is False


def test_detect_returns_untruncated_cmdline(monkeypatch):
    """The detector hands callers the full argv, not a 120-char prefix."""
    psutil = pytest.importorskip("psutil")
    if not update_cmd._m()._is_windows():
        pytest.skip("Windows-only detector")

    venv_python = str(update_cmd._m().PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    argv = [venv_python, "-m", "hermes_cli.main", "gateway", "run", "x" * 200]

    class FakeProc:
        info = {
            "pid": 999999,
            "exe": venv_python,
            "name": "python.exe",
            "cmdline": argv,
            "cwd": "",
        }

    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: [FakeProc()])

    matches = update_cmd._detect_venv_python_processes()
    assert matches, "fake venv holder should be detected"
    assert matches[0][2] == " ".join(argv)


def test_holder_message_still_truncates():
    msg = _format_venv_python_holders_message([(1, "python.exe", "z" * 500)])
    assert "z" * 120 in msg
    assert "z" * 121 not in msg
