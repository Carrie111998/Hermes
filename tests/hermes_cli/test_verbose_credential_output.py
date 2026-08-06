"""Regression coverage for credential-safe verbose CLI initialization.

The synthetic credential is generated at runtime and all output is captured in
memory so a failing assertion cannot persist or print credential material.
"""

import logging
import os
import secrets
import socket
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SAFE_CREDENTIAL_DIAGNOSTIC = "🔑 Authentication credential: present"


def _synthetic_credential() -> tuple[str, str, str]:
    """Return a runtime-only credential with distinguishable random segments."""
    alphabet = "QWXZJVK2346789"

    def random_part() -> str:
        return "".join(secrets.choice(alphabet) for _ in range(16))

    prefix = f"PFX-{random_part()}"
    suffix = f"SFX-{random_part()}"
    return f"{prefix}-MID-{random_part()}-{suffix}", prefix, suffix


def _assert_credential_absent(
    output: str,
    credential: str,
    prefix: str,
    suffix: str,
) -> None:
    """Fail without echoing captured output or credential-derived material."""
    forbidden = (
        credential,
        prefix,
        suffix,
        credential[:8],
        credential[-4:],
        f"{credential[:8]}...{credential[-4:]}",
    )
    if any(fragment in output for fragment in forbidden):
        pytest.fail("verbose CLI output exposed synthetic credential material", pytrace=False)


def _assert_artifacts_are_credential_free(
    hermes_home: Path,
    credential: str,
    prefix: str,
    suffix: str,
) -> None:
    """Ensure temporary state contains no credential or derived preview."""
    forbidden = tuple(
        fragment.encode()
        for fragment in (
            credential,
            prefix,
            suffix,
            f"{credential[:8]}...{credential[-4:]}",
        )
    )
    for path in hermes_home.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(fragment in data for fragment in forbidden):
            pytest.fail(
                "CLI probe persisted synthetic credential material",
                pytrace=False,
            )


def _reset_hermes_logging() -> None:
    """Detach per-test file and verbose handlers from global logging state."""
    import hermes_logging

    hermes_logging._reset_queued_handlers()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_hermes_verbose", False):
            root.removeHandler(handler)
            handler.close()
    hermes_logging._logging_initialized = False


@pytest.mark.parametrize(
    ("provider", "api_mode", "base_url", "model"),
    [
        ("openrouter", "chat_completions", "https://example.invalid/v1", "test/model"),
        ("anthropic", "anthropic_messages", "https://api.example.invalid", "claude-test"),
    ],
)
def test_verbose_cli_initialization_reports_credential_without_exposing_it(
    monkeypatch,
    tmp_path,
    provider,
    api_mode,
    base_url,
    model,
):
    """``-v`` keeps safe diagnostics while exposing no credential characters."""
    isolated_home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    isolated_home.mkdir()
    for directory in ("sessions", "cron", "memories", "skills"):
        (hermes_home / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")

    network_attempts = []

    def reject_network(*_args, **_kwargs):
        network_attempts.append(True)
        raise AssertionError("network access is forbidden in this test")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)

    import cli as cli_module

    clean_config = {
        "model": {
            "default": model,
            "base_url": base_url,
            "provider": provider,
        },
        "display": {
            "compact": False,
            "tool_progress": "all",
            "resume_display": "full",
            "persistent_output": False,
        },
        "agent": {},
        "terminal": {"env_type": "local"},
    }

    monkeypatch.setattr(cli_module, "CLI_CONFIG", clean_config)
    monkeypatch.setattr(cli_module, "get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr(cli_module, "_prepare_deferred_agent_startup", lambda: None)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda: {})
    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length",
        lambda *_args, **_kwargs: 256_000,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda *_args, **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.wait_for_mcp_discovery",
        lambda: None,
    )

    credential, prefix, suffix = _synthetic_credential()
    _reset_hermes_logging()
    stdout = StringIO()
    stderr = StringIO()
    initialized = False
    probe_raised = False
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli = cli_module.HermesCLI(
                    model=model,
                    provider=provider,
                    api_key=credential,
                    base_url=base_url,
                    verbose=True,
                    max_turns=1,
                    toolsets=[],
                    ignore_rules=True,
                )
                monkeypatch.setattr(cli, "_ensure_runtime_credentials", lambda: True)
                monkeypatch.setattr(cli, "_ensure_tirith_security", lambda: None)
                initialized = cli._init_agent(
                    runtime_override={
                        "api_key": credential,
                        "base_url": base_url,
                        "provider": provider,
                        "requested_provider": provider,
                        "api_mode": api_mode,
                        "command": None,
                        "args": [],
                        "credential_pool": None,
                    }
                )
            except Exception:
                probe_raised = True
    finally:
        stdout_output = stdout.getvalue()
        stderr_output = stderr.getvalue()
        _reset_hermes_logging()

    if probe_raised or not initialized:
        pytest.fail("CLI probe failed before output assertions", pytrace=False)
    if network_attempts:
        pytest.fail("CLI probe attempted network access", pytrace=False)
    _assert_credential_absent(stdout_output, credential, prefix, suffix)
    _assert_credential_absent(stderr_output, credential, prefix, suffix)
    _assert_artifacts_are_credential_free(hermes_home, credential, prefix, suffix)
    if SAFE_CREDENTIAL_DIAGNOSTIC not in stdout_output + stderr_output:
        pytest.fail("safe credential diagnostic was not emitted", pytrace=False)
