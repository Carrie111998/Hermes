"""Credential binding regressions for ``hermes -z --model <alias>``."""


def test_oneshot_forwards_direct_alias_key_to_runtime_resolution(monkeypatch):
    """The non-interactive alias path must not drop its configured key and
    fall back to a credential-pool secret for the alias endpoint (#83612)."""
    import hermes_cli.oneshot as oneshot
    import hermes_cli.model_switch as ms
    from hermes_cli.model_switch import DirectAlias

    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {
            "theta": DirectAlias(
                "glm_5_2",
                "custom",
                "https://theta.example/infer_request",
                "theta-key",
            )
        },
    )
    monkeypatch.setattr(ms, "_ensure_direct_aliases", lambda: None)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kwargs: None,
    )
    captured = {}

    def fake_runtime(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "custom",
            "requested_provider": "custom",
            "base_url": kwargs["explicit_base_url"],
            "api_key": kwargs["explicit_api_key"],
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_runtime)

    class FakeAgent:
        def __init__(self, **_kwargs):
            self._session_messages = []

        def run_conversation(self, _prompt):
            return {"final_response": "ok"}

        def shutdown_memory_provider(self, *_args):
            pass

        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

    response, _result = oneshot._run_agent("ping", model="theta")

    assert response == "ok"
    assert captured["requested"] == "custom"
    assert captured["explicit_base_url"] == "https://theta.example/infer_request"
    assert captured["explicit_api_key"] == "theta-key"


def test_oneshot_resolves_direct_alias_key_env(monkeypatch):
    """``key_env`` is an alias credential too, not merely interactive
    picker metadata (#83612)."""
    import hermes_cli.oneshot as oneshot
    import hermes_cli.model_switch as ms
    from hermes_cli.model_switch import DirectAlias

    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {
            "theta": DirectAlias(
                "glm_5_2",
                "custom",
                "https://theta.example/infer_request",
                "",
                "THETA_API_KEY",
            )
        },
    )
    monkeypatch.setattr(ms, "_ensure_direct_aliases", lambda: None)
    monkeypatch.setattr(ms, "_scoped_key_env", lambda name: "theta-env-key" if name == "THETA_API_KEY" else "")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **kwargs: None)
    captured = {}

    def fake_runtime(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "custom", "requested_provider": "custom",
            "base_url": kwargs["explicit_base_url"], "api_key": kwargs["explicit_api_key"],
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_runtime)

    class FakeAgent:
        def __init__(self, **_kwargs):
            self._session_messages = []
        def run_conversation(self, _prompt):
            return {"final_response": "ok"}
        def shutdown_memory_provider(self, *_args):
            pass
        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    oneshot._run_agent("ping", model="theta")

    assert captured["explicit_api_key"] == "theta-env-key"
