"""Focused contracts for model-specific OpenRouter endpoint routing."""

import inspect
from types import SimpleNamespace

import pytest

from agent import agent_init
from agent.chat_completion_helpers import _provider_preferences_for_agent
from hermes_cli.config import validate_config_structure
from providers import get_provider_profile


def _resolve(
    *, provider="openrouter", model="deepseek/deepseek-v4-flash", routing=None
):
    return agent_init._resolve_openrouter_provider_routing(
        provider=provider,
        model=model,
        routing=routing or {},
    )


def _agent(**overrides):
    values = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_sort": None,
        "provider_require_parameters": False,
        "provider_data_collection": None,
        "provider_quantizations": None,
        "provider_allow_fallbacks": None,
        "_provider_routing_config": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_existing_global_fields_remain_unchanged():
    routing = {
        "sort": "throughput",
        "only": ["anthropic"],
        "ignore": ["deepinfra"],
        "order": ["anthropic", "google"],
        "require_parameters": True,
        "data_collection": "deny",
    }

    assert _resolve(routing=routing) == routing


def test_quantizations_are_emitted_in_provider_preferences():
    prefs = _provider_preferences_for_agent(
        _agent(provider_quantizations=["fp8", "bf16"])
    )

    assert prefs["quantizations"] == ["fp8", "bf16"]


def test_allow_fallbacks_false_is_not_dropped():
    prefs = _provider_preferences_for_agent(_agent(provider_allow_fallbacks=False))

    assert prefs["allow_fallbacks"] is False


def test_openrouter_profile_preserves_complete_provider_object():
    preferences = {
        "only": ["baidu"],
        "quantizations": ["fp8"],
        "allow_fallbacks": False,
    }

    body = get_provider_profile("openrouter").build_extra_body(
        provider_preferences=preferences
    )

    assert body["provider"] == preferences


def test_matching_model_override_replaces_global_only():
    result = _resolve(
        routing={
            "only": ["anthropic"],
            "sort": "price",
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
                },
            },
        }
    )

    assert result == {"only": ["baidu"], "sort": "price"}


def test_model_override_is_isolated_from_other_openrouter_models():
    routing = {
        "only": ["anthropic"],
        "model_overrides": {
            "openrouter": {
                "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
            },
        },
    }

    assert _resolve(model="openai/gpt-5.4", routing=routing) == {"only": ["anthropic"]}


def test_openrouter_model_prefix_is_a_hermes_side_override_alias():
    routing = {
        "model_overrides": {
            "openrouter": {
                "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
            },
        },
    }

    assert _resolve(
        model="openrouter/deepseek/deepseek-v4-flash", routing=routing
    ) == {"only": ["baidu"]}


def test_model_override_does_not_affect_direct_provider():
    routing = {
        "only": ["anthropic"],
        "model_overrides": {
            "openrouter": {
                "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
            },
        },
    }

    assert _resolve(provider="anthropic", routing=routing) == {}


def test_empty_model_list_explicitly_clears_inherited_restriction():
    result = _resolve(
        routing={
            "only": ["anthropic"],
            "order": ["anthropic"],
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"only": []},
                },
            },
        }
    )

    assert "only" not in result
    assert result["order"] == ["anthropic"]


def test_null_model_field_omits_inherited_value():
    result = _resolve(
        routing={
            "sort": "price",
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"sort": None},
                },
            },
        }
    )

    assert "sort" not in result


@pytest.mark.parametrize(
    "routing",
    [
        {"quantizations": "fp8"},
        {"allow_fallbacks": "false"},
        {"sort": "fastest"},
        {"model_overrides": {"openrouter": {"model": {"mystery": True}}}},
        {"unknown_routing_key": True},
    ],
)
def test_invalid_routing_values_are_config_errors(routing):
    issues = validate_config_structure({"provider_routing": routing})

    assert any(
        issue.severity == "error" and "provider_routing" in issue.message
        for issue in issues
    )


def test_model_override_ignore_composes_with_only_and_fallbacks():
    result = _resolve(
        routing={
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {
                        "order": ["baidu"],
                        "ignore": ["fireworks"],
                        "allow_fallbacks": True,
                    },
                },
            },
        }
    )

    assert result == {
        "order": ["baidu"],
        "ignore": ["fireworks"],
        "allow_fallbacks": True,
    }


def test_model_override_ignore_inherits_global_value_when_missing():
    result = _resolve(
        routing={
            "ignore": ["deepinfra"],
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"order": ["baidu"]},
                },
            },
        }
    )

    assert result["ignore"] == ["deepinfra"]
    assert result["order"] == ["baidu"]


def test_empty_model_override_ignore_clears_global_value():
    result = _resolve(
        routing={
            "ignore": ["deepinfra"],
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"ignore": []},
                },
            },
        }
    )

    assert "ignore" not in result


def test_model_override_ignore_does_not_leak_to_other_models_or_providers():
    routing = {
        "ignore": ["deepinfra"],
        "model_overrides": {
            "openrouter": {
                "deepseek/deepseek-v4-flash": {"ignore": ["fireworks"]},
            },
        },
    }

    assert _resolve(model="openai/gpt-5.4", routing=routing) == {
        "ignore": ["deepinfra"]
    }
    assert _resolve(provider="anthropic", routing=routing) == {}


@pytest.mark.parametrize(
    "routing",
    [
        {"only": ["baidu"], "ignore": ["baidu"]},
        {"order": ["baidu"], "ignore": ["baidu"]},
        {
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {
                        "only": ["baidu"],
                        "ignore": ["baidu"],
                    },
                },
            },
        },
        {
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {
                        "order": ["baidu"],
                        "ignore": ["baidu"],
                    },
                },
            },
        },
    ],
)
def test_conflicting_only_or_order_and_ignore_are_config_errors(routing):
    issues = validate_config_structure({"provider_routing": routing})

    assert any(
        issue.severity == "error"
        and "cannot appear in both" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("routing", "field"),
    [
        ({"ignore": [{"name": "fireworks"}], "only": ["baidu"]}, "ignore"),
        ({"only": [{"name": "baidu"}], "ignore": ["fireworks"]}, "only"),
        ({"ignore": [1]}, "ignore"),
    ],
)
def test_non_string_routing_list_items_return_validation_errors(routing, field):
    issues = validate_config_structure({"provider_routing": routing})

    assert any(
        issue.severity == "error" and f"provider_routing.{field}" in issue.message
        for issue in issues
    )


def test_merged_global_and_model_ignore_conflict_is_rejected():
    issues = validate_config_structure(
        {
            "provider_routing": {
                "only": ["baidu"],
                "model_overrides": {
                    "openrouter": {
                        "deepseek/deepseek-v4-flash": {"ignore": ["baidu"]},
                    },
                },
            }
        }
    )

    assert any(
        issue.severity == "error" and "cannot appear in both" in issue.message
        for issue in issues
    )


def test_request_time_model_switch_drops_other_model_override_and_restores_it():
    agent = _agent(
        _provider_routing_config={
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
                },
            },
        }
    )

    assert _provider_preferences_for_agent(agent) == {"only": ["baidu"]}

    agent.model = "openai/gpt-5.4"
    assert _provider_preferences_for_agent(agent) == {}

    agent.model = "deepseek/deepseek-v4-flash"
    assert _provider_preferences_for_agent(agent) == {"only": ["baidu"]}


def test_request_time_provider_fallback_uses_fallback_model_routing():
    agent = _agent(
        _provider_routing_config={
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"only": ["baidu"]},
                    "openai/gpt-5.4": {"order": ["openai"]},
                },
            },
        }
    )

    assert _provider_preferences_for_agent(agent) == {"only": ["baidu"]}

    agent.model = "openai/gpt-5.4"
    assert _provider_preferences_for_agent(agent) == {"order": ["openai"]}


def test_bad_request_time_override_degrades_to_global_routing(caplog):
    agent = _agent(
        _provider_routing_config={
            "order": ["global-provider"],
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {
                        "only": ["baidu"],
                        "ignore": ["baidu"],
                    },
                },
            },
        }
    )

    with caplog.at_level("WARNING"):
        result = _provider_preferences_for_agent(agent)
        second_result = _provider_preferences_for_agent(agent)

    assert result == {"order": ["global-provider"]}
    assert second_result == result
    assert "Invalid OpenRouter routing" in caplog.text
    assert sum("Invalid OpenRouter routing" in record.message for record in caplog.records) == 1


def test_request_time_builder_observes_current_provider_and_model(monkeypatch):
    calls = []

    def fake_resolve(*, provider, model, routing):
        calls.append((provider, model, routing))
        return {}

    from agent import agent_init

    monkeypatch.setattr(agent_init, "_resolve_openrouter_provider_routing", fake_resolve)
    agent = _agent(_provider_routing_config={"order": ["global-provider"]})

    _provider_preferences_for_agent(agent)
    agent.provider = "anthropic"
    agent.model = 123
    _provider_preferences_for_agent(agent)

    assert [(provider, model) for provider, model, _routing in calls] == [
        ("openrouter", "deepseek/deepseek-v4-flash"),
        ("anthropic", 123),
    ]


def test_invalid_override_and_global_warnings_describe_distinct_degradation_rungs(
    caplog,
):
    agent = _agent(
        _provider_routing_config={
            "only": "baidu",
            "allow_fallbacks": False,
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {"order": "baidu"},
                },
            },
        }
    )

    with caplog.at_level("WARNING"):
        assert _provider_preferences_for_agent(agent) == {}

    messages = [record.message for record in caplog.records]
    assert any("using global routing defaults" in message for message in messages)
    assert any("sending no provider preferences" in message for message in messages)


def test_provider_change_is_resolved_independently_of_model():
    agent = _agent(
        _provider_routing_config={
            "only": ["baidu"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
        }
    )

    agent.provider = "anthropic"
    assert _provider_preferences_for_agent(agent) == {}

    agent.provider = "openrouter"
    assert _provider_preferences_for_agent(agent) == {
        "only": ["baidu"],
        "quantizations": ["fp8"],
        "allow_fallbacks": False,
    }


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google", "nous"])
def test_openrouter_only_fields_are_not_sent_to_non_openrouter_providers(provider):
    agent = _agent(
        provider=provider,
        _provider_routing_config={"quantizations": ["fp8"], "allow_fallbacks": False},
    )
    result = _provider_preferences_for_agent(agent)

    assert "quantizations" not in result
    assert "allow_fallbacks" not in result


def test_legacy_provider_attributes_remain_available_without_raw_routing_config():
    agent = _agent(
        provider="anthropic",
        providers_allowed=["legacy-only"],
        providers_ignored=["legacy-ignore"],
        _provider_routing_config=None,
    )

    assert _provider_preferences_for_agent(agent) == {
        "only": ["legacy-only"],
        "ignore": ["legacy-ignore"],
    }


def test_nous_does_not_receive_openrouter_raw_routing_config():
    agent = _agent(
        provider="nous",
        _provider_routing_config={
            "only": ["baidu"],
            "ignore": ["fireworks"],
            "order": ["baidu"],
            "sort": "price",
            "require_parameters": True,
            "data_collection": "deny",
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
        },
    )

    assert _provider_preferences_for_agent(agent) == {}


def test_empty_tag_lock_is_rejected_instead_of_becoming_unrestricted():
    with pytest.raises(ValueError, match="allow_fallbacks.*only"):
        _resolve(
            routing={
                "model_overrides": {
                    "openrouter": {
                        "deepseek/deepseek-v4-flash": {
                            "only": [],
                            "allow_fallbacks": False,
                        },
                    },
                },
            }
        )


def test_empty_tag_lock_is_reported_by_config_validation():
    issues = validate_config_structure(
        {
            "provider_routing": {
                "model_overrides": {
                    "openrouter": {
                        "deepseek/deepseek-v4-flash": {
                            "only": [],
                            "allow_fallbacks": False,
                        },
                    },
                },
            }
        }
    )

    assert any(
        issue.severity == "error" and "allow_fallbacks" in issue.message
        for issue in issues
    )


def test_empty_tag_lock_without_model_overrides_is_reported_by_config_validation():
    issues = validate_config_structure(
        {"provider_routing": {"only": [], "allow_fallbacks": False}}
    )

    assert any(
        issue.severity == "error" and "allow_fallbacks" in issue.message
        for issue in issues
    )


def test_normalization_empty_lock_shapes_are_reported_by_config_validation():
    configs = [
        {"only": ["   "], "allow_fallbacks": False},
        {"only": "baidu", "allow_fallbacks": False},
        {
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {
                        "only": ["   "],
                        "allow_fallbacks": False,
                    },
                },
            },
        },
    ]

    for provider_routing in configs:
        issues = validate_config_structure({"provider_routing": provider_routing})
        assert any(
            issue.severity == "error" and "allow_fallbacks" in issue.message
            for issue in issues
        )


def test_whitespace_only_lock_tag_is_rejected_after_normalization():
    with pytest.raises(ValueError, match="must be a list of strings"):
        _resolve(routing={"only": ["   "], "allow_fallbacks": False})


def test_mixed_blank_and_valid_lock_tags_are_rejected_fail_closed():
    with pytest.raises(ValueError, match="must be a list of strings"):
        _resolve(routing={"only": ["  ", "baidu"], "allow_fallbacks": False})


def test_scalar_only_lock_is_rejected_at_runtime():
    with pytest.raises(ValueError, match="must be a list of strings"):
        _resolve(routing={"only": "baidu", "allow_fallbacks": False})


def test_model_override_clearing_inherited_lock_is_rejected():
    with pytest.raises(ValueError, match="allow_fallbacks.*only"):
        _resolve(
            routing={
                "only": ["baidu"],
                "allow_fallbacks": False,
                "model_overrides": {
                    "openrouter": {
                        "deepseek/deepseek-v4-flash": {"only": []},
                    },
                },
            }
        )


def test_scalar_global_routing_lists_degrade_without_partial_lock(caplog):
    for field in ("only", "order", "ignore", "quantizations"):
        routing = {
            field: "baidu",
            "only": ["baidu"] if field != "only" else "baidu",
            "allow_fallbacks": False,
        }
        agent = _agent(_provider_routing_config=routing)
        with caplog.at_level("WARNING"):
            preferences = _provider_preferences_for_agent(agent)

        assert not (
            preferences.get("allow_fallbacks") is False and not preferences.get("only")
        )
    assert "Invalid OpenRouter routing" in caplog.text


def test_scalar_model_override_lists_degrade_without_partial_lock(caplog):
    for field in ("only", "order", "ignore", "quantizations"):
        routing = {
            "only": ["baidu"],
            "allow_fallbacks": False,
            "model_overrides": {
                "openrouter": {
                    "deepseek/deepseek-v4-flash": {field: "baidu"},
                },
            },
        }
        agent = _agent(_provider_routing_config=routing)
        with caplog.at_level("WARNING"):
            preferences = _provider_preferences_for_agent(agent)

        assert not (
            preferences.get("allow_fallbacks") is False and not preferences.get("only")
        )
    assert "Invalid OpenRouter routing" in caplog.text
