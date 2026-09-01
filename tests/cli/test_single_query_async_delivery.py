"""Single-query CLI sessions cannot receive detached completions."""

from __future__ import annotations

from argparse import Namespace
import json
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("quiet", "oneshot", "stdin_tty"),
    [
        pytest.param(True, False, True, id="quiet"),
        pytest.param(False, True, True, id="query-file"),
        pytest.param(False, False, False, id="non-tty-query"),
    ],
)
def test_single_query_cli_disables_async_delivery(
    monkeypatch, quiet, oneshot, stdin_tty
):
    import cli as cli_mod
    from gateway import session_context
    import tools.delegate_tool as delegate_tool

    results = []
    dispatched = []

    class Parent:
        _delegate_depth = 0
        _subagent_id = None

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.session_id = "single-query-test"

        def _claim_active_session(self, _surface, *, stderr=False):
            assert "HERMES_KANBAN_TASK" not in cli_mod.os.environ
            results.append(
                json.loads(
                    delegate_tool.delegate_task(
                        goal="review the spec",
                        background=True,
                        parent_agent=Parent(),
                    )
                )
            )
            return False

    fake_child = SimpleNamespace(_subagent_id="subagent-0")
    credentials = {
        "model": "test-model",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "command": None,
        "args": None,
    }

    session_context.reset_session_vars()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: fake_child)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda task_index, goal, *_args, **_kwargs: {
            "task_index": task_index,
            "status": "completed",
            "summary": f"done: {goal}",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "test-model",
            "exit_reason": "completed",
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: credentials,
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda *_args, **_kwargs: dispatched.append(True),
    )
    monkeypatch.setattr(cli_mod.sys, "stdin", SimpleNamespace(isatty=lambda: stdin_tty))

    try:
        with pytest.raises(SystemExit):
            cli_mod.main(
                query="review this",
                quiet=quiet,
                oneshot=oneshot,
                toolsets="terminal",
            )
    finally:
        session_context.reset_session_vars()

    assert not dispatched
    assert results[0]["results"][0]["summary"] == "done: review the spec"


def test_query_file_is_single_query_without_extra_flag(tmp_path, monkeypatch):
    import hermes_cli.main as main_mod

    query_file = tmp_path / "query.txt"
    query_file.write_text("review this", encoding="utf-8")
    captured = {}
    tui_launched = []

    args = Namespace(
        query=None,
        query_file=str(query_file),
        oneshot_exit=False,
        model=None,
        toolsets=None,
        tui=False,
        quiet=False,
    )
    monkeypatch.setenv("HERMES_TUI", "1")
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(main_mod.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        main_mod, "_launch_tui", lambda *_args, **_kwargs: tui_launched.append(True)
    )
    monkeypatch.setattr(main_mod, "_apply_safe_mode", lambda _args: None)
    monkeypatch.setattr(main_mod, "_resolve_continue_arg", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(
        main_mod, "_confirm_startup_expensive_model_override", lambda _args: None
    )
    monkeypatch.setitem(sys.modules, "cli", SimpleNamespace(main=captured.update))

    main_mod.cmd_chat(args)

    assert not tui_launched
    assert captured["query"] == "review this"
    assert captured["oneshot"] is True
