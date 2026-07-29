"""Tests for hermes_cli/_scan_venv_blockers.py.

Tests call the real production functions (``main``, ``_redact_sensitive_cmdline``).
The detector is patched directly so no real process table interaction occurs.
"""

from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.redact as redact_module
from hermes_cli._scan_venv_blockers import (
    _gateway_managed_holder_pids,
    _redact_sensitive_cmdline,
    main,
)


# ---------------------------------------------------------------------------
# main() — stdout, stderr, exit code (with patched detector)
# ---------------------------------------------------------------------------


def _psutil_fake() -> dict:
    """Return a sys.modules dict entry that makes psutil appear available."""
    return {"psutil": types.SimpleNamespace(Process=lambda *a: MagicMock())}


def test_main_no_holders_prints_clear_json(tmp_path: Path, capsys) -> None:
    from hermes_cli import main as cli_main

    fake_detect = MagicMock(return_value=[])
    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.object(cli_main, "_detect_venv_python_processes", fake_detect), patch.dict(
        sys.modules, _psutil_fake()
    ):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "ok": True,
        "schema_version": 2,
        "blocked": False,
        "processes": [],
        "updater_managed_processes": [],
    }


def test_main_holders_prints_blocked_json(tmp_path: Path, capsys) -> None:
    from hermes_cli import main as cli_main

    fake_detect = MagicMock(
        return_value=[(101, "python.exe", "python.exe -m hermes_cli.main serve --host 10.0.0.1")]
    )
    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.object(cli_main, "_detect_venv_python_processes", fake_detect), patch(
        "hermes_cli._scan_venv_blockers._gateway_managed_holder_pids", return_value=set()
    ), patch.dict(
        sys.modules, _psutil_fake()
    ):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert data["blocked"] is True
    assert len(data["processes"]) == 1
    p = data["processes"][0]
    assert p["pid"] == 101
    assert p["name"] == "python.exe"
    assert "serve" in p["cmdline"]
    assert data["updater_managed_processes"] == []


def test_main_gateway_tree_is_deferred_to_official_updater(tmp_path: Path, capsys) -> None:
    from hermes_cli import main as cli_main

    matches = [
        (101, "python.exe", "python.exe -m hermes_cli.main gateway run --replace"),
        (202, "python.exe", "runtime-python gateway child"),
    ]
    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.object(cli_main, "_detect_venv_python_processes", return_value=matches), patch(
        "hermes_cli._scan_venv_blockers._gateway_managed_holder_pids", return_value={101, 202}
    ), patch.dict(sys.modules, _psutil_fake()):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["blocked"] is False
    assert data["processes"] == []
    assert [entry["pid"] for entry in data["updater_managed_processes"]] == [101, 202]


def test_main_keeps_unrelated_venv_holder_blocked_when_gateway_is_deferred(
    tmp_path: Path, capsys
) -> None:
    from hermes_cli import main as cli_main

    matches = [
        (101, "python.exe", "python.exe -m hermes_cli.main gateway run --replace"),
        (303, "python.exe", "python.exe -m hermes_cli.main serve"),
    ]
    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.object(cli_main, "_detect_venv_python_processes", return_value=matches), patch(
        "hermes_cli._scan_venv_blockers._gateway_managed_holder_pids", return_value={101}
    ), patch.dict(sys.modules, _psutil_fake()):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["blocked"] is True
    assert [entry["pid"] for entry in data["processes"]] == [303]
    assert [entry["pid"] for entry in data["updater_managed_processes"]] == [101]


def test_main_detector_exception_exits_nonzero(tmp_path: Path, capsys) -> None:
    from hermes_cli import main as cli_main

    with patch.object(
        cli_main, "_detect_venv_python_processes", side_effect=RuntimeError("boom")
    ), patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.dict(sys.modules, _psutil_fake()):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code != 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "ok": False,
        "schema_version": 2,
        "blocked": False,
        "processes": [],
        "updater_managed_processes": [],
    }
    assert "boom" in captured.err


def test_main_psutil_unavailable_exits_nonzero(tmp_path: Path, capsys) -> None:
    from hermes_cli import main as cli_main

    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.dict(sys.modules, {"psutil": None}):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code != 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "ok": False,
        "schema_version": 2,
        "blocked": False,
        "processes": [],
        "updater_managed_processes": [],
    }


def test_main_import_hermes_cli_main_fails(tmp_path: Path, capsys) -> None:
    """When the import of hermes_cli.main raises, main() must produce one
    parseable ok=false JSON on stdout, the diagnostic on stderr, and exit
    non-zero."""
    from hermes_cli import main as cli_main

    real_import = builtins.__import__

    def selective_import(name, *args, **kwargs):
        if name == "hermes_cli.main":
            raise ImportError("detector import failed")
        return real_import(name, *args, **kwargs)

    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "PROJECT_ROOT", tmp_path
    ), patch.dict(sys.modules, _psutil_fake()), patch.object(
        builtins, "__import__", selective_import
    ):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code != 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "ok": False,
        "schema_version": 2,
        "blocked": False,
        "processes": [],
        "updater_managed_processes": [],
    }
    assert "detector import failed" in captured.err


# ---------------------------------------------------------------------------
# _gateway_managed_holder_pids -- strict Gateway process-tree classification
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid: int, *, parents=(), children=()):
        self.pid = pid
        self._parents = list(parents)
        self._children = list(children)

    def parents(self):
        return self._parents

    def children(self, *, recursive: bool):
        assert recursive is True
        return self._children


def test_gateway_classifier_marks_wrapper_and_runtime_child() -> None:
    matches = [
        (101, "python.exe", "gateway wrapper"),
        (202, "python.exe", "runtime child"),
    ]
    processes = {
        202: _FakeProcess(202, parents=[SimpleNamespace(pid=101)]),
    }

    managed = _gateway_managed_holder_pids(
        matches,
        gateway_pid_finder=lambda **_kwargs: [202],
        process_factory=processes.__getitem__,
    )

    assert managed == {101, 202}


def test_gateway_classifier_does_not_exempt_unrelated_venv_process() -> None:
    matches = [
        (101, "python.exe", "gateway wrapper"),
        (202, "python.exe", "runtime child"),
        (303, "python.exe", "unrelated serve"),
    ]
    processes = {
        202: _FakeProcess(202, parents=[SimpleNamespace(pid=101)]),
    }

    managed = _gateway_managed_holder_pids(
        matches,
        gateway_pid_finder=lambda **_kwargs: [202],
        process_factory=processes.__getitem__,
    )

    assert managed == {101, 202}


def test_gateway_classifier_keeps_holders_blocked_when_no_gateway_is_found() -> None:
    matches = [(101, "python.exe", "python.exe -m hermes_cli.main serve")]

    assert _gateway_managed_holder_pids(
        matches,
        gateway_pid_finder=lambda **_kwargs: [],
        process_factory=MagicMock(),
    ) == set()


def test_gateway_classifier_fails_closed_when_gateway_discovery_fails() -> None:
    matches = [(101, "python.exe", "gateway wrapper")]

    assert _gateway_managed_holder_pids(
        matches,
        gateway_pid_finder=MagicMock(side_effect=RuntimeError("gateway discovery failed")),
        process_factory=MagicMock(),
    ) == set()


def test_gateway_classifier_fails_closed_when_process_tree_cannot_be_read() -> None:
    class UnreadableTreeProcess:
        pid = 101

        def parents(self):
            raise PermissionError("parent access denied")

        def children(self, *, recursive: bool):
            raise AssertionError("children must not be read after parent failure")

    assert _gateway_managed_holder_pids(
        [(101, "python.exe", "gateway wrapper")],
        gateway_pid_finder=lambda **_kwargs: [101],
        process_factory=lambda _pid: UnreadableTreeProcess(),
    ) == set()


def test_gateway_classifier_fails_closed_when_gateway_exits_during_inspection() -> None:
    matches = [(101, "python.exe", "gateway wrapper")]

    assert _gateway_managed_holder_pids(
        matches,
        gateway_pid_finder=lambda **_kwargs: [101],
        process_factory=MagicMock(side_effect=ProcessLookupError),
    ) == set()


# ---------------------------------------------------------------------------
# _redact_sensitive_cmdline
# ---------------------------------------------------------------------------


def test_redact_long_flag_value_space_separated() -> None:
    """--token SECRET must preserve --token and emit --token <redacted>."""
    raw = "python.exe -m hermes_cli.main serve --token ghp_abc123 --host 10.0.0.1"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m hermes_cli.main serve --token <redacted>"
    assert "ghp_abc123" not in result


def test_redact_long_flag_equals_form() -> None:
    """--api-key=SECRET must preserve --api-key= and emit --api-key=<redacted>."""
    raw = "python.exe --api-key=sk-1234567890abcdef serve"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe --api-key=<redacted>"
    assert "sk-1234567890abcdef" not in result


def test_redact_sensitive_text_failure_returns_fully_redacted() -> None:
    """When agent.redact.redact_sensitive_text raises, the entire result
    must equal '<redacted>' so PID and name still provide diagnostics."""
    with patch.object(
        redact_module,
        "redact_sensitive_text",
        side_effect=RuntimeError("no redactor"),
    ):
        result = _redact_sensitive_cmdline("python.exe --token abc123")

    assert result == "<redacted>"


def test_redact_session_key() -> None:
    """--session-key <identifier> must redact the value and everything after."""
    raw = "python.exe -m tui_gateway.slash_worker --session-key 20260712-abcdef --model test"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m tui_gateway.slash_worker --session-key <redacted>"


def test_redact_normal_host_port_profile_remain() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 10.0.0.1 --port 9119 --profile work"
    result = _redact_sensitive_cmdline(raw)
    assert "10.0.0.1" in result
    assert "9119" in result
    assert "work" in result


def test_redact_no_sensitive_flags_is_noop() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 127.0.0.1"
    assert _redact_sensitive_cmdline(raw) == raw


def test_redact_empty_string() -> None:
    assert _redact_sensitive_cmdline("") == ""


def test_redact_short_flags_not_redacted() -> None:
    """Short flags -t (toolset), -p (profile), -k are NOT redacted."""
    raw = "python.exe -m hermes_cli.main serve -t web -p default -k somearg"
    result = _redact_sensitive_cmdline(raw)
    assert result == raw  # short flags pass through unchanged
