"""Regression tests for the s3 shard mixin extraction of ``run_agent.py``.

Covers the PURE methods lifted into the six s3 mixin modules, exercising
them through the ``AIAgent`` class (MRO) exactly the way production code and
pre-existing tests do.  The extraction is behavior-neutral (verbatim lifts),
so these tests assert observable behavior — they would catch a broken
class line, a dropped import, or a mangled body after any future refactor.
"""

from __future__ import annotations

import json
import types
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    """Build an AIAgent without running ``__init__`` (bare-agent pattern)."""
    return object.__new__(AIAgent)


# ---------------------------------------------------------------------------
# message_sanitization_mixin — tool-call id/name extraction (c10)
# ---------------------------------------------------------------------------


class TestToolCallIdNameStatic:
    def test_dict_call_id(self):
        tc = {"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}
        assert AIAgent._get_tool_call_id_static(tc) == "call_1"

    def test_sdk_object_call_id(self):
        tc = types.SimpleNamespace(
            id="call_2", function=types.SimpleNamespace(name="todo", arguments="{}")
        )
        assert AIAgent._get_tool_call_id_static(tc) == "call_2"

    def test_dict_function_name(self):
        tc = {"id": "call_3", "function": {"name": "read_file", "arguments": "{}"}}
        assert AIAgent._get_tool_call_name_static(tc) == "read_file"

    def test_missing_function_name_returns_empty(self):
        tc = {"id": "call_4", "function": {}}
        assert AIAgent._get_tool_call_name_static(tc) == ""
        assert AIAgent._get_tool_call_name_static({"id": "call_5"}) == ""

    def test_sdk_object_function_name(self):
        tc = types.SimpleNamespace(
            id="call_6", function=types.SimpleNamespace(name="todo", arguments="{}")
        )
        assert AIAgent._get_tool_call_name_static(tc) == "todo"


class TestDeduplicateToolCalls:
    def _calls(self, *pairs):
        return [
            types.SimpleNamespace(
                id=f"c{i}", function=types.SimpleNamespace(name=n, arguments=a)
            )
            for i, (n, a) in enumerate(pairs)
        ]

    def test_duplicate_pair_removed(self):
        tcs = self._calls(("terminal", "{}"), ("terminal", "{}"), ("read_file", "{}"))
        out = AIAgent._deduplicate_tool_calls(tcs)
        assert len(out) == 2
        assert out[0].function.name == "terminal"
        assert out[1].function.name == "read_file"

    def test_distinct_pairs_preserved(self):
        tcs = self._calls(("terminal", '{"a":1}'), ("terminal", '{"a":2}'))
        out = AIAgent._deduplicate_tool_calls(tcs)
        assert out == tcs

    def test_empty_input(self):
        assert AIAgent._deduplicate_tool_calls([]) == []


class TestUniquifyToolCallIds:
    def test_duplicate_ids_get_deterministic_suffix(self):
        tcs = [
            types.SimpleNamespace(
                id="same", function=types.SimpleNamespace(name="terminal", arguments="{}")
            ),
            types.SimpleNamespace(
                id="same", function=types.SimpleNamespace(name="read_file", arguments="{}")
            ),
        ]
        out = AIAgent._uniquify_tool_call_ids(tcs)
        ids = [t.id for t in out]
        assert len(set(ids)) == 2
        assert ids[0] == "same"
        assert ids[1].startswith("same_d")

    def test_distinct_ids_untouched(self):
        tcs = [
            types.SimpleNamespace(
                id="a", function=types.SimpleNamespace(name="terminal", arguments="{}")
            ),
            types.SimpleNamespace(
                id="b", function=types.SimpleNamespace(name="read_file", arguments="{}")
            ),
        ]
        out = AIAgent._uniquify_tool_call_ids(tcs)
        assert [t.id for t in out] == ["a", "b"]


class TestDeterministicCallId:
    def test_deterministic_across_calls(self):
        a = AIAgent._deterministic_call_id("read_file", '{"path": "/x"}')
        b = AIAgent._deterministic_call_id("read_file", '{"path": "/x"}')
        assert a == b

    def test_differs_by_index(self):
        a = AIAgent._deterministic_call_id("read_file", "{}", 0)
        b = AIAgent._deterministic_call_id("read_file", "{}", 1)
        assert a != b


class TestSplitResponsesToolId:
    def test_pipe_split(self):
        assert AIAgent._split_responses_tool_id("call_1|item_2") == ("call_1", "item_2")

    def test_fc_prefix_is_response_item(self):
        assert AIAgent._split_responses_tool_id("fc_123") == (None, "fc_123")

    def test_plain_id(self):
        assert AIAgent._split_responses_tool_id("call_9") == ("call_9", None)

    def test_non_string(self):
        assert AIAgent._split_responses_tool_id(None) == (None, None)


class TestSanitizeApiMessages:
    def test_orphaned_result_removed(self):
        msgs = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "tool", "tool_call_id": "c_ORPHAN", "content": "stray"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert all(m.get("tool_call_id") != "c_ORPHAN" for m in out)

    def test_empty_list(self):
        assert AIAgent._sanitize_api_messages([]) == []


class TestIsThinkingOnlyAssistant:
    def test_empty_content_with_reasoning_is_thinking_only(self):
        msg = {"role": "assistant", "content": "", "reasoning_content": "think"}
        assert AIAgent._is_thinking_only_assistant(msg) is True

    def test_real_text_is_not_thinking_only(self):
        msg = {"role": "assistant", "content": "hello", "reasoning_content": "think"}
        assert AIAgent._is_thinking_only_assistant(msg) is False

    def test_tool_calls_are_not_thinking_only(self):
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]}
        assert AIAgent._is_thinking_only_assistant(msg) is False

    def test_non_assistant_is_not_thinking_only(self):
        assert AIAgent._is_thinking_only_assistant({"role": "user", "content": ""}) is False


# ---------------------------------------------------------------------------
# message_sanitization_mixin — system prompt forwarders (c9)
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_build_system_prompt_parts_forwarder(self):
        agent = _bare_agent()
        with patch("agent.system_prompt.build_system_prompt_parts", return_value={"parts": 1}) as m:
            assert agent._build_system_prompt_parts("sys") == {"parts": 1}
        m.assert_called_once_with(agent, system_message="sys")

    def test_build_system_prompt_forwarder(self):
        agent = _bare_agent()
        with patch("agent.system_prompt.build_system_prompt", return_value="built") as m:
            assert agent._build_system_prompt("sys") == "built"
        m.assert_called_once_with(agent, system_message="sys")

    def test_invalidate_system_prompt_forwarder(self):
        agent = _bare_agent()
        with patch("agent.system_prompt.invalidate_system_prompt") as m:
            agent._invalidate_system_prompt()
        m.assert_called_once_with(agent)


# ---------------------------------------------------------------------------
# turn_footers_mixin — footer rendering (c2/c3)
# ---------------------------------------------------------------------------


class TestNeutralizeFooterPaths:
    def test_bare_abs_path_wrapped(self):
        out = AIAgent._neutralize_footer_paths("see /tmp/a.txt now")
        assert "`/tmp/a.txt`" in out

    def test_already_backticked_not_double_wrapped(self):
        out = AIAgent._neutralize_footer_paths("see `/tmp/a.txt` now")
        assert out.count("`/tmp/a.txt`") == 1

    def test_empty_input(self):
        assert AIAgent._neutralize_footer_paths("") == ""
        assert AIAgent._neutralize_footer_paths(None) is None


class TestFormatFileMutationFailureFooter:
    def test_empty_dict_returns_empty_string(self):
        assert AIAgent._format_file_mutation_failure_footer({}) == ""

    def test_paths_backticked(self):
        out = AIAgent._format_file_mutation_failure_footer(
            {"/tmp/x.md": {"tool": "patch", "error_preview": "not found"}}
        )
        assert "`/tmp/x.md`" in out
        assert "patch" in out

    def test_cap_at_ten(self):
        failed = {f"/tmp/f{i}.md": {"tool": "patch", "error_preview": "e"} for i in range(15)}
        out = AIAgent._format_file_mutation_failure_footer(failed)
        assert "and 5 more" in out


class TestFormatTurnCompletionExplanation:
    def test_normal_exit_is_silent(self):
        assert AIAgent._format_turn_completion_explanation("text_response(ok)") == ""

    def test_empty_reason_is_silent(self):
        assert AIAgent._format_turn_completion_explanation("") == ""

    def test_abnormal_exit_explained(self):
        out = AIAgent._format_turn_completion_explanation("budget_exhausted")
        assert out.startswith("⚠️ No reply:")

    def test_unknown_reason_is_silent(self):
        assert AIAgent._format_turn_completion_explanation("unknown") == ""


class TestFileMutationVerifierEnabled:
    def test_env_disables(self, monkeypatch):
        monkeypatch.setenv("HERMES_FILE_MUTATION_VERIFIER", "0")
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False

    def test_env_enables(self, monkeypatch):
        monkeypatch.setenv("HERMES_FILE_MUTATION_VERIFIER", "1")
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is True

    def test_default_enabled_without_env_or_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_FILE_MUTATION_VERIFIER", raising=False)
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {})
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is True


class TestRecordFileMutationResult:
    def test_non_mutating_tool_ignored(self):
        agent = _bare_agent()
        agent._turn_failed_file_mutations = {}
        agent._record_file_mutation_result("read_file", {"path": "/x"}, "{}", is_error=True)
        assert agent._turn_failed_file_mutations == {}

    def test_failure_recorded_then_success_clears(self):
        agent = _bare_agent()
        agent._turn_failed_file_mutations = {}
        agent._turn_file_mutation_paths = set()
        result = json.dumps({"success": False, "error": "Could not find old_string"})
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            result, is_error=True,
        )
        assert "/tmp/a.md" in agent._turn_failed_file_mutations
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "real", "new_string": "fixed"},
            json.dumps({"success": True, "diff": "..."}), is_error=False,
        )
        assert agent._turn_failed_file_mutations == {}
        assert agent._turn_file_mutation_paths == {"/tmp/a.md"}


# ---------------------------------------------------------------------------
# usage_telemetry_mixin — rate limits / credits (c5)
# ---------------------------------------------------------------------------


class TestRateLimitState:
    def test_get_rate_limit_state_defaults_to_none(self):
        agent = _bare_agent()
        agent._rate_limit_state = None
        assert agent.get_rate_limit_state() is None

    def test_capture_rate_limits_stores_state(self):
        agent = _bare_agent()
        agent._rate_limit_state = None
        agent.provider = "openai"
        resp = types.SimpleNamespace(headers={"x-ratelimit-remaining-requests": "10"})
        with patch("agent.rate_limit_tracker.parse_rate_limit_headers", return_value="STATE") as m:
            agent._capture_rate_limits(resp)
        m.assert_called_once()
        assert agent.get_rate_limit_state() == "STATE"

    def test_capture_rate_limits_none_response(self):
        agent = _bare_agent()
        agent._rate_limit_state = None
        agent._capture_rate_limits(None)
        assert agent.get_rate_limit_state() is None


class TestCredits:
    def test_credits_state_defaults_to_none(self):
        agent = _bare_agent()
        agent._credits_state = None
        assert agent.get_credits_state() is None

    def test_credits_spent_micros_none_without_data(self):
        agent = _bare_agent()
        agent._credits_state = None
        agent._credits_session_start_micros = None
        assert agent.get_credits_spent_micros() is None

    def test_capture_anthropic_response_headers_calls_both_captures(self):
        agent = _bare_agent()
        with patch.object(agent, "_capture_rate_limits") as rl, patch.object(agent, "_capture_credits") as cr:
            agent._capture_anthropic_response_headers("RESP")
        rl.assert_called_once_with("RESP")
        cr.assert_called_once_with("RESP")

    def test_check_openrouter_cache_status_hit_increments(self):
        agent = _bare_agent()
        agent._or_cache_hits = 0
        resp = types.SimpleNamespace(headers={"x-openrouter-cache-status": "HIT"})
        agent._check_openrouter_cache_status(resp)
        assert agent._or_cache_hits == 1

    def test_credits_notices_enabled_caches_true_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_DEV_CREDITS", raising=False)
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {})
        agent = _bare_agent()
        assert agent._credits_notices_enabled() is True
        # cached on the instance so a config flip mid-session is ignored
        assert agent._credits_notices_enabled() is True


# ---------------------------------------------------------------------------
# activity_mixin — session activity (c4)
# ---------------------------------------------------------------------------


class TestActivity:
    def test_activity_summary_defaults(self):
        agent = _bare_agent()
        agent._current_tool = None
        agent._api_call_count = 0
        agent.max_iterations = 10
        agent.iteration_budget = types.SimpleNamespace(used=0, max_total=10)
        summary = agent.get_activity_summary()
        assert summary["last_activity_at"] is None
        assert summary["last_activity_description"] == ""

    def test_touch_activity_updates_stamp(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _bare_agent()
        agent._session_db = None
        agent._touch_activity("running tool")
        assert agent._last_activity_desc == "running tool"
        assert agent._last_activity_ts is not None

    def test_reset_activity_labels_after_turn(self):
        agent = _bare_agent()
        agent._session_db = None
        agent._last_activity_desc = "busy"
        agent._reset_activity_labels_after_turn()
        assert agent._last_activity_desc == ""


# ---------------------------------------------------------------------------
# todo_hydration_mixin — todo store recovery (c8)
# ---------------------------------------------------------------------------


class TestTodoHydration:
    def _bare_agent_with_todo(self):
        agent = _bare_agent()
        agent._todo_store = MagicMock()
        agent.session_id = "sess-1"
        agent.quiet_mode = True
        agent.log_prefix = ""
        agent._vprint = lambda *a, **k: None
        return agent

    def test_no_todo_in_history_is_noop(self):
        agent = self._bare_agent_with_todo()
        with patch("run_agent._set_interrupt") as si:
            agent._hydrate_todo_store([
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ])
        si.assert_called_once_with(False)
        agent._todo_store.write.assert_not_called()

    def test_todo_response_without_matching_call_not_hydrated(self):
        agent = self._bare_agent_with_todo()
        agent._hydrate_todo_store([
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "orphan", "content": json.dumps({"todos": [{"id": 1}]})},
        ])
        agent._todo_store.write.assert_not_called()

    def test_matching_todo_response_hydrates_store(self):
        agent = self._bare_agent_with_todo()
        history = [
            {"role": "user", "content": "add todo"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"todos": [{"id": 1, "title": "x"}]})},
        ]
        with patch("run_agent._set_interrupt"):
            agent._hydrate_todo_store(history)
        agent._todo_store.write.assert_called_once_with([{"id": 1, "title": "x"}], merge=False)


# ---------------------------------------------------------------------------
# memory_lifecycle_mixin — external memory (c6)
# ---------------------------------------------------------------------------


class TestMemoryLifecycle:
    def test_sync_external_memory_skips_interrupted_turns(self):
        agent = _bare_agent()
        agent._memory_manager = MagicMock()
        agent.session_id = "sess-1"
        agent._sync_external_memory_for_turn(
            original_user_message="What time is it?",
            final_response="It is 3pm.",
            interrupted=True,
        )
        agent._memory_manager.sync_all.assert_not_called()
        agent._memory_manager.queue_prefetch_all.assert_not_called()

    def test_sync_external_memory_normal_turn(self):
        agent = _bare_agent()
        agent._memory_manager = MagicMock()
        agent.session_id = "sess-1"
        agent._sync_external_memory_for_turn(
            original_user_message="hello world",
            final_response="hi there",
            interrupted=False,
        )
        agent._memory_manager.sync_all.assert_called_once()
        agent._memory_manager.queue_prefetch_all.assert_called_once()

    def test_commit_memory_session_without_manager_is_safe(self):
        agent = _bare_agent()
        agent._memory_manager = None
        agent.session_id = "sess-1"
        agent.commit_memory_session([])  # must not raise

    def test_shutdown_memory_provider_without_manager_is_safe(self):
        agent = _bare_agent()
        agent._memory_manager = None
        agent.session_id = "sess-1"
        agent.shutdown_memory_provider([])  # must not raise
