from hermes_cli.oneshot import _normalize_toolsets, _validate_explicit_toolsets


def test_none_toolset_is_an_explicit_empty_tool_list():
    toolsets, error = _validate_explicit_toolsets("none")
    assert error is None
    assert toolsets == []


def test_explicit_empty_tool_list_survives_agent_construction_normalization():
    assert _normalize_toolsets([]) == []
    assert _normalize_toolsets(None) is None
