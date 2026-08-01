"""RecursiveIntell context-governor compressor for Hermes.

Drop-in replacement for ``ContextCompressor`` that uses the Rust
``context-governor`` crate via PyO3 instead of an auxiliary LLM.

Usage (in agent_init or run_agent)::

    from agent.transports.ri_context_compressor import RiContextCompressor
    agent.context_compressor = RiContextCompressor(
        token_budget=8000, name="ri-context-governor"
    )
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NATIVE_AVAILABLE = False
try:
    from context_governor._native import compact as _native_compact

    _NATIVE_AVAILABLE = True
except ImportError:
    logger.debug("context-governor native extension not available")


class RiContextCompressor:
    """Rust-backed context compressor using context-governor."""

    def __init__(
        self,
        token_budget: int = 8000,
        name: str = "ri-context-governor",
    ):
        self.token_budget = token_budget
        self.name = name
        self._last_summary_dropped_count = 0
        self._last_summary_fallback_used = False
        self._last_summary_error: Optional[str] = None
        self._last_compress_aborted = False
        self._last_compression_made_progress = False

    @property
    def available(self) -> bool:
        return _NATIVE_AVAILABLE

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Compress conversation messages using Rust context-governor.

        Returns the compacted message list.  Falls back to returning
        messages unchanged if the native extension is unavailable or
        the call fails.
        """
        if not _NATIVE_AVAILABLE:
            logger.warning(
                "RiContextCompressor: native extension unavailable, "
                "returning messages unchanged"
            )
            return messages

        if len(messages) < 4:
            return messages

        try:
            # Convert messages to JSON format expected by the Rust compact()
            msg_dicts = [
                {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
                for m in messages
            ]
            messages_json = json.dumps(msg_dicts)
            session_id = f"ctxr_{uuid.uuid4().hex[:12]}"

            # Run Rust compaction
            result_json = _native_compact(messages_json, session_id, self.token_budget)
            result = json.loads(result_json)

            compacted = result.get("compacted_messages", [])
            if not compacted:
                logger.warning(
                    "context-governor returned no compacted messages"
                )
                self._last_compress_aborted = True
                return messages

            # Update tracking fields
            self._last_compression_made_progress = (
                len(compacted) < len(messages)
            )
            savings = result.get("token_savings_estimate", 0)
            logger.info(
                "context-governor: %d messages → %d (saved ~%d tokens, "
                "receipt=%s)",
                len(messages),
                len(compacted),
                savings,
                result.get("receipt_id", "?"),
            )

            return compacted

        except Exception as exc:
            logger.error(
                "RiContextCompressor.compress failed: %s", exc, exc_info=True
            )
            self._last_summary_error = str(exc)
            self._last_compress_aborted = True
            return messages

    def __repr__(self) -> str:
        status = "native" if self.available else "unavailable"
        return f"RiContextCompressor(budget={self.token_budget}, {status})"
