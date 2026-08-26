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


# ── Generic per-tool caps: loop_caps.per_tool (#92476) ─────────────────────


def test_per_tool_mapping_parses_and_ignores_legacy_names():
    cfg = LoopCapConfig.from_mapping(
        {"max_web_searches": 7, "per_tool": {"todo": 10, "text_to_speech": 0}}
    )
    assert cfg.max_web_searches == 7
    assert cfg.per_tool == {"todo": 10, "text_to_speech": 0}


def test_per_tool_unknown_loop_caps_key_warns_instead_of_silence(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.tool_guardrails"):
        LoopCapConfig.from_mapping({"max_web_searches": 50, "todo": 10})
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "todo" in joined and "per_tool" in joined, (
        "a dropped cap key must warn — silence looks like a working cap"
    )


def test_per_tool_invalid_cap_warns_and_stays_uncapped(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.tool_guardrails"):
        cfg = LoopCapConfig.from_mapping(
            {"per_tool": {"web_search": "ten", "todo": -3, "tts": 4}}
        )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "web_search" in joined and "invalid cap" in joined
    assert "todo" in joined and "invalid cap" in joined
    # Valid caps still parse; invalid ones degrade to uncapped (0).
    assert cfg.per_tool["tts"] == 4
    assert cfg.per_tool["web_search"] == 0
    assert cfg.per_tool["todo"] == 0


def test_per_tool_keys_normalized_case_insensitively(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.tool_guardrails"):
        cfg = LoopCapConfig.from_mapping({"per_tool": {"TODO": 2}})
    # Lookup at runtime uses the lowercase tool name.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, loop_caps=cfg)
    )
    for _ in range(2):
        assert controller.before_call("todo", {"n": 1}).action == "allow"
    assert controller.before_call("todo", {"n": 1}).action == "block"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "normalized to 'todo'" in joined


def test_per_tool_mapping_is_read_only():
    cfg = LoopCapConfig.from_mapping({"per_tool": {"todo": 3}})
    try:
        cfg.per_tool["todo"] = 99  # type: ignore[index]
    except TypeError:
        return
    raise AssertionError("per_tool mapping must be immutable")


def test_per_tool_cap_blocks_any_named_tool_regardless_of_hard_stop():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(per_tool={"todo": 2}),
        )
    )
    for i in range(2):
        decision = controller.before_call("todo", {"action": f"write{i}"})
        assert decision.action == "allow"
    decision = controller.before_call("todo", {"action": "write3"})
    assert decision.action == "block"
    assert decision.code == "loop_tool_cap"
    assert decision.should_halt is True
    assert "per_tool" in decision.message


def test_per_tool_zero_means_unlimited():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(per_tool={"text_to_speech": 0}),
        )
    )
    for i in range(5):
        assert controller.before_call("text_to_speech", {"n": i}).action == "allow"


def test_per_tool_uncapped_tools_unaffected():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(per_tool={"todo": 1}),
        )
    )
    for i in range(4):
        assert controller.before_call("read_file", {"path": f"f{i}"}).action == "allow"


def test_per_tool_counters_reset_each_turn():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(per_tool={"todo": 1}),
        )
    )
    assert controller.before_call("todo", {}).action == "allow"
    assert controller.before_call("todo", {}).action == "block"
    controller.reset_for_turn()
    assert controller.before_call("todo", {}).action == "allow"


def test_legacy_web_search_field_wins_over_per_tool_entry():
    """Back-compat: the named field takes precedence if a tool appears in both."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=2, per_tool={"web_search": 99}),
        )
    )
    for i in range(2):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q3"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"










