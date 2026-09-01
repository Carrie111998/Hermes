"""Regression: named custom providers may supply callable key_cmd credentials."""


def test_named_custom_provider_mints_key_cmd_before_openai_client(monkeypatch):
    import agent.auxiliary_client as auxiliary_client
    import agent.command_token_source as command_token_source
    import hermes_cli.runtime_provider as runtime_provider

    minted_key = lambda: "minted-token"
    captured = {}

    monkeypatch.setattr(
        runtime_provider,
        "_get_named_custom_provider",
        lambda _provider: {
            "name": "CLI Proxy API",
            "base_url": "https://proxy.example/v1",
            "api_mode": "chat_completions",
            "model": "gpt-5.6-terra",
            "key_cmd": "mint-key",
        },
    )
    monkeypatch.setattr(
        command_token_source,
        "build_command_token_provider",
        lambda *_args: minted_key,
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_create_openai_client",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    _client, model = auxiliary_client.resolve_provider_client(
        "custom:cli-proxy-api", "gpt-5.6-terra"
    )

    assert model == "gpt-5.6-terra"
    assert captured["api_key"] == "minted-token"
