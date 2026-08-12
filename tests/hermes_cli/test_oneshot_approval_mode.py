"""Approval policy tests for ``hermes -z`` one-shot sessions."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest


_UNREADABLE = object()


def _write_config(home: Path, content: str | None | object) -> None:
    home.mkdir()
    if content is _UNREADABLE:
        (home / "config.yaml").mkdir()
    elif content is not None:
        (home / "config.yaml").write_text(content, encoding="utf-8")


def _invoke_oneshot_sinks(
    monkeypatch,
    tmp_path: Path,
    config: str | None | object,
    *,
    suppress_user_config: str | None = None,
) -> dict:
    """Run the public one-shot entry point and both real execution sinks."""
    home = tmp_path / "hermes-home"
    _write_config(home, config)
    terminal_target = tmp_path / "terminal-target"
    execute_target = tmp_path / "execute-target"
    terminal_target.write_text("keep", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    if suppress_user_config is not None:
        monkeypatch.setenv(suppress_user_config, "1")

    # Reproduce the plugin-import ordering concern: approval freezes inherited
    # YOLO before the one-shot invocation resolves its own policy.
    from tools import approval
    from tools import code_execution_tool, terminal_tool

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    observed: dict = {}

    def invoke_real_sinks(*_args, **_kwargs):
        observed["terminal"] = json.loads(
            terminal_tool.terminal_tool(f"rm -rf {terminal_target}")
        )
        observed["execute_code"] = json.loads(
            code_execution_tool.execute_code(
                f"from pathlib import Path\nPath({str(execute_target)!r}).write_text('ran')"
            )
        )
        return "ok", {"final_response": "ok"}

    import hermes_cli.oneshot as oneshot

    monkeypatch.setattr(oneshot, "_run_agent", invoke_real_sinks)
    observed["returncode"] = oneshot.run_oneshot("exercise approval sinks")
    from hermes_cli.oneshot_policy import current_oneshot_yolo_policy

    observed["policy_after"] = current_oneshot_yolo_policy()
    observed["yolo_after"] = os.environ.get("HERMES_YOLO_MODE")
    observed["terminal_target_exists"] = terminal_target.exists()
    observed["execute_target_exists"] = execute_target.exists()
    return observed


@pytest.mark.parametrize(
    "config",
    [
        None,
        "approvals:\n  oneshot_yolo: false\n",
        "approvals:\n  oneshot_yolo: 'true'\n",
        "approvals:\n  oneshot_yolo: 1\n",
        "approvals: [unterminated\n",
        _UNREADABLE,
    ],
    ids=[
        "missing",
        "false",
        "string-true",
        "integer-one",
        "malformed",
        "unreadable",
    ],
)
def test_oneshot_invocation_blocks_both_sinks_without_exact_true(
    monkeypatch, tmp_path, config
):
    observed = _invoke_oneshot_sinks(monkeypatch, tmp_path, config)

    assert observed["returncode"] == 0
    assert observed["terminal"]["status"] == "blocked"
    assert observed["execute_code"]["status"] == "error"
    assert observed["policy_after"] is None
    assert observed["yolo_after"] == "1"
    assert observed["terminal_target_exists"] is True
    assert observed["execute_target_exists"] is False


def test_oneshot_invocation_allows_both_sinks_for_exact_true(monkeypatch, tmp_path):
    observed = _invoke_oneshot_sinks(
        monkeypatch,
        tmp_path,
        "approvals:\n  oneshot_yolo: true\n",
    )

    assert observed["returncode"] == 0
    assert observed["policy_after"] is None
    assert observed["yolo_after"] == "1"
    assert observed["terminal"]["exit_code"] == 0
    assert observed["terminal_target_exists"] is False
    assert "one-shot mode" not in observed["execute_code"].get("error", "")


@pytest.mark.parametrize(
    "suppress_user_config",
    ["HERMES_IGNORE_USER_CONFIG", "HERMES_SAFE_MODE"],
    ids=["ignore-user-config", "safe-mode"],
)
def test_oneshot_invocation_suppresses_opt_in_with_user_config_disabled(
    monkeypatch, tmp_path, suppress_user_config
):
    observed = _invoke_oneshot_sinks(
        monkeypatch,
        tmp_path,
        "approvals:\n  oneshot_yolo: true\n",
        suppress_user_config=suppress_user_config,
    )

    assert observed["returncode"] == 0
    assert observed["terminal"]["status"] == "blocked"
    assert observed["execute_code"]["status"] == "error"
    assert observed["terminal_target_exists"] is True
    assert observed["execute_target_exists"] is False


@pytest.mark.parametrize("configured_value", ["false", "true"])
def test_oneshot_policy_reaches_both_sinks_in_plain_worker(
    monkeypatch, tmp_path, configured_value
):
    """An unwrapped worker neither fails open nor inherits an allow capability."""
    home = tmp_path / "hermes-home"
    _write_config(
        home,
        f"approvals:\n  oneshot_yolo: {configured_value}\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

    from hermes_cli.oneshot_policy import (
        configure_oneshot_approval_policy,
        current_oneshot_yolo_policy,
        reset_oneshot_approval_policy,
    )
    from tools import approval

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    observed = {}

    def worker():
        observed["policy"] = current_oneshot_yolo_policy()
        observed["terminal"] = approval.check_all_command_guards(
            "rm -rf ./worker-target", "local"
        )
        observed["execute_code"] = approval.check_execute_code_guard(
            "from pathlib import Path; Path('worker-target').unlink()", "local"
        )

    policy_token = configure_oneshot_approval_policy()
    assert policy_token is not None
    try:
        assert current_oneshot_yolo_policy() is (configured_value == "true")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        reset_oneshot_approval_policy(policy_token)

    assert observed["policy"] is False
    assert observed["terminal"]["approved"] is False
    assert observed["execute_code"]["approved"] is False


@pytest.mark.parametrize(
    ("oneshot_policy", "frozen_yolo", "expected"),
    [
        (False, True, False),
        (True, False, True),
        (None, True, True),
        (None, False, False),
    ],
    ids=[
        "oneshot-deny-overrides-inherited",
        "oneshot-exact-true",
        "normal-yolo",
        "normal-no-yolo",
    ],
)
def test_session_persistence_uses_invocation_policy(
    monkeypatch, oneshot_policy, frozen_yolo, expected
):
    """A fail-closed one-shot cannot persist inherited YOLO for later resume."""
    from agent import agent_init
    from hermes_cli import oneshot_policy as policy_module
    from tools import approval

    monkeypatch.setattr(
        policy_module, "current_oneshot_yolo_policy", lambda: oneshot_policy
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", frozen_yolo)

    assert agent_init._should_persist_initial_yolo_mode() is expected


@pytest.mark.parametrize("termux", [False, True], ids=["normal", "termux"])
def test_oneshot_policy_is_resolved_before_agent_startup(
    monkeypatch, tmp_path, termux
):
    import hermes_cli.main as main_mod
    from hermes_cli import oneshot_policy

    seen = []

    def capture_startup(_args):
        from hermes_cli.oneshot_policy import current_oneshot_yolo_policy

        seen.append(current_oneshot_yolo_policy())

    monkeypatch.setattr(main_mod, "_prepare_agent_startup", capture_startup)

    def stop_after_capture(*_args, **_kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(main_mod, "_run_and_exit_oneshot", stop_after_capture)
    monkeypatch.setattr(main_mod, "_is_termux_startup_environment", lambda: termux)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr(sys, "argv", ["hermes", "-z", "hello"])

    context_token = oneshot_policy._ONESHOT_YOLO_POLICY.set(None)
    monkeypatch.setattr(oneshot_policy, "_PROCESS_ONESHOT_YOLO_POLICY", None)
    try:
        with pytest.raises(SystemExit, match="0"):
            main_mod.main()
    finally:
        oneshot_policy._ONESHOT_YOLO_POLICY.reset(context_token)

    assert seen == [False]


@pytest.mark.parametrize("termux", [False, True], ids=["normal", "termux"])
@pytest.mark.parametrize(
    "option",
    ["--ignore-user-config", "--safe-mode"],
    ids=["ignore-user-config", "safe-mode"],
)
def test_oneshot_startup_options_suppress_config_opt_in_before_discovery(
    monkeypatch, tmp_path, termux, option
):
    import hermes_cli.main as main_mod
    from hermes_cli import oneshot_policy

    (tmp_path / "config.yaml").write_text(
        "approvals:\n  oneshot_yolo: true\n", encoding="utf-8"
    )
    seen = []

    def capture_startup(_args):
        seen.append(oneshot_policy.current_oneshot_yolo_policy())

    monkeypatch.setattr(main_mod, "_prepare_agent_startup", capture_startup)
    monkeypatch.setattr(
        main_mod,
        "_run_and_exit_oneshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setattr(main_mod, "_is_termux_startup_environment", lambda: termux)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr(sys, "argv", ["hermes", "-z", "hello", option])

    context_token = oneshot_policy._ONESHOT_YOLO_POLICY.set(None)
    monkeypatch.setattr(oneshot_policy, "_PROCESS_ONESHOT_YOLO_POLICY", None)
    try:
        with pytest.raises(SystemExit, match="0"):
            main_mod.main()
    finally:
        oneshot_policy._ONESHOT_YOLO_POLICY.reset(context_token)

    assert seen == [False]
