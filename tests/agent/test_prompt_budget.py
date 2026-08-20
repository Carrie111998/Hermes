"""Coarse regression guards for the fixed model-visible prompt surface."""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli.prompt_size import _compute_tools_breakdown, compute_prompt_breakdown

# Measured against a fresh default CLI profile on 2026-08-20. The fixed prompt
# was 83,180 bytes and the largest tool description was 4,333 characters.
# These ceilings leave roughly 50% headroom so ordinary edits do not create a
# threshold ratchet; they catch the return of an entire block/tool surface.
MAX_FIXED_PREFIX_BYTES = 125_000
MAX_TOOL_DESCRIPTION_CHARS = 7_000


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        upper = name.upper()
        if any(marker in upper for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            monkeypatch.delenv(name, raising=False)
        elif upper.startswith(("XAI_", "HASS_", "HOMEASSISTANT_")):
            monkeypatch.delenv(name, raising=False)
    return hermes_home


def test_default_cli_fixed_prefix_stays_within_coarse_budget(isolated_home):
    data = compute_prompt_breakdown("cli")
    fixed_bytes = data["system_prompt"]["bytes"] + data["tools"]["json_bytes"]

    assert fixed_bytes <= MAX_FIXED_PREFIX_BYTES, {
        "fixed_bytes": fixed_bytes,
        "system_prompt_bytes": data["system_prompt"]["bytes"],
        "tool_schema_bytes": data["tools"]["json_bytes"],
        "largest_toolsets": data["toolsets_breakdown"][:5],
    }


def test_no_default_tool_description_is_an_essay(isolated_home):
    data = compute_prompt_breakdown("cli")
    over = [
        row
        for row in data["tools_breakdown"]
        if row["description_chars"] > MAX_TOOL_DESCRIPTION_CHARS
    ]

    assert not over, over


def test_tool_breakdown_exposes_oversized_description_for_diagnosis():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "small",
                "description": "short",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "oversized",
                "description": "x" * (MAX_TOOL_DESCRIPTION_CHARS + 1),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "schema-heavy",
                "description": "small description",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "choice": {"type": "string", "enum": ["x" * 8_000]},
                    },
                },
            },
        },
    ]

    rows = _compute_tools_breakdown(tools)

    assert rows[0]["name"] == "oversized"
    assert rows[0]["description_chars"] == MAX_TOOL_DESCRIPTION_CHARS + 1
    by_name = {row["name"]: row for row in rows}
    for tool in tools:
        name = tool["function"]["name"]
        assert by_name[name]["json_bytes"] == len(
            json.dumps(tool, ensure_ascii=False).encode("utf-8")
        )
    assert by_name["schema-heavy"]["json_bytes"] > by_name["oversized"]["json_bytes"]
