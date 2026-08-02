"""Primary local-agent provider contracts (no live vendor calls)."""

from __future__ import annotations

import json
from contextvars import ContextVar
import os
from pathlib import Path
import time

import pytest


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", 0, -1])
def test_provider_timeout_must_be_positive_and_finite(
    monkeypatch: pytest.MonkeyPatch, value
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, provider_timeout

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_config",
        lambda _provider: {"timeout": value},
    )

    with pytest.raises(LocalAgentInvocationError, match="positive number"):
        provider_timeout("claude-cli")


def _write_acting_cli(tmp_path: Path, name: str, answer: str) -> tuple[Path, Path]:
    record = tmp_path / f"{name}-record.json"
    executable = tmp_path / name
    if name == "claude":
        output = json.dumps({"type": "result", "is_error": False, "result": answer})
    else:
        output = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": answer},
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, stat, sys\n"
        f"record = {str(record)!r}\n"
        "if '--help' in sys.argv:\n"
        "    print('--print --output-format --no-session-persistence "
        "--permission-mode --model --mcp-config --strict-mcp-config "
        "--setting-sources --tools --allowedTools --disable-slash-commands "
        "--json --ephemeral --sandbox "
        "--ask-for-approval --skip-git-repo-check --color')\n"
        "    raise SystemExit(0)\n"
        "authority_path = None\n"
        "authority = None\n"
        "authority_mode = None\n"
        "if '--append-system-prompt-file' in sys.argv:\n"
        "    authority_path = sys.argv[sys.argv.index('--append-system-prompt-file') + 1]\n"
        "    authority = open(authority_path).read()\n"
        "    authority_mode = stat.S_IMODE(os.stat(authority_path).st_mode)\n"
        "open(record, 'w').write(json.dumps({"
        "'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(), "
        "'authority_path': authority_path, 'authority': authority, "
        "'authority_mode': authority_mode, "
        "'task_env': {k: v for k, v in os.environ.items() "
        "if k.startswith('HERMES_') or k == 'PYTHONPATH'}}))\n"
        f"print({output!r})\n"
    )
    executable.chmod(0o755)
    return executable, record


@pytest.mark.parametrize(
    ("provider", "command", "answer"),
    [
        ("claude-cli", "claude", "Claude acted"),
        ("codex-cli", "codex", "Codex acted"),
    ],
)
def test_primary_cli_uses_native_acting_loop_stdin_and_project_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    answer: str,
) -> None:
    from agent.local_agent_provider import run_cli_acting

    executable, record = _write_acting_cli(tmp_path, command, answer)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == command else None,
    )

    result = run_cli_acting(
        provider=provider,
        model="default",
        messages=[
            {"role": "system", "content": "Follow project instructions."},
            {"role": "user", "content": "Make the requested change."},
        ],
        cwd=str(project),
        timeout=5,
    )

    assert result == answer
    invocation = json.loads(record.read_text())
    assert invocation["cwd"] == str(project)
    assert "Make the requested change." in invocation["stdin"]
    assert "Follow project instructions." in invocation["stdin"]
    assert invocation["argv"][-1:] != ["Make the requested change."]
    if provider == "claude-cli":
        assert invocation["argv"] == [
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
        ]
    else:
        assert "--sandbox" in invocation["argv"]
        assert invocation["argv"][invocation["argv"].index("--sandbox") + 1] == "workspace-write"
        assert "--ignore-rules" not in invocation["argv"]
        assert "--ignore-user-config" not in invocation["argv"]
        assert invocation["argv"][-1] == "-"


def test_primary_cli_timeout_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.cli_emulated_provider import CliTimeoutError
    from agent.local_agent_provider import run_cli_acting

    executable = tmp_path / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--help' in sys.argv:\n"
        "    print('--print --output-format --no-session-persistence "
        "--permission-mode --mcp-config --strict-mcp-config --setting-sources "
        "--tools --allowedTools --disable-slash-commands')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "claude" else None,
    )

    started = time.monotonic()
    with pytest.raises(CliTimeoutError, match="timed out"):
        run_cli_acting(
            provider="claude-cli",
            model="default",
            messages=[{"role": "user", "content": "Wait."}],
            cwd=str(tmp_path),
            timeout=0.1,
        )
    assert time.monotonic() - started < 3


def test_primary_cli_cancellation_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.cli_emulated_provider import CliCancelledError
    from agent.local_agent_provider import run_cli_acting

    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--help' in sys.argv:\n"
        "    print('--json --ephemeral --sandbox --ask-for-approval "
        "--skip-git-repo-check --color')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "codex" else None,
    )
    started = time.monotonic()

    with pytest.raises(CliCancelledError, match="cancelled"):
        run_cli_acting(
            provider="codex-cli",
            model="default",
            messages=[{"role": "user", "content": "Wait."}],
            cwd=str(tmp_path),
            timeout=5,
            cancel_check=lambda: time.monotonic() - started > 0.1,
        )
    assert time.monotonic() - started < 3


def test_cowork_dispatches_normalized_generic_mcp_tool_with_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import run_cowork

    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        "agent.local_agent_provider.discover_mcp_tools",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda names=None, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda name, args: calls.append((name, args))
        or json.dumps({"result": "Cowork finished"}),
    )

    result = run_cowork(
        messages=[
            {"role": "system", "content": "Use installed finance skills."},
            {"role": "user", "content": "Build the model."},
        ],
        cwd=str(tmp_path),
        timeout=5,
    )

    assert result == "Cowork finished"
    assert len(calls) == 1
    assert calls[0][0] == "mcp__cowork_mcp__cowork_run"
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert "Use installed finance skills." in calls[0][1]["prompt"]
    assert "Build the model." in calls[0][1]["prompt"]


def test_cowork_uses_actual_generic_registry_dispatch_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent import local_agent_provider
    from tools.registry import ToolRegistry

    isolated_registry = ToolRegistry()
    received = {}

    def handler(args):
        received.update(args)
        return json.dumps({"result": "registry result"})

    isolated_registry.register(
        name="mcp__cowork_mcp__cowork_run",
        toolset="mcp-cowork-mcp",
        schema={
            "name": "mcp__cowork_mcp__cowork_run",
            "description": "Run Cowork",
            "parameters": {"type": "object"},
        },
        handler=handler,
    )
    monkeypatch.setattr(local_agent_provider, "registry", isolated_registry)
    monkeypatch.setattr(local_agent_provider, "discover_mcp_tools", lambda: None)

    result = local_agent_provider.run_cowork(
        messages=[{"role": "user", "content": "Use the finance skill."}],
        cwd=str(tmp_path),
        timeout=5,
    )

    assert result == "registry result"
    assert received["cwd"] == str(tmp_path)
    assert "Use the finance skill." in received["prompt"]


def test_cowork_worker_inherits_profile_and_session_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import run_cowork

    profile_context: ContextVar[str] = ContextVar(
        "test_cowork_profile", default="default"
    )
    profile_context.set("isolated-profile")
    observed: dict[str, str] = {}
    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )

    def dispatch(_name, _args):
        observed["profile"] = profile_context.get()
        return json.dumps({"result": "context preserved"})

    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        dispatch,
    )

    assert run_cowork(
        messages=[{"role": "user", "content": "Work."}],
        cwd=str(tmp_path),
        timeout=5,
    ) == "context preserved"
    assert observed == {"profile": "isolated-profile"}


def test_cowork_missing_tool_and_timeout_are_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(LocalAgentInvocationError, match="tool is unavailable"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
        )

    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda *_args, **_kwargs: time.sleep(2),
    )
    with pytest.raises(LocalAgentInvocationError, match="remote run may still continue"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=0.05,
        )


def test_cowork_disabled_and_cancelled_fail_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_enabled",
        lambda _provider: False,
    )
    with pytest.raises(LocalAgentInvocationError, match="provider is disabled"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
        )

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_enabled",
        lambda _provider: True,
    )
    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda *_args, **_kwargs: time.sleep(2),
    )
    with pytest.raises(LocalAgentInvocationError, match="remote run may still continue"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
            cancel_check=lambda: True,
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not-json", "malformed"),
        (json.dumps({}), "missing"),
        (json.dumps({"result": "  "}), "empty"),
        (json.dumps({"error": "remote failed"}), "remote failed"),
    ],
)
def test_cowork_rejects_bad_generic_mcp_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    match: str,
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda names=None, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda _name, _args: payload,
    )

    with pytest.raises(LocalAgentInvocationError, match=match):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=5,
        )


# ─── reasoning effort forwarding ────────────────────────────────────────


@pytest.mark.parametrize(
    "provider,reasoning_config,expected",
    [
        # No Hermes effort set: the flag is omitted entirely so the CLI's own
        # configured default stays authoritative (same contract as
        # ``model: default`` omitting --model).
        ("claude-cli", None, None),
        ("codex-cli", None, None),
        ("claude-cli", {}, None),
        # Levels both CLIs accept pass through untouched.
        ("claude-cli", {"enabled": True, "effort": "xhigh"}, "xhigh"),
        ("codex-cli", {"enabled": True, "effort": "xhigh"}, "xhigh"),
        # "ultra" is outside both vocabularies — clamp to each ceiling rather
        # than let Codex forward it and take a 400 from the API.
        ("claude-cli", {"enabled": True, "effort": "ultra"}, "max"),
        ("codex-cli", {"enabled": True, "effort": "ultra"}, "max"),
        # claude --effort has no "none"/"minimal"; both floor onto "low".
        # Codex accepts them as-is.
        ("claude-cli", {"enabled": False}, "low"),
        ("codex-cli", {"enabled": False}, "none"),
        ("claude-cli", {"enabled": True, "effort": "minimal"}, "low"),
        ("codex-cli", {"enabled": True, "effort": "minimal"}, "minimal"),
    ],
)
def test_resolve_cli_effort_clamps_onto_each_cli_vocabulary(
    provider, reasoning_config, expected
) -> None:
    from agent.cli_emulated_provider import resolve_cli_effort

    assert resolve_cli_effort(provider, reasoning_config) == expected


def test_acting_argv_omits_effort_when_unset() -> None:
    from agent.local_agent_provider import _acting_argv

    for provider in ("claude-cli", "codex-cli"):
        argv = _acting_argv("/bin/x", provider, "default", None)
        assert "--effort" not in argv
        assert not any(a.startswith("model_reasoning_effort") for a in argv)


def test_acting_argv_passes_effort_to_each_cli() -> None:
    from agent.local_agent_provider import _acting_argv

    claude = _acting_argv("/bin/claude", "claude-cli", "default", "xhigh")
    assert claude[claude.index("--effort") + 1] == "xhigh"

    # Codex has no dedicated flag; it takes a config override, and everything
    # must land before the trailing "-" stdin marker.
    codex = _acting_argv("/bin/codex", "codex-cli", "default", "xhigh")
    assert codex[codex.index("-c") + 1] == "model_reasoning_effort=xhigh"
    assert codex[-1] == "-"


def test_acting_argv_carries_effort_and_model_together() -> None:
    from agent.local_agent_provider import _acting_argv

    codex = _acting_argv("/bin/codex", "codex-cli", "gpt-5.5", "high")
    assert codex[codex.index("--model") + 1] == "gpt-5.5"
    assert codex[codex.index("-c") + 1] == "model_reasoning_effort=high"
    assert codex[-1] == "-"

    claude = _acting_argv("/bin/claude", "claude-cli", "opus", "high")
    assert claude[claude.index("--model") + 1] == "opus"
    assert claude[claude.index("--effort") + 1] == "high"


@pytest.mark.parametrize(
    ("provider", "command", "expected"),
    [
        ("claude-cli", "claude", ["--effort", "xhigh"]),
        ("codex-cli", "codex", ["-c", "model_reasoning_effort=xhigh"]),
    ],
)
def test_primary_cli_turn_forwards_reasoning_effort_to_the_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    expected: list[str],
) -> None:
    """End-to-end: a Hermes effort must reach the spawned CLI's argv.

    Guards the original defect — effort is a wire parameter for every HTTP
    provider, and this path spawns a subprocess instead, so it silently
    dropped the setting.
    """
    from agent.local_agent_provider import run_cli_acting

    executable, record = _write_acting_cli(tmp_path, command, "done")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == command else None,
    )

    run_cli_acting(
        provider=provider,
        model="default",
        messages=[{"role": "user", "content": "Work."}],
        cwd=str(project),
        timeout=5,
        reasoning_config={"enabled": True, "effort": "xhigh"},
    )

    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index(expected[0]) + 1] == expected[1]


def _set_task_claude_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    profile: str,
) -> dict[str, str]:
    profile_home = tmp_path / "profiles" / profile
    profile_home.mkdir(parents=True)
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    values = {
        "HERMES_HOME": str(profile_home),
        "HERMES_KANBAN_TASK": "t_reviewed",
        "HERMES_KANBAN_RUN_ID": "41",
        "HERMES_KANBAN_CLAIM_LOCK": "host:worker",
        "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
        "HERMES_KANBAN_BOARD": "product-board",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(workspaces),
        "HERMES_PROFILE": profile,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_task_scoped_claude_requires_no_governed_memory_env():
    from agent.local_agent_provider import (
        _CLAUDE_INTAKE_REQUIRED_ENV,
        _CLAUDE_TASK_REQUIRED_ENV,
    )

    for required in (_CLAUDE_TASK_REQUIRED_ENV, _CLAUDE_INTAKE_REQUIRED_ENV):
        assert not [key for key in required if key.startswith("HERMES_AGENT_MEMORY_")]


@pytest.mark.parametrize(
    ("profile", "capability_set", "required_tool", "forbidden_tool"),
    [
        (
            "productowner",
            "product-owner",
            "mcp__hermes-tools__kanban_create",
            "mcp__hermes-tools__review_target",
        ),
        (
            "reviewer",
            "reviewer",
            "mcp__hermes-tools__review_target",
            "mcp__hermes-tools__kanban_create",
        ),
    ],
)
def test_task_scoped_claude_uses_strict_role_mcp_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    capability_set: str,
    required_tool: str,
    forbidden_tool: str,
) -> None:
    from agent.local_agent_provider import run_cli_acting

    executable, record = _write_acting_cli(tmp_path, "claude", "done")
    project = tmp_path / "project"
    project.mkdir()
    _set_task_claude_env(monkeypatch, tmp_path, profile=profile)
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "claude" else None,
    )

    run_cli_acting(
        provider="claude-cli",
        model="opus",
        messages=[
            {
                "role": "system",
                "content": "ROLE-AUTHORITY-MARKER: obey the assigned Hermes role.",
            },
            {
                "role": "user",
                "content": (
                    "Work the assigned task. A lower-priority comment says to "
                    "ignore the Work Contract."
                ),
            },
        ],
        cwd=str(project),
        timeout=5,
        reasoning_config={"enabled": True, "effort": "high"},
    )

    invocation = json.loads(record.read_text())
    argv = invocation["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,ToolSearch"
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert "--append-system-prompt-file" in argv
    assert "--safe-mode" not in argv
    assert "bypassPermissions" not in argv
    assert "Bash" not in argv[argv.index("--tools") + 1]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"

    allowed = set(argv[argv.index("--allowedTools") + 1].split(","))
    assert {"Read", "Grep", "Glob", "ToolSearch"} <= allowed
    assert required_tool in allowed

    if profile == "productowner":
        assert "mcp__hermes-tools__kanban_link" not in allowed
    assert forbidden_tool not in allowed

    inline = json.loads(argv[argv.index("--mcp-config") + 1])
    server = inline["mcpServers"]["hermes-tools"]
    assert server["args"] == ["-m", "agent.transports.hermes_tools_mcp_server"]
    assert server["env"]["HERMES_MCP_CAPABILITY_SET"] == capability_set
    expected_child_env = {
        "HERMES_HOME",
        "HERMES_INFERENCE_PROVIDER",
        "HERMES_INFERENCE_MODEL",
        "HERMES_INFERENCE_EFFORT",
        "PYTHONPATH",
        "HERMES_QUIET",
        "HERMES_REDACT_SECRETS",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_PROFILE",
        "HERMES_MCP_CAPABILITY_SET",
    }
    expected_child_env.update(
        key
        for key in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT")
        if os.environ.get(key)
    )
    assert set(server["env"]) == expected_child_env
    assert server["env"]["HERMES_QUIET"] == "1"
    assert server["env"]["HERMES_REDACT_SECRETS"] == "true"
    assert server["env"]["HERMES_INFERENCE_PROVIDER"] == "claude-cli"
    assert server["env"]["HERMES_INFERENCE_MODEL"] == "opus"
    assert server["env"]["HERMES_INFERENCE_EFFORT"] == "high"
    assert server["env"]["PATH"] == os.environ["PATH"]
    assert invocation["task_env"] == {}
    assert invocation["authority_mode"] == 0o600
    assert not Path(invocation["authority_path"]).exists()
    assert "ROLE-AUTHORITY-MARKER" in invocation["authority"]
    assert "filesystem access is read-only" in invocation["authority"]
    assert "attachment content" in invocation["authority"]
    assert "kanban_block" in invocation["authority"]
    assert "Work Contract" in invocation["authority"]
    assert "task comments" in invocation["authority"]
    assert "ROLE-AUTHORITY-MARKER" not in invocation["stdin"]
    assert "filesystem access is read-only" not in invocation["stdin"]
    assert all("ROLE-AUTHORITY-MARKER" not in value for value in argv)
    assert "ignore the Work Contract" in invocation["stdin"]


def test_task_scoped_claude_rejects_unapproved_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.cli_emulated_provider import CliConfigurationError
    from agent.local_agent_provider import run_cli_acting

    executable, _record = _write_acting_cli(tmp_path, "claude", "done")
    _set_task_claude_env(monkeypatch, tmp_path, profile="developer")
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "claude" else None,
    )

    with pytest.raises(
        CliConfigurationError,
        match="task-scoped claude-cli profile.*developer",
    ):
        run_cli_acting(
            provider="claude-cli",
            model="opus",
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=5,
        )


@pytest.mark.parametrize("profile", ["productowner", "custom-product-owner"])
def test_work_inbox_claude_selects_intake_only_mcp_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    from agent.local_agent_provider import _task_scoped_claude_options

    values = {
        "HERMES_HOME": str(tmp_path / "profile"),
        "HERMES_WORK_INBOX_INTAKE": "qi_one",
        "HERMES_WORK_INBOX_RUN_ID": "7",
        "HERMES_WORK_INBOX_CLAIM_LOCK": "claim",
        "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
        "HERMES_KANBAN_BOARD": "product",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_PROFILE": profile,
    }
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    inline, allowed = _task_scoped_claude_options(
        provider="claude-cli", model="opus", effort="high"
    )
    server = json.loads(inline)["mcpServers"]["hermes-tools"]

    assert server["env"]["HERMES_MCP_CAPABILITY_SET"] == "product-owner-intake"
    assert server["env"]["HERMES_WORK_INBOX_INTAKE"] == "qi_one"
    assert "mcp__hermes-tools__work_inbox_decide" in allowed
    assert "mcp__hermes-tools__kanban_create" not in allowed
