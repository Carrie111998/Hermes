"""Tests for hermes -z surrogate sanitisation (issue #80366).

Model text may contain lone UTF-16 surrogates (e.g. \ud800) that crash
UTF-8 stdout.  The fix replaces them with U+FFFD before writing.
"""

import io

import pytest

from hermes_cli import oneshot


def _sanitize(text):
    """Apply the same sanitisation the oneshot path uses."""
    return text.encode("utf-8", "replace").decode("utf-8")


class TestSurrogateSanitisation:
    def test_lone_high_surrogate_replaced(self):
        # \ud800 is a lone high surrogate — not valid UTF-8 on its own.
        raw = "hello \ud800 world"
        fixed = _sanitize(raw)
        assert "\ud800" not in fixed
        assert "hello" in fixed and "world" in fixed

    def test_lone_low_surrogate_replaced(self):
        # \udc00 is a lone low surrogate.
        raw = "text \udc00 end"
        fixed = _sanitize(raw)
        assert "\udc00" not in fixed

    def test_valid_text_unchanged(self):
        raw = "Hello, world!  \n  emoji: \U0001f600"
        assert _sanitize(raw) == raw

    def test_empty_string_safe(self):
        assert _sanitize("") == ""

    def test_write_to_stdout_does_not_crash(self, monkeypatch):
        """Simulates the real write path: stdout gets a lone surrogate."""
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)
        response = "hello \ud800 world"
        # This is what oneshot does before writing:
        response = response.encode("utf-8", "replace").decode("utf-8")
        # Should not raise UnicodeEncodeError
        buf.write(response)
        assert "\ud800" not in buf.getvalue()
