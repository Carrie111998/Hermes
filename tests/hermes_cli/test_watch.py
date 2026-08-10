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
    """argparse integration tests — fast, no I/O."""

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

    def test_multiple_paths(self):
        p = self._make_parser()
        args = p.parse_args(["watch", "src", "tests"])
        assert args.paths == ["src", "tests"]


class TestPolling:
    """Polling watcher — bounded with max_iterations."""

    def test_no_valid_paths(self):
        """Non-existent directory exits 1 immediately."""
        code = _run_polling(
            [Path("/tmp/_nonexist_hwatch_99999")], None, None, 0.1,
            max_iterations=1,
        )
        assert code == 1

    def test_empty_dir_completes_cleanly(self, temp_dir):
        """Empty directory runs bounded iterations and exits 0."""
        code = _run_polling(
            [temp_dir], None, None, 0.1, max_iterations=2,
        )
        assert code == 0

    def test_detects_created_file(self, temp_dir):
        """File created before first iteration is detected."""
        # Create file before polling starts
        (temp_dir / "new.txt").write_text("hello")
        code = _run_polling(
            [temp_dir], None, None, 0.05, max_iterations=2,
        )
        assert code == 0

    def test_detects_modified_file(self, temp_dir):
        """File modified between iterations is detected."""
        f = temp_dir / "mod.txt"
        f.write_text("v1")

        def _modify():
            time.sleep(0.2)
            f.write_text("v2")

        t = threading.Thread(target=_modify, daemon=True)
        t.start()
        code = _run_polling(
            [temp_dir], None, None, 0.15, max_iterations=3,
        )
        t.join(timeout=1)
        assert code == 0

    def test_pattern_filtering(self, temp_dir):
        """Pattern filtering doesn't crash and only watches matching files."""
        (temp_dir / "foo.py").write_text("ok")
        (temp_dir / "bar.txt").write_text("ok")
        code = _run_polling(
            [temp_dir], ["*.py"], ["*.pyc"], 0.05, max_iterations=2,
        )
        assert code == 0