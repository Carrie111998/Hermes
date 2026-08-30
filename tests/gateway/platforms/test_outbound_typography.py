"""Tests for sanitize_outbound_typography.

Outbound messaging text can carry typographic Unicode (curly quotes, en/em
dashes, non-breaking and zero-width spaces) from LLM prose and phone
keyboards' smart punctuation. When a user copies a command from a message
bubble into a terminal, those characters break shell parsing. The sanitizer
forces shell-safe ASCII equivalents on outbound text.
"""
from __future__ import annotations

from gateway.platforms.base import sanitize_outbound_typography


def test_curly_quotes_become_straight() -> None:
    assert (
        sanitize_outbound_typography("\u201chello\u201d and \u2018world\u2019")
        == '"hello" and \'world\''
    )


def test_dashes_become_hyphen() -> None:
    # ASCII double-dash flags must pass through untouched.
    assert sanitize_outbound_typography("rsync -av --delete /src/ /dst/") == (
        "rsync -av --delete /src/ /dst/"
    )
    # En/em dashes and the Unicode minus map to a plain hyphen.
    assert sanitize_outbound_typography("a \u2013 b \u2014 c \u2212 1") == "a - b - c - 1"


def test_non_breaking_and_thin_spaces_become_space() -> None:
    # The shell must see ordinary spaces so tokens split correctly instead of
    # one giant argument.
    assert sanitize_outbound_typography("ssh\u00a0user@host") == "ssh user@host"
    assert sanitize_outbound_typography("a\u2009b\u202fc") == "a b c"


def test_zero_width_characters_removed() -> None:
    # Invisible characters are dropped entirely.
    assert sanitize_outbound_typography("ssh\u200b-i") == "ssh-i"
    assert sanitize_outbound_typography("\ufeffsystemctl status") == "systemctl status"


def test_ellipsis_expanded() -> None:
    assert sanitize_outbound_typography("run\u2026") == "run..."


def test_ascii_passthrough() -> None:
    clean = "sudo apt update && echo done"
    assert sanitize_outbound_typography(clean) == clean


def test_cjk_and_emoji_left_alone() -> None:
    # Real non-ASCII content (filenames, emoji) is not touched — only the
    # shell-hostile lookalikes are.
    text = "ls \u6587\u4ef6.txt \U0001f600"
    assert sanitize_outbound_typography(text) == text


def test_empty_and_short_inputs() -> None:
    assert sanitize_outbound_typography("") == ""
    assert sanitize_outbound_typography("plain") == "plain"
