"""Per-turn token deltas in run_conversation results.

run_agent's ``session_input_tokens`` / ``session_output_tokens`` counters are
CUMULATIVE for the agent's lifetime, and gateway agents are cached across
turns — so ``result["input_tokens"]`` grows monotonically per turn (observed
in sk-day26-v6: pa_turns input_tokens 80k -> 2.28M over 14 turns instead of
per-turn values). run_conversation must ALSO expose turn-scoped deltas
(``turn_input_tokens`` / ``turn_output_tokens``) so per-turn consumers (PA
turn-recording) record this turn's API usage only.

Harness mirrors tests/run_agent/test_context_token_tracking.py.
"""

import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent


def _patch_bootstrap(monkeypatch):
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [{
        "type": "function",
        "function": {"name": "t", "description": "t", "parameters": {"type": "object", "properties": {}}},
    }])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})


def _make_agent(monkeypatch, response_fn):
    _patch_bootstrap(monkeypatch)

    class _A(run_agent.AIAgent):
        def __init__(self, *a, **kw):
            kw.update(skip_context_files=True, skip_memory=True, max_iterations=4)
            super().__init__(*a, **kw)
            self._cleanup_task_resources = self._persist_session = lambda *a, **k: None
            self._save_trajectory = self._save_session_log = lambda *a, **k: None

        def run_conversation(self, msg, conversation_history=None, task_id=None):
            self._interruptible_api_call = lambda kw: response_fn()
            self._disable_streaming = True
            return super().run_conversation(msg, conversation_history=conversation_history, task_id=task_id)

    return _A(
        model="test-model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        provider="openrouter",
        api_mode="chat_completions",
    )


def _chat_resp(prompt_tokens, completion_tokens):
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, message=SimpleNamespace(
            role="assistant", content="ok", tool_calls=None, reasoning_content=None,
        ), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model="gpt-4o",
    )


def test_turn_tokens_stay_per_turn_across_cached_agent_turns(monkeypatch):
    """REPRO of the sk-day26-v6 cumulative-token bug: two turns on the SAME
    agent object (the cached-gateway-agent shape). The cumulative fields grow;
    the turn-scoped fields must equal each turn's own usage. FAILS on pre-fix
    code (turn_* fields absent)."""
    agent = _make_agent(monkeypatch, lambda: _chat_resp(5000, 100))

    r1 = agent.run_conversation("hi")
    r2 = agent.run_conversation("again")

    # Cumulative contract unchanged (existing consumers keep working).
    assert r1["input_tokens"] == 5000
    assert r2["input_tokens"] == 10000
    assert r1["output_tokens"] == 100
    assert r2["output_tokens"] == 200

    # Turn-scoped deltas: THIS call's API usage only.
    assert r1["turn_input_tokens"] == 5000
    assert r1["turn_output_tokens"] == 100
    assert r2["turn_input_tokens"] == 5000  # NOT 10000
    assert r2["turn_output_tokens"] == 100  # NOT 200


def test_turn_tokens_sum_multiple_api_calls_within_one_turn(monkeypatch):
    """A turn with several API calls (tool loop) reports the SUM of that
    turn's calls — still scoped to the turn, not the session."""
    calls = {"n": 0}

    def _resp():
        calls["n"] += 1
        return _chat_resp(1000, 10)

    agent = _make_agent(monkeypatch, _resp)
    r1 = agent.run_conversation("hi")
    n1 = calls["n"]
    r2 = agent.run_conversation("again")
    n2 = calls["n"] - n1

    assert r1["turn_input_tokens"] == 1000 * n1
    assert r2["turn_input_tokens"] == 1000 * n2
    assert r2["input_tokens"] == 1000 * (n1 + n2)  # cumulative


def test_turn_context_window_peak_resets_per_turn(monkeypatch):
    """turn_context_window_peak is the largest single-call prompt THIS turn
    (the context the model actually saw). It must reset per
    run_conversation call — a smaller second turn reports its own smaller
    peak, not a session-lifetime max (and never a sum)."""
    sizes = iter([9000, 2000, 2000, 2000])

    def _resp():
        return _chat_resp(next(sizes), 10)

    agent = _make_agent(monkeypatch, _resp)
    r1 = agent.run_conversation("hi")
    r2 = agent.run_conversation("again")

    assert r1["turn_context_window_peak"] == 9000
    # Turn 2's calls all saw 2000-token prompts: per-turn max, not the
    # session max (9000) and not a sum of turn-2 calls.
    assert r2["turn_context_window_peak"] == 2000
