"""Kanban workers preserve provider-quota failures as temporary exits."""

import pytest

import cli
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE


@pytest.mark.parametrize("failure_reason", ["rate_limit", "billing"])
def test_kanban_worker_provider_quota_exits_tempfail(monkeypatch, failure_reason):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_rate_limited")

    assert (
        cli._kanban_worker_rate_limit_exit_code(
            {"failed": True, "failure_reason": failure_reason}
        )
        == KANBAN_RATE_LIMIT_EXIT_CODE
    )


@pytest.mark.parametrize(
    "result",
    [
        {"failed": False, "failure_reason": "rate_limit"},
        {"failed": True, "failure_reason": "provider_error"},
        None,
    ],
)
def test_kanban_worker_non_quota_result_keeps_normal_exit(monkeypatch, result):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_rate_limited")

    assert cli._kanban_worker_rate_limit_exit_code(result) is None


def test_non_kanban_quota_failure_keeps_normal_exit(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    assert (
        cli._kanban_worker_rate_limit_exit_code(
            {"failed": True, "failure_reason": "rate_limit"}
        )
        is None
    )


def test_human_readable_worker_prints_summary_before_tempfail(monkeypatch):
    calls = []

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.session_id = "kanban-worker-session"
            self.agent = type(
                "Agent",
                (),
                {"session_id": self.session_id, "platform": "cli"},
            )()
            self.console = type(
                "Console",
                (),
                {"print": lambda _self, *args, **kwargs: calls.append(
                    ("console", args, kwargs)
                )},
            )()
            self._kanban_worker_exit_code = None

        def _claim_active_session(self, surface, *, stderr=False):
            calls.append(("claim", surface, stderr))
            return True

        def _show_security_advisories(self):
            calls.append("advisories")

        def chat(self, query, images=None):
            calls.append(("chat", query, images))
            self._kanban_worker_exit_code = KANBAN_RATE_LIMIT_EXIT_CODE

        def _print_exit_summary(self, clear_screen=True):
            calls.append(("summary", clear_screen))

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_rate_limited")
    monkeypatch.setattr(cli, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_finalize_single_query",
        lambda fake_cli: calls.append(("finalize", fake_cli.session_id)),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="work kanban task t_rate_limited", quiet=False)

    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE
    assert calls.index(("summary", False)) < calls.index(
        ("finalize", "kanban-worker-session")
    )
