"""Auto-capture engine for holographic memory.

Buffers conversation turns from sync_turn() and periodically compresses
them into structured facts via the host-owned LLM (ctx.llm).

Architecture
------------
CaptureEngine.observe_turn(messages) is called from sync_turn() after
each turn. It appends the turn's messages to an internal ring buffer.
Every ``interval`` turns (or on explicit compress_and_store()), it:

1. Builds a compression prompt from the buffered messages
2. Calls ctx.llm.complete() to extract structured observations
3. Parses the LLM response (one observation per line)
4. Stores each via store.add_fact() with category="auto_capture"

The buffer is then cleared so the next interval starts fresh.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_COMPRESSION_PROMPT = """You are observing a conversation between a user and an AI agent.
Extract compact, durable facts from the conversation below.

Rules:
- One fact per line, each starting with "- "
- Keep facts short (under 200 chars each)
- Only extract facts the user would want the agent to remember long-term
- Skip: greetings, pleasantries, trivial chat
- Skip: instructions the agent just followed (those are ephemeral)
- Include: preferences, tools used, decisions made, project details, environment facts

Conversation:
{conversation}

Facts:"""


class CaptureEngine:
    """Buffers turns and periodically compresses into facts via LLM."""

    def __init__(
        self,
        store: Any,
        llm: Any,
        interval: int = 5,
    ):
        self._store = store
        self._llm = llm
        # Clamp: interval 0 (or negative) would break the modulo in
        # observe_turn. The provider already validates config before
        # constructing us; this is a defensive floor for direct callers.
        self._interval = interval if interval and interval > 0 else 5
        self._buffer: List[Dict[str, Any]] = []
        self._turn_count = 0

    def observe_turn(self, messages: List[Dict[str, Any]]) -> None:
        """Feed a completed turn's messages into the capture buffer.

        Called from sync_turn(). Every ``interval`` turns the buffer is
        automatically compressed into facts.
        """
        if not messages:
            return
        self._buffer.extend(messages)
        self._turn_count += 1
        if self._turn_count % self._interval == 0:
            self.compress_and_store()

    def compress_and_store(self) -> int:
        """Compress buffered messages into facts and store them.

        Returns the number of facts stored. Called automatically at the
        interval boundary and explicitly at session end.
        """
        if not self._buffer:
            return 0

        # Build a compact conversation transcript for the LLM
        transcript_lines = []
        for msg in self._buffer:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Tool calls with multi-part content (text + image_urls)
                texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(texts)
            if isinstance(content, str) and content.strip():
                transcript_lines.append(f"{role}: {content.strip()[:500]}")

        if not transcript_lines:
            self._buffer.clear()
            return 0

        conversation_text = "\n".join(transcript_lines[-50:])  # last 50 messages max
        prompt = _COMPRESSION_PROMPT.format(conversation=conversation_text)

        try:
            result = self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                purpose="auto-capture",
            )
        except Exception as e:
            logger.warning("Auto-capture LLM call failed: %s", e)
            self._buffer.clear()
            return 0

        self._buffer.clear()
        facts = self._parse_facts(result.text)
        stored = 0
        for fact in facts:
            if len(fact) < 10:
                continue  # skip too-short fragments
            try:
                self._store.add_fact(
                    fact[:400],
                    category="auto_capture",
                    tags="auto_capture",
                )
                stored += 1
            except Exception as e:
                logger.debug("Auto-capture store failed: %s", e)

        if stored:
            logger.info("Auto-captured %d facts from %d turns", stored, self._turn_count)
        return stored

    def turn_count(self) -> int:
        """Return total turns observed since creation."""
        return self._turn_count

    @staticmethod
    def _parse_facts(text: str) -> List[str]:
        """Parse LLM response into fact strings.

        Expects one fact per line starting with ``- ``.
        Falls back to splitting on newlines if no ``- `` found.
        """
        if not text or not text.strip():
            return []
        lines = text.strip().split("\n")
        facts = []
        for line in lines:
            line = line.strip()
            if line.startswith("- "):
                facts.append(line[2:].strip())
            elif line and not line.startswith("#") and not line.startswith("Facts:"):
                facts.append(line)
        return facts
