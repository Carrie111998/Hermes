"""Seam identity for context_compressor_budget extract (LB3).

Part of #78645 + #78647.
"""

from agent import context_compressor as cc
from agent import context_compressor_budget as b


def test_all_members_resolve_is_identical_through_godfile():
    members = [
        "HISTORICAL_TASK_HEADING",
        "_CHARS_PER_TOKEN",
        "_IMAGE_TOKEN_ESTIMATE",
        "_IMAGE_CHAR_EQUIVALENT",
        "_SUMMARY_FAILURE_COOLDOWN_SECONDS",
        "_FALLBACK_SUMMARY_MAX_CHARS",
        "_FALLBACK_PREVIOUS_SUMMARY_MAX_CHARS",
        "_FALLBACK_TURN_MAX_CHARS",
        "_AUTO_FOCUS_MAX_TURNS",
        "_AUTO_FOCUS_TURN_MAX_CHARS",
        "_AUTO_FOCUS_MAX_CHARS",
        "_ACTIVE_TASK_MAX_CHARS",
        "_MAX_TAIL_MESSAGE_FLOOR",
        "_FEASIBILITY_SKIP_MIDDLE_FRACTION",
        "_PRESSURE_KEEP_RECENT_MESSAGES",
        "_SMALL_CTX_WINDOW_LIMIT",
        "_SMALL_CTX_THRESHOLD_PERCENT",
        "_PATH_MENTION_RE",
        "_MEDIA_DIRECTIVE_RE",
        "_HISTORICAL_TASK_SECTION_RE",
        "_REPLAY_BUDGET_KEYS",
        "_dedupe_append",
        "_extract_tool_call_name_and_args",
        "_extract_tool_call_id",
        "_collect_path_mentions",
        "_content_length_for_budget",
        "_serialized_length_for_budget",
        "_reasoning_details_text_chars",
        "_estimate_msg_budget_tokens",
    ]
    for m in members:
        assert getattr(cc, m) is getattr(b, m), f"{m} not is-identical"


def test_no_duplicate_defs_in_godfile():
    from pathlib import Path

    src = Path(cc.__file__).read_text(encoding="utf-8")
    for name in [
        "_content_length_for_budget",
        "_serialized_length_for_budget",
        "_reasoning_details_text_chars",
        "_estimate_msg_budget_tokens",
        "_dedupe_append",
        "_extract_tool_call_name_and_args",
        "_extract_tool_call_id",
        "_collect_path_mentions",
    ]:
        assert src.count(f"def {name}") == 0, f"duplicate def {name} left in godfile"
    assert src.count("HISTORICAL_TASK_HEADING = ") == 0
    assert "context_compressor_budget" in src


def test_behavior_smoke():
    # constants preserved
    assert cc._CHARS_PER_TOKEN == 4
    assert cc.HISTORICAL_TASK_HEADING == "## Historical Task Snapshot"
    # budget estimator works
    tokens = cc._estimate_msg_budget_tokens(
        {"role": "user", "content": "hello world"}
    )
    assert tokens > 0
    # content length for budget
    assert cc._content_length_for_budget("abc") == 3
    # dedupe append
    items: list[str] = []
    cc._dedupe_append(items, "x", limit=2)
    cc._dedupe_append(items, "x", limit=2)
    assert items == ["x"]


def test_import_orders_no_cycle():
    import importlib

    import agent.context_compressor_budget as a
    import agent.context_compressor as b

    importlib.reload(a)
    importlib.reload(b)
    assert b._CHARS_PER_TOKEN is a._CHARS_PER_TOKEN
