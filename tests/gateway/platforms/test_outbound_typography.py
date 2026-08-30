"""Tests for sanitize_outbound_typography.

Outbound messaging text can carry typographic Unicode (curly quotes, en/em
dashes, non-breaking and zero-width spaces) from LLM prose and phone
keyboards' smart punctuation. When a user copies a command from a message
bubble into a terminal, those characters break shell parsing. The sanitizer
forces shell-safe ASCII equivalents on outbound text while preserving real
non-ASCII content — including ZWJ/ZWNJ, which are meaningful inside emoji
ZWJ sequences (family/profession emoji) and in scripts such as Persian.
"""
from __future__ import annotations

import pytest

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


def test_invisible_formatting_removed() -> None:
    # Invisible characters are dropped entirely: zero-width space, word
    # joiner, and BOM/ZWNBSP. These are unambiguously accidental in outbound
    # text and invisible to the reader, so removing them is safe.
    assert sanitize_outbound_typography("ssh\u200b-i") == "ssh-i"
    assert sanitize_outbound_typography("a\u2060b") == "ab"
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


def test_emoji_zwj_sequences_preserved() -> None:
    # U+200D ZWJ is the glue inside multi-codepoint emoji (family, couples,
    # professions, skin-tone). Stripping it would render them as separate
    # glyphs, so it must survive untouched.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"  # 👨👩👧👦
    assert sanitize_outbound_typography(family) == family
    profession = "\U0001f469\u200d\U0001f4bb"  # 👩💻
    assert sanitize_outbound_typography(profession) == profession


def test_zwnj_text_preserved() -> None:
    # U+200C ZWNJ is a required orthographic character in Persian/Urdu and
    # several Indic scripts; removing it changes spelling. (e.g. میخواهم)
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    assert sanitize_outbound_typography(persian) == persian


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # plain prose
        ("Run \u201cinstall\u201d now", 'Run "install" now'),
        # fenced command text
        ("```\necho \u201chi\u201d\n```", '```\necho "hi"\n```'),
        # inline command text
        ("run `df \u2013h`", "run `df -h`"),
        # CJK plus emoji
        (
            "\u5220\u9664 \u6587\u4ef6.txt \U0001f600",
            "\u5220\u9664 \u6587\u4ef6.txt \U0001f600",
        ),
        # emoji ZWJ sequence
        (
            "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
            "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
        ),
        # ZWNJ text
        ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"),
        # non-ASCII filename
        ("ls \u6587\u4ef6.txt", "ls \u6587\u4ef6.txt"),
        # zero-width space
        ("ssh\u200b-i", "ssh-i"),
        # word joiner
        ("a\u2060b", "ab"),
        # BOM
        ("\ufeffsystemctl status", "systemctl status"),
    ],
    ids=[
        "plain-prose",
        "fenced-command",
        "inline-command",
        "cjk-plus-emoji",
        "emoji-zwj-sequence",
        "zwnj-text",
        "non-ascii-filename",
        "zero-width-space",
        "word-joiner",
        "bom",
    ],
)
def test_regression_matrix(text: str, expected: str) -> None:
    assert sanitize_outbound_typography(text) == expected


def test_empty_and_short_inputs() -> None:
    assert sanitize_outbound_typography("") == ""
    assert sanitize_outbound_typography("plain") == "plain"
