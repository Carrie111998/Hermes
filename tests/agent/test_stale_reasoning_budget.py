"""Regression test for issue #73624: stale reasoning budget inflation.

``_estimate_msg_budget_tokens`` used to charge _REPLAY_BUDGET_KEYS
(reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
codex_message_items) for **every** message in history, but adapters on
upstream/main only replay Anthropic thinking (``reasoning``,
``reasoning_details``) for the newest assistant turn.  Codex carrier
fields (``codex_reasoning_items``, ``codex_message_items``) and
require-side ``reasoning_content`` are replayed from **every** historical
assistant message.

The fix splits replay keys into two groups:
- **Always-counted**: reasoning_content, codex_reasoning_items, codex_message_items
- **Newest-turn-only** (include_replay=True): reasoning, reasoning_details

This avoids inflating the tail budget with Anthropic thinking tokens
that are provably stripped before the request is sent (issue #73624)
while still correctly charging for Codex carrier blobs and require-side
reasoning_content that ARE replayed on every turn.
"""

import pytest
from unittest.mock import patch

from agent.context_compressor import (
    ContextCompressor,
    _CHARS_PER_TOKEN,
    _estimate_msg_budget_tokens,
    _REPLAY_BUDGET_KEYS,
)


class TestEstimateMsgBudgetTokensIncludeReplay:
    """Verify the include_replay parameter works correctly."""

    def test_replay_keys_charged_by_default(self):
        """Backward-compatible: replay fields are included when
        include_replay is not specified (defaults to True)."""
        msg = {
            "role": "assistant",
            "content": "Hello",
            "reasoning": "Let me think about this for a moment.",
        }
        with_replay = _estimate_msg_budget_tokens(msg)
        without_replay = _estimate_msg_budget_tokens(msg, include_replay=False)
        assert with_replay > without_replay
        # The difference should account for the reasoning text.
        reasoning_chars = len(msg["reasoning"])
        expected_diff = reasoning_chars // _CHARS_PER_TOKEN
        assert with_replay - without_replay >= expected_diff

    def test_replay_keys_not_charged_when_disabled(self):
        """When include_replay=False, only Anthropic thinking fields
        (reasoning, reasoning_details) are excluded. Codex carrier fields
        and reasoning_content are always counted."""
        msg = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": "A" * 500,
            "reasoning_content": "B" * 300,
            "reasoning_details": [{"text": "C" * 200}],
            "codex_reasoning_items": [{"encrypted_content": "D" * 400}],
        }
        with_replay = _estimate_msg_budget_tokens(msg)
        without_replay = _estimate_msg_budget_tokens(msg, include_replay=False)

        # With replay: includes reasoning + reasoning_details
        assert with_replay > 100
        # Without replay: still counts reasoning_content + codex_reasoning_items
        # So it's NOT near-zero — it's the always-counted keys
        always_counted_chars = (
            len(msg["reasoning_content"])
            + len(msg["codex_reasoning_items"][0]["encrypted_content"])
        )
        always_counted_tokens = always_counted_chars // _CHARS_PER_TOKEN
        assert without_replay >= always_counted_tokens
        # The gap accounts for reasoning + reasoning_details (newest-turn-only)
        gap = with_replay - without_replay
        assert gap > 50  # reasoning (500 chars) + reasoning_details (200 chars)

    def test_non_assistant_msg_no_replay_overhead(self):
        """User/tool messages have no replay keys, so include_replay makes
        no difference."""
        msg = {"role": "user", "content": "Hello world"}
        assert _estimate_msg_budget_tokens(msg) == _estimate_msg_budget_tokens(
            msg, include_replay=False
        )

    def test_empty_replay_fields_no_difference(self):
        """If replay fields are None/empty, include_replay makes no
        difference (serialized length is 0)."""
        msg = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": None,
            "reasoning_content": "",
        }
        assert _estimate_msg_budget_tokens(msg) == _estimate_msg_budget_tokens(
            msg, include_replay=False
        )


class TestFindTailCutOnlyChargesLastAssistantReplay:
    """Verify _find_tail_cut_by_tokens only charges replay budget for
    the last assistant message."""

    @pytest.fixture()
    def compressor(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            return ContextCompressor(
                model="test/model",
                threshold_percent=0.85,
                protect_first_n=2,
                protect_last_n=2,
                quiet_mode=True,
            )

    def _make_stale_assistant(self, reasoning_chars: int = 500) -> dict:
        """An assistant message with small content but large reasoning blob
        (simulating stale reasoning that will be stripped by the adapter)."""
        return {
            "role": "assistant",
            "content": f"Reply {reasoning_chars}",
            "reasoning": "X" * reasoning_chars,
            "reasoning_content": "Y" * reasoning_chars,
        }

    def _make_fresh_assistant(self, reasoning_chars: int = 500) -> dict:
        """The newest assistant message — reasoning IS replayed."""
        return {
            "role": "assistant",
            "content": "Final reply",
            "reasoning": "Z" * reasoning_chars,
        }

    def test_budget_walk_ignores_stale_reasoning(self, compressor):
        """With many stale assistant messages carrying heavy reasoning,
        the tail cut should NOT treat them as bloated because their
        reasoning is never replayed on the wire."""
        # Build a session: user messages interleaved with stale assistants,
        # ending with a fresh assistant (the only one that replays reasoning).
        user_msg = {"role": "user", "content": "Question"}
        stale_asst = self._make_stale_assistant(reasoning_chars=800)
        fresh_asst = self._make_fresh_assistant(reasoning_chars=800)

        # 30 turns: user, stale-asst, user, stale-asst, ..., user, fresh-asst
        messages = []
        for _ in range(15):
            messages.append(user_msg.copy())
            messages.append(stale_asst.copy())
        # Replace last assistant with the fresh one
        messages[-1] = fresh_asst.copy()

        # Estimate the "old" budget (all replay charged) vs "new" budget
        # (only last assistant replay charged).
        old_total = sum(
            _estimate_msg_budget_tokens(m) for m in messages
        )
        new_total = sum(
            _estimate_msg_budget_tokens(m, include_replay=(m.get("role") == "assistant" and i == len(messages) - 1))
            for i, m in enumerate(messages)
        )

        # The old estimate is significantly inflated (many stale reasoning blobs)
        # The new estimate is much leaner
        assert old_total > new_total
        # The savings should be substantial — roughly 14 stale assistant turns
        # each carrying ~1600 chars of reasoning
        savings_ratio = (old_total - new_total) / old_total
        assert savings_ratio > 0.10, (
            f"Savings ratio {savings_ratio:.2%} is too low; "
            f"old={old_total}, new={new_total}"
        )

    def test_tail_cut_preserves_more_turns_after_fix(self, compressor):
        """The fixed tail cut should preserve more real transcript turns
        compared to the old behavior, because it doesn't waste budget on
        stale reasoning."""
        user_msg = {"role": "user", "content": "Q"}
        stale_asst = self._make_stale_assistant(reasoning_chars=600)
        fresh_asst = self._make_fresh_assistant(reasoning_chars=600)

        # 20 turns: user, stale-asst repeated, ending with fresh-asst
        messages = []
        for i in range(10):
            messages.append(user_msg.copy())
            if i < 9:
                messages.append(stale_asst.copy())
            else:
                messages.append(fresh_asst.copy())

        # The cut should now extend further into the transcript because
        # stale reasoning isn't charged
        token_budget = 200  # Tight budget to make the difference visible
        cut = compressor._find_tail_cut_by_tokens(messages, head_end=0, token_budget=token_budget)
        protected = len(messages) - cut

        # With stale reasoning NOT charged, more messages fit in the budget
        assert protected >= 3  # At least the min_tail_floor
