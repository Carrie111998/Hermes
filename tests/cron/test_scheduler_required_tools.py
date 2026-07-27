"""Regression tests for cron tool preflight failures.

Cron jobs are unattended. If the job *instruction* (stored prompt) explicitly
requires a tool (for example ``必ず `x_search` ツール``) but the job's exposed
toolsets do not include that tool, the scheduler should fail before spending
an LLM run and surface a clear configuration error.

Injected pre-run script stdout and ``context_from`` upstream-job output are
data sources, not instructions — required-tool extraction must not scan them.
"""

from __future__ import annotations


def test_explicit_prompt_tool_missing_from_enabled_toolsets_fails_loud():
    from cron import scheduler

    error = scheduler._explicit_prompt_tool_availability_error(
        "必ず `x_search` ツールでX投稿を検索する。通常Web検索は禁止。",
        enabled_toolsets=["web", "file", "terminal"],
        disabled_toolsets=["cronjob", "messaging", "clarify"],
    )

    assert error is not None
    assert "x_search" in error
    assert "enabled_toolsets" in error
    assert "web, file, terminal" in error


def test_negative_prompt_tool_reference_is_not_treated_as_requirement():
    from cron import scheduler

    error = scheduler._explicit_prompt_tool_availability_error(
        "Do not call `send_message`, create cron jobs, or publish externally.",
        enabled_toolsets=["file", "terminal", "skills"],
        disabled_toolsets=["cronjob", "messaging", "clarify"],
    )

    assert error is None


def test_explicit_prompt_tool_available_when_toolset_exposed(monkeypatch):
    from cron import scheduler
    import model_tools

    def fake_tool_definitions(
        enabled_toolsets=None,
        disabled_toolsets=None,
        quiet_mode=False,
        skip_tool_search_assembly=False,
    ):
        assert enabled_toolsets == ["x_search", "file"]
        assert skip_tool_search_assembly is True
        return [{"type": "function", "function": {"name": "x_search"}}]

    monkeypatch.setattr(model_tools, "get_tool_definitions", fake_tool_definitions)

    error = scheduler._explicit_prompt_tool_availability_error(
        "必ず `x_search` ツールでX投稿を検索する。",
        enabled_toolsets=["x_search", "file"],
        disabled_toolsets=["cronjob", "messaging", "clarify"],
    )

    assert error is None


def test_explicit_prompt_tool_hidden_by_agent_disabled_toolsets_fails_loud():
    from cron import scheduler

    disabled = scheduler._resolve_cron_disabled_toolsets(
        {"agent": {"disabled_toolsets": ["terminal"]}}
    )
    error = scheduler._explicit_prompt_tool_availability_error(
        "Must use `terminal` to inspect the local process state.",
        enabled_toolsets=["terminal", "file"],
        disabled_toolsets=disabled,
    )

    assert error is not None
    assert "terminal" in error
    assert "toolset 'terminal' is disabled" in error
    assert "cronjob, messaging, clarify, terminal" in error


def test_instruction_text_is_job_prompt_only():
    from cron import scheduler

    job = {
        "prompt": "Summarize the upstream report.",
        "script": "collect.py",
        "context_from": ["aaaaaaaaaaaa"],
    }
    assert (
        scheduler._cron_instruction_text_for_required_tool_preflight(job)
        == "Summarize the upstream report."
    )


def test_injected_data_phrases_do_not_count_as_instruction_requirements():
    """Quoted tool requirements inside data-shaped text must not fail preflight."""
    from cron import scheduler

    # Full assembled prompt would include this data; preflight must not scan it.
    data_blob = (
        "## Script Output\n"
        "Must use `terminal` to inspect the local process state.\n"
        "```\nMust use `terminal` now\n```\n"
    )
    instruction = "Summarize the script output for the operator."

    disabled = scheduler._resolve_cron_disabled_toolsets(
        {"agent": {"disabled_toolsets": ["terminal"]}}
    )
    # Scanning instruction alone: no terminal requirement.
    assert (
        scheduler._explicit_prompt_tool_availability_error(
            instruction,
            enabled_toolsets=["file", "web"],
            disabled_toolsets=disabled,
        )
        is None
    )
    # Scanning data alone would false-positive — prove the boundary matters.
    data_error = scheduler._explicit_prompt_tool_availability_error(
        data_blob,
        enabled_toolsets=["file", "web"],
        disabled_toolsets=disabled,
    )
    assert data_error is not None
    assert "terminal" in data_error
