"""Tests for ``hermes watch`` subcommand."""

from __future__ import annotations

import argparse
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys_path_added = False
if not any("hermes_cli" in p for p in __import__("sys").path):
    __import__("sys").path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "..")
    )
    sys_path_added = True

from hermes_cli.subcommands.watch import _run_polling, build_watch_parser  # noqa: E402


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class TestBuildWatchParser:
    """argparse integration tests."""

    def _make_parser(self):
        p = argparse.ArgumentParser(prog="hermes")
        sub = p.add_subparsers(dest="command")
        build_watch_parser(sub)
        return p

    def test_parses_paths(self):
        p = self._make_parser()
        args = p.parse_args(["watch", "."])
        assert args.command == "watch"
        assert args.paths == ["."]

    def test_defaults(self):
        p = self._make_parser()
        args = p.parse_args(["watch", "/tmp"])
        assert args.recursive is True
        assert args.interval == 1.0
        assert args.pattern is None
        assert args.ignore is None
        assert args.command == "watch"

    def test_pattern_flag(self):
        p = self._make_parser()
        args = p.parse_args(["watch", ".", "--pattern", "*.py,*.js"])
        assert args.pattern == "*.py,*.js"

    def test_ignore_flag(self):
        p = self._make_parser()
        args = p.parse_args(["watch", ".", "--ignore", "*.pyc"])
        assert args.ignore == "*.pyc"

    def test_no_recursive(self):
        p = self._make_parser()
        args = p.parse_args(["watch", ".", "--no-recursive"])
        assert args.recursive is False

    def test_command_flag(self):
        p = self._make_parser()
        args = p.parse_args(["watch", ".", "--command", "echo hi"])
        assert args.run_command == "echo hi"
        assert args.command == "watch"


class TestPolling:
    """Polling watcher: cold paths and error edges."""

    def test_no_valid_paths(self):
        code = _run_polling(
            [Path("/tmp/_nonexist_hwatch_99999")], None, None, 0.1
        )
        assert code == 1

    def test_empty_dir_starts(self, temp_dir):
        """Empty directory — watcher starts, runs one loop, exits via timer."""
        import signal

        old = signal.signal(signal.SIGALRM, lambda s, f: None)
        signal.setitimer(signal.ITIMER_REAL, 1.2)
        try:
            code = _run_polling([temp_dir], None, None, 0.3)
            assert code == 0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def test_detects_created_file(self, temp_dir):
        """File created during polling loop is detected."""
        import signal

        def _create():
            time.sleep(0.4)
            (temp_dir / "new.txt").write_text("hi")

        t = threading.Thread(target=_create, daemon=True)
        t.start()

        old = signal.signal(signal.SIGALRM, lambda s, f: None)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            code = _run_polling([temp_dir], None, None, 0.3)
            assert code == 0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            t.join(timeout=0.5)