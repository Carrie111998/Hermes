"""GigaChat tool-schema width sanitization tests."""

from agent.gigachat_schema import (
    MAX_GIGACHAT_PROPERTIES,
    is_gigachat_model,
    sanitize_gigachat_tool_parameters,
    sanitize_gigachat_tools,
)


def _params(count, required=None):
    return {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "description": f"d{i}"} for i in range(count)},
        **({"required": required} if required else {}),
    }


def test_is_gigachat_model_matches_gigachat_families_only():
    assert is_gigachat_model("GigaChat-2-Max")
    assert is_gigachat_model("gigachat")
    assert not is_gigachat_model("claude-opus-5")
    assert not is_gigachat_model("kimi-k2")
    assert not is_gigachat_model(None)


def test_narrow_schema_is_returned_unchanged_by_identity():
    params = _params(MAX_GIGACHAT_PROPERTIES)
    assert sanitize_gigachat_tool_parameters(params) is params


def test_wide_schema_is_trimmed_to_the_cap():
    out = sanitize_gigachat_tool_parameters(_params(20))
    assert len(out["properties"]) == MAX_GIGACHAT_PROPERTIES


def test_required_properties_survive_trimming():
    # p19 sits past the cap in declaration order but is required.
    out = sanitize_gigachat_tool_parameters(_params(20, required=["p19"]))
    assert "p19" in out["properties"]
    assert len(out["properties"]) == MAX_GIGACHAT_PROPERTIES


def test_required_set_wider_than_the_cap_is_kept_whole():
    required = [f"p{i}" for i in range(12)]
    out = sanitize_gigachat_tool_parameters(_params(20, required=required))
    assert all(name in out["properties"] for name in required)


def test_trimming_preserves_declaration_order():
    out = sanitize_gigachat_tool_parameters(_params(20, required=["p19"]))
    names = list(out["properties"])
    assert names == sorted(names, key=lambda n: int(n[1:]))


def test_original_schema_is_not_mutated():
    params = _params(20)
    before = list(params["properties"])
    sanitize_gigachat_tool_parameters(params)
    assert list(params["properties"]) == before


def test_sanitize_tools_returns_identity_when_nothing_is_wide():
    tools = [{"type": "function", "function": {"name": "t", "parameters": _params(3)}}]
    assert sanitize_gigachat_tools(tools) is tools


def test_sanitize_tools_trims_only_the_wide_tool():
    tools = [
        {"type": "function", "function": {"name": "narrow", "parameters": _params(3)}},
        {"type": "function", "function": {"name": "wide", "parameters": _params(20)}},
    ]
    out = sanitize_gigachat_tools(tools)
    assert len(out[0]["function"]["parameters"]["properties"]) == 3
    assert len(out[1]["function"]["parameters"]["properties"]) == MAX_GIGACHAT_PROPERTIES


def test_malformed_tools_pass_through_untouched():
    tools = [{"no_function": True}, "not-a-dict", {"type": "function", "function": {"name": "x"}}]
    assert sanitize_gigachat_tools(tools) == tools
    assert sanitize_gigachat_tools([]) == []


def test_shipped_cronjob_schema_keeps_its_core_fields():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    out = sanitize_gigachat_tools([{"type": "function", "function": CRONJOB_SCHEMA}])
    kept = out[0]["function"]["parameters"]["properties"]
    assert len(kept) == MAX_GIGACHAT_PROPERTIES
    for field in ("action", "schedule", "prompt", "job_id"):
        assert field in kept, f"{field} must survive for cronjob to be usable"
