"""Regression tests for post-update ``git gc --auto`` (#58172).

Kept in a dedicated module so the fix does not need to touch the large
``test_cmd_update.py`` file (whose pre-existing fixtures trip the
whole-file windows-footgun scan).
"""

from unittest.mock import patch


def test_run_git_auto_gc_invokes_git_gc_auto():
    """Post-update maintenance runs ``git gc --auto`` to bound .git growth (#58172)."""
    from hermes_cli import update_cmd

    with patch.object(update_cmd.subprocess, "run") as mock_run:
        update_cmd._run_git_auto_gc(["git"], "/repo")

    assert mock_run.call_count == 1
    args, kwargs = mock_run.call_args
    assert args[0] == ["git", "gc", "--auto"]
    assert kwargs["cwd"] == "/repo"
    # Must never raise / abort the update on a nonzero gc exit.
    assert kwargs["check"] is False


def test_run_git_auto_gc_swallows_os_error():
    """A git launch failure must not propagate and fail the update."""
    from hermes_cli import update_cmd

    with patch.object(update_cmd.subprocess, "run", side_effect=OSError("git missing")):
        # Should return cleanly rather than raising.
        assert update_cmd._run_git_auto_gc(["git"], "/repo") is None
