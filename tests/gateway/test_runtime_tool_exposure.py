from __future__ import annotations

import json

import pytest

from gateway.runtime_tool_exposure import build_runtime_tool_exposure


def _definition(name: str, *, exposure: str = "") -> dict:
    value = {
        "name": name,
        "description": f"Use {name} for media work",
        "input_schema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
        },
    }
    if exposure:
        value["exposure"] = exposure
    return value


def _schema(definition: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": definition["input_schema"],
        },
    }


def test_runtime_exposure_separates_direct_deferred_and_hidden_tools():
    definitions = [
        _definition("ask_user_question"),
        _definition("media.generate_image"),
        _definition("platform.internal_reconcile", exposure="hidden"),
    ]
    exposure = build_runtime_tool_exposure(
        definitions,
        [_schema(item) for item in definitions],
    )

    visible_names = {
        item["function"]["name"] for item in exposure.model_schemas
    }
    assert visible_names == {
        "ask_user_question",
        "tool_search",
        "tool_describe",
        "tool_call",
    }
    assert exposure.deferred_names == {"media.generate_image"}
    assert exposure.hidden_names == {"platform.internal_reconcile"}

    search = json.loads(exposure.search({"query": "generate image"}))
    assert [item["name"] for item in search["matches"]] == [
        "media.generate_image",
    ]
    described = json.loads(exposure.describe({"name": "media.generate_image"}))
    assert described["parameters"] == definitions[1]["input_schema"]
    name, arguments, error = exposure.resolve_call({
        "name": "media.generate_image",
        "arguments": {"prompt": "a studio portrait"},
    })
    assert (name, arguments, error) == (
        "media.generate_image",
        {"prompt": "a studio portrait"},
        None,
    )


def test_hidden_and_direct_tools_cannot_be_invoked_through_tool_call():
    definitions = [
        _definition("ask_user_question"),
        _definition("platform.internal_reconcile", exposure="hidden"),
    ]
    exposure = build_runtime_tool_exposure(
        definitions,
        [_schema(item) for item in definitions],
    )
    for name in ("ask_user_question", "platform.internal_reconcile"):
        resolved, arguments, error = exposure.resolve_call({
            "name": name,
            "arguments": {},
        })
        assert resolved is None
        assert arguments == {}
        assert "not a deferred Tool" in error


def test_runtime_exposure_rejects_reserved_or_invalid_classification():
    for definition in (
        _definition("tool_call"),
        _definition("media.generate_image", exposure="sometimes"),
    ):
        with pytest.raises(ValueError):
            build_runtime_tool_exposure([definition], [_schema(definition)])
