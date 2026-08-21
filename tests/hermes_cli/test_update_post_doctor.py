"""Behavior contracts for post-update doctor verification."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd
from hermes_cli import update_receipt
from hermes_cli.subcommands.update import build_update_parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)
    return parser


def test_update_parser_enables_doctor_by_default_and_supports_opt_out():
    assert _parser().parse_args(["update"]).no_doctor is False
    assert _parser().parse_args(["update", "--no-doctor"]).no_doctor is True


def test_post_update_doctor_runs_fresh_read_only_process(
    monkeypatch, tmp_path: Path, capsys
):
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    calls = []
    receipt_steps = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "doctor healthy\n", "")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        update_receipt,
        "record_step",
        lambda name, ok, detail: receipt_steps.append((name, ok, detail)),
    )

    assert update_cmd._run_post_update_doctor(SimpleNamespace(no_doctor=False)) is True
    assert calls == [
        (
            [str(interpreter), "-m", "hermes_cli.main", "doctor"],
            {
                "cwd": tmp_path,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 300,
            },
        )
    ]
    assert receipt_steps == [("post_update_doctor", True, "exit_code=0")]
    assert "doctor healthy" in capsys.readouterr().out


def test_post_update_doctor_failure_is_reported_and_recorded(
    monkeypatch, tmp_path: Path, capsys
):
    receipt_steps = []

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 2, "", "doctor found a problem\n"
        ),
    )
    monkeypatch.setattr(
        update_receipt,
        "record_step",
        lambda name, ok, detail: receipt_steps.append((name, ok, detail)),
    )

    assert update_cmd._run_post_update_doctor(SimpleNamespace(no_doctor=False)) is False
    assert receipt_steps == [("post_update_doctor", False, "exit_code=2")]
    output = capsys.readouterr().out
    assert "doctor found a problem" in output
    assert "exited with code 2" in output


def test_post_update_doctor_opt_out_and_legacy_callers_do_not_spawn(
    monkeypatch
):
    calls = []
    receipt_steps = []

    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        update_receipt,
        "record_step",
        lambda name, ok, detail: receipt_steps.append((name, ok, detail)),
    )

    assert update_cmd._run_post_update_doctor(SimpleNamespace(no_doctor=True)) is True
    assert update_cmd._run_post_update_doctor(SimpleNamespace()) is True
    assert calls == []
    assert receipt_steps == [
        ("post_update_doctor", True, "skipped by --no-doctor")
    ]
