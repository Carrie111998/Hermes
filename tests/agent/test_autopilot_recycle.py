"""Tests for autopilot session-recycle (opt-in) — agent/autopilot/recycle.py.

Pins the recycle contract:
  * fires at the utilization threshold when enabled; no-op when disabled;
  * cheap seed (ledger+adr+tail) when context.engine != cmx;
  * carries goal + ledger + adr + verbatim tail into the fresh list;
  * DOES NOT mutate the cached system prefix (prompt-cache invariant, #51312);
  * fails open (returns None) on any internal error;
  * preserves the durable goal state (side-stores untouched).

All offline; no council, no cmx, no network.
"""

from __future__ import annotations

import types

import pytest

from agent.autopilot import recycle


class FakeEngine:
    """Minimal stand-in for agent.context_compressor."""

    def __init__(self, *, name="compressor", context_length=100_000, last_prompt_tokens=0):
        self._name = name
        self.context_length = context_length
        self.last_prompt_tokens = last_prompt_tokens

    @property
    def name(self):
        return self._name


def make_agent(*, enabled=True, engine_name="compressor", util_tokens=90_000,
               ctx_len=100_000, goal="ship the widget", **overrides):
    a = types.SimpleNamespace()
    a.autopilot_mode = True
    a._autopilot_recycle_enabled = enabled
    a._autopilot_goal = goal
    a.context_compressor = FakeEngine(
        name=engine_name, context_length=ctx_len, last_prompt_tokens=util_tokens
    )
    # Keep durable side-stores from touching the real workspace: point ledger/adr
    # reads at a tmp path via the agent override attrs the modules honor.
    a._status = []
    a._emit_status = lambda msg: a._status.append(msg)
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


def sample_messages():
    return [
        {"role": "system", "content": "SYSTEM PREFIX — cached, must not change"},
        {"role": "user", "content": "start working on the widget"},
        {"role": "assistant", "content": "did step 1"},
        {"role": "user", "content": "keep going"},
        {"role": "assistant", "content": "did step 2"},
        {"role": "user", "content": "and again"},
        {"role": "assistant", "content": "did step 3, latest state here"},
    ]


# --------------------------------------------------------------------------- #
# enable / disable + threshold                                                 #
# --------------------------------------------------------------------------- #
def test_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_SESSION_RECYCLE", raising=False)
    a = make_agent(enabled=False)
    assert recycle.maybe_recycle(a, sample_messages()) is None


def test_noop_when_not_autopilot():
    a = make_agent()
    a.autopilot_mode = False
    assert recycle.maybe_recycle(a, sample_messages()) is None


def test_noop_below_threshold():
    # util = 40% < default 75% threshold -> no recycle.
    a = make_agent(util_tokens=40_000, ctx_len=100_000)
    assert recycle.maybe_recycle(a, sample_messages()) is None


def test_noop_when_utilization_unknown():
    # No usable token count -> treat as "don't recycle" (fail-open).
    a = make_agent(util_tokens=0)
    assert recycle.maybe_recycle(a, sample_messages()) is None


def test_fires_at_threshold_when_enabled():
    a = make_agent(util_tokens=90_000, ctx_len=100_000)  # 90% >= 75%
    fresh = recycle.maybe_recycle(a, sample_messages())
    assert isinstance(fresh, list) and fresh
    # A fresh list was produced with the recycle seed as its role:user turn.
    assert any(m.get("_autopilot_recycle_seed") for m in fresh)


def test_trims_a_long_history():
    # A long conversation + a small tail proves the history is actually trimmed.
    long_msgs = [{"role": "system", "content": "SYS"}]
    for i in range(40):
        long_msgs.append({"role": "user", "content": f"u{i}"})
        long_msgs.append({"role": "assistant", "content": f"a{i}"})
    a = make_agent(_autopilot_recycle_tail_turns=4)
    fresh = recycle.maybe_recycle(a, long_msgs)
    assert fresh is not None
    # system + seed + a bounded tail — far smaller than the 81-message original.
    assert len(fresh) < len(long_msgs)
    assert len(fresh) <= 8


def test_custom_threshold_via_attr():
    a = make_agent(util_tokens=60_000, ctx_len=100_000,
                   _autopilot_recycle_threshold_pct=50)
    assert recycle.maybe_recycle(a, sample_messages()) is not None
    a2 = make_agent(util_tokens=60_000, ctx_len=100_000,
                    _autopilot_recycle_threshold_pct=80)
    assert recycle.maybe_recycle(a2, sample_messages()) is None


# --------------------------------------------------------------------------- #
# seed composition: goal + ledger + adr + tail                                 #
# --------------------------------------------------------------------------- #
def test_seed_carries_goal_and_tail():
    a = make_agent(goal="ship the widget by friday")
    fresh = recycle.maybe_recycle(a, sample_messages())
    # First non-system message is the role:user resume seed.
    seed = next(m for m in fresh if m.get("role") == "user")
    assert seed["_autopilot_recycle_seed"] is True
    assert "ship the widget by friday" in seed["content"]
    # The verbatim tail (recent turns) is inlined into the seed digest.
    assert "did step 3, latest state here" in seed["content"]


def test_seed_carries_ledger_and_adr(tmp_path, monkeypatch):
    # Point the durable ledger + adr reads at tmp files with known content.
    ledger_file = tmp_path / "GOAL-LEDGER.md"
    ledger_file.write_text("## milestone\n- closed criterion ALPHA\n", encoding="utf-8")
    adr_file = tmp_path / "ADR.md"
    adr_file.write_text("## decision\n- chose approach BETA\n", encoding="utf-8")
    monkeypatch.setattr(recycle._ledger, "ledger_path", lambda agent=None: ledger_file)
    monkeypatch.setattr(recycle._adr, "adr_path", lambda agent=None: adr_file)

    a = make_agent()
    fresh = recycle.maybe_recycle(a, sample_messages())
    seed = next(m for m in fresh if m.get("role") == "user")["content"]
    assert "ALPHA" in seed  # ledger milestone carried
    assert "BETA" in seed   # adr decision carried


# --------------------------------------------------------------------------- #
# CMX-optional degradation                                                     #
# --------------------------------------------------------------------------- #
def test_cheap_seed_when_engine_not_cmx():
    a = make_agent(engine_name="compressor")
    fresh = recycle.maybe_recycle(a, sample_messages())
    seed = next(m for m in fresh if m.get("role") == "user")["content"]
    # No CMX briefing section when the engine isn't cmx.
    assert "CMX BRIEFING" not in seed


def test_cmx_briefing_used_when_engine_is_cmx():
    a = make_agent(engine_name="cmx")
    # Give the fake engine a briefing surface.
    a.context_compressor.resume_briefing = lambda goal="": "CMX-DIGEST: goal state recalled"
    fresh = recycle.maybe_recycle(a, sample_messages())
    seed = next(m for m in fresh if m.get("role") == "user")["content"]
    assert "CMX BRIEFING" in seed
    assert "CMX-DIGEST" in seed


def test_cmx_degrades_to_cheap_when_no_briefing_surface():
    a = make_agent(engine_name="cmx")  # cmx engine but no briefing method
    fresh = recycle.maybe_recycle(a, sample_messages())
    seed = next(m for m in fresh if m.get("role") == "user")["content"]
    # Falls back cleanly to the cheap seed (no CMX section, still has goal).
    assert "CMX BRIEFING" not in seed
    assert "ship the widget" in seed


# --------------------------------------------------------------------------- #
# prompt-cache invariant: system prefix untouched (#51312-style assert)        #
# --------------------------------------------------------------------------- #
def test_does_not_mutate_system_prefix():
    original = sample_messages()
    system_before = dict(original[0])
    a = make_agent()
    fresh = recycle.maybe_recycle(a, original)
    # The leading system message is preserved verbatim as the first element.
    assert fresh[0] == system_before
    assert fresh[0]["role"] == "system"
    assert fresh[0]["content"] == "SYSTEM PREFIX — cached, must not change"
    # The seed is a role:user turn, NOT a system turn.
    assert fresh[1]["role"] == "user"
    # No system messages beyond the preserved prefix.
    assert sum(1 for m in fresh if m.get("role") == "system") == 1


def test_seed_is_user_turn_not_system_when_no_prefix():
    # Even with no leading system message, the seed is a role:user turn.
    msgs = [m for m in sample_messages() if m.get("role") != "system"]
    a = make_agent()
    fresh = recycle.maybe_recycle(a, msgs)
    assert fresh[0]["role"] == "user"
    assert fresh[0]["_autopilot_recycle_seed"] is True
    assert not any(m.get("role") == "system" for m in fresh)


def test_role_alternation_preserved():
    a = make_agent()
    fresh = recycle.maybe_recycle(a, sample_messages())
    # Walk the non-system turns: no two consecutive user turns, no orphan tool.
    conv = [m for m in fresh if m.get("role") != "system"]
    for prev, cur in zip(conv, conv[1:]):
        assert not (prev["role"] == "user" and cur["role"] == "user")
    assert not any(m.get("role") == "tool" for m in fresh)


# --------------------------------------------------------------------------- #
# fail-open + goal-state preservation                                          #
# --------------------------------------------------------------------------- #
def test_fail_open_on_internal_error(monkeypatch):
    a = make_agent()

    def _boom(*args, **kwargs):
        raise RuntimeError("seed compose exploded")

    monkeypatch.setattr(recycle, "_compose_seed_text", _boom)
    # Any internal error -> None (caller behaves exactly as today).
    assert recycle.maybe_recycle(a, sample_messages()) is None


def test_preserves_goal_state():
    a = make_agent(goal="the durable goal")
    a._autopilot_continuations = 7
    a._autopilot_concluded_goal = None
    recycle.maybe_recycle(a, sample_messages())
    # Recycle only trims the message list — it never clears goal bookkeeping.
    assert a._autopilot_continuations == 7
    assert a._autopilot_goal == "the durable goal"


def test_tail_turns_zero_still_seeds_goal():
    a = make_agent(_autopilot_recycle_tail_turns=0)
    fresh = recycle.maybe_recycle(a, sample_messages())
    assert fresh is not None
    seed = next(m for m in fresh if m.get("role") == "user")["content"]
    assert "ship the widget" in seed
