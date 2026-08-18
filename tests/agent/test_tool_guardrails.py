"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)




def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
                "repeated_success": 6,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
                "repeated_success": 9,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.repeated_success_warn_after == 6
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8
    assert cfg.repeated_success_block_after == 9


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2














def test_mutating_or_unknown_tools_warn_without_blocking_repeated_success_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            repeated_success_warn_after=2,
            repeated_success_block_after=2,
        )
    )

    calls = [
        ("write_file", {"path": "/tmp/x", "content": "x"}),
        ("custom_tool", {"x": 1}),
    ]
    for tool_name, args in calls:
        assert controller.before_call(tool_name, args).action == "allow"
        assert controller.after_call(tool_name, args, "ok", failed=False).action == "allow"

        assert controller.before_call(tool_name, args).action == "allow"
        warning = controller.after_call(tool_name, args, "ok", failed=False)
        assert warning.action == "warn"
        assert warning.code == "repeated_success_warning"
        assert warning.count == 2

        # Hard stops remain opt-in even after the configured threshold.
        assert controller.before_call(tool_name, args).action == "allow"


def test_hard_stop_blocks_repeated_success_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            repeated_success_warn_after=2,
            repeated_success_block_after=2,
        )
    )
    args = {"command": "hermes config get memory.provider"}
    result = '{"output":"local","exit_code":0,"error":null}'

    assert controller.before_call("terminal", args).action == "allow"
    assert controller.after_call("terminal", args, result, failed=False).action == "allow"
    assert controller.before_call("terminal", args).action == "allow"
    warning = controller.after_call("terminal", args, result, failed=False)
    assert warning.code == "repeated_success_warning"

    blocked = controller.before_call("terminal", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_success_block"
    assert blocked.count == 2


def test_changed_success_result_resets_repeated_success_count():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(repeated_success_warn_after=2)
    )
    args = {"command": "check-state"}

    first = controller.after_call("terminal", args, "pending", failed=False)
    repeated = controller.after_call("terminal", args, "pending", failed=False)
    changed = controller.after_call("terminal", args, "ready", failed=False)

    assert first.action == "allow"
    assert repeated.code == "repeated_success_warning"
    assert changed.action == "allow"
    assert changed.count == 1






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig  # noqa: E402






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True









