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
        _definition("media.generate_image", exposure="deferred"),
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
    }
    assert exposure.deferred_names == {"media.generate_image"}
    assert exposure.hidden_names == {"platform.internal_reconcile"}
    assert exposure.is_callable("ask_user_question") is True
    assert exposure.is_callable("media.generate_image") is False

    search = json.loads(exposure.search_and_activate({"query": "generate image"}))
    assert [item["name"] for item in search["matches"]] == [
        "media.generate_image",
    ]
    assert search["loaded_tools"] == ["media.generate_image"]
    assert search["already_loaded"] == []
    assert search["callable_on_next_step"] is True
    assert exposure.is_callable("media.generate_image") is True
    assert {
        item["function"]["name"] for item in exposure.model_schemas
    } == {"ask_user_question", "media.generate_image", "tool_search"}

    repeated = json.loads(exposure.search_and_activate({"query": "generate image"}))
    assert repeated["already_loaded"] == ["media.generate_image"]

    no_match = json.loads(exposure.search_and_activate({"query": "quantum ledger"}))
    assert no_match["loaded_tools"] == []
    assert no_match["callable_on_next_step"] is False


def test_small_runtime_tool_surface_stays_direct_without_search_bridges():
    definitions = [
        _definition("ask_user_question"),
        _definition("media.generate_image"),
        _definition("media.generate_video"),
        _definition("platform.prompt_enhance"),
    ]
    exposure = build_runtime_tool_exposure(
        definitions,
        [_schema(item) for item in definitions],
    )

    assert [
        item["function"]["name"] for item in exposure.model_schemas
    ] == [item["name"] for item in definitions]
    assert exposure.deferred_names == set()


def test_large_runtime_tool_surface_stays_direct_without_explicit_exposure():
    definition = _definition("media.generate_image")
    definition["description"] = "generate image " + ("x" * 80_000)
    exposure = build_runtime_tool_exposure(
        [definition],
        [_schema(definition)],
    )

    assert exposure.deferred_names == set()
    assert {
        item["function"]["name"] for item in exposure.model_schemas
    } == {"media.generate_image"}


def test_deferred_tool_search_recognizes_exact_name_inside_chinese_query():
    definition = _definition("media.generate_image", exposure="deferred")
    exposure = build_runtime_tool_exposure(
        [definition],
        [_schema(definition)],
    )

    result = json.loads(exposure.search_and_activate({
        "query": "请找到 media.generate_image 这个图片生成工具",
    }))
    assert [item["name"] for item in result["matches"]] == [
        "media.generate_image",
    ]
    assert result["loaded_tools"] == ["media.generate_image"]
    assert "media.generate_image" in {
        item["function"]["name"] for item in exposure.model_schemas
    }


def test_hidden_tools_never_become_callable():
    definitions = [
        _definition("ask_user_question"),
        _definition("platform.internal_reconcile", exposure="hidden"),
    ]
    exposure = build_runtime_tool_exposure(
        definitions,
        [_schema(item) for item in definitions],
    )
    assert exposure.is_callable("ask_user_question") is True
    exposure.activate_names({"platform.internal_reconcile"})
    assert exposure.is_callable("platform.internal_reconcile") is False


def test_runtime_exposure_rejects_reserved_or_invalid_classification():
    for definition in (
        _definition("tool_call"),
        _definition("media.generate_image", exposure="sometimes"),
    ):
        with pytest.raises(ValueError):
            build_runtime_tool_exposure([definition], [_schema(definition)])
