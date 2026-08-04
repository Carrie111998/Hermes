"""Regression coverage for operator-configured delegation routes."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as delegate_tool
from hermes_cli.config_defaults import DEFAULT_CONFIG
from run_agent import AIAgent


@pytest.fixture
def route_config() -> dict:
    return {
        "model": "fallback-model",
        "provider": "fallback-provider",
        "reasoning_effort": "high",
        "default_route": "luna",
        "routes": {
            "luna": {
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "description": "Mechanical JSON, CSV, logs, and test loops.",
            },
            "sol": {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
                "description": "Quality-sensitive implementation and review.",
            },
        },
    }


def _parent() -> MagicMock:
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "parent-model"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._interrupt_requested = False
    return parent


def test_default_config_exposes_named_route_container() -> None:
    cfg = DEFAULT_CONFIG["delegation"]
    assert cfg["default_route"] == ""
    assert cfg["routes"] == {}


def test_default_and_explicit_routes_are_allowlisted(route_config: dict) -> None:
    default_cfg, default_name = delegate_tool._resolve_delegation_route(
        route_config, None
    )
    explicit_cfg, explicit_name = delegate_tool._resolve_delegation_route(
        route_config, "sol"
    )

    assert default_name == "luna"
    assert default_cfg["model"] == "gpt-5.6-luna"
    assert default_cfg["reasoning_effort"] == "low"
    assert explicit_name == "sol"
    assert explicit_cfg["model"] == "gpt-5.6-sol"
    assert explicit_cfg["provider"] == "openai-codex"


def test_unknown_route_reports_available_names(route_config: dict) -> None:
    with pytest.raises(ValueError, match="Unknown delegation route 'missing'") as exc:
        delegate_tool._resolve_delegation_route(route_config, "missing")
    assert "luna" in str(exc.value)
    assert "sol" in str(exc.value)


def test_dynamic_schema_advertises_only_configured_routes(route_config: dict) -> None:
    with patch.object(delegate_tool, "_load_config", return_value=route_config):
        overrides = delegate_tool._build_dynamic_schema_overrides()

    props = overrides["parameters"]["properties"]
    assert props["route"]["enum"] == ["luna", "sol"]
    assert props["route"]["default"] == "luna"
    assert props["tasks"]["items"]["properties"]["route"]["enum"] == [
        "luna",
        "sol",
    ]
    assert "Mechanical JSON" in props["route"]["description"]
    assert "Quality-sensitive" in props["route"]["description"]


def test_agent_dispatch_forwards_top_level_route() -> None:
    parent = _parent()
    with patch("tools.delegate_tool.delegate_task", return_value="{}") as delegated:
        AIAgent._dispatch_delegate_task(
            parent,
            {"goal": "review the change", "route": "sol"},
        )

    assert delegated.call_args.kwargs["route"] == "sol"


def test_mixed_route_batch_reaches_child_construction(route_config: dict) -> None:
    parent = _parent()
    children = [MagicMock(model="gpt-5.6-luna"), MagicMock(model="gpt-5.6-sol")]

    def resolve(cfg: dict, _parent_agent) -> dict:
        return {
            "model": cfg.get("model"),
            "provider": cfg.get("provider"),
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
            "command": None,
            "args": None,
        }

    with (
        patch.object(delegate_tool, "_load_config", return_value=route_config),
        patch.object(
            delegate_tool,
            "_resolve_delegation_credentials",
            side_effect=resolve,
        ),
        patch.object(
            delegate_tool,
            "_build_child_preserving_parent_tools",
            side_effect=children,
        ) as build_child,
        patch.object(
            delegate_tool,
            "_run_single_child",
            side_effect=[
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "L",
                    "api_calls": 1,
                    "duration_seconds": 0.1,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "S",
                    "api_calls": 1,
                    "duration_seconds": 0.1,
                },
            ],
        ),
        patch.object(delegate_tool, "_finalize_child_results", return_value=None),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=("test-delegation", [], []),
        ),
        patch("tools.delegation_live_log.update_manifest_statuses"),
    ):
        result = json.loads(
            delegate_tool.delegate_task(
                tasks=[
                    {"goal": "format json", "route": "luna"},
                    {"goal": "review code", "route": "sol"},
                ],
                background=False,
                parent_agent=parent,
            )
        )

    assert [entry["summary"] for entry in result["results"]] == ["L", "S"]
    assert [entry["route"] for entry in result["results"]] == ["luna", "sol"]
    kwargs = [call.kwargs for call in build_child.call_args_list]
    assert [item["model"] for item in kwargs] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert [item["override_reasoning_effort"] for item in kwargs] == [
        "low",
        "medium",
    ]
    assert [item["route"] for item in kwargs] == ["luna", "sol"]
