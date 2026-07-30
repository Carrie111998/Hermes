from __future__ import annotations


class _FakeAgent:
    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.session_id = "oneshot-session"
        self.calls = []
        self.suppress_status_output = False
        self.stream_delta_callback = object()
        self.tool_gen_callback = object()
        self.__class__.instances.append(self)

    def run_conversation(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return {"final_response": "ok", "messages": []}

    def shutdown_memory_provider(self, *_args):
        return None

    def close(self):
        return None


def _install_oneshot_fakes(monkeypatch):
    from hermes_cli import config as config_module
    from hermes_cli import oneshot
    from hermes_cli import runtime_provider
    from hermes_cli import tools_config
    import run_agent

    _FakeAgent.instances.clear()
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "model": {"default": "k3", "provider": "kimi-coding"},
        },
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **kwargs: {
            "provider": kwargs.get("requested") or "kimi-coding",
            "requested_provider": kwargs.get("requested") or "kimi-coding",
            "api_key": None,
            "base_url": None,
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: set())
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_routing_config",
        lambda: {"mode": "auto"},
    )
    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_moa_config",
        lambda: {"presets": {}},
    )
    return oneshot


def test_oneshot_explicit_model_is_manual_and_default_model_is_router_eligible(monkeypatch):
    oneshot = _install_oneshot_fakes(monkeypatch)

    response, _result = oneshot._run_agent(
        "explicit",
        model="gpt-manual",
        provider="openai",
        use_config_toolsets=False,
    )
    explicit_agent = _FakeAgent.instances[-1]
    explicit_request = explicit_agent.calls[-1][1]["turn_routing_request"]

    assert response == "ok"
    assert explicit_request.surface == "oneshot"
    assert explicit_request.user_text == "explicit"
    assert explicit_request.session_pinned is True
    assert explicit_request.manual_mode is True
    assert explicit_request.explicit_turn_override is False
    assert explicit_request.explicit_moa_override is False
    assert explicit_request.explicit_target is None

    response, _result = oneshot._run_agent(
        "automatic",
        use_config_toolsets=False,
    )
    automatic_agent = _FakeAgent.instances[-1]
    automatic_request = automatic_agent.calls[-1][1]["turn_routing_request"]

    assert response == "ok"
    assert automatic_request.surface == "oneshot"
    assert automatic_request.session_pinned is False
    assert automatic_request.manual_mode is False
    assert automatic_request.explicit_target is None
