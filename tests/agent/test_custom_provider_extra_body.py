from unittest.mock import MagicMock

import agent.agent_init as agent_init
from run_agent import AIAgent


def _install_task1_constructor_guards(monkeypatch):
    probes = {
        "context": MagicMock(return_value=262_144),
        "endpoint": MagicMock(
            side_effect=AssertionError("constructor endpoint probe forbidden")
        ),
        "local": MagicMock(
            side_effect=AssertionError("constructor local-service probe forbidden")
        ),
    }
    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length", probes["context"],
    )
    monkeypatch.setattr(
        "agent.model_metadata.fetch_endpoint_model_metadata", probes["endpoint"],
    )
    monkeypatch.setattr(
        "agent.model_metadata.detect_local_server_type", probes["local"],
    )
    return probes


def _assert_task1_constructor_guards(probes):
    probes["context"].assert_called_once()
    probes["endpoint"].assert_not_called()
    probes["local"].assert_not_called()



def test_custom_provider_extra_body_preserves_caller_override(tmp_path, monkeypatch):
    provider_overrides = agent_init._provider_request_overrides_for_route(
        requested_provider="custom",
        provider="custom",
        model="google/gemma-4-31b-it",
        base_url="https://example.test/v1",
        custom_providers=[
            {
                "name": "gemma",
                "base_url": "https://example.test/v1",
                "model": "google/gemma-4-31b-it",
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            }
        ],
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))
    probes = _install_task1_constructor_guards(monkeypatch)
    agent = AIAgent(
        api_key="synthetic-key",
        base_url="https://example.test/v1",
        provider="custom",
        requested_provider="custom",
        model="google/gemma-4-31b-it",
        request_overrides={
            "extra_body": {
                "reasoning_effort": "low",
                "caller_only": True,
            }
        },
        provider_request_overrides=provider_overrides,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent.request_overrides["extra_body"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
        "caller_only": True,
    }
    _assert_task1_constructor_guards(probes)




def test_named_custom_provider_extra_body_matches_provider_key(tmp_path, monkeypatch):
    provider_overrides = agent_init._provider_request_overrides_for_route(
        requested_provider="custom:zai-coding-plan",
        provider="custom:zai-coding-plan",
        model="glm-5.2",
        base_url="https://api.z.ai/api/coding/paas/v4",
        custom_providers=[
            {
                "provider_key": "other-provider",
                "name": "Other Provider",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": True},
            },
            {
                "provider_key": "zai-coding-plan",
                "name": "Z.AI Coding Plan",
                "base_url": "https://api.z.ai/api/coding/paas/v4/",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": False},
            },
        ],
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))
    probes = _install_task1_constructor_guards(monkeypatch)
    agent = AIAgent(
        api_key="synthetic-key",
        base_url="https://api.z.ai/api/coding/paas/v4",
        provider="custom:zai-coding-plan",
        requested_provider="custom:zai-coding-plan",
        model="glm-5.2",
        request_overrides={},
        provider_request_overrides=provider_overrides,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent.request_overrides == {"extra_body": {"enable_thinking": False}}
    _assert_task1_constructor_guards(probes)


def test_constructor_separates_layers_deep_copies_and_whole_replacement(
    tmp_path, monkeypatch
):
    provider = {
        "provider_option": {"marker": "provider"},
        "extra_body": {
            "auth": {"provider_marker": "must-disappear", "mode": "provider"},
            "provider_only": {"value": 1},
        },
    }
    caller = {
        "caller_option": {"marker": "caller"},
        "extra_body": {
            "auth": {"mode": "caller"},
            "caller_only": {"value": 2},
        },
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))
    probes = _install_task1_constructor_guards(monkeypatch)
    agent = AIAgent(
        api_key="synthetic-key",
        base_url="https://remote.example/v1",
        provider="custom",
        requested_provider="custom:remote",
        model="synthetic-model",
        request_overrides=caller,
        provider_request_overrides=provider,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    provider["extra_body"]["auth"]["provider_marker"] = "mutated-input"
    caller["extra_body"]["auth"]["mode"] = "mutated-input"
    assert agent._provider_request_overrides["extra_body"]["auth"]["provider_marker"] == "must-disappear"
    assert agent._caller_request_overrides["extra_body"]["auth"] == {"mode": "caller"}
    assert agent.request_overrides["extra_body"] == {
        "auth": {"mode": "caller"},
        "provider_only": {"value": 1},
        "caller_only": {"value": 2},
    }

    replacement = {"speed": "fast", "extra_body": {"auth": {"mode": "replacement"}}}
    agent.request_overrides = replacement
    replacement["extra_body"]["auth"]["mode"] = "mutated-after-assignment"
    assert agent._caller_request_overrides == {
        "speed": "fast",
        "extra_body": {"auth": {"mode": "replacement"}},
    }
    assert agent.request_overrides["extra_body"]["auth"] == {"mode": "replacement"}
    assert "provider_marker" not in agent.request_overrides["extra_body"]["auth"]
    _assert_task1_constructor_guards(probes)


def test_provider_layer_parameter_is_appended_after_requested_provider(monkeypatch):
    import inspect

    from agent.agent_init import init_agent

    constructor_parameters = list(inspect.signature(AIAgent.__init__).parameters.values())[1:]
    initializer_parameters = list(inspect.signature(init_agent).parameters.values())[1:]
    assert [item.name for item in constructor_parameters[-2:]] == [
        "requested_provider",
        "provider_request_overrides",
    ]
    assert [item.name for item in initializer_parameters[-2:]] == [
        "requested_provider",
        "provider_request_overrides",
    ]
    assert all(item.default is not inspect.Parameter.empty for item in constructor_parameters)

    old_positional_arguments = [item.default for item in constructor_parameters[:-2]]
    old_positional_arguments.append("custom:positional-proof")
    forwarded = MagicMock()
    monkeypatch.setattr("agent.agent_init.init_agent", forwarded)
    AIAgent(*old_positional_arguments)

    assert forwarded.call_args.kwargs["requested_provider"] == "custom:positional-proof"
    assert forwarded.call_args.kwargs["provider_request_overrides"] is None
