"""Pure tool-call guardrail primitive tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

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


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5


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


def test_unattended_default_can_enable_hard_stop_without_changing_plain_default():
    cfg = ToolCallGuardrailConfig.from_mapping({}, default_hard_stop_enabled=True)

    assert cfg.hard_stop_enabled is True


def test_unattended_explicit_soft_value_requires_opt_in():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": False},
        default_hard_stop_enabled=True,
    )

    assert cfg.hard_stop_enabled is True
    assert "unattended_soft_mode=true" in cfg.configuration_warning

    soft_cfg = ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": False, "unattended_soft_mode": True},
        default_hard_stop_enabled=True,
    )
    assert soft_cfg.hard_stop_enabled is False
    assert soft_cfg.configuration_warning == ""


def test_auto_hard_stop_follows_runtime_default():
    assert ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": "auto"}, default_hard_stop_enabled=True
    ).hard_stop_enabled is True
    assert ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": "auto"}, default_hard_stop_enabled=False
    ).hard_stop_enabled is False


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


def test_success_resets_exact_signature_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, same_tool_failure_halt_after=99)
    )
    args = {"query": "same"}

    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"ok":true}', failed=False)

    assert controller.before_call("web_search", args).action == "allow"
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert controller.before_call("web_search", args).action == "allow"


def test_file_mutation_lint_error_result_is_not_a_tool_failure():
    write_result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })
    patch_result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert classify_tool_failure("write_file", write_result) == (False, "")
    assert classify_tool_failure("patch", patch_result) == (False, "")


def test_same_tool_varying_args_warns_by_default_without_halting():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2, same_tool_failure_halt_after=3)
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    fourth = controller.after_call("terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True)

    assert first.action == "allow"
    assert [second.action, third.action, fourth.action] == ["warn", "warn", "warn"]
    assert {second.code, third.code, fourth.code} == {"same_tool_failure_warning"}
    assert "Do not switch to text-only replies" in second.message
    assert "keep using tools" in second.message
    assert "diagnose before retrying" in second.message
    assert "different tool" in second.message
    assert controller.halt_decision is None


def test_hard_stop_enabled_halts_same_tool_varying_args_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=3,
        )
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    assert third.action == "halt"
    assert third.code == "same_tool_failure_halt"
    assert third.count == 3


def test_idempotent_no_progress_repeated_result_warns_without_blocking_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    for _ in range(4):
        assert controller.before_call("read_file", args).action == "allow"
        decision = controller.after_call("read_file", args, result, failed=False)

    assert decision.action == "warn"
    assert decision.code == "idempotent_no_progress_warning"
    assert controller.before_call("read_file", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_idempotent_no_progress_future_repeat():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    assert controller.before_call("read_file", args).action == "allow"
    assert controller.after_call("read_file", args, result, failed=False).action == "allow"
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, no_progress_block_after=2)
    )
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"


def test_cross_turn_deterministic_blocker_is_target_scoped_and_session_bounded():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    first_args = {"path": "/tmp/blocked.txt", "content": "secret-one"}
    second_args = {"path": "/tmp/blocked.txt", "content": "secret-two"}
    missing = '{"error":"No such file or directory"}'

    controller.after_call("write_file", first_args, missing, failed=True)
    controller.reset_for_turn()
    controller.after_call("write_file", second_args, missing, failed=True)

    blocked = controller.before_call("write_file", second_args)
    assert blocked.action == "block"
    assert blocked.code == "cross_turn_deterministic_blocker"
    assert "secret-one" not in json.dumps(blocked.to_metadata())
    assert "secret-two" not in json.dumps(blocked.to_metadata())

    assert controller.before_call("write_file", {"path": "/tmp/other.txt"}).action == "allow"
    controller.reset_for_session()
    assert controller.before_call("write_file", second_args).action == "allow"


def test_cross_turn_success_resets_target_blocker_progress():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"path": "/tmp/recover.txt"}
    controller.after_call("write_file", args, '{"error":"Permission denied"}', failed=True)
    controller.reset_for_turn()
    controller.after_call("write_file", args, '{"bytes_written":1}', failed=False)
    controller.reset_for_turn()
    controller.after_call("write_file", args, '{"error":"Permission denied"}', failed=True)

    assert controller.before_call("write_file", args).action == "allow"


def test_cross_turn_ledger_distinguishes_url_and_named_targets():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    failure = '{"error":"Permission denied"}'

    controller.after_call(
        "browser_navigate", {"url": "https://one.example"}, failure, failed=True
    )
    controller.reset_for_turn()
    controller.after_call(
        "browser_navigate", {"url": "https://two.example"}, failure, failed=True
    )

    assert controller.before_call(
        "browser_navigate", {"url": "https://two.example"}
    ).action == "allow"

    controller.reset_for_session()
    controller.after_call(
        "skill_manage", {"name": "skill-one"}, failure, failed=True
    )
    controller.reset_for_turn()
    controller.after_call(
        "skill_manage", {"name": "skill-two"}, failure, failed=True
    )
    assert controller.before_call(
        "skill_manage", {"name": "skill-two"}
    ).action == "allow"


def test_cross_turn_ledger_serializes_concurrent_failures():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=4,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"path": "/tmp/concurrent.txt"}
    failure = '{"error":"Permission denied"}'

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda _index: controller.after_call("write_file", args, failure, failed=True),
            range(4),
        ))

    assert controller.before_call("write_file", args).code == "cross_turn_deterministic_blocker"


def test_cross_turn_ledger_expires_entries():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=2,
            cross_turn_ttl_seconds=10,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"path": "/tmp/expired.txt"}
    failure = '{"error":"Permission denied"}'
    controller.after_call("write_file", args, failure, failed=True)
    target_key = next(iter(controller._cross_turn_failures))
    controller._cross_turn_failures[target_key] = (2, 0.0, "permission")

    with patch("agent.tool_guardrails.time.monotonic", return_value=11.0):
        assert controller.before_call("write_file", args).action == "allow"


def test_cross_turn_ledger_evicts_oldest_entry_when_bounded():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            cross_turn_failure_halt_after=2,
            cross_turn_ledger_max_entries=2,
            same_tool_failure_halt_after=99,
        )
    )
    failure = '{"error":"Permission denied"}'
    first = {"path": "/tmp/first.txt"}
    second = {"path": "/tmp/second.txt"}
    third = {"path": "/tmp/third.txt"}
    controller.after_call("write_file", first, failure, failed=True)
    controller.after_call("write_file", first, failure, failed=True)
    controller.after_call("write_file", second, failure, failed=True)
    controller.after_call("write_file", second, failure, failed=True)
    controller.after_call("write_file", third, failure, failed=True)

    assert len(controller._cross_turn_failures) == 2
    assert controller.before_call("write_file", first).action == "allow"
