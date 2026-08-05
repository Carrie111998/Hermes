"""Mixin extracted verbatim from ``run_agent.py`` (godfile extraction wave 1).

The methods in this module were moved character-for-character from the
``AIAgent`` class in ``run_agent.py``; class attributes referenced via
``self.``/``cls.`` still resolve through the MRO on ``AIAgent``.
"""

import re
from typing import Any, Dict, List, Optional


class ReasoningHeuristicsMixin:
    def _has_content_after_think_block(self, content: str) -> bool:
        """
        Check if content has actual text after any reasoning/thinking blocks.

        This detects cases where the model only outputs reasoning but no actual
        response, which indicates an incomplete generation that should be retried.
        Must stay in sync with _strip_think_blocks() tag variants.

        Args:
            content: The assistant message content to check

        Returns:
            True if there's meaningful content after think blocks, False otherwise
        """
        if not content:
            return False

        # Remove all reasoning tag variants (must match _strip_think_blocks)
        cleaned = self._strip_think_blocks(content)

        # Check if there's any non-whitespace content remaining
        return bool(cleaned.strip())

    def _strip_think_blocks(self, content: str) -> str:
        """Forwarder — see ``agent.agent_runtime_helpers.strip_think_blocks``."""
        from agent.agent_runtime_helpers import strip_think_blocks
        return strip_think_blocks(self, content)

    @staticmethod
    def _has_natural_response_ending(content: str) -> bool:
        """Heuristic: does visible assistant text look intentionally finished?"""
        if not content:
            return False
        stripped = content.rstrip()
        if not stripped:
            return False
        if stripped.endswith("```"):
            return True
        if stripped.endswith('^'):
            return True
        last = stripped[-1]
        if last in '.!?:)"\']}。！？：）】」』》^':
            return True
        # Emoji ranges (Misc Symbols, Dingbats, Emoticons, Supplemental, etc.)
        if ord(last) >= 0x1F300:
            return True
        return False

    def _is_ollama_glm_backend(self) -> bool:
        """Detect Ollama-hosted GLM models affected by stop misreports.

        Ollama can misreport truncated output as finish_reason='stop'.
        Detection relies on explicit Ollama signatures:
        - Port 11434 (Ollama default)
        - "ollama" in the base URL (e.g. ollama.local, /ollama/ path)
        - provider explicitly set to "ollama"

        Crucially it does NOT match arbitrary local/private endpoints
        (LiteLLM/sglang/vLLM/LM Studio proxies, Tailscale boxes), which
        report finish_reason correctly and were the source of #13971's
        false-positive truncation continuations.
        """
        model_lower = (self.model or "").lower()
        provider_lower = (self.provider or "").lower()
        if "glm" not in model_lower and provider_lower != "zai":
            return False
        if "ollama" in self._base_url_lower or ":11434" in self._base_url_lower:
            return True
        return provider_lower == "ollama"

    def _should_treat_stop_as_truncated(
        self,
        finish_reason: str,
        assistant_message,
        messages: Optional[list] = None,
    ) -> bool:
        """Detect conservative stop->length misreports for Ollama-hosted GLM models."""
        if finish_reason != "stop" or self.api_mode != "chat_completions":
            return False
        if not self._is_ollama_glm_backend():
            return False
        if not any(
            isinstance(msg, dict) and msg.get("role") == "tool"
            for msg in (messages or [])
        ):
            return False
        if assistant_message is None or getattr(assistant_message, "tool_calls", None):
            return False

        content = getattr(assistant_message, "content", None)
        if not isinstance(content, str):
            return False

        visible_text = self._strip_think_blocks(content).strip()
        if not visible_text:
            return False
        if len(visible_text) < 20 or not re.search(r"\s", visible_text):
            return False

        return not self._has_natural_response_ending(visible_text)

    def _looks_like_codex_intermediate_ack(
        self,
        user_message: str,
        assistant_content: str,
        messages: List[Dict[str, Any]],
        require_workspace: bool = True,
    ) -> bool:
        """Forwarder — see ``agent.agent_runtime_helpers.looks_like_codex_intermediate_ack``."""
        from agent.agent_runtime_helpers import looks_like_codex_intermediate_ack
        return looks_like_codex_intermediate_ack(
            self, user_message, assistant_content, messages, require_workspace
        )

    def _extract_reasoning(self, assistant_message) -> Optional[str]:
        """Forwarder — see ``agent.agent_runtime_helpers.extract_reasoning``."""
        from agent.agent_runtime_helpers import extract_reasoning
        return extract_reasoning(self, assistant_message)
