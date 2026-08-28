"""Streamed reasoning_details preservation + per-block delta merging.

Port of earendil-works/pi#8605 (commit c5ad7c1b0), adapted to hermes'
streaming accumulator in agent/chat_completion_helpers.py.

Before this fix the streaming path dropped ``delta.reasoning_details``
entirely — only non-streaming responses preserved the OpenRouter unified
reasoning replay data (signatures, encrypted blocks) that providers require
for multi-turn reasoning continuity. And when accumulating, consecutive
``reasoning.text`` / ``reasoning.summary`` delta fragments must merge into
one logical entry (OpenRouter streams them as word-level deltas), while
encrypted entries stay discrete.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import _append_streamed_reasoning_detail


def _make_chunk(content=None, finish_reason=None, model=None,
                reasoning_details=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    if reasoning_details is not None:
        delta.reasoning_details = reasoning_details
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)],
        model=model,
        usage=usage,
    )


class TestAppendStreamedReasoningDetail:
    def test_merges_consecutive_text_fragments(self):
        acc = []
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.text", "text": "The user "})
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.text", "text": "wants X."})
        assert len(acc) == 1
        assert acc[0]["text"] == "The user wants X."

    def test_merges_consecutive_summary_fragments(self):
        acc = []
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.summary", "summary": "Part 1 "})
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.summary", "summary": "part 2"})
        assert len(acc) == 1
        assert acc[0]["summary"] == "Part 1 part 2"

    def test_signature_from_final_fragment_is_kept(self):
        acc = []
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.text", "text": "a"})
        _append_streamed_reasoning_detail(
            acc, {"type": "reasoning.text", "text": "b", "signature": "sig123", "id": "r1"},
        )
        assert len(acc) == 1
        assert acc[0]["signature"] == "sig123"
        assert acc[0]["id"] == "r1"

    def test_encrypted_entries_stay_discrete(self):
        acc = []
        _append_streamed_reasoning_detail(
            acc, {"type": "reasoning.encrypted", "data": "AAAA"})
        _append_streamed_reasoning_detail(
            acc, {"type": "reasoning.encrypted", "data": "BBBB"})
        assert len(acc) == 2

    def test_type_change_starts_new_entry(self):
        acc = []
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.text", "text": "a"})
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.summary", "summary": "s"})
        _append_streamed_reasoning_detail(acc, {"type": "reasoning.text", "text": "b"})
        assert len(acc) == 3

    def test_object_shaped_detail_normalized(self):
        acc = []
        _append_streamed_reasoning_detail(
            acc, SimpleNamespace(type="reasoning.text", text="obj frag "))
        _append_streamed_reasoning_detail(
            acc, SimpleNamespace(type="reasoning.text", text="merged"))
        assert len(acc) == 1
        assert acc[0]["text"] == "obj frag merged"


class TestStreamingPreservesReasoningDetails:
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_streamed_details_land_on_final_message(self, mock_close, mock_create):
        from run_agent import AIAgent

        chunks = [
            _make_chunk(reasoning_details=[{"type": "reasoning.text", "text": "I should "}]),
            _make_chunk(reasoning_details=[{"type": "reasoning.text", "text": "answer.",
                                            "signature": "sigZ"}]),
            _make_chunk(content="Hello!", finish_reason="stop", model="test-model"),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        mock_create.return_value = mock_client

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        response = agent._interruptible_streaming_api_call({})

        msg = response.choices[0].message
        assert msg.content == "Hello!"
        rd = getattr(msg, "reasoning_details", None)
        assert rd is not None
        assert len(rd) == 1
        assert rd[0]["text"] == "I should answer."
        assert rd[0]["signature"] == "sigZ"

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_no_details_leaves_attribute_absent(self, mock_close, mock_create):
        from run_agent import AIAgent

        chunks = [
            _make_chunk(content="plain", finish_reason="stop", model="test-model"),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        mock_create.return_value = mock_client

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False

        response = agent._interruptible_streaming_api_call({})
        assert not hasattr(response.choices[0].message, "reasoning_details")
