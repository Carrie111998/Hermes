"""Tests for ``hermes doctor logs`` one-shot log rotation (t_57aac3e7 fix 1)."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import textwrap
import time

import pytest


def _build_doctor_parser():
    """Build just the doctor subparser via the real builder."""
    from hermes_cli.subcommands.doctor import build_doctor_parser

    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    build_doctor_parser(sub, cmd_doctor=lambda args: None)
    return root


def test_doctor_logs_parser_wiring():
    """``doctor logs <path> --force --backups N --max-bytes M`` parses."""
    root = _build_doctor_parser()
    ns = root.parse_args(
        ["doctor", "logs", "/tmp/x.log", "--force", "--backups", "2", "--max-bytes", "100"]
    )
    assert ns.command == "doctor"
    assert ns.doctor_command == "logs"
    assert ns.path == "/tmp/x.log"
    assert ns.force is True
    assert ns.backups == 2
    assert ns.max_bytes == 100


def test_doctor_logs_parser_accepts_reopen_command():
    root = _build_doctor_parser()
    ns = root.parse_args(
        ["doctor", "logs", "/tmp/x.log", "--reopen-command", "/bin/kill", "-HUP", "123"]
    )
    assert ns.reopen_command == ["/bin/kill", "-HUP", "123"]


def test_plain_doctor_has_no_logs_subcommand():
    """Bare ``hermes doctor`` still parses (no doctor_command attr set)."""
    root = _build_doctor_parser()
    ns = root.parse_args(["doctor"])
    assert ns.command == "doctor"
    assert getattr(ns, "doctor_command", None) is None


def test_run_doctor_logs_forced_rotation(tmp_path):
    """--force rotates an existing file even under the cap; .1 appears."""
    from hermes_cli.doctor_logs import run_doctor_logs

    log = tmp_path / "server.log"
    log.write_text("x" * 100, encoding="utf-8")
    args = argparse.Namespace(path=str(log), max_bytes=None, backups=3, force=True)
    rc = run_doctor_logs(args)
    assert rc == 0
    # The original inode was renamed to .1
    assert log.with_suffix(".log.1").exists()
    content = log.with_suffix(".log.1").read_text(encoding="utf-8")
    assert len(content) == 100


def test_run_doctor_logs_noop_under_cap_without_force(tmp_path):
    """Without --force, a small file is left alone (size-gated)."""
    from hermes_cli.doctor_logs import run_doctor_logs

    log = tmp_path / "small.log"
    log.write_text("tiny", encoding="utf-8")
    args = argparse.Namespace(path=str(log), max_bytes=None, backups=3, force=False)
    rc = run_doctor_logs(args)
    assert rc == 0
    assert not log.with_suffix(".log.1").exists()
    assert log.exists()


def test_run_doctor_logs_missing_path_is_clean(tmp_path):
    """A missing path reports cleanly and exits 0 (nothing to do)."""
    from hermes_cli.doctor_logs import run_doctor_logs

    args = argparse.Namespace(
        path=str(tmp_path / "nope.log"), max_bytes=None, backups=3, force=True
    )
    rc = run_doctor_logs(args)
    assert rc == 0


def test_run_doctor_logs_bad_args_exit_2(tmp_path):
    """Negative --backups is a usage error (exit 2)."""
    from hermes_cli.doctor_logs import run_doctor_logs

    log = tmp_path / "x.log"
    log.write_text("data", encoding="utf-8")
    args = argparse.Namespace(path=str(log), max_bytes=None, backups=-1, force=True)
    rc = run_doctor_logs(args)
    assert rc == 2


def test_omlx_log_refuses_rotation_without_reopen_command(tmp_path):
    from hermes_cli.doctor_logs import run_doctor_logs

    log = tmp_path / "Library" / "Application Support" / "oMLX" / "logs" / "server.log"
    log.parent.mkdir(parents=True)
    log.write_text("before\n", encoding="utf-8")
    rc = run_doctor_logs(
        argparse.Namespace(path=str(log), max_bytes=None, backups=3, force=True)
    )
    assert rc == 3
    assert log.exists()
    assert not log.with_suffix(".log.1").exists()


def test_omlx_log_rotation_requires_writer_handoff(tmp_path):
    from hermes_cli.doctor_logs import run_doctor_logs

    log = tmp_path / "Library" / "Application Support" / "oMLX" / "logs" / "server.log"
    log.parent.mkdir(parents=True)
    control = tmp_path / "reopen"
    log.write_text("before\n", encoding="utf-8")
    writer = textwrap.dedent(
        """
        import pathlib, signal, sys, time
        path = pathlib.Path(sys.argv[1]); control = pathlib.Path(sys.argv[2])
        handle = path.open('a', encoding='utf-8'); handle.write('writer before\\n'); handle.flush()
        def reopen(*_):
            global handle
            handle.close(); handle = path.open('a', encoding='utf-8')
            handle.write('writer after\\n'); handle.flush()
        signal.signal(signal.SIGHUP, reopen)
        while not control.exists(): time.sleep(.02)
        handle.close()
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", writer, str(log), str(control)])
    try:
        deadline = time.monotonic() + 3
        while "writer before" not in log.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        rc = run_doctor_logs(
            argparse.Namespace(
                path=str(log), max_bytes=None, backups=3, force=True,
                reopen_command=["/bin/kill", "-HUP", str(proc.pid)], reopen_timeout=3,
            )
        )
        assert rc == 0
        assert "writer after" in log.read_text(encoding="utf-8")
        assert "writer after" not in log.with_suffix(".log.1").read_text(encoding="utf-8")
    finally:
        control.touch()
        proc.wait(timeout=3)
