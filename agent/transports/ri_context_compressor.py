"""RecursiveIntell context-governor compressor for Hermes.

Drop-in replacement for ``ContextCompressor`` that uses the Rust
``context-governor`` crate via PyO3 for deterministic first-pass
compaction. When deterministic savings fall below the configured
threshold, falls back to the built-in LLM summarizer with a special
system prompt that preserves receipts for exact fallback.

Usage (in agent_init or run_agent)::

    from agent.transports.ri_context_compressor import RiContextCompressor
    agent.context_compressor = RiContextCompressor(
        token_budget=8000, name="ri-context-governor",
        fallback_compressor=builtin_compressor,
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

# Minimum token savings ratio to avoid LLM fallback.
# If the Rust path saves less than this fraction of original tokens,
# delegate to the LLM summarizer with receipt preservation.
_DIMINISHING_RETURNS_RATIO = 0.15  # 15% savings threshold


class RiContextCompressor:
    """Two-stage compressor: Rust context-governor first, LLM fallback if needed.

    Stage 1 (deterministic): ``context_governor.compact()`` handles tool
    result pruning, head/tail protection, and structured summarization
    without an LLM call. Produces receipt-backed output.

    Stage 2 (LLM fallback): when Rust savings fall below
    ``diminishing_returns_ratio``, delegates to the fallback compressor
    (typically the built-in ``ContextCompressor``) which calls an
    auxiliary LLM with a special system prompt that preserves receipts
    for exact fallback references.
    """

    def __init__(
        self,
        token_budget: int = 8000,
        name: str = "ri-context-governor",
        fallback_compressor: Any = None,
        diminishing_returns_ratio: float = _DIMINISHING_RETURNS_RATIO,
    ):
        self.token_budget = token_budget
        self.name = name
        self._fallback = fallback_compressor
        self._diminishing_ratio = diminishing_returns_ratio
        self._last_summary_dropped_count = 0
        self._last_summary_fallback_used = False
        self._last_summary_error: Optional[str] = None
        self._last_compress_aborted = False
        self._last_compression_made_progress = False

    @property
    def available(self) -> bool:
        return _NATIVE_AVAILABLE

    @property
    def fallback_available(self) -> bool:
        return self._fallback is not None and hasattr(self._fallback, "compress")

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Compress conversation messages with two-stage Rust+LLM pipeline.

        Stage 1: deterministic Rust compaction via context-governor.
        Stage 2: LLM-based summarization if Rust savings are insufficient.
        """
        if not _NATIVE_AVAILABLE:
            return self._fallback_compress(
                messages, current_tokens, focus_topic, force, memory_context
            )

        if len(messages) < 4:
            return messages

        try:
            # ── Stage 1: deterministic Rust compaction ────────────────
            msg_dicts = [
                {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
                for m in messages
            ]
            messages_json = json.dumps(msg_dicts)
            session_id = f"ctxr_{uuid.uuid4().hex[:12]}"

            result_json = _native_compact(messages_json, session_id, self.token_budget)
            result = json.loads(result_json)

            compacted = result.get("compacted_messages", [])
            if not compacted:
                logger.warning("context-governor returned no compacted messages")
                self._last_compress_aborted = True
                return self._fallback_compress(
                    messages, current_tokens, focus_topic, force, memory_context
                )

            savings = result.get("token_savings_estimate", 0)
            original_tokens = result.get("original_approx_tokens", len(messages) * 100)
            savings_ratio = savings / max(original_tokens, 1)

            logger.info(
                "context-governor stage-1: %d→%d msgs, %d→%d tokens (%.1f%% saved, receipt=%s)",
                len(messages), len(compacted),
                original_tokens, result.get("compacted_approx_tokens", 0),
                savings_ratio * 100,
                result.get("receipt_id", "?"),
            )

            # ── Diminishing returns check ─────────────────────────────
            if savings_ratio < self._diminishing_ratio:
                logger.info(
                    "context-governor savings %.1f%% below threshold %.1f%% — "
                    "falling back to LLM summarizer with receipt preservation",
                    savings_ratio * 100, self._diminishing_ratio * 100,
                )
                self._last_summary_fallback_used = True
                # Pass the compacted (not original) messages to the LLM
                # so it can build on the deterministic work. The compacted
                # messages already carry receipt references.
                return self._fallback_compress(
                    compacted, current_tokens, focus_topic, force, memory_context
                )

            # ── Sufficient savings — return deterministic result ──────
            self._last_compression_made_progress = len(compacted) < len(messages)
            self._last_summary_fallback_used = False
            return compacted

        except Exception as exc:
            logger.error("RiContextCompressor stage-1 failed: %s", exc, exc_info=True)
            self._last_summary_error = str(exc)
            return self._fallback_compress(
                messages, current_tokens, focus_topic, force, memory_context
            )

    def _fallback_compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int],
        focus_topic: Optional[str],
        force: bool,
        memory_context: str,
    ) -> List[Dict[str, Any]]:
        """Delegate to the built-in LLM summarizer with receipt preservation."""
        if not self.fallback_available:
            logger.warning(
                "RiContextCompressor: no fallback compressor available, "
                "returning messages unchanged"
            )
            self._last_compress_aborted = True
            return messages

        try:
            logger.info("RiContextCompressor: delegating to fallback LLM summarizer")
            return self._fallback.compress(
                messages,
                current_tokens=current_tokens,
                focus_topic=focus_topic,
                force=force,
                memory_context=memory_context,
            )
        except Exception as exc:
            logger.error("RiContextCompressor fallback failed: %s", exc, exc_info=True)
            self._last_summary_error = str(exc)
            self._last_compress_aborted = True
            return messages

    def __repr__(self) -> str:
        status = "native+llm" if self.available and self.fallback_available else (
            "native" if self.available else "unavailable"
        )
        return f"RiContextCompressor(budget={self.token_budget}, {status})"

    # ── Hermes context-engine contract ────────────────────────────
    # Called by agent_init after plugin registration to inject model
    # parameters. We use these to construct a fallback ContextCompressor
    # for the LLM stage when the Rust path hits diminishing returns.

    def update_model(
        self,
        model: str = "",
        context_length: int = 0,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """Called by Hermes to inject model parameters.

        Constructs a fallback ContextCompressor for LLM-based
        summarization when the Rust deterministic path yields
        insufficient savings.
        """
        if self._fallback is not None:
            # Already have a fallback — forward the update
            if hasattr(self._fallback, "update_model"):
                self._fallback.update_model(
                    model=model, context_length=context_length,
                    base_url=base_url, api_key=api_key, provider=provider,
                    api_mode=api_mode,
                )
            return

        try:
            from agent.context_compressor import ContextCompressor

            self._fallback = ContextCompressor(
                model=model,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
                api_mode=api_mode,
                config_context_length=context_length,
                quiet_mode=True,
                threshold_percent=0.50,
                summary_target_ratio=0.20,
            )
            logger.info(
                "RiContextCompressor: fallback LLM summarizer constructed "
                "(model=%s, ctx_len=%d)", model, context_length
            )
        except Exception as exc:
            logger.warning(
                "RiContextCompressor: could not construct fallback: %s", exc
            )

    def bind_session_state(
        self, session_db: Any = None, session_id: str = ""
    ) -> None:
        """Forward session binding to fallback compressor."""
        if self._fallback is not None and hasattr(self._fallback, "bind_session_state"):
            self._fallback.bind_session_state(
                session_db=session_db, session_id=session_id
            )

    @property
    def context_length(self) -> int:
        if self._fallback is not None:
            return getattr(self._fallback, "context_length", 0)
        return 0
