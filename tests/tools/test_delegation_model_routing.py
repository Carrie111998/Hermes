"""End-to-end delegation model-evidence routing contracts (#98934)."""

import json
from unittest.mock import MagicMock

import tools.delegate_tool as dt


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.model = "parent/model"
    parent.provider = "parent-provider"
    parent.base_url = "https://parent.example/v1"
    parent.api_key = "test-key"
    parent.session_id = "parent-session"
    parent._active_children = []
    parent._active_children_lock = None
    parent.enabled_toolsets = []
    parent.disabled_toolsets = []
    parent._session_db = None
    parent._interrupt_requested = False
    return parent


def _resolved_creds(cfg, _parent_agent):
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


def test_model_facing_batch_schema_exposes_per_task_route_overrides():
    properties = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_properties = properties["tasks"]["items"]["properties"]

    assert "model" in task_properties
    assert "provider" in task_properties
    assert "model" not in properties
    assert "provider" not in properties


def test_batch_retains_inherited_and_per_task_model_evidence(monkeypatch):
    parent = _parent()
    built = []

    def build_child(**kwargs):
        child = MagicMock()
        child.model = kwargs.get("model") or parent.model
        child.provider = kwargs.get("override_provider") or parent.provider
        child._delegate_role = "leaf"
        child._subagent_id = f"child-{len(built)}"
        child._parent_session_id = parent.session_id
        child._parent_subagent_id = None
        child._delegate_depth = 1
        built.append(child)
        return child

    def run_child(task_index, child=None, **_kwargs):
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "exit_reason": "completed",
            "model_evidence": child._delegation_model_evidence,
        }

    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", _resolved_creds)
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build_child)
    monkeypatch.setattr(dt, "_run_single_child", run_child)
    monkeypatch.setattr(
        "tools.delegation_live_log.create_live_transcripts",
        lambda *args, **kwargs: (None, [None, None], []),
    )

    payload = json.loads(
        dt.delegate_task(
            tasks=[
                {
                    "goal": "Use an explicit route for this sufficiently detailed task",
                    "provider": "task-provider",
                    "model": "task/model",
                },
                {
                    "goal": "Inherit the parent route for this other detailed task",
                },
            ],
            parent_agent=parent,
        )
    )

    explicit = payload["results"][0]["model_evidence"]
    inherited = payload["results"][1]["model_evidence"]
    assert explicit["selection_source"] == "task"
    assert explicit["requested"] == {
        "provider": "task-provider",
        "model": "task/model",
    }
    assert explicit["resolved"] == {
        "provider": "task-provider",
        "model": "task/model",
    }
    assert inherited["selection_source"] == "parent"
    assert inherited["requested"] == {
        "provider": "parent-provider",
        "model": "parent/model",
    }
    assert inherited["resolved"] == {
        "provider": "parent-provider",
        "model": "parent/model",
    }


def test_top_level_provider_requires_top_level_model():
    payload = json.loads(
        dt.delegate_task(
            tasks=[{"goal": "A sufficiently detailed delegated task goal"}],
            provider="other-provider",
            parent_agent=_parent(),
        )
    )

    assert "requires 'model'" in payload["error"]
