"""Headless stdout line-buffering (#92281).

When hermes runs under a headless supervisor (stdout piped to a log),
Python's default block buffering delivered minutes of steady agent work
as one flush burst — indistinguishable from a hang from the log stream.
``configure_headless_stdout_buffering()`` must line-buffer non-TTY
stdout and leave interactive TTYs untouched.
"""

import io
from unittest.mock import patch

from hermes_cli.stdio import configure_headless_stdout_buffering


class _FakeStream(io.TextIOBase):
    """Text stream double that records reconfigure() calls."""

    def __init__(self, tty):
        self._tty = tty
        self.reconfigure_kwargs = None

    def isatty(self):
        return self._tty

    def reconfigure(self, **kwargs):
        self.reconfigure_kwargs = kwargs

    def write(self, text):
        return len(text)


def test_piped_stdout_gets_line_buffering():
    stream = _FakeStream(tty=False)
    with patch("sys.stdout", stream):
        assert configure_headless_stdout_buffering() is True
    assert stream.reconfigure_kwargs == {"line_buffering": True}


def test_tty_stdout_is_left_alone():
    stream = _FakeStream(tty=True)
    with patch("sys.stdout", stream):
        assert configure_headless_stdout_buffering() is False
    assert stream.reconfigure_kwargs is None


def test_stream_without_reconfigure_support_is_skipped():
    class _Legacy(_FakeStream):
        reconfigure = None  # type: ignore[assignment]

    stream = _Legacy(tty=False)
    with patch("sys.stdout", stream):
        assert configure_headless_stdout_buffering() is False


def test_missing_stdout_is_survivable():
    with patch("sys.stdout", None):
        assert configure_headless_stdout_buffering() is False


def test_real_piped_textiowrapper_is_reconfigured():
    """Use a real TextIOWrapper (has reconfigure, isatty=False when wrapped
    around BytesIO) to verify the reconfigure call actually happens."""
    stream = io.TextIOWrapper(io.BytesIO())
    assert not stream.isatty()
    reconfigured = []
    original_reconfigure = stream.reconfigure
    stream.reconfigure = lambda **kw: (reconfigured.append(kw), original_reconfigure(**kw))
    with patch("sys.stdout", stream):
        result = configure_headless_stdout_buffering()
    assert result is True
    assert reconfigured == [{"line_buffering": True}]
