"""Regression tests for sanitize_api_messages() post-dedup content repair.

When dedup of duplicate tool_call_ids empties an assistant message's
``tool_calls`` list, the pre-call sanitizer pass
(``repair_empty_non_final_messages``, runs first in
``sanitize_api_messages``) saw the message as "has payload" and skipped
it. Dedup then dropped the ``tool_calls`` key (upstream #64335), leaving
``{role: assistant, content: null}`` on the wire — which strict
non-empty-content providers (Anthropic native, litellm/Bedrock) reject
with HTTP 400.

The key-drop behavior itself is already covered upstream by
``test_sanitize_dedup_drops_tool_calls_key_when_all_removed``
(tests/run_agent/test_message_sequence_repair.py, #64335). These tests
cover the follow-up: re-running ``repair_empty_non_final_messages``
after dedup so the placeholder ("[response interrupted]") is also
injected into the post-dedup shape.

Note on trigger scope after #93251: the dedup pass re-arms a
tool_call_id once its tool result arrives, so a later assistant turn
reusing an ANSWERED id is no longer deduplicated (that is a legal
id-reuse provider, #70724), and the pairing pass stubs any outstanding
call the transcript never answers. The emptying path below therefore
fires when a resume replay re-emits the assistant call BEFORE the
delayed tool result arrives — the same trigger as upstream's
``test_sanitize_dedup_drops_tool_calls_key_when_all_removed``.
"""

from agent.agent_runtime_helpers import sanitize_api_messages


def _user(content="hi"):
    return {"role": "user", "content": content}


def _assistant_tool_call(call_id, *, content=""):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }


def _tool_result(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _has_visible_content(msg):
    content = msg.get("content")
    return isinstance(content, str) and content.strip()


class TestSanitizeEmptyToolCalls:
    def test_cross_message_all_duplicates_healed(self):
        """Reviewer's scenario from the PR #74906 review, adapted to the
        post-#93251 dedup semantics.

        Transcript shape (a crash/resume replay re-emits the assistant
        call BEFORE the tool result arrives, so call_X is still
        outstanding — the same trigger as upstream
        test_sanitize_dedup_drops_tool_calls_key_when_all_removed):
            [user,
             assistant(content:'', tool_calls:[call_X]),  # live, outstanding
             assistant(content:None, tool_calls:[call_X]),  # non-final, all dups
             tool(call_X),   # the delayed result, answers the live call
             user]

        After sanitize:
            - the live assistant keeps its tool_calls; the tool result
              survives (no stub needed — the real result follows).
            - the replayed assistant has no tool_calls (dedup stripped
              them) AND must have visible content (post-dedup repair
              re-runs and substitutes the placeholder).
        """
        messages = [
            _user("first question"),
            _assistant_tool_call("call_X"),
            _assistant_tool_call("call_X", content=None),  # dup + content-null
            _tool_result("call_X"),
            _user("follow up"),
        ]
        out = sanitize_api_messages(messages)
        healed = out[2]
        assert "tool_calls" not in healed, (
            "dedup should strip empty tool_calls; instead got: "
            f"{healed.get('tool_calls')!r}"
        )
        assert _has_visible_content(healed), (
            "post-dedup repair should inject visible content; instead "
            f"got: {healed.get('content')!r}"
        )
        # Earlier call's assistant turn keeps its tool_calls intact, and
        # the real tool result survives to answer it.
        assert out[1]["tool_calls"][0]["id"] == "call_X"
        assert out[3]["role"] == "tool" and out[3]["tool_call_id"] == "call_X"
        # Both user turns preserved verbatim.
        assert out[0]["role"] == "user" and out[0]["content"] == "first question"
        assert out[4]["role"] == "user" and out[4]["content"] == "follow up"

    def test_in_message_all_duplicates_healed(self):
        """Single non-final assistant turn whose two tool_calls share one
        id that an earlier, still-outstanding assistant turn already
        registered (the delayed result follows the replay).

        After dedup, the message loses both tool_calls. Post-dedup repair
        then injects the placeholder content.
        """
        messages = [
            _user("q"),
            _assistant_tool_call("call_A"),  # registers call_A, outstanding
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_A", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "call_A", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}},
                ],
            },
            _tool_result("call_A"),
            _user("next"),
        ]
        out = sanitize_api_messages(messages)
        healed = out[2]
        assert "tool_calls" not in healed, (
            "all tool_calls in the message were duplicates of an earlier "
            f"turn; expected stripped: {healed.get('tool_calls')!r}"
        )
        assert _has_visible_content(healed), (
            "post-dedup repair should inject visible content; instead "
            f"got: {healed.get('content')!r}"
        )

    def test_pre_existing_empty_tool_calls_with_null_content(self):
        """Dict already has ``tool_calls: []`` and ``content: null``.

        Pass 0 (drop empty tool_calls) and repair_empty_non_final_messages
        together produce a non-empty placeholder turn.
        """
        messages = [
            _user("q"),
            {"role": "assistant", "content": None, "tool_calls": []},
            _user("next"),
        ]
        out = sanitize_api_messages(messages)
        healed = out[1]
        assert "tool_calls" not in healed
        assert _has_visible_content(healed)

    def test_assistant_with_visible_content_unchanged(self):
        """Guard rail: messages with legitimate visible content are
        not touched by either pass.
        """
        messages = [
            _user("q"),
            {"role": "assistant", "content": "好的，我来处理。"},
            _user("next"),
        ]
        out = sanitize_api_messages(messages)
        assert out[1]["content"] == "好的，我来处理。"
        assert "tool_calls" not in out[1]

    def test_user_messages_with_content_unchanged(self):
        """Guard rail: user messages with content are never substituted.
        repair_empty_non_final_messages runs over (assistant, user) but
        only inserts a placeholder when the message has no payload; a
        user turn with text must stay untouched.
        """
        messages = [
            _user("middle"),
            {"role": "assistant", "content": "hi"},
            _user("end"),
        ]
        out = sanitize_api_messages(messages)
        assert out[0]["content"] == "middle"
        assert out[2]["content"] == "end"

    def test_final_message_exempt_from_repair(self):
        """The final assistant message is exempt from
        repair_empty_non_final_messages by design (an empty final
        assistant turn is legal). Our fix must preserve that.
        """
        messages = [
            _user("q"),
            {"role": "assistant", "content": None, "tool_calls": []},
        ]
        out = sanitize_api_messages(messages)
        assert out[-1]["role"] == "assistant"
        # No placeholder injected — final empty assistant is allowed.
        assert out[-1].get("content") is None
        assert "tool_calls" not in out[-1]