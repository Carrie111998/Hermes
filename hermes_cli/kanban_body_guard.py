"""Reject cards that carry no real instruction.

A kanban card's body IS the job spec. A card with no instruction is not a
task — it is a worker guessing. Between 2026-07 and 2026-08 roughly a third
of all cards on this board (98 of 289 in a 30-day window) were created with
a body of exactly ``-``: the shell "read from stdin" idiom, which
``hermes kanban create`` never implemented. argparse happily accepts a bare
``-`` as an option value, so the placeholder was stored verbatim and the
heredoc that was supposed to supply the real brief was silently discarded.

Two defences live here, used by every agent-facing creation door:

* :func:`resolve_body` turns ``--body -`` back into the real thing by
  reading stdin, and fails loudly when stdin has nothing to give.
* :func:`validate_body` rejects null / empty / whitespace-only bodies and
  punctuation placeholders that a naive ``if body and body.strip()`` check
  waves through.

Deliberately dependency-free so it can be imported from the CLI and from
the MCP tool layer without dragging in either one's imports.
"""

from __future__ import annotations

import sys
from typing import Optional

__all__ = [
    "BlankBodyError",
    "PLACEHOLDER_BODIES",
    "is_placeholder_body",
    "resolve_body",
    "validate_body",
]


class BlankBodyError(ValueError):
    """Raised when a card would be created without a usable instruction."""


# Lowercased, stripped values that look like a body but carry no instruction.
# `-` is the one that actually happened 98 times; the rest are the obvious
# neighbours a model reaches for when it has nothing to say.
PLACEHOLDER_BODIES = frozenset({
    "-",
    "--",
    "---",
    ".",
    "..",
    "...",
    "n/a",
    "na",
    "n.a.",
    "none",
    "null",
    "nil",
    "tbd",
    "tba",
    "todo",
    "?",
    "??",
    "(none)",
    "(empty)",
    "(no body)",
    "no body",
    "no description",
    "placeholder",
})

# Minimum length for a body to plausibly be an instruction. Anything shorter
# than this is a token, not a brief. Kept low so terse-but-real bodies pass.
MIN_BODY_CHARS = 8

_STDIN_SENTINEL = "-"


def is_placeholder_body(body: Optional[str]) -> bool:
    """True when ``body`` carries no usable instruction.

    Covers null, empty, whitespace-only, and the punctuation/word
    placeholders in :data:`PLACEHOLDER_BODIES`. Note that the naive guard
    ``bool(body and body.strip())`` returns True for ``"-"`` — that is
    precisely the hole this closes.
    """
    if body is None:
        return True
    stripped = body.strip()
    if not stripped:
        return True
    if stripped.lower() in PLACEHOLDER_BODIES:
        return True
    # A body made up entirely of punctuation / dashes / underscores is a
    # separator someone pasted, not an instruction.
    if not any(ch.isalnum() for ch in stripped):
        return True
    return False


def resolve_body(body: Optional[str], *, stdin=None) -> Optional[str]:
    """Turn a ``--body -`` request into the real body from stdin.

    ``-`` is the near-universal CLI convention for "read this from stdin",
    and models emit it with a heredoc attached. Honour it instead of storing
    the dash. If stdin is a terminal or empty, raise rather than substitute
    a placeholder — a card with no instruction must not be creatable.

    Any other value (including ``None``) is returned unchanged; validation
    is :func:`validate_body`'s job.
    """
    if body is None or body.strip() != _STDIN_SENTINEL:
        return body

    stream = stdin if stdin is not None else sys.stdin
    if stream is None or getattr(stream, "isatty", lambda: False)():
        raise BlankBodyError(
            "--body - means 'read the body from stdin', but stdin is a "
            "terminal. Pipe the body in (--body - <<'EOF' ... EOF) or pass "
            "the text directly with --body \"...\"."
        )
    try:
        piped = stream.read()
    except Exception as exc:  # pragma: no cover - stdin read failure
        raise BlankBodyError(f"--body -: could not read stdin: {exc}") from exc

    if is_placeholder_body(piped):
        raise BlankBodyError(
            "--body - was given but stdin was empty. The card would have "
            "been created with no instruction. Supply the real body."
        )
    return piped


def validate_body(body: Optional[str], *, allow_missing: bool = False) -> Optional[str]:
    """Return ``body`` if it is a real instruction, else raise.

    ``allow_missing=True`` permits ``None`` (used for ``--triage`` cards,
    whose whole purpose is to be fleshed out by a specifier before they can
    be dispatched). Even then a placeholder body is rejected — an explicit
    ``-`` is a lie, not an omission.
    """
    if body is None and allow_missing:
        return None

    if is_placeholder_body(body):
        shown = "<missing>" if body is None else repr(body.strip()[:40])
        raise BlankBodyError(
            f"refusing to create a card with no instruction (body={shown}). "
            "The body IS the job spec: a worker with a blank brief guesses. "
            "Pass a real --body, or use --triage if the spec genuinely does "
            "not exist yet."
        )

    stripped = body.strip()
    if len(stripped) < MIN_BODY_CHARS:
        raise BlankBodyError(
            f"body is too short to be an instruction ({len(stripped)} chars, "
            f"minimum {MIN_BODY_CHARS}): {stripped!r}. State what the worker "
            "should actually do."
        )
    return body
