"""Honest handoff: strict mode surfaces an LLM failure instead of a silent
extractive fallback labelled as an LLM handoff.

An explicit manual handoff request is strict by default: if the auxiliary LLM
cannot produce the structured handoff, the caller must be told and offered an
explicit extractive recovery — NOT handed a low-fidelity extractive handoff
presented as an LLM one. Auto-compaction stays non-strict (the extractive
fallback is fine there — it is a durable raw record, not context loss).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import agent.handoff as ho
from agent.handoff import HandoffLLMError, generate_handoff


class TestCallAuxiliaryLLMStrict:
    def test_strict_reraises_as_handoff_error(self):
        with patch("agent.auxiliary_client.call_llm", side_effect=Exception("401 user not found")):
            with pytest.raises(HandoffLLMError):
                ho._call_auxiliary_llm("prompt", strict=True)

    def test_non_strict_swallows_to_none(self):
        with patch("agent.auxiliary_client.call_llm", side_effect=Exception("401 user not found")):
            assert ho._call_auxiliary_llm("prompt", strict=False) is None


class TestGenerateHandoffStrict:
    def test_strict_llm_failure_raises(self, tmp_path):
        """strict_llm=True + LLM error → HandoffLLMError (no silent extractive)."""
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", side_effect=HandoffLLMError("503 overloaded")):
            with pytest.raises(HandoffLLMError):
                generate_handoff(
                    session_id="s1",
                    messages=[{"role": "user", "content": "hi"}],
                    reason="manual_handoff",
                    llm_summarize=True,
                    strict_llm=True,
                )

    def test_strict_llm_empty_raises(self, tmp_path):
        """strict_llm=True + empty LLM content → HandoffLLMError (not extractive)."""
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value=None):
            with pytest.raises(HandoffLLMError):
                generate_handoff(
                    session_id="s1",
                    messages=[{"role": "user", "content": "hi"}],
                    reason="manual_handoff",
                    llm_summarize=True,
                    strict_llm=True,
                )

    def test_strict_llm_success_returns_path(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value="# LLM Handoff\n\nGoal: x"):
            path = generate_handoff(
                session_id="s1",
                messages=[{"role": "user", "content": "hi"}],
                reason="manual_handoff",
                llm_summarize=True,
                strict_llm=True,
            )
            assert path.exists()
            assert "LLM Handoff" in path.read_text(encoding="utf-8")

    def test_non_strict_failure_still_extractive(self, tmp_path):
        """Auto-compaction default: strict_llm=False + LLM fail → extractive path, no raise."""
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value=None):
            path = generate_handoff(
                session_id="s1",
                messages=[{"role": "user", "content": "hi"}],
                reason="compression",
                llm_summarize=True,
                strict_llm=False,
            )
            assert path.exists()  # extractive handoff written, no exception
