"""Tokenising shell commands without destroying Windows paths.

`shlex.split` defaults to POSIX mode, where a backslash escapes the next
character and is then discarded. On Windows that silently turns every native
path in a command into an unusable token — `C:\\proj\\app.ts` becomes
`C:projapp.ts`, which resolves to nothing. The failure is silent: no exception,
just a path that never matches, so features degrade to "never fires" rather
than erroring.
"""
from __future__ import annotations

import os

import pytest

from agent.shell_tokens import split_command


def test_plain_posix_command_splits_normally():
    assert split_command("cat /tmp/app/index.ts") == ["cat", "/tmp/app/index.ts"]


def test_quoted_argument_stays_one_token_without_quotes():
    assert split_command('echo "hello world"') == ["echo", "hello world"]


def test_single_quoted_argument_is_unwrapped():
    assert split_command("echo 'a b'") == ["echo", "a b"]


def test_hash_is_not_a_comment():
    # shlex.split() sets commenters="" too; a '#' in a path must survive.
    assert split_command("cat /tmp/a#b.txt") == ["cat", "/tmp/a#b.txt"]


def test_empty_command_yields_no_tokens():
    assert split_command("") == []


@pytest.mark.skipif(os.name != "nt", reason="backslash is a real escape on POSIX")
def test_windows_path_separators_survive():
    assert split_command(r"cat C:\Users\diego\proj\index.ts") == [
        "cat", r"C:\Users\diego\proj\index.ts",
    ]


@pytest.mark.skipif(os.name != "nt", reason="backslash is a real escape on POSIX")
def test_quoted_windows_path_with_spaces_survives():
    assert split_command(r'cat "C:\Program Files\app\x.ts"') == [
        "cat", r"C:\Program Files\app\x.ts",
    ]


@pytest.mark.skipif(os.name == "nt", reason="Windows deliberately keeps backslashes")
def test_posix_escape_semantics_are_preserved():
    # On POSIX a backslash still escapes — changing that would break real shell
    # commands, so the Windows behaviour must not leak across platforms.
    assert split_command(r"echo a\ b") == ["echo", "a b"]


def test_unbalanced_quote_raises_valueerror():
    # Callers rely on catching ValueError to fall back to a naive split.
    with pytest.raises(ValueError):
        split_command('cat "unterminated')
