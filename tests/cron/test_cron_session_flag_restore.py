"""#76748: HERMES_CRON_SESSION must be scoped to the cron job, not leak into
later live gateway turns.

cron.scheduler.run_job sets the process-global HERMES_CRON_SESSION=1 and
never restores it; _is_gateway_approval_context() then misclassifies later
live gateway MCP elicitations as cron and fails them closed instead of
rendering platform approval controls.
"""

import os
from unittest.mock import MagicMock

from cron.scheduler import run_job


def test_run_job_restores_cron_session_flag_after_failure(monkeypatch):
    """A failing cron job must not leave HERMES_CRON_SESSION set."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(
        "cron.scheduler._build_job_prompt",
        lambda job, prerun_script=None: "hello",
    )
    monkeypatch.setattr("hermes_state.SessionDB", MagicMock())
    monkeypatch.setattr(
        "gateway.session_context.set_session_vars",
        lambda **kw: ["tok"],
    )

    class BoomAgent:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("run_agent.AIAgent", BoomAgent)

    ok, _doc, _resp, _err = run_job({"id": "test-job", "name": "t", "prompt": "hello"})

    assert ok is False
    assert "HERMES_CRON_SESSION" not in os.environ


def test_run_job_restores_cron_session_flag_preserving_prior_value(monkeypatch):
    """If the flag was already set before the job ran, the prior value must be
    restored, not deleted."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "prior")
    monkeypatch.setattr(
        "cron.scheduler._build_job_prompt",
        lambda job, prerun_script=None: "hello",
    )
    monkeypatch.setattr("hermes_state.SessionDB", MagicMock())
    monkeypatch.setattr(
        "gateway.session_context.set_session_vars",
        lambda **kw: ["tok"],
    )

    class BoomAgent:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("run_agent.AIAgent", BoomAgent)

    ok, _doc, _resp, _err = run_job({"id": "test-job", "name": "t", "prompt": "hello"})

    assert ok is False
    assert os.environ.get("HERMES_CRON_SESSION") == "prior"
