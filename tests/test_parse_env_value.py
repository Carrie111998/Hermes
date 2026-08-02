"""Tests for .env value parsing in ``hermes_cli.config.load_env()``.

Regression coverage for the inline-comment bug (#76544): a value like
``MINIMAX_BASE_URL=https://api.minimax.io/anthropic  # official`` was
stored including the comment text, corrupting the resolved base URL —
requests then went to ``/anthropic%20%20/v1/messages`` and failed with
an opaque 404. The two loaders disagreed: python-dotenv (used by
``hermes_cli/env_loader.py``) strips inline comments, while
``_parse_env_value`` (used by ``load_env``) did not.

Behaviour contract asserted here:
- ``#`` preceded by whitespace starts a comment (matching python-dotenv
  and bash), so the trailing comment is dropped and trailing whitespace
  stripped.
- A ``#`` with no preceding whitespace is part of the value (URL
  fragments, opaque tokens).
- A ``#`` inside a quoted value is data, not a comment.
- Quoted-value handling (single/double quotes, escaped quotes) is
  unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import config as hermes_config  # noqa: E402


# ---------------------------------------------------------------------------
# Unit level — _parse_env_value
# ---------------------------------------------------------------------------


def test_inline_comment_is_stripped():
    assert (
        hermes_config._parse_env_value(
            "https://api.minimax.io/anthropic  # Official endpoint"
        )
        == "https://api.minimax.io/anthropic"
    )


def test_inline_comment_after_tab_is_stripped():
    assert hermes_config._parse_env_value("sk-abc\t# note") == "sk-abc"


def test_hash_without_whitespace_is_data():
    # URL fragments and hashes glued to the value are not comments.
    assert (
        hermes_config._parse_env_value("https://x.test/path#frag")
        == "https://x.test/path#frag"
    )
    assert hermes_config._parse_env_value("abc#def") == "abc#def"


def test_hash_inside_quotes_is_data():
    assert hermes_config._parse_env_value('"a # b"') == "a # b"
    assert hermes_config._parse_env_value("'a # b'") == "a # b"


def test_plain_value_unchanged():
    assert hermes_config._parse_env_value("plain") == "plain"


def test_quoted_value_unescaping_unchanged():
    assert hermes_config._parse_env_value('"a\\"b"') == 'a"b'
    assert hermes_config._parse_env_value("'single'") == "single"


def test_comment_trailing_quoted_value_is_stripped():
    # The comment ends the line; the value before it is still unquoted.
    assert hermes_config._parse_env_value('"a # b"  # note') == "a # b"
    assert hermes_config._parse_env_value('"a\\"b"  # note') == 'a"b'


# ---------------------------------------------------------------------------
# E2E — load_env() on a real temp HERMES_HOME .env (the #76544 report path)
# ---------------------------------------------------------------------------


def test_load_env_strips_inline_comment_on_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "MINIMAX_BASE_URL=https://api.minimax.io/anthropic  # official\n"
        "MINIMAX_API_KEY=sk-cp-test\n",
        encoding="utf-8",
    )
    env_vars = hermes_config.load_env()
    assert env_vars["MINIMAX_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert env_vars["MINIMAX_API_KEY"] == "sk-cp-test"


def test_load_env_handles_mixed_comment_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "PLAIN=value\n"
        "COMMENTED=value  # trailing note\n"
        "HASH=value#notacomment\n"
        'QUOTED="a # b"\n'
        'ESCAPED="a\\"b"  # note\n',
        encoding="utf-8",
    )
    env_vars = hermes_config.load_env()
    assert env_vars["PLAIN"] == "value"
    assert env_vars["COMMENTED"] == "value"
    assert env_vars["HASH"] == "value#notacomment"
    assert env_vars["QUOTED"] == "a # b"
    assert env_vars["ESCAPED"] == 'a"b'
