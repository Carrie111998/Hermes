from types import SimpleNamespace

from agent.copilot_acp_client import CopilotACPClient, ExternalACPClient
from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    ProviderConfig,
    get_external_process_provider_status,
    resolve_external_process_provider_credentials,
)


def test_external_acp_profile_controls_command_and_auth(monkeypatch):
    profile = SimpleNamespace(
        external_command="python",
        external_args=("-m", "example_acp"),
        external_command_env="TEST_ACP_COMMAND",
        external_args_env="TEST_ACP_ARGS",
        external_auth_args=("-c", "raise SystemExit(0)"),
        external_model_arg="--model",
    )
    config = ProviderConfig(
        id="test-acp",
        name="Test ACP",
        auth_type="external_process",
        inference_base_url="acp://test",
    )
    monkeypatch.setitem(PROVIDER_REGISTRY, "test-acp", config)
    monkeypatch.setattr("providers.get_provider_profile", lambda _: profile)

    status = get_external_process_provider_status("test-acp")
    credentials = resolve_external_process_provider_credentials("test-acp")

    assert status["logged_in"] is True
    assert status["args"] == ["-m", "example_acp"]
    assert credentials["provider"] == "test-acp"
    assert credentials["api_key"] == "test-acp"
    assert credentials["base_url"] == "acp://test"
    assert credentials["args"] == ["-m", "example_acp"]
    assert credentials["model_arg"] == "--model"


def test_external_acp_environment_overrides_profile(monkeypatch):
    profile = SimpleNamespace(
        external_command="python",
        external_args=("default",),
        external_command_env="TEST_ACP_COMMAND",
        external_args_env="TEST_ACP_ARGS",
        external_auth_args=(),
        external_model_arg="",
    )
    config = ProviderConfig(
        id="test-acp-env",
        name="Test ACP Env",
        auth_type="external_process",
        inference_base_url="acp://test-env",
    )
    monkeypatch.setitem(PROVIDER_REGISTRY, "test-acp-env", config)
    monkeypatch.setattr("providers.get_provider_profile", lambda _: profile)
    monkeypatch.setenv("TEST_ACP_COMMAND", "python")
    monkeypatch.setenv("TEST_ACP_ARGS", "-m overridden")

    credentials = resolve_external_process_provider_credentials("test-acp-env")

    assert credentials["args"] == ["-m", "overridden"]


def test_external_acp_client_binds_selected_model_to_process():
    client = ExternalACPClient(
        api_key="devin-acp",
        command="devin",
        args=["acp"],
        provider_name="devin-acp",
        display_name="Devin Subscription",
        model_arg="--model",
    )

    assert client._build_process_args("gpt-5-6-sol-none") == [
        "devin",
        "acp",
        "--model",
        "gpt-5-6-sol-none",
    ]
    assert CopilotACPClient is ExternalACPClient
