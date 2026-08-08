"""Seam identity for context_compressor_summarizers extract (LB5a).

Part of #78645 + #78647.
"""

from agent import context_compressor as cc
from agent import context_compressor_summarizers as cs


def test_all_members_resolve_is_identical_through_godfile():
    members = [
        "_str_arg",
        "_summarize_tool_result",
        "_summarize_tool_result_unguarded",
    ]
    for m in members:
        assert getattr(cc, m) is getattr(cs, m), f"{m} not is-identical"


def test_no_duplicate_defs_in_godfile():
    from pathlib import Path

    src = Path(cc.__file__).read_text(encoding="utf-8")
    for name in ["_str_arg", "_summarize_tool_result", "_summarize_tool_result_unguarded"]:
        assert src.count(f"def {name}") == 0, f"duplicate def {name} left in godfile"
    assert "context_compressor_summarizers" in src


def test_behavior_smoke():
    # str_arg
    assert cc._str_arg({"a": "x"}, "a") == "x"
    assert cc._str_arg({"a": 5}, "a") == "5"
    assert cc._str_arg({}, "missing") == ""
    # summarize tool result basic
    out = cc._summarize_tool_result("bash", '{"command": "echo hi"}', "hi\n")
    assert isinstance(out, str) and len(out) > 0
    # unguarded variant with skill_view pruning
    out2 = cc._summarize_tool_result_unguarded(
        "skill_view", '{"skill_name": "some-skill"}', "result text"
    )
    assert isinstance(out2, str)
    assert out2.startswith("[skill_view]")
    # long skill_view results get pruned (not summarized verbatim)
    long_result = "some-skill instructions " * 200
    out3 = cc._summarize_tool_result_unguarded(
        "skill_view", '{"skill_name": "some-skill"}', long_result
    )
    assert "[skill_view]" in out3
    assert len(out3) < len(long_result)


def test_import_orders_no_cycle():
    import importlib

    import agent.context_compressor_summarizers as a
    import agent.context_compressor as b

    importlib.reload(a)
    importlib.reload(b)
    assert b._summarize_tool_result is a._summarize_tool_result
