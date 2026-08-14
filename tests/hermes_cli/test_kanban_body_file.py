"""Tests for ``hermes kanban create --body-file`` secure body input."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


# ---------------------------------------------------------------------------
# Happy-path: --body-file with a real file
# ---------------------------------------------------------------------------

class TestBodyFile:
    def test_body_from_file(self, kanban_home, tmp_path):
        """--body-file PATH reads and persists the file content as task body."""
        body_file = tmp_path / "body.md"
        body_file.write_text("detailed task description", encoding="utf-8")
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "file-task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].body == "detailed task description"

    def test_body_from_stdin(self, kanban_home, monkeypatch):
        """--body-file - reads body from stdin."""
        parser = _build_parser()
        monkeypatch.setattr(
            "sys.stdin",
            io.TextIOWrapper(io.BytesIO(b"stdin body content")),
        )
        args = parser.parse_args(
            ["kanban", "create", "stdin-task", "--body-file", "-"]
        )
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].body == "stdin body content"

    def test_empty_body_file(self, kanban_home, tmp_path):
        """An empty file results in an empty-string body (preserving semantics)."""
        body_file = tmp_path / "empty.md"
        body_file.write_text("", encoding="utf-8")
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "empty-task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].body == ""

    def test_exactly_1mib(self, kanban_home, tmp_path):
        """Exactly 1 MiB body is accepted."""
        body_file = tmp_path / "big.md"
        content = "A" * 1_048_576
        body_file.write_text(content, encoding="utf-8")
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "big-task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert len(tasks[0].body) == 1_048_576


# ---------------------------------------------------------------------------
# Mutual exclusion
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    def test_body_and_body_file_are_mutually_exclusive(self, kanban_home, tmp_path):
        """--body and --body-file cannot be used together (argparse-level)."""
        body_file = tmp_path / "body.md"
        body_file.write_text("content", encoding="utf-8")
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(
                ["kanban", "create", "task", "--body", "inline",
                 "--body-file", str(body_file)]
            )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Error handling: fail closed, exit 2, no task creation
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_file_not_found(self, kanban_home, tmp_path, capsys):
        """Non-existent file -> exit 2, no task created."""
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", str(tmp_path / "nope.md")]
        )
        rc = kc.kanban_command(args)
        assert rc == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "nope.md" in captured.err

    def test_error_message_bounds_long_path(self, kanban_home, capsys):
        """A hostile path cannot make the error output unbounded."""
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", "x" * 10_000]
        )
        assert kc.kanban_command(args) == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        assert len(capsys.readouterr().err) <= 300

    def test_error_message_escapes_path_control_characters(
        self, kanban_home, capsys
    ):
        """A hostile path cannot inject terminal controls or extra lines."""
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", "bad\n\x1b[31m.md"]
        )
        assert kc.kanban_command(args) == 2
        error = capsys.readouterr().err
        assert error.count("\n") == 1
        assert "\x1b" not in error

    def test_oversized_input(self, kanban_home, tmp_path, capsys):
        """Input exceeding 1 MiB -> exit 2, no task created."""
        body_file = tmp_path / "huge.md"
        body_file.write_bytes(b"X" * (1_048_576 + 1))
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "1 MiB" in captured.err or "1048576" in captured.err

    def test_invalid_utf8(self, kanban_home, tmp_path, capsys):
        """Non-UTF-8 content -> exit 2, no task created."""
        body_file = tmp_path / "bad.bin"
        body_file.write_bytes(b"\x80\x81\x82\xff")
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "utf" in captured.err.lower() or "decode" in captured.err.lower()

    def test_oversized_stdin(self, kanban_home, monkeypatch, capsys):
        """Oversized stdin is bounded and rejected before task creation."""
        monkeypatch.setattr(
            "sys.stdin",
            io.TextIOWrapper(io.BytesIO(b"X" * (1_048_576 + 1))),
        )
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", "-"]
        )
        assert kc.kanban_command(args) == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        assert "1 MiB" in capsys.readouterr().err

    def test_invalid_utf8_stdin(self, kanban_home, monkeypatch, capsys):
        """Invalid UTF-8 on stdin is rejected before task creation."""
        monkeypatch.setattr(
            "sys.stdin",
            io.TextIOWrapper(io.BytesIO(b"SENTINEL_SECRET_DATA\x80\xff")),
        )
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", "-"]
        )
        assert kc.kanban_command(args) == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "utf" in captured.err.lower()
        assert "SENTINEL_SECRET_DATA" not in captured.err

    def test_text_only_stdin_fails_closed(self, kanban_home, monkeypatch, capsys):
        """An embedded stdin without a binary buffer does not traceback."""
        monkeypatch.setattr("sys.stdin", io.StringIO("SENTINEL_SECRET_DATA"))
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", "-"]
        )
        assert kc.kanban_command(args) == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "cannot read" in captured.err
        assert "SENTINEL_SECRET_DATA" not in captured.err

    def test_invalid_workspace_does_not_consume_stdin(
        self, kanban_home, monkeypatch, capsys
    ):
        """Cheap flag validation happens before potentially blocking stdin reads."""
        class StdinMustNotBeRead:
            @property
            def buffer(self):
                raise AssertionError("stdin was consumed")

        monkeypatch.setattr("sys.stdin", StdinMustNotBeRead())
        parser = _build_parser()
        args = parser.parse_args(
            [
                "kanban", "create", "task", "--body-file", "-",
                "--workspace", "invalid",
            ]
        )
        assert kc.kanban_command(args) == 2
        assert "unknown" in capsys.readouterr().err

    def test_permission_error(self, kanban_home, tmp_path, capsys, monkeypatch):
        """Unreadable file -> exit 2, no task created."""
        body_file = tmp_path / "secret.md"
        body_file.write_text("secret", encoding="utf-8")
        def deny_open(*_args, **_kwargs):
            raise PermissionError("SENTINEL_SECRET_DATA")

        monkeypatch.setattr("builtins.open", deny_open)
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 2
        with kb.connect_closing() as conn:
            assert len(kb.list_tasks(conn)) == 0
        captured = capsys.readouterr()
        assert "cannot read" in captured.err
        assert "SENTINEL_SECRET_DATA" not in captured.err

    def test_no_body_content_in_stderr(self, kanban_home, tmp_path, capsys):
        """Error messages must not leak body file content."""
        body_file = tmp_path / "bad.bin"
        # Use recognizable sentinel bytes that would stand out if leaked
        body_file.write_bytes(b"SENTINEL_SECRET_DATA\x80\xff")
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "task", "--body-file", str(body_file)]
        )
        rc = kc.kanban_command(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "SENTINEL_SECRET_DATA" not in captured.err
        assert "SENTINEL_SECRET_DATA" not in captured.out


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_body_inline_still_works(self, kanban_home):
        """Existing --body flag continues to work."""
        parser = _build_parser()
        args = parser.parse_args(
            ["kanban", "create", "inline-task", "--body", "inline body"]
        )
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].body == "inline body"

    def test_no_body_flag_gives_none(self, kanban_home):
        """No --body or --body-file -> body is None."""
        parser = _build_parser()
        args = parser.parse_args(["kanban", "create", "bare-task"])
        rc = kc.kanban_command(args)
        assert rc == 0
        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].body is None
