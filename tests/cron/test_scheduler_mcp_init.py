"""Regression tests for cron MCP-discovery isolation.

Cron jobs must not initialize or enumerate configured MCP servers before the
agent is constructed. ``enabled_toolsets`` / ``no_mcp`` is a model-visible
allowlist and is too late to prevent MCP startup side effects.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch


def test_no_agent_cron_job_does_not_initialize_mcp():
    """Cron jobs with no_agent=True are script-only — no AIAgent, no MCP
    tools needed. We must NOT pay the MCP init cost for those."""
    from cron import scheduler

    job = {
        "id": "noagent-job",
        "name": "noagent-job",
        "no_agent": True,
        "script": "/nonexistent/script.sh",
    }

    discover_called = []

    def fake_discover():
        discover_called.append(True)
        return []

    # _run_job_script returns (ok, output); make it fail cleanly so we
    # don't need a real script file.
    with patch("tools.mcp_tool.discover_mcp_tools", side_effect=fake_discover), \
         patch("cron.scheduler._run_job_script", return_value=(False, "no such file")):
        scheduler.run_job(job)

    assert not discover_called, (
        "discover_mcp_tools was called for a no_agent job — wasted MCP init "
        "for a script-only cron tick"
    )


def test_llm_cron_scheduler_path_does_not_discover_mcp(monkeypatch):
    """The LLM cron construction boundary must bypass MCP discovery too."""
    from cron import scheduler

    discover_called = []

    def fake_discover():
        discover_called.append(True)
        return ["mcp_should_not_be_seen"]

    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", fake_discover)
    # The guard belongs in the scheduler's LLM path, not only in the job
    # metadata. Inspect the complete function source so a future reintroduction
    # of the startup call fails this regression test even if it is wrapped or
    # moved away from the current line number.
    source = inspect.getsource(scheduler.run_job)
    assert "discover_mcp_tools()" not in source
    assert "discover_mcp_tools" not in source
    assert "MCP discovery is intentionally skipped for cron jobs" in source
    assert not discover_called
