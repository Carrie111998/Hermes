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
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


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














def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"






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


def test_vision_cap_blocks_after_limit_with_distinct_args():
    # The incident contract: vision_analyze's cost lives in the PAYLOAD (each
    # call embeds an image that then rides in history), so DISTINCT arguments
    # must still consume the cap. The result-identity no-progress detector can
    # never see this — a runaway re-analysis loop varies the question and/or
    # cycles a handful of images, so every call looks novel to it.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_vision_calls=3),
        )
    )
    for i in range(3):
        decision = controller.before_call(
            "vision_analyze", {"image_url": f"/tmp/sheet{i}.jpg", "question": f"q{i}"}
        )
        assert decision.action == "allow"
    decision = controller.before_call(
        "vision_analyze", {"image_url": "/tmp/sheet3.jpg", "question": "q3"}
    )
    assert decision.action == "block"
    assert decision.code == "loop_vision_cap"
    assert decision.should_halt is True


def test_vision_cap_zero_disables():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_vision_calls=0))
    )
    for i in range(100):
        assert (
            controller.before_call("vision_analyze", {"image_url": f"/tmp/{i}.jpg"}).action
            == "allow"
        )


def test_vision_cap_resets_between_turns():
    # Caps bound a single agent loop, not the whole session: reset_for_turn()
    # must clear the counter or a long conversation would exhaust the budget.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_vision_calls=2))
    )
    for i in range(2):
        assert controller.before_call("vision_analyze", {"image_url": f"/tmp/{i}.jpg"}).action == "allow"
    assert controller.before_call("vision_analyze", {"image_url": "/tmp/x.jpg"}).action == "block"

    controller.reset_for_turn()
    assert controller.before_call("vision_analyze", {"image_url": "/tmp/y.jpg"}).action == "allow"


def test_vision_cap_from_mapping_round_trips_and_falls_back():
    # Relational, not a frozen literal: an explicit value survives, an absent
    # key yields the dataclass default, and junk/negative falls back to it.
    assert LoopCapConfig.from_mapping({"max_vision_calls": 7}).max_vision_calls == 7
    assert LoopCapConfig.from_mapping({"max_vision_calls": 0}).max_vision_calls == 0
    default = LoopCapConfig().max_vision_calls
    assert LoopCapConfig.from_mapping({}).max_vision_calls == default
    assert LoopCapConfig.from_mapping({"max_vision_calls": -5}).max_vision_calls == default
    assert LoopCapConfig.from_mapping({"max_vision_calls": "nope"}).max_vision_calls == default


def test_vision_cap_does_not_consume_sibling_cap_budgets():
    # Each capped tool keeps an independent per-turn counter.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            loop_caps=LoopCapConfig(max_vision_calls=2, max_web_searches=2),
        )
    )
    for i in range(2):
        assert controller.before_call("vision_analyze", {"image_url": f"/tmp/{i}.jpg"}).action == "allow"
    assert controller.before_call("vision_analyze", {"image_url": "/tmp/x.jpg"}).action == "block"
    # web_search budget is untouched by the exhausted vision budget.
    assert controller.before_call("web_search", {"query": "q"}).action == "allow"


def test_default_config_enables_a_vision_cap():
    # Config -> dataclass propagation, asserted as an invariant rather than a
    # frozen literal: the shipped default must be a positive (enabled) cap.
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    shipped = DEFAULT_CONFIG["tool_loop_guardrails"]["loop_caps"]["max_vision_calls"]
    assert shipped > 0
    assert LoopCapConfig.from_mapping({"max_vision_calls": shipped}).max_vision_calls == shipped










