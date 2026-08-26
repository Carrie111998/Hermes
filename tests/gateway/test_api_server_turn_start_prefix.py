"""Regression tests for the Responses chaining turn-start prefix match (#95137).

``previous_response_id`` / ``conversation`` chaining relied on a full-dict
prefix comparison between the API layer's bare ``{"role", "content"}`` history
and the agent-layer transcript copies, which carry extra keys stamped by the
durability/flush path (``timestamp``, ``_db_persisted``, ``reasoning`` …).
The comparison failed every turn, so the fallback concatenated the prior
history on top of an agent transcript that already contained it — the stored
snapshot near-doubled per turn (measured 3 → 8 → 17 → 34 → 67 over five short
turns). The fix matches prefixes on the normalized ``(role, content)`` identity.
"""

from gateway.platforms.api_server import APIServerAdapter


def _agent_copy(msg):
    """A durability-stamped copy of a bare transcript entry."""
    stamped = dict(msg)
    stamped["timestamp"] = 1787702418.2069075
    stamped["_db_persisted"] = True
    return stamped


class TestTurnStartPrefixMatch:
    def test_stamped_agent_copies_match_bare_prefix(self):
        prior = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        current_user_message = "what time is it?"
        # The agent transcript echoes prior + the new user message, but every
        # entry carries the durability-stamped extra keys.
        agent_messages = [
            _agent_copy(m) for m in prior + [{"role": "user", "content": current_user_message}]
        ] + [_agent_copy({"role": "assistant", "content": "noon"})]
        result = {"messages": agent_messages}

        start = APIServerAdapter._response_messages_turn_start_index(
            prior, current_user_message, result
        )
        assert start == len(prior) + 1
        # And the stored transcript is exactly the agent transcript — no
        # duplication of prior on front.
        assert APIServerAdapter._build_response_conversation_history(
            prior, current_user_message, {"messages": agent_messages}, "noon"
        ) == agent_messages

    def test_assistant_reasoning_keys_do_not_break_match(self):
        prior = [{"role": "user", "content": "q1"}]
        agent_messages = [
            _agent_copy({"role": "user", "content": "q1"}),
            {
                "role": "user",
                "content": "q2",
                "timestamp": 1.0,
                "_db_persisted": True,
            },
            {
                "role": "assistant",
                "content": "a2",
                "reasoning": "thinking...",
                "finish_reason": "stop",
                "codex_message_items": [],
            },
        ]
        start = APIServerAdapter._response_messages_turn_start_index(
            prior, "q2", {"messages": agent_messages}
        )
        assert start == 2

    def test_compression_summary_prefix_still_returns_zero(self):
        # A compressed transcript starts with a summary that does NOT share
        # the prior prefix — the (role, content) match must still fail so the
        # caller's ``_compressed`` branch takes over.
        prior = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]
        agent_messages = [
            {"role": "assistant", "content": "[summary of earlier conversation]"},
            {"role": "user", "content": "new question", "timestamp": 2.0},
            {"role": "assistant", "content": "new answer"},
        ]
        start = APIServerAdapter._response_messages_turn_start_index(
            prior, "new question", {"messages": agent_messages, "_compressed": True}
        )
        assert start == 0

    def test_content_mismatch_still_returns_zero(self):
        prior = [{"role": "user", "content": "first"}]
        agent_messages = [
            _agent_copy({"role": "user", "content": "a different message"}),
        ]
        start = APIServerAdapter._response_messages_turn_start_index(
            prior, "second", {"messages": agent_messages}
        )
        assert start == 0

    def test_non_dict_entry_forces_mismatch(self):
        prior = [{"role": "user", "content": "hi"}]
        agent_messages = ["not-a-dict", {"role": "user", "content": "hi"}]
        assert not APIServerAdapter._normalized_prefix_match(agent_messages, prior)

    def test_identity_is_none_for_unknown_role(self):
        assert APIServerAdapter._normalized_message_identity("junk") is None
        assert APIServerAdapter._normalized_message_identity({"role": "weird"}) is None
        assert APIServerAdapter._normalized_message_identity(
            {"role": "user", "content": "x"}
        ) == ("user", "x")
