import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import hermes_cli.plugins as plugins_mod
import tools.terminal_tool as terminal_tool_module
from tools.environments.local import LocalEnvironment


_UNSET = object()


def _make_env_config(tmp_path, **overrides):
    config = {
        "env_type": "local",
        "timeout": 30,
        "cwd": str(tmp_path),
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    config.update(overrides)
    return config


def _run_terminal(
    monkeypatch,
    tmp_path,
    *,
    output,
    returncode=0,
    invoke_hook=_UNSET,
    approval=None,
    command="echo hello",
    raw_output=False,
):
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": output, "returncode": returncode}

    monkeypatch.setattr(
        terminal_tool_module, "_get_env_config", lambda: _make_env_config(tmp_path)
    )
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: approval or {"approved": True},
    )
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", mock_env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)

    if invoke_hook is not _UNSET:
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)

    result = json.loads(
        terminal_tool_module.terminal_tool(command=command, raw_output=raw_output)
    )
    return result, mock_env


def test_terminal_output_unchanged_when_transform_hook_not_registered(monkeypatch, tmp_path):
    result, _mock_env = _run_terminal(monkeypatch, tmp_path, output="plain output")

    assert result["output"] == "plain output"
    assert result["exit_code"] == 0
    assert result["error"] is None


def test_terminal_output_transform_still_runs_strip_and_redact(monkeypatch, tmp_path):
    # Ensure redaction is active regardless of host HERMES_REDACT_SECRETS state
    # or collection-time import order (the module snapshots env at import).
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)

    secret = "sk-proj-abc123def456ghi789jkl012mno345"
    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output="plain output",
        invoke_hook=lambda hook_name, **kwargs: [f" \x1b[31mOPENAI_API_KEY={secret}\x1b[0m "],
    )

    assert "\x1b" not in result["output"]
    # Terminal output now passes code_file=True: ENV-assignment redaction is
    # skipped (so code constants like MAX_TOKENS=100 aren't corrupted), but a
    # real sk-/ghp_/JWT-shaped value is STILL masked by _PREFIX_RE. The full
    # secret never survives; only the leading prefix marker remains. (#33801)
    assert secret not in result["output"]
    assert "OPENAI_API_KEY=" in result["output"]
    assert "sk-pro" in result["output"]  # prefix marker from _mask_token
    assert "abc123def456" not in result["output"]  # secret body is gone


def test_large_process_output_is_bounded_before_sudo_and_plugin_hooks(
    monkeypatch, tmp_path
):
    limit = 10_000
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: limit)
    monkeypatch.setattr(
        terminal_tool_module, "_get_env_config", lambda: _make_env_config(tmp_path)
    )
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {"approved": True},
    )

    sudo_input_lengths = []
    hook_inputs = []

    def _sudo_spy(output):
        sudo_input_lengths.append(len(output))
        return False

    def _hook_spy(hook_name, **kwargs):
        if hook_name == "transform_terminal_output":
            hook_inputs.append(kwargs["output"])
        return []

    monkeypatch.setattr(
        terminal_tool_module, "_sudo_wrong_password_failure", _sudo_spy
    )
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _hook_spy)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=10)
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)
    try:
        command = (
            "python3 -c \"import sys; "
            "sys.stdout.write('HEAD-SENTINEL\\n' + 'x' * 2000000 + "
            "'\\nTAIL-SENTINEL')\""
        )
        result = json.loads(terminal_tool_module.terminal_tool(command=command))
    finally:
        env.cleanup()

    assert sudo_input_lengths
    assert max(sudo_input_lengths) <= limit
    assert len(hook_inputs) == 1
    assert len(hook_inputs[0]) <= limit
    assert hook_inputs[0].startswith("HEAD-SENTINEL")
    assert hook_inputs[0].endswith("TAIL-SENTINEL")
    assert "[OUTPUT TRUNCATED" in hook_inputs[0]
    assert len(result["output"]) <= limit


def test_terminal_output_transform_does_not_change_approval_or_exit_code_meaning(monkeypatch, tmp_path):
    approval = {
        "approved": True,
        "user_approved": True,
        "description": "dangerous command",
    }
    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output="original output",
        returncode=1,
        approval=approval,
        command="grep foo bar",
        invoke_hook=lambda hook_name, **kwargs: ["replaced output"],
    )

    assert result["output"] == "replaced output"
    assert result["approval"] == (
        "Command required approval (dangerous command) and was approved by the user."
    )
    assert result["exit_code_meaning"] == "No matches found (not an error)"


def test_raw_output_bypasses_terminal_transform_for_reviewer_evidence(monkeypatch, tmp_path):
    calls = []

    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output="exact raw evidence",
        raw_output=True,
        invoke_hook=lambda hook_name, **kwargs: calls.append((hook_name, kwargs)) or [
            "compressed"
        ],
    )

    assert result["output"] == "exact raw evidence"
    assert result["output_mode"] == "raw"
    assert calls == []


def test_profile_can_disable_raw_output_bypass(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"rtk": {"raw_bypass": False}}
    )

    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output="exact raw evidence",
        raw_output=True,
        invoke_hook=lambda hook_name, **kwargs: ["compressed"],
    )

    assert result["output"] == "compressed"
    assert result["raw_bypass_denied"] is True
    assert "output_mode" not in result


def test_compressed_output_is_not_recorded_as_exact_verification_evidence(
    monkeypatch, tmp_path
):
    recorded = []
    monkeypatch.setattr(
        "agent.verification_evidence.record_terminal_result",
        lambda **kwargs: recorded.append(kwargs),
    )

    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output="raw failing diagnostic",
        returncode=1,
        command="python -m pytest tests/example.py",
        invoke_hook=lambda hook_name, **kwargs: ["1 test failed"],
    )

    assert result["output"] == "1 test failed"
    assert recorded[0]["output"] == "raw failing diagnostic"


def test_terminal_output_transform_integration_with_real_plugin(monkeypatch, tmp_path):
    import yaml

    hermes_home = Path(os.environ["HERMES_HOME"])
    plugins_dir = hermes_home / "plugins"
    plugin_dir = plugins_dir / "terminal_transform"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: terminal_transform\n", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        '    ctx.register_hook("transform_terminal_output", '
        'lambda **kw: "PLUGIN-HEAD\\n" + kw["output"] + "\\nPLUGIN-TAIL")\n',
        encoding="utf-8",
    )
    # Plugins are opt-in — must be listed in plugins.enabled to load.
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"plugins": {"enabled": ["terminal_transform"]}}),
        encoding="utf-8",
    )

    # Force a fresh plugin manager so the new config is picked up.
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins()

    long_output = "X" * 60000
    result, _mock_env = _run_terminal(
        monkeypatch,
        tmp_path,
        output=long_output,
    )

    assert "PLUGIN-HEAD" in result["output"]
    assert "PLUGIN-TAIL" in result["output"]
    assert "[OUTPUT TRUNCATED" in result["output"]
