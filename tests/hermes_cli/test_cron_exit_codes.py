"""Tests that cron_command exit codes propagate through cmd_cron.

Issue #4671 — ``cron remove <job_id>`` that fails (job not found) printed
an error message but exited 0 because ``cmd_cron()`` dropped the return
value from ``cron_command()``.

Every ``cmd_*`` function should forward its handler's return code (which
the main dispatch loop at line 14028 reads via ``args.func(args)``).  A
dropped return means None, which is not isinstance(int), so the process
always exits 0 — even when the underlying command signals failure.
"""

from unittest.mock import patch

import pytest


def test_cmd_cron_propagates_return_code_via_func():
    """cmd_cron must return the int that cron_command returns, not None.

    The main dispatch at main.py:14028 does ``rc = args.func(args)`` and
    only exits non-zero when rc is isinstance(int) and rc != 0.  If
    cmd_cron drops the return, rc is None and the exit code is always 0.
    """
    from hermes_cli.main import cmd_cron

    args = type(
        "Args",
        (),
        {"cron_command": "list", "all": False, "__dict__": {"cron_command": "list", "all": False}},
    )()

    # Patch cron_command at the module where cmd_cron imports it.
    with patch("hermes_cli.cron.cron_command", return_value=42) as mock:
        rc = cmd_cron(args)

    assert rc == 42, (
        f"Expected cmd_cron to return 42 (the cron_command return), "
        f"got {rc!r}.  This means cmd_cron drops the return value — "
        f"add 'return' before cron_command(args)."
    )


class TestCmdCronExitCodePropagation:
    """E2E-style: verify that every failing cron subcommand path that returns a
    non-zero int actually surfaces it through cmd_cron.

    Uses the real cron_command dispatch but targets a non-existent job to
    produce a failure return code without needing an actual cron environment.
    """

    def test_remove_job_not_found_returns_nonzero_via_cmd_cron(self):
        """cron_command with subcmd="remove" for a non-existent job returns 1
        (printed error).  cmd_cron must forward that 1."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {
                "cron_command": "remove",
                "job_id": "non-existent-job-id-12345",
                "all": False,
                "__dict__": {},
            },
        )()

        rc = cmd_cron(args)

        assert rc == 1, (
            f"Expected cmd_cron to return 1 (remove of unknown job), "
            f"got {rc!r}.  cmd_cron must 'return cron_command(args)'."
        )

    def test_edit_job_not_found_returns_nonzero_via_cmd_cron(self):
        """cron_command with subcmd="edit" for a non-existent job returns 1."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {
                "cron_command": "edit",
                "job_id": "non-existent-job-12345",
                "all": False,
                "schedule": None,
                "prompt": None,
                "name": None,
                "deliver": None,
                "repeat": None,
                "skill": None,
                "skills": None,
                "clear_skills": False,
                "add_skills": None,
                "remove_skills": None,
                "script": None,
                "workdir": None,
                "no_agent": None,
                "__dict__": {},
            },
        )()

        rc = cmd_cron(args)

        assert rc == 1, (
            f"Expected cmd_cron to return 1 (edit of unknown job), "
            f"got {rc!r}."
        )

    def test_notepad_missing_job_id_returns_nonzero_via_cmd_cron(self):
        """cron_command with subcmd="notepad" and no job_id returns 1 through cmd_cron."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {
                "cron_command": "notepad",
                "job_id": "",
                "notepad_action": "set",
                "key": "foo",
                "value": "bar",
                "text": None,
                "clear": False,
                "__dict__": {},
            },
        )()

        rc = cmd_cron(args)

        assert rc == 1, (
            f"Expected cmd_cron to return 1 (notepad with empty job_id), "
            f"got {rc!r}."
        )

    def test_create_failure_returns_nonzero_via_cmd_cron(self):
        """cron_command with subcmd="create" that fails returns 1 through cmd_cron."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {
                "cron_command": "create",
                "schedule": "every day",
                "prompt": "",
                "name": None,
                "deliver": None,
                "repeat": None,
                "skill": None,
                "skills": None,
                "script": None,
                "workdir": None,
                "no_agent": False,
                "__dict__": {},
            },
        )()

        rc = cmd_cron(args)

        assert rc == 1, (
            f"Expected cmd_cron to return 1 (create with empty prompt), "
            f"got {rc!r}."
        )

    def test_list_success_returns_zero_via_cmd_cron(self):
        """A successful cron_command path (list) must still return 0 through cmd_cron."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {"cron_command": "list", "all": False, "__dict__": {"cron_command": "list", "all": False}},
        )()

        rc = cmd_cron(args)

        assert rc == 0, (
            f"Expected cmd_cron to return 0 (successful list), "
            f"got {rc!r}."
        )

    def test_status_success_returns_zero_via_cmd_cron(self):
        """A successful cron_command path (status) must still return 0 through cmd_cron."""
        from hermes_cli.main import cmd_cron

        args = type(
            "Args",
            (),
            {"cron_command": "status", "__dict__": {"cron_command": "status"}},
        )()

        rc = cmd_cron(args)

        assert rc == 0, (
            f"Expected cmd_cron to return 0 (successful status), "
            f"got {rc!r}."
        )


def test_actual_dispatch_propagates_cron_exit_code():
    """Integration: run through args.func(args) pattern like main() does.

    This validates that even the real argparse wiring (set_defaults(func=...))
    surfaces a non-zero exit from cron_command.
    """
    from hermes_cli.subcommands.cron import build_cron_parser
    from hermes_cli.main import cmd_cron

    parser = type("Parser", (), {"subparsers": None})()

    def fake_set_defaults(**kw):
        parser.func = kw["func"]

    def fake_parse(args):
        ns = type("NS", (), {"cron_command": "remove", "job_id": "no-such-job-99999", "func": parser.func, "__dict__": {}})()
        return ns

    import argparse
    real_parser = argparse.ArgumentParser(prog="hermes")
    subparsers = real_parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cmd_cron)

    args = real_parser.parse_args(["cron", "remove", "no-such-job-99999"])
    rc = args.func(args)

    assert rc == 1, (
        f"Expected args.func(args) to return 1 for remove of unknown job, "
        f"got {rc!r}."
    )