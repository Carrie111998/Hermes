from types import SimpleNamespace

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


def test_external_acp_environment_overrides_profile(monkeypatch):
    profile = SimpleNamespace(
        external_command="python",
        external_args=("default",),
        external_command_env="TEST_ACP_COMMAND",
        external_args_env="TEST_ACP_ARGS",
        external_auth_args=(),
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
