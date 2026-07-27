"""A failing git command must not be reassured away.

_interpret_exit_code appends a note telling the model a non-zero exit was
expected, so it doesn't waste turns investigating. The git entry applied to
EVERY subcommand: "Non-zero exit (often normal — e.g. 'git diff' returns 1 when
files differ)". A failed `git push`, `git commit` or `git merge` therefore
arrived labelled as probably-fine. H-26.
"""

from __future__ import annotations

import pytest

from tools.terminal_tool import _interpret_exit_code


def _note(command: str, code: int = 1):
    return _interpret_exit_code(command, code)


# ── exit 1 genuinely means "differences"/"no match" ──────────────────────────

@pytest.mark.parametrize("command", [
    "git diff",
    "git diff --stat HEAD",
    "git diff-index --quiet HEAD",
    "git grep needle",
    "git check-ignore build/",
    "git merge-base --is-ancestor a b",
])
def test_query_subcommands_are_explained(command):
    note = _note(command)
    assert note and "expected" in note.lower()


@pytest.mark.parametrize("command", [
    "git -C /repo diff",
    "git -c user.name=x diff",
    "git --no-pager diff",
    "git --git-dir=/d/.git diff",
])
def test_global_flags_do_not_hide_the_subcommand(command):
    """`git -C <path> diff` read "/path" as the subcommand and lost the note."""
    assert _note(command), f"subcommand not resolved through global flags: {command}"


# ── exit 1 is a real failure ─────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git push origin main",
    "git commit -m 'x'",
    "git merge feature",
    "git pull",
    "git fetch origin",
    "git rebase main",
    "git clone https://example.invalid/r.git",
    "git apply patch.diff",
    "git -C /repo push origin main",
])
def test_real_failures_get_no_reassurance(command):
    assert _note(command) is None, (
        f"{command!r} exiting 1 is a failure; a note saying otherwise invites "
        "the model to treat it as done"
    )


# ── unchanged behaviour ──────────────────────────────────────────────────────

def test_success_never_annotated():
    assert _interpret_exit_code("git push origin main", 0) is None
    assert _interpret_exit_code("git diff", 0) is None


def test_other_commands_keep_their_notes():
    assert "No matches" in (_note("grep needle file") or "")
    assert "false" in (_note("test -f missing") or "").lower()


def test_pipeline_uses_the_last_command():
    """The last command in a chain determines the exit code."""
    assert _note("echo hi | git diff") is not None
    assert _note("git diff && git push origin main") is None


@pytest.mark.parametrize("code", [2, 128, 129])
def test_other_git_exit_codes_are_not_explained_away(code):
    assert _interpret_exit_code("git diff", code) is None
