"""Behavior contract for config-controlled delegate_task capability lanes."""

import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_child_agent,
    _build_dynamic_schema_overrides,
    _resolve_task_lane_routing,
    delegate_task,
)


LANE_CONFIG = {
    "model": "",
    "provider": "",
    "reasoning_effort": "",
    "lanes": {
        "explore": {
            "provider": "openrouter",
            "model": "x-ai/grok-4.5",
            "reasoning_effort": "low",
        },
        "engineer": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
        },
    },
}


def _parent():
    parent = MagicMock()
    parent.base_url = "https://chatgpt.com/backend-api/codex"
    parent.api_key = "test-only"
    parent.api_mode = "codex_responses"
    parent.provider = "openai-codex"
    parent.model = "gpt-5.6-sol"
    parent.platform = "cli"
    parent.enabled_toolsets = []
    parent.disabled_toolsets = []
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    return parent


def _fake_credentials(config, _parent_agent):
    return {
        "model": config.get("model") or None,
        "provider": config.get("provider") or None,
        "base_url": "https://example.invalid/v1",
        "api_key": "test-only",
        "api_mode": "chat_completions",
        "request_overrides": None,
        "max_output_tokens": None,
    }


def test_static_schema_exposes_only_lane_not_raw_routing_fields():
    parameters = cast(dict[str, Any], DELEGATE_TASK_SCHEMA["parameters"])
    props = cast(dict[str, Any], parameters["properties"])
    task_props = cast(
        dict[str, Any], props["tasks"]["items"]["properties"]
    )

    assert props["lane"]["enum"] == ["explore", "engineer", "review"]
    assert task_props["lane"]["enum"] == ["explore", "engineer", "review"]
    for forbidden in ("model", "provider", "reasoning_effort", "base_url", "api_key"):
        assert forbidden not in props
        assert forbidden not in task_props


def test_dynamic_schema_only_advertises_operator_configured_lanes():
    with patch("tools.delegate_tool._load_config", return_value=LANE_CONFIG):
        schema = _build_dynamic_schema_overrides()["parameters"]

    props = schema["properties"]
    assert props["lane"]["enum"] == ["explore", "engineer"]
    assert props["tasks"]["items"]["properties"]["lane"]["enum"] == [
        "explore",
        "engineer",
    ]
    assert "configured lanes: explore, engineer" in props["lane"]["description"]


def test_dynamic_schema_hides_lane_when_operator_config_has_none():
    with patch("tools.delegate_tool._load_config", return_value={"lanes": {}}):
        schema = _build_dynamic_schema_overrides()["parameters"]

    props = schema["properties"]
    assert "lane" not in props
    assert "lane" not in props["tasks"]["items"]["properties"]


def test_dynamic_schema_excludes_incomplete_lane():
    config = {
        "lanes": {
            "explore": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.5",
            },
            "engineer": cast(dict[str, Any], LANE_CONFIG["lanes"])["engineer"],
        }
    }

    with patch("tools.delegate_tool._load_config", return_value=config):
        schema = _build_dynamic_schema_overrides()["parameters"]

    props = schema["properties"]
    assert props["lane"]["enum"] == ["engineer"]
    assert props["tasks"]["items"]["properties"]["lane"]["enum"] == [
        "engineer"
    ]


def test_mixed_batch_resolves_each_unique_lane_once_and_per_task_wins():
    tasks = [
        {"goal": "research", "lane": "explore"},
        {"goal": "build"},
        {"goal": "review", "lane": "explore"},
    ]

    with patch(
        "tools.delegate_tool._resolve_delegation_credentials",
        side_effect=_fake_credentials,
    ) as resolver:
        routes = _resolve_task_lane_routing(
            LANE_CONFIG,
            tasks,
            top_lane="engineer",
            parent_agent=_parent(),
        )

    assert [route["lane"] for route in routes] == [
        "explore",
        "engineer",
        "explore",
    ]
    assert [route["credentials"]["model"] for route in routes] == [
        "x-ai/grok-4.5",
        "gpt-5.6-luna",
        "x-ai/grok-4.5",
    ]
    assert [route["reasoning_effort"] for route in routes] == [
        "low",
        "medium",
        "low",
    ]
    assert resolver.call_count == 2


def test_registry_handler_forwards_lane_and_filters_task_routing_fields():
    from tools.registry import registry

    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
        result = registry.dispatch(
            "delegate_task",
            {
                "goal": "task",
                "lane": "engineer",
                "tasks": [
                    {
                        "goal": "nested",
                        "lane": "explore",
                        "model": "must-not-pass",
                        "provider": "must-not-pass",
                        "reasoning_effort": "must-not-pass",
                        "base_url": "must-not-pass",
                        "api_key": "must-not-pass",
                        "request_overrides": {"must-not-pass": True},
                        "max_output_tokens": 1,
                        "command": "must-not-pass",
                        "args": ["must-not-pass"],
                    }
                ],
            },
            parent_agent=_parent(),
        )

    assert result == "{}"
    assert captured["lane"] == "engineer"
    assert captured["tasks"] == [{"goal": "nested", "lane": "explore"}]


def test_delegate_task_builds_each_batch_child_from_its_resolved_lane():
    built = []
    child = MagicMock()
    run_results = [
        {
            "task_index": 0,
            "status": "completed",
            "summary": "explored",
            "api_calls": 1,
            "duration_seconds": 0.1,
        },
        {
            "task_index": 1,
            "status": "completed",
            "summary": "engineered",
            "api_calls": 1,
            "duration_seconds": 0.1,
        },
    ]

    def fake_build(**kwargs):
        built.append(kwargs)
        return child

    with (
        patch("tools.delegate_tool._load_config", return_value=LANE_CONFIG),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            side_effect=_fake_credentials,
        ),
        patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=fake_build),
        patch("tools.delegate_tool._run_single_child", side_effect=run_results),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=("delegation-id", [], []),
        ),
        patch("tools.delegation_live_log.update_manifest_statuses"),
    ):
        result = json.loads(
            delegate_task(
                lane="engineer",
                tasks=[
                    {"goal": "research", "lane": "explore"},
                    {"goal": "build"},
                ],
                parent_agent=_parent(),
            )
        )

    assert [kwargs["override_provider"] for kwargs in built] == [
        "openrouter",
        "openai-codex",
    ]
    assert [kwargs["model"] for kwargs in built] == [
        "x-ai/grok-4.5",
        "gpt-5.6-luna",
    ]
    assert [kwargs["override_reasoning_effort"] for kwargs in built] == [
        "low",
        "medium",
    ]
    assert [entry["summary"] for entry in result["results"]] == [
        "explored",
        "engineered",
    ]


def test_no_lane_preserves_legacy_global_override_resolution():
    cfg = {
        "provider": "openrouter",
        "model": "legacy/model",
        "reasoning_effort": "high",
    }
    tasks = [{"goal": "one"}, {"goal": "two"}]
    parent = _parent()

    with patch(
        "tools.delegate_tool._resolve_delegation_credentials",
        side_effect=_fake_credentials,
    ) as resolver:
        routes = _resolve_task_lane_routing(
            cfg,
            tasks,
            top_lane=None,
            parent_agent=parent,
        )

    assert [route["lane"] for route in routes] == [None, None]
    assert [route["credentials"]["model"] for route in routes] == [
        "legacy/model",
        "legacy/model",
    ]
    assert [route["reasoning_effort"] for route in routes] == [None, None]
    resolver.assert_called_once_with(cfg, parent)


@pytest.mark.parametrize("requested", ["cheap", "gpt-5.6-luna", "EXPLORE", 7])
def test_unknown_or_non_string_lane_fails_closed(requested):
    with pytest.raises(ValueError, match="lane"):
        _resolve_task_lane_routing(
            LANE_CONFIG,
            [{"goal": "task", "lane": requested}],
            top_lane=None,
            parent_agent=_parent(),
        )


def test_unconfigured_lane_fails_closed_before_credentials_are_resolved():
    with patch("tools.delegate_tool._resolve_delegation_credentials") as resolver:
        with pytest.raises(ValueError, match="review.*not configured"):
            _resolve_task_lane_routing(
                LANE_CONFIG,
                [{"goal": "task", "lane": "review"}],
                top_lane=None,
                parent_agent=_parent(),
            )
    resolver.assert_not_called()


def test_disabled_lane_fails_closed_before_child_construction():
    cfg = {
        "lanes": {
            "review": {
                "enabled": False,
            }
        }
    }
    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("tools.delegate_tool._build_child_preserving_parent_tools") as builder,
    ):
        response = json.loads(
            delegate_task(
                goal="must not spawn",
                lane="review",
                parent_agent=_parent(),
            )
        )

    assert "unsupported fields" in response["error"]
    builder.assert_not_called()


def test_lane_config_rejects_secret_or_transport_escape_hatches():
    cfg = {
        "lanes": {
            "explore": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.5",
                "reasoning_effort": "low",
                "api_key": "must-not-be-accepted",
            }
        }
    }
    with pytest.raises(ValueError, match="unsupported fields.*api_key"):
        _resolve_task_lane_routing(
            cfg,
            [{"goal": "task", "lane": "explore"}],
            top_lane=None,
            parent_agent=_parent(),
        )


def test_lane_config_requires_provider_model_and_valid_effort():
    for spec, error in [
        ({"model": "m", "reasoning_effort": "low"}, "provider"),
        ({"provider": "openrouter", "reasoning_effort": "low"}, "model"),
        ({"provider": "openrouter", "model": "m"}, "reasoning_effort"),
        (
            {
                "provider": "openrouter",
                "model": "m",
                "reasoning_effort": "impossible",
            },
            "reasoning_effort",
        ),
    ]:
        with pytest.raises(ValueError, match=error):
            _resolve_task_lane_routing(
                {"lanes": {"explore": spec}},
                [{"goal": "task", "lane": "explore"}],
                top_lane=None,
                parent_agent=_parent(),
            )


def test_run_agent_fast_path_forwards_lane_and_strips_raw_routing_fields():
    import run_agent

    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "{}"

    parent = _parent()
    with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
        run_agent.AIAgent._dispatch_delegate_task(
            parent,
            {
                "goal": "task",
                "lane": "engineer",
                "tasks": [
                    {
                        "goal": "nested",
                        "lane": "review",
                        "model": "must-not-pass",
                        "provider": "must-not-pass",
                        "reasoning_effort": "must-not-pass",
                        "base_url": "must-not-pass",
                        "api_key": "must-not-pass",
                        "request_overrides": {"must-not-pass": True},
                        "max_output_tokens": 1,
                        "command": "must-not-pass",
                        "args": ["must-not-pass"],
                    }
                ],
            },
        )

    assert captured["lane"] == "engineer"
    assert captured["tasks"] == [{"goal": "nested", "lane": "review"}]


def test_lane_reasoning_override_beats_global_delegation_effort():
    parent = _parent()
    parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

    with (
        patch(
            "tools.delegate_tool._load_config",
            return_value={"reasoning_effort": "high"},
        ),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        agent_cls.return_value = MagicMock()
        _build_child_agent(
            task_index=0,
            goal="task",
            context=None,
            toolsets=None,
            model="gpt-5.6-luna",
            max_iterations=50,
            task_count=1,
            parent_agent=parent,
            override_provider="openai-codex",
            override_reasoning_effort="medium",
        )

    assert agent_cls.call_args.kwargs["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }
