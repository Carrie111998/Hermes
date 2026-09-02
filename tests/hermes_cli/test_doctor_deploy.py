"""Tests for `hermes doctor deploy` — the standalone deploy-verification command
(deploy-discipline t_beb21efa §A).

Covers: listing discovered processes, flagging STALE when a process's
HERMES_AGENT_HEAD differs from the current install HEAD (including the
process-started-before-current-HEAD case), passing when all are current,
failing closed when the install HEAD cannot be resolved, and passing when no
processes are running.
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli.doctor_deploy import run_doctor_deploy
from hermes_cli.process_discovery import AgentProcess


def _d_proc(pid: int, kind: str = "serve", head: str | None = "abc123") -> AgentProcess:
    return AgentProcess(
        pid=pid,
        kind=kind,
        start_time=1000.0,
        head_at_start=head,
    )


class TestDoctorDeploy:
    def test_zero_stale_passes(self, capsys):
        """All processes on current HEAD -> exit 0, 'ok' marks."""
        with (
            patch("hermes_cli.doctor_deploy._discover", return_value=[
                _d_proc(1, "serve", "new456"),
                _d_proc(2, "gateway", "new456"),
            ]),
            patch("hermes_cli.doctor_deploy._current_head", return_value="new456"),
        ):
            rc = run_doctor_deploy()
        out = capsys.readouterr().out
        assert rc == 0
        assert "No stale processes" in out

    def test_stale_process_started_before_current_head_fails(self, capsys):
        """A process whose HEAD-at-start predates current HEAD -> STALE, exit 1.

        This is the canonical deploy-discipline case: a fix is merged (HEAD
        advances) but a long-lived process is still running the pre-fix code.
        """
        with (
            patch("hermes_cli.doctor_deploy._discover", return_value=[
                # Started on the OLD code; current HEAD is the new fix.
                _d_proc(1, "serve", "old123"),
                _d_proc(2, "gateway", "new456"),
            ]),
            patch("hermes_cli.doctor_deploy._current_head", return_value="new456"),
        ):
            rc = run_doctor_deploy()
        out = capsys.readouterr().out
        assert rc == 1
        assert "STALE" in out
        assert "1 process" in out

    def test_unknown_head_not_treated_as_stale(self, capsys):
        """A process with no readable HERMES_AGENT_HEAD is '?', not a hard fail."""
        with (
            patch("hermes_cli.doctor_deploy._discover", return_value=[
                _d_proc(1, "serve", None),
            ]),
            patch("hermes_cli.doctor_deploy._current_head", return_value="new456"),
        ):
            rc = run_doctor_deploy()
        out = capsys.readouterr().out
        assert rc == 0
        assert "?" in out

    def test_unresolvable_head_fails_closed(self, capsys):
        """Cannot resolve current HEAD -> non-zero (verifier cannot attest clean)."""
        with (
            patch("hermes_cli.doctor_deploy._discover", return_value=[]),
            patch("hermes_cli.doctor_deploy._current_head", return_value=None),
        ):
            rc = run_doctor_deploy()
        out = capsys.readouterr().out
        assert rc == 1
        assert "Cannot resolve current install HEAD" in out

    def test_no_processes_passes(self, capsys):
        """No running long-lived processes -> exit 0 (nothing verifiable)."""
        with (
            patch("hermes_cli.doctor_deploy._discover", return_value=[]),
            patch("hermes_cli.doctor_deploy._current_head", return_value="new456"),
        ):
            rc = run_doctor_deploy()
        out = capsys.readouterr().out
        assert rc == 0
        assert "No running hermes-agent long-lived processes" in out
