"""
Tests for interim commentary streaming and truncation handling (#88954).

Ensures that partial streams truncated at tool-call boundaries are not falsely
marked as `already_streamed` via `_interim_content_fully_streamed`, preventing
tail-character data loss on streaming platforms like Telegram.
"""

import pytest
from run_agent import AIAgent


class TestInterimStreamTruncation:
    def test_interim_content_fully_streamed_exact_match(self):
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = "I am going to check the weather in Tokyo."
        
        # Exact match
        assert agent._interim_content_fully_streamed("I am going to check the weather in Tokyo.") is True

    def test_interim_content_fully_streamed_with_think_blocks(self):
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = "<think>Let me see</think>I will search for the file."
        
        # Exact match after think-block stripping
        assert agent._interim_content_fully_streamed("<think>Thinking...</think>I will search for the file.") is True

    def test_interim_content_fully_streamed_truncated_prefix_returns_false(self):
        """Regression test for #88954:
        Partial stream truncated at tool-call boundary must return False in _interim_content_fully_streamed.
        """
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = "...to pick it u"
        full_content = "...to pick it up"
        
        assert agent._interim_content_fully_streamed(full_content) is False

    def test_interim_content_fully_streamed_empty_stream(self):
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = ""
        assert agent._interim_content_fully_streamed("Some content") is False
        
        agent._current_streamed_assistant_text = None
        assert agent._interim_content_fully_streamed("Some content") is False

    def test_interim_content_fully_streamed_empty_content(self):
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = "Some streamed text"
        assert agent._interim_content_fully_streamed("") is False
        assert agent._interim_content_fully_streamed(None) is False

    def test_interim_content_was_streamed_preserves_prefix_for_preview(self):
        """_interim_content_was_streamed preserves prefix-match contract for internal preview marking."""
        agent = AIAgent.__new__(AIAgent)
        agent._current_streamed_assistant_text = "hello"
        assert agent._interim_content_was_streamed("hello world") is True

    def test_emit_interim_assistant_message_passes_already_streamed_false_on_truncation(self):
        """When commentary is truncated in stream, _emit_interim_assistant_message
        must pass already_streamed=False to callback so on_commentary delivers full text.
        """
        agent = AIAgent.__new__(AIAgent)
        agent.show_commentary = True
        agent.session_id = "test-session"
        agent.model = "test-model"
        agent.provider = "test-provider"
        agent.platform = "telegram"
        agent._delivered_interim_texts = set()
        
        # Stream got cut off
        agent._current_streamed_assistant_text = "Checking database with a delayed sta"
        
        delivered_calls = []
        def mock_callback(text, already_streamed=False):
            delivered_calls.append((text, already_streamed))
            
        agent.interim_assistant_callback = mock_callback
        
        full_message = {
            "role": "assistant",
            "content": "Checking database with a delayed start",
            "tool_calls": [{"id": "call_1", "function": {"name": "db_query", "arguments": "{}"}}],
        }
        
        agent._emit_interim_assistant_message(full_message)
        
        assert len(delivered_calls) == 1
        text, already_streamed = delivered_calls[0]
        assert text == "Checking database with a delayed start"
        assert already_streamed is False  # Must be False so gateway calls on_commentary()

    def test_emit_interim_assistant_message_passes_already_streamed_true_on_complete_stream(self):
        """When commentary is fully streamed, _emit_interim_assistant_message
        passes already_streamed=True so gateway calls on_segment_break() without duplicate send.
        """
        agent = AIAgent.__new__(AIAgent)
        agent.show_commentary = True
        agent.session_id = "test-session"
        agent.model = "test-model"
        agent.provider = "test-provider"
        agent.platform = "telegram"
        agent._delivered_interim_texts = set()
        
        agent._current_streamed_assistant_text = "Checking database with a delayed start"
        
        delivered_calls = []
        def mock_callback(text, already_streamed=False):
            delivered_calls.append((text, already_streamed))
            
        agent.interim_assistant_callback = mock_callback
        
        full_message = {
            "role": "assistant",
            "content": "Checking database with a delayed start",
            "tool_calls": [{"id": "call_1", "function": {"name": "db_query", "arguments": "{}"}}],
        }
        
        agent._emit_interim_assistant_message(full_message)
        
        assert len(delivered_calls) == 1
        text, already_streamed = delivered_calls[0]
        assert text == "Checking database with a delayed start"
        assert already_streamed is True  # True -> on_segment_break()
