"""Turn-end file-mutation footer delivery on interrupted/empty turns."""

import json

import pytest

from agent.file_mutation_verifier import TurnFileMutationVerifier, sync_legacy_failed_state
from agent.turn_finalizer import finalize_turn


class _FooterAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = type(
            "B", (), {"used": 1, "max_total": 90, "remaining": 1},
        )()
        self.context_compressor = type("C", (), {"last_prompt_tokens": 0})()
        self.model = "stub"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = "s1"
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.request_overrides = {}
        self._turn_failed_file_mutations = {}
        self._turn_file_mutation_paths = set()
        self._file_mutation_verifier = TurnFileMutationVerifier(use_subprocess_fingerprint=False)
        self._file_mutation_verifier.reset_turn(1)
        for attr in (
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"

    def _file_mutation_verifier_enabled(self):
        return True

    def _format_file_mutation_failure_footer(self, failed):
        from run_agent import AIAgent

        return AIAgent._format_file_mutation_failure_footer(failed)

    def _turn_completion_explainer_enabled(self):
        return False

    def _save_trajectory(self, *a, **k):
        pass

    def _cleanup_task_resources(self, *a, **k):
        pass

    def _drop_trailing_empty_response_scaffolding(self, *a, **k):
        pass

    def _persist_session(self, *a, **k):
        pass

    def _emit_status(self, *a, **k):
        pass

    def _safe_print(self, *a, **k):
        pass

    def _handle_max_iterations(self, messages, n):
        return ""

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **k):
        pass


def _finalize(agent, *, final_response="", interrupted=False):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=interrupted,
        failed=False,
        messages=[{"role": "user", "content": "hi"}],
        conversation_history=None,
        effective_task_id="default",
        turn_id="t1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
    )


def test_interrupted_empty_turn_still_gets_footer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "miss.py").write_text("same\n", encoding="utf-8")
    agent = _FooterAgent()
    fail = json.dumps({"error": "denied"})
    agent._file_mutation_verifier.record_tool_outcome(
        tool_name="patch",
        effective_args={
            "mode": "replace",
            "path": "miss.py",
            "old_string": "x",
            "new_string": "y",
        },
        effective_task_id="default",
        raw_result=fail,
        dispatch=__import__(
            "agent.file_mutation_verifier", fromlist=["DispatchTriState"]
        ).DispatchTriState.DISPATCHED,
        model_is_error=True,
        turn_generation=1,
    )
    sync_legacy_failed_state(agent)
    result = _finalize(agent, final_response="", interrupted=True)
    assert "File-mutation verifier" in (result["final_response"] or "")


def test_formatter_exception_uses_generic_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_text("x\n", encoding="utf-8")
    agent = _FooterAgent()
    fail = json.dumps({"error": "nope"})
    agent._file_mutation_verifier.record_tool_outcome(
        tool_name="write_file",
        effective_args={"path": "bad.py", "content": "y\n"},
        effective_task_id="default",
        raw_result=fail,
        dispatch=__import__(
            "agent.file_mutation_verifier", fromlist=["DispatchTriState"]
        ).DispatchTriState.DISPATCHED,
        model_is_error=True,
        turn_generation=1,
    )
    sync_legacy_failed_state(agent)

    def _boom(_failed):
        raise RuntimeError("format broke")

    agent._format_file_mutation_failure_footer = _boom
    result = _finalize(agent, final_response="partial answer")
    text = result["final_response"] or ""
    assert "partial answer" in text
    assert "File-mutation verifier" in text
    assert "git status" in text.lower() or "read_file" in text
