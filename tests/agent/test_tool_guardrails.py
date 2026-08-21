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


# ── HTTP status in terminal output (curl exits 0 on 4xx/5xx without -f) ────

def _term_result(output: str, exit_code: int = 0) -> str:
    return json.dumps({"output": output, "exit_code": exit_code})


class TestClassifyTerminalHttpStatus:
    """`curl` without -f exits 0 even on HTTP 404/5xx — the guardrail must
    classify the embedded status as a failure or a deterministic HTTP retry
    loop is invisible (the terminal x49 '404' phantom-task root cause)."""

    def test_http_404_in_output_classified_permanent_failure(self):
        result = _term_result(
            "curl: (22) The requested URL returned error: 404\n"
            "<html><title>404 Not Found</title></html>"
        )
        is_failure, suffix = classify_tool_failure("terminal", result)
        assert is_failure is True
        assert "404" in suffix and "permanent" in suffix

    def test_http_status_line_classified_permanent(self):
        result = _term_result("HTTP/1.1 404 Not Found\nContent-Type: text/html")
        is_failure, suffix = classify_tool_failure("terminal", result)
        assert is_failure is True
        assert "404" in suffix and "permanent" in suffix

    def test_http_503_classified_transient(self):
        result = _term_result("HTTP/1.1 503 Service Unavailable\nRetry-After: 5")
        is_failure, suffix = classify_tool_failure("terminal", result)
        assert is_failure is True
        assert "503" in suffix and "transient" in suffix

    def test_json_status_field_classified(self):
        result = _term_result('{"status_code": 429, "message": "rate limited"}')
        is_failure, suffix = classify_tool_failure("terminal", result)
        assert is_failure is True
        assert "429" in suffix and "permanent" in suffix

    def test_clean_exit_zero_output_not_failure(self):
        result = _term_result("hello world\n")
        assert classify_tool_failure("terminal", result) == (False, "")

    def test_plain_404_string_in_output_not_failure(self):
        # A bare "404" in ordinary output (grep hit, log excerpt, page body
        # that merely mentions a status) is data, not an HTTP error response.
        result = _term_result("grep hit: line 12 mentions 404 once\n")
        assert classify_tool_failure("terminal", result) == (False, "")

    def test_nonzero_exit_still_dominates(self):
        result = _term_result("boom", exit_code=127)
        is_failure, suffix = classify_tool_failure("terminal", result)
        assert is_failure is True
        assert "127" in suffix


class TestGuardrailFiresOnDeterministic404Loop:
    """A guaranteed-404 fetch loop must trip the loop guard by the exact
    failure threshold — the harness pivots because guidance is appended to the
    tool result — while a transient 5xx backoff that succeeds is not flagged."""

    def test_identical_404_loop_warns_by_n_repeats_with_permanent_guidance(self):
        controller = ToolCallGuardrailController()
        args = {"command": "curl -s https://example.com/missing-xyz"}
        result = _term_result("HTTP/1.1 404 Not Found\n<!doctype html>")

        decisions = []
        for _ in range(4):
            assert controller.before_call("terminal", args).action == "allow"
            decisions.append(
                controller.after_call("terminal", args, result, failed=True)
            )

        assert decisions[0].action == "allow"
        # warn_after.exact_failure=2 → warning from the 2nd identical call on
        assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn"]
        assert decisions[1].code == "repeated_exact_failure_warning"
        # Guidance teaches the permanent-vs-transient classification.
        assert "PERMANENT" in decisions[1].message
        assert "404" in decisions[1].message
        assert "Never blind-retry a 4xx without modifying the request" in decisions[1].message
        assert controller.halt_decision is None  # soft warning, not a stop

    def test_transient_5xx_backoff_that_succeeds_is_not_false_flagged(self):
        controller = ToolCallGuardrailController()
        args = {"command": "curl -s https://example.com/flaky"}
        flaky = _term_result("HTTP/1.1 503 Service Unavailable")
        ok = _term_result("200 OK")

        assert controller.after_call("terminal", args, flaky, failed=True).action == "allow"
        assert controller.after_call("terminal", args, flaky, failed=True).action == "warn"
        # Backoff retry succeeds → counters reset → no lingering flag.
        assert controller.after_call("terminal", args, ok, failed=False).action == "allow"
        assert controller.after_call("terminal", args, flaky, failed=True).action == "allow"
        assert controller.halt_decision is None










