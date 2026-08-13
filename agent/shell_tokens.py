"""Tokenising a shell command string without destroying Windows paths.

``shlex.split`` runs in POSIX mode by default, where a backslash escapes the
following character and is then dropped. On Windows that quietly mangles every
native path a command contains::

    shlex.split(r"cat C:\\Users\\diego\\proj\\index.ts")
    ['cat', 'C:Usersdiegoprojindex.ts']

Nothing raises. The token simply stops being a path, so anything downstream
that resolves it — an ``isfile`` check, a directory scan, a spawn — silently
finds nothing. Features built on it degrade to "never fires" rather than
failing loudly, which is why this class of bug survives on a Windows-only host.

Clearing ``escape`` on Windows makes the backslash literal while leaving quote
handling intact, so ``"C:\\Program Files\\app.exe"`` still tokenises as one
argument. POSIX escape semantics are untouched on POSIX, where a backslash
genuinely is an escape and changing it would break real commands.

Two older copies of this helper exist — ``hermes_cli/kanban.py`` and
``plugins/disk-cleanup``. They are left alone: both are correct and one lives
in a plugin that should not import agent internals. New callers use this.
"""

from __future__ import annotations

import os
import shlex
from typing import List

__all__ = ["split_command"]


def split_command(cmd: str) -> List[str]:
    """Split ``cmd`` into tokens, keeping Windows path separators intact.

    Raises :class:`ValueError` on unbalanced quotes, exactly as
    ``shlex.split`` does, so existing callers can keep their fallbacks.
    """
    lex = shlex.shlex(cmd, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""  # as shlex.split() does — never treat "#" as a comment
    if os.name == "nt":
        lex.escape = ""
    return list(lex)
