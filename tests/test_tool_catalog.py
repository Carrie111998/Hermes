"""Unit tests for tool_catalog — the selectable surface + token estimates."""

import tool_catalog


# ── Pure estimation helpers ──────────────────────────────────────────────────

def test_est_tokens_from_chars_rounds_up():
    # chars/4 with ceil: 5 chars -> 2 tokens, 8 -> 2, 9 -> 3.
    assert tool_catalog._est_tokens_from_chars(5) == 2
    assert tool_catalog._est_tokens_from_chars(8) == 2
    assert tool_catalog._est_tokens_from_chars(9) == 3


def test_est_tokens_from_chars_non_positive_is_zero():
    assert tool_catalog._est_tokens_from_chars(0) == 0
    assert tool_catalog._est_tokens_from_chars(-3) == 0


def test_schema_tokens_positive_for_real_schema():
    schema = {"function": {"name": "x", "description": "does a thing"}}
    assert tool_catalog._schema_tokens(schema) > 0


def test_schema_tokens_handles_unserializable():
    # Falls back to str() rather than raising.
    assert tool_catalog._schema_tokens({"bad": {1, 2, 3}}) > 0


def test_skill_index_tokens_described_costs_more_than_bare():
    described = tool_catalog._skill_index_tokens("pdf", "Read and write PDFs")
    bare = tool_catalog._skill_index_tokens("pdf", "")
    assert described > bare > 0


# ── build_catalog integration ────────────────────────────────────────────────

def test_build_catalog_shape_and_core_tokens():
    cat = tool_catalog.build_catalog()
    assert set(cat.keys()) == {"core_tokens", "toolsets", "mcp_servers", "skills"}
    assert isinstance(cat["toolsets"], list)
    assert isinstance(cat["mcp_servers"], list)
    assert isinstance(cat["skills"], list)
    # No unconditional baseline: every tool is surfaced under its toolset.
    assert cat["core_tokens"] == 0


def test_build_catalog_toolset_est_tokens_is_sum_of_its_tools():
    cat = tool_catalog.build_catalog()
    assert cat["toolsets"], "expected at least one toolset in the catalog"
    for ts in cat["toolsets"]:
        assert ts["est_tokens"] == sum(t["est_tokens"] for t in ts["tools"])
        # Tools are sorted by name for stable rendering.
        names = [t["name"] for t in ts["tools"]]
        assert names == sorted(names)
