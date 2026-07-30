from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.model_switch import ModelSwitchResult


class _FakeAgent:
    def __init__(self):
        self.calls = []
        self.model = "old/model"
        self.provider = "openrouter"

    def switch_model(self, **kwargs):
        self.calls.append(kwargs)
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]


class _StubCLI:
    session_id = "cli-session"
    model = "old/model"
    provider = "openrouter"
    requested_provider = "openrouter"
    api_key = "sk-old"
    _explicit_api_key = "sk-old"
    base_url = "https://openrouter.ai/api/v1"
    _explicit_base_url = "https://openrouter.ai/api/v1"
    api_mode = "chat_completions"
    agent = None
    _pending_model_switch_note = None
    _pending_one_turn_model_restore = None
    _pending_turn_route_target = None
    _session_model_pinned = False

    def _confirm_expensive_model_switch(self, result):
        return True

    def _take_turn_routing_request(self, **kwargs):
        raise NotImplementedError


def test_cli_model_once_queues_typed_target_without_switching_resident_runtime(monkeypatch):
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()
    stub._snapshot_model_runtime = cli_mod.HermesCLI._snapshot_model_runtime.__get__(stub)
    stub._take_turn_routing_request = (
        cli_mod.HermesCLI._take_turn_routing_request.__get__(stub)
    )
    printed = []

    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: printed.append(str(s)))
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not persist")))
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: SimpleNamespace(
            user_providers=None,
            custom_providers=None,
            with_overrides=lambda **_: SimpleNamespace(user_providers=None, custom_providers=None),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_: ModelSwitchResult(
            success=True,
            new_model="claude-sonnet-4.6",
            target_provider="anthropic",
            api_key="sk-ant",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            provider_label="Anthropic",
        ),
    )
    monkeypatch.setattr("hermes_cli.model_switch.resolve_display_context_length", lambda *a, **k: None)

    cli_mod.HermesCLI._handle_model_switch(
        stub,
        "/model claude-sonnet-4.6 --provider anthropic --once",
    )

    assert stub.model == "old/model"
    assert stub.provider == "openrouter"
    assert stub.agent is not None
    assert stub.agent.calls == []
    assert stub._pending_one_turn_model_restore is None
    assert stub._pending_turn_route_target == {
        "kind": "model",
        "provider": "anthropic",
        "model": "claude-sonnet-4.6",
    }
    assert "next turn only" in printed[-1]

    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_routing_config", lambda: {"mode": "off"}
    )
    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_moa_config", lambda: {"presets": {}}
    )
    request = stub._take_turn_routing_request(
        user_text="raw user request",
        api_user_message="[sidecar] raw user request",
        persist_user_message="raw user request",
    )
    assert request.explicit_turn_override is True
    assert request.explicit_moa_override is False
    assert request.user_text == "raw user request"
    assert request.explicit_target == {
        "kind": "model",
        "provider": "anthropic",
        "model": "claude-sonnet-4.6",
    }
    assert stub._pending_turn_route_target is None
    prepared = request.prepare_user_message("ignored routed message")
    assert prepared.user_message == "[sidecar] raw user request"
    assert prepared.persist_user_message == "raw user request"


def test_cli_one_turn_moa_uses_only_moa_intent_flag(monkeypatch):
    import cli as cli_mod

    stub = _StubCLI()
    setattr(stub, "agent", _FakeAgent())
    setattr(stub, "_pending_turn_route_target", {"kind": "moa", "preset": "deep"})
    take_request = cli_mod.HermesCLI._take_turn_routing_request.__get__(stub)
    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_routing_config", lambda: {"mode": "off"}
    )
    monkeypatch.setattr(
        "agent.turn_routing_runtime.load_turn_moa_config", lambda: {"presets": {}}
    )

    request = take_request(
        user_text="compare",
        api_user_message="compare",
        persist_user_message="compare",
    )

    assert request.explicit_turn_override is False
    assert request.explicit_moa_override is True
    assert request.explicit_target == {"kind": "moa", "preset": "deep"}
    assert stub._pending_turn_route_target is None


def test_cli_restore_model_runtime_snapshot_restores_agent():
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()
    snapshot = {
        "model": "old/model",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_key": "sk-old",
        "explicit_api_key": "sk-old",
        "base_url": "https://openrouter.ai/api/v1",
        "explicit_base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }

    cli_mod.HermesCLI._restore_model_runtime_snapshot(stub, snapshot)

    assert stub.model == "old/model"
    assert stub.provider == "openrouter"
    assert stub.agent.calls[-1]["new_model"] == "old/model"


def test_cli_restore_model_runtime_prefers_primary_runtime():
    import cli as cli_mod

    class Agent(_FakeAgent):
        _primary_runtime = None
        _rate_limited_until = 123

        def __init__(self):
            super().__init__()
            self.model = "temp/model"
            self.provider = "anthropic"

        def _restore_primary_runtime(self):
            self.model = self._primary_runtime["model"]
            self.provider = self._primary_runtime["provider"]
            return True

    stub = _StubCLI()
    stub.agent = Agent()
    snapshot = {
        "model": "old/model",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_key": "sk-old",
        "explicit_api_key": "sk-old",
        "base_url": "",
        "explicit_base_url": "",
        "api_mode": "chat_completions",
        "agent_primary_runtime": {
            "model": "old/model",
            "provider": "openrouter",
        },
    }

    cli_mod.HermesCLI._restore_model_runtime_snapshot(stub, snapshot)

    assert stub.agent is not None
    assert stub.agent.model == "old/model"
    assert stub.agent.provider == "openrouter"
    assert stub.agent.calls == []


def test_cli_moa_one_shot_queues_typed_target_without_switching_resident_runtime(
    monkeypatch,
):
    import cli as cli_mod

    shell = object.__new__(cli_mod.HermesCLI)
    resident_agent = object()
    shell.config = {"moa": {"default_preset": "default", "presets": {}}}
    shell.requested_provider = "openrouter"
    shell.provider = "openrouter"
    shell.model = "primary-model"
    shell.api_key = "primary-key"
    shell.base_url = "https://primary.example/v1"
    shell.api_mode = "chat_completions"
    shell.agent = resident_agent
    shell._pending_turn_route_target = None
    shell._pending_moa_restore_model = None
    shell._pending_moa_disable_after_turn = False
    shell._pending_agent_seed = None
    printed = []
    monkeypatch.setattr(
        cli_mod,
        "_cprint",
        lambda value, *args, **kwargs: printed.append(str(value)),
    )

    assert shell.process_command("/moa explain this") is True

    assert shell.requested_provider == "openrouter"
    assert shell.provider == "openrouter"
    assert shell.model == "primary-model"
    assert shell.api_key == "primary-key"
    assert shell.base_url == "https://primary.example/v1"
    assert shell.api_mode == "chat_completions"
    assert shell.agent is resident_agent
    assert shell._pending_turn_route_target == {
        "kind": "moa",
        "preset": "default",
    }
    assert shell._pending_agent_seed == "explain this"
    assert shell._pending_moa_restore_model is None
    assert shell._pending_moa_disable_after_turn is False
    assert "resident model is unchanged" in printed[-1]
