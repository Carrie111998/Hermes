"""Regression tests: summary budget must measure CURRENT context, not the
session-lifetime token accumulator.

``_parent_summary_char_budget`` used ``parent_agent.session_prompt_tokens``
as "current context usage". That attribute is a lifetime accumulator
(agent_init = 0, then ``+=`` on every API call, never reset), so on any
long session the computed headroom goes permanently negative and every
batch summary collapses to the minimum-char floor even when the live
context is tiny. These tests pin the fix: use the compressor's last real
prompt size (``last_prompt_tokens``), falling back to
``last_real_prompt_tokens`` (post-compression boundary), then to the
accumulator only as a last resort.
"""

import tools.delegate_tool as dt


class _BudgetFakeCompressor:
    def __init__(self, context_length, max_tokens, last_prompt_tokens=None,
                 last_real_prompt_tokens=None):
        self.context_length = context_length
        self.max_tokens = max_tokens
        self.last_prompt_tokens = last_prompt_tokens
        self.last_real_prompt_tokens = last_real_prompt_tokens


class _BudgetFakeParent:
    def __init__(self, *, context_length, max_tokens,
                 session_prompt_tokens=0,
                 last_prompt_tokens=None,
                 last_real_prompt_tokens=None):
        self.context_compressor = _BudgetFakeCompressor(
            context_length, max_tokens,
            last_prompt_tokens=last_prompt_tokens,
            last_real_prompt_tokens=last_real_prompt_tokens,
        )
        # Lifetime accumulator: large on a long session, regardless of the
        # live context being small (e.g. right after a fresh compaction).
        self.session_prompt_tokens = session_prompt_tokens


def test_budget_uses_last_prompt_tokens_not_lifetime_accumulator():
    # Long session: accumulator says 900k, but the live context (last real
    # prompt) is only 30k of a 200k window. Headroom must be computed from
    # 30k — NOT from 900k, which would collapse the budget to the floor.
    parent = _BudgetFakeParent(
        context_length=200_000, max_tokens=8_000,
        session_prompt_tokens=900_000,          # lifetime accumulator (misleading)
        last_prompt_tokens=30_000,              # actual current usage
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=4)
    assert budget is not None
    assert budget > dt._MIN_SUMMARY_CHARS


def test_budget_falls_back_to_last_real_prompt_tokens():
    # Right after a compression boundary last_prompt_tokens may be 0/-1;
    # the post-compression measurement must be used next.
    parent = _BudgetFakeParent(
        context_length=200_000, max_tokens=8_000,
        session_prompt_tokens=900_000,
        last_prompt_tokens=0,
        last_real_prompt_tokens=25_000,
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=4)
    assert budget is not None
    assert budget > dt._MIN_SUMMARY_CHARS


def test_budget_accumulator_is_last_resort_only():
    # No real measurements available: only then may the accumulator be used.
    parent = _BudgetFakeParent(
        context_length=200_000, max_tokens=8_000,
        session_prompt_tokens=195_000,          # genuinely over budget
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=4)
    assert budget == dt._MIN_SUMMARY_CHARS
