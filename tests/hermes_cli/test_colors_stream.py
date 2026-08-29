"""Tests for hermes_cli.colors stream-aware color gating.

``color()`` / ``should_use_color()`` default to ``sys.stdout`` but accept a
``stream=`` parameter so stderr writers gate ANSI on the *stderr* stream
(default keeps backward compatibility for the ~25 stdout callers).
"""

import io
import sys


class _FakeStream:
    """A stream stand-in that reports a fixed TTY status and records output."""

    def __init__(self, tty: bool):
        self._tty = tty
        self.buf = io.StringIO()

    def isatty(self):
        return self._tty

    def write(self, s):
        self.buf.write(s)
        return len(s)


class TestColorStreamParam:
    """The stream= parameter selects which stream the TTY check applies to."""

    def test_default_uses_stdout(self, monkeypatch):
        from hermes_cli.colors import color

        monkeypatch.setattr("sys.stdout", _FakeStream(tty=True))
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=False))
        assert "\033[" in color("x", "1")  # stdout is a TTY

    def test_stream_stderr_gates_on_stderr(self, monkeypatch):
        from hermes_cli.colors import color

        # stdout is a TTY but stderr is piped -> stderr writes must be plain
        monkeypatch.setattr("sys.stdout", _FakeStream(tty=True))
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=False))
        assert "\033[" not in color("x", "1", stream=sys.stderr)

    def test_stream_stderr_tty_emits_color(self, monkeypatch):
        from hermes_cli.colors import color

        monkeypatch.setattr("sys.stdout", _FakeStream(tty=False))
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=True))
        assert "\033[" in color("x", "1", stream=sys.stderr)


class TestShouldUseColorStream:
    """should_use_color(stream=...) checks the passed stream's isatty()."""

    def test_default_uses_stdout(self, monkeypatch):
        from hermes_cli.colors import should_use_color

        monkeypatch.setattr("sys.stdout", _FakeStream(tty=True))
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=False))
        assert should_use_color() is True

    def test_stream_overrides(self, monkeypatch):
        from hermes_cli.colors import should_use_color

        monkeypatch.setattr("sys.stdout", _FakeStream(tty=True))
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=False))
        assert should_use_color(stream=sys.stderr) is False
        monkeypatch.setattr("sys.stderr", _FakeStream(tty=True))
        assert should_use_color(stream=sys.stderr) is True
