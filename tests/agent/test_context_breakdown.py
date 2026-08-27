"""Tests for live session context breakdown."""

from unittest.mock import MagicMock, patch

from agent.context_breakdown import compute_session_context_breakdown


def _make_agent(
    *,
    stable: str = "identity and guidance",
    context: str = "",
    volatile: str = "timestamp line",
    tools: list | None = None,
    context_length: int = 200_000,
    last_prompt_tokens: int = 0,
):
    agent = MagicMock()
    agent.model = "openai/gpt-5.4"
    agent.tools = tools or [
        {"type": "function", "function": {"name": "terminal", "description": "run"}},
        {"type": "function", "function": {"name": "mcp_demo_tool", "description": "mcp"}},
        {"type": "function", "function": {"name": "delegate_task", "description": "spawn"}},
    ]
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent.context_compressor = MagicMock(
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
    )
    return agent, {"stable": stable, "context": context, "volatile": volatile}


def test_breakdown_includes_major_categories():
    stable = (
        "base guidance\n"
        "<available_skills>\n  demo:\n    - hello: hi\n</available_skills>"
    )
    context = "# Project Context\nFollow AGENTS.md"
    volatile = "Current time: now"
    history = [{"role": "user", "content": "hello there"}]
    agent, parts = _make_agent(stable=stable, context=context, volatile=volatile)

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    ids = {item["id"] for item in data["categories"]}
    assert {"system_prompt", "tool_definitions", "rules", "skills", "mcp", "subagent_definitions", "conversation"} <= ids
    assert data["context_max"] == 200_000
    assert data["estimated_total"] > 0



# ── /context renderers (pure functions over the payload) ────────────────────

from agent.context_breakdown import (  # noqa: E402
    compute_context_details,
    render_context_breakdown_lines,
    render_context_category_lines,
    render_context_details_lines,
    render_context_grid,
)


def _payload(**overrides):
    base = {
        "categories": [
            {"id": "system_prompt", "label": "System prompt", "tokens": 10_000},
            {"id": "tool_definitions", "label": "Tool definitions", "tokens": 20_000},
            {"id": "skills", "label": "Skills", "tokens": 5_000},
            {"id": "conversation", "label": "Conversation", "tokens": 15_000},
        ],
        "context_max": 200_000,
        "context_percent": 25,
        "context_used": 50_000,
        "estimated_total": 50_000,
        "model": "openai/gpt-test",
    }
    base.update(overrides)
    return base


def test_grid_is_5x20_and_mostly_free():
    rows = render_context_grid(_payload())
    assert len(rows) == 5
    cells = " ".join(rows).split(" ")
    assert len(cells) == 100
    # 50k / 200k → 25 used cells, 75 free
    assert cells.count("·") == 75
    # Category glyphs proportional: 10k→5, 20k→10, 5k→2-3, 15k→7-8 cells
    assert cells.count("■") == 5
    assert cells.count("▣") == 10










def test_breakdown_lines_grid_toggle():
    with_grid = render_context_breakdown_lines(_payload(), grid=True)
    without = render_context_breakdown_lines(_payload(), grid=False)
    assert any("·" in line for line in with_grid[:5])
    assert not any("·" in line for line in without[:2])
    # Both include the window summary and the expand hint
    for lines in (with_grid, without):
        text = "\n".join(lines)
        assert "Context window: 50,000 / 200,000 tokens (25%)" in text
        assert "/context all" in text




def test_details_lines_caps_listing():
    details = {
        "skills": [
            {"name": f"skill-{i}", "index_tokens": 10, "skill_md_tokens": 100}
            for i in range(20)
        ],
        "toolsets": [],
    }
    lines = render_context_details_lines(details)
    assert any("… and 5 more" in line for line in lines)




# ── Files carved out of the conversation blob ───────────────────────────────


def _file_call(call_id: str, name: str, arguments: str):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _history_with_file_io():
    body = "x" * 4_000

    return [
        {"role": "user", "content": "read the config"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("c1", "read_file", '{"path": "/repo/app.py"}')],
        },
        {"role": "tool", "tool_call_id": "c1", "content": body},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                _file_call("c2", "write_file", '{"path": "/repo/app.py", "content": "%s"}' % body)
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("c3", "terminal", '{"command": "ls"}')],
        },
        {"role": "tool", "tool_call_id": "c3", "content": "app.py"},
    ]


def _breakdown_for(history):
    agent, parts = _make_agent()

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        return compute_session_context_breakdown(agent, history)


def _category(data, category_id):
    return next((item for item in data["categories"] if item["id"] == category_id), None)


def test_files_category_carries_tokens_and_a_distinct_file_count():
    data = _breakdown_for(_history_with_file_io())
    files = _category(data, "files")

    assert files is not None
    assert files["tokens"] > 0
    # /repo/app.py was read AND written — one distinct file, not two.
    assert files["count"] == 1
    assert files["color"] == "var(--context-usage-files)"


def test_file_tokens_come_out_of_conversation_not_on_top_of_it():
    history = _history_with_file_io()
    with_files = _breakdown_for(history)

    from agent.model_metadata import estimate_messages_tokens_rough

    transcript = estimate_messages_tokens_rough(history)
    files = _category(with_files, "files")
    conversation = _category(with_files, "conversation")

    assert files["tokens"] + conversation["tokens"] == transcript
    # Neither side may be swallowed: an equality that only holds because the
    # clamp zeroed Conversation would prove nothing.
    assert 0 < files["tokens"] < transcript
    assert conversation["tokens"] > 0


def test_terminal_results_stay_in_conversation():
    history = [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("t1", "terminal", '{"command": "ls"}')],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "x" * 4_000},
    ]

    data = _breakdown_for(history)

    assert _category(data, "files") is None
    assert _category(data, "conversation")["tokens"] > 0


def test_a_tool_result_is_claimed_by_its_own_call_id_never_by_position():
    # The file call is never answered (interrupted turn); an unrelated result
    # follows it. Matching by position would wrongly bill it to Files.
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("c1", "read_file", '{"path": "/repo/app.py"}')],
        },
        {"role": "tool", "tool_call_id": "other", "content": "y" * 4_000},
    ]

    data = _breakdown_for(history)
    files = _category(data, "files")

    # The call itself still costs (it is in the window), but the foreign result
    # stays in Conversation.
    assert files["count"] == 1
    assert files["tokens"] < _category(data, "conversation")["tokens"]


def test_search_files_costs_file_tokens_but_is_not_a_countable_file():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("s1", "search_files", '{"pattern": "def", "path": "/repo"}')],
        },
        {"role": "tool", "tool_call_id": "s1", "content": "z" * 4_000},
    ]

    data = _breakdown_for(history)
    files = _category(data, "files")

    assert files["tokens"] > 0
    assert files["count"] == 0


def test_malformed_tool_call_arguments_never_raise():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_file_call("c1", "read_file", "{not json")],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "w" * 2_000},
        {"role": "assistant", "content": "", "tool_calls": ["not-a-dict"]},
        "not-a-message",
    ]

    data = _breakdown_for(history)
    files = _category(data, "files")

    assert files["tokens"] > 0
    assert files["count"] == 0
