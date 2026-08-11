"""The blank-body guard must cover the failure distribution that actually happened.

98 of 99 unusable cards in the 30 days to 2026-08-05 had a body of exactly
``-``. A guard written as ``bool(body and body.strip())`` returns True for
``-`` and would have closed 1 of those 99. These tests pin the real cases.
"""

import io

import pytest

from hermes_cli.kanban_body_guard import (
    BlankBodyError,
    is_placeholder_body,
    resolve_body,
    validate_body,
)


# The exact values observed on the board, plus the neighbours.
@pytest.mark.parametrize("body", [
    None, "", "   ", "\n", "\t\n  ",
    "-", " - ", "--", "---",
    ".", "..", "...", "?", "??",
    "n/a", "N/A", "na", "None", "null", "nil",
    "tbd", "TBD", "tba", "todo", "TODO",
    "(none)", "(empty)", "no body", "placeholder",
    "***", "___", "###",
    0, False, [], {},
])
def test_placeholders_are_rejected(body):
    assert is_placeholder_body(body) is True
    with pytest.raises(BlankBodyError):
        validate_body(body)


def test_the_naive_guard_would_have_passed_the_dash():
    """Regression pin: the shipped-but-unmerged guard accepted '-'."""
    naive = bool("-" and "-".strip())
    assert naive is True          # what the old guard did
    assert is_placeholder_body("-") is True   # what this one does


@pytest.mark.parametrize("body", [
    "Rotate the expired GitHub PAT and verify with git ls-remote.",
    "Fix bug #12 in auth.py",
    "- audit the board\n- report the rate",
    "Ship it",
    "Deploy.",
    "Fix #1",
    "修复登录错误",
])
def test_real_bodies_pass(body):
    assert is_placeholder_body(body) is False
    assert validate_body(body) == body


def test_missing_allowed_only_for_triage():
    assert validate_body(None, allow_missing=True) is None
    with pytest.raises(BlankBodyError):
        validate_body(None, allow_missing=False)


def test_non_string_body_fails_loudly_instead_of_crashing():
    with pytest.raises(BlankBodyError, match="body=<int>"):
        validate_body(7)


def test_triage_still_rejects_an_explicit_placeholder():
    """Omission is forgivable; an explicit '-' is a lie."""
    with pytest.raises(BlankBodyError):
        validate_body("-", allow_missing=True)


def test_dash_reads_stdin_instead_of_being_stored():
    real = "Do the actual work described here in full."
    assert resolve_body("-", stdin=io.StringIO(real)) == real


def test_dash_with_empty_stdin_fails_loudly():
    with pytest.raises(BlankBodyError, match="stdin was empty"):
        resolve_body("-", stdin=io.StringIO(""))


def test_dash_on_a_tty_fails_loudly():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    with pytest.raises(BlankBodyError, match="stdin is a"):
        resolve_body("-", stdin=Tty(""))


def test_non_dash_bodies_pass_through_resolve_untouched():
    assert resolve_body("a real body here") == "a real body here"
    assert resolve_body(None) is None
