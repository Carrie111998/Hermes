"""Summary handoff/provenance kernel mixin for ContextCompressor (LB8).

This module contains the ten summary-prefix, metadata, and user-provenance
methods extracted mechanically from ``context_compressor.py``. The compatibility
bindings at the bottom are deliberately after the class definition so either
module can be imported first without a partially-initialized-class failure.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from agent.context_compressor_text_utils import _content_text_for_contains
from tools.todo_tool import TODO_INJECTION_HEADER


# fmt: off
class ContextCompressorSummaryKernelMixin:
    """Summary handoff classification and user-provenance helpers."""

    @staticmethod
    def _strip_summary_prefix(summary: str) -> str:
        """Return summary body without the current, legacy, or any historical
        handoff prefix.

        Historical prefixes must be stripped too: a handoff persisted under an
        older prefix can be inherited into a resumed lineage (#35344), and if we
        only re-prepend the current prefix without removing the old one, the
        stale directive it carried stays embedded in the body.
        """
        text = (summary or "").strip()
        # Merge-into-tail summaries wrap prior tail content before the summary
        # body. Drop everything up to and including the delimiter so only the
        # real summary body is carried forward on re-compaction — otherwise the
        # [PRIOR CONTEXT] header and stale tail content leak into the next
        # summarizer prompt.
        if _MERGED_SUMMARY_DELIMITER in text:
            text = text.split(_MERGED_SUMMARY_DELIMITER, 1)[1].strip()
        for prefix in (SUMMARY_PREFIX, LEGACY_SUMMARY_PREFIX, *_HISTORICAL_SUMMARY_PREFIXES):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        # Strip the end marker too — a rehydrated handoff body that keeps it
        # would leak the boundary directive into the iterative-update
        # summarizer prompt (and the marker is re-appended on insertion anyway).
        # Forced user-leading merged summaries keep the live tail request after
        # this marker, so truncate at the marker even when it is not the final
        # content.
        marker_idx = text.find(_SUMMARY_END_MARKER)
        if marker_idx >= 0:
            text = text[:marker_idx].rstrip()
        return text

    @classmethod
    def _with_summary_prefix(cls, summary: str) -> str:
        """Normalize summary text to the current compaction handoff format."""
        text = cls._strip_summary_prefix(summary)
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    @staticmethod
    def _starts_with_summary_prefix(text: str) -> bool:
        """Return True if *text* begins with any known handoff prefix."""
        if text.startswith(SUMMARY_PREFIX) or text.startswith(LEGACY_SUMMARY_PREFIX):
            return True
        return any(text.startswith(p) for p in _HISTORICAL_SUMMARY_PREFIXES)

    @classmethod
    def classify_summary_content(cls, content: Any) -> Optional[str]:
        """Classify how *content* relates to a compaction summary.

        Returns:
            ``"standalone"``: the entire message IS a compaction handoff
            (current, legacy, or historical prefix at the start). Frontends
            may restyle/collapse the whole message as a summary.

            ``"merged"``: a merge-into-tail message — real preserved turn
            content wrapped under ``_MERGED_PRIOR_CONTEXT_HEADER``, followed by
            ``_MERGED_SUMMARY_DELIMITER`` and the summary body. The message
            *contains* a summary but is not only a summary; collapsing the
            whole message would hide the preserved content.

            ``None``: no compaction summary detected.
        """
        text = _content_text_for_contains(content).lstrip()
        # Merge-into-tail summaries wrap prior tail content before the summary,
        # so the handoff prefix lands after _MERGED_SUMMARY_DELIMITER rather than
        # at the start. Detect the summary in that region too, otherwise callers
        # (auto-focus skip, carry-forward summary find, last-real-user anchor)
        # mistake a merged summary message for a real user turn.
        if _MERGED_SUMMARY_DELIMITER in text:
            after = text.split(_MERGED_SUMMARY_DELIMITER, 1)[1].lstrip()
            return "merged" if cls._starts_with_summary_prefix(after) else None
        return "standalone" if cls._starts_with_summary_prefix(text) else None

    @classmethod
    def _is_context_summary_content(cls, content: Any) -> bool:
        return cls.classify_summary_content(content) is not None

    @staticmethod
    def _has_compressed_summary_metadata(message: Any) -> bool:
        """Return True if *message* carries the compressed-summary flag.

        Callers (frontends, CLI, gateway) can use this to distinguish context
        compaction summaries from real assistant or user messages without
        relying on content-prefix heuristics.  The flag is in-process only —
        the wire sanitizers strip underscore-prefixed keys before API calls.
        """
        if not isinstance(message, dict):
            return False
        return bool(message.get(COMPRESSED_SUMMARY_METADATA_KEY))

    @classmethod
    def _transcript_has_real_user_turn(cls, messages: List[Dict[str, Any]]) -> bool:
        """Return whether *messages* contain a user-authored turn.

        Compaction summaries can deliberately carry ``role="user"`` to keep
        strict provider transcripts valid. The metadata/content checks prevent
        those synthetic transport rows from becoming evidence of a real user.
        """
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if cls._is_synthetic_compression_user_turn(message):
                continue
            return True
        return False

    @classmethod
    def _is_synthetic_compression_user_turn(cls, message: Any) -> bool:
        """Recognize internal user-role rows after SessionDB projection.

        SessionDB preserves role/content but not underscore-prefixed metadata,
        so the stable todo and continuation content markers are authoritative.
        """
        if not isinstance(message, dict) or message.get("role") != "user":
            return False
        if cls._has_compressed_summary_metadata(message):
            return True
        content = message.get("content")
        if cls._is_context_summary_content(content):
            return True
        text = _content_text_for_contains(content).strip()
        return text in {
            COMPRESSION_CONTINUATION_USER_CONTENT,
            _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
        } or text.startswith(
            TODO_INJECTION_HEADER + "\n"
        )

    @staticmethod
    def _validate_summary_user_provenance(summary: str, has_user_turn: bool) -> None:
        """Reject user attribution when the source transcript has no user."""
        if has_user_turn:
            return
        match = re.search(
            rf"(?ms)^{re.escape(HISTORICAL_TASK_HEADING)}\s*\n(.*?)(?=\n##\s|\Z)",
            summary,
        )
        task_snapshot = match.group(1).strip() if match else ""
        # NOTE: the "User asked:" scan covers the WHOLE summary, so tool output
        # quoted verbatim in e.g. Completed Actions can false-positive in a
        # zero-user session. That is acceptable: the RuntimeError only rides
        # the existing retry/deterministic-fallback path (which emits the
        # no-user sentinel itself), so a rare false positive costs one retry
        # rather than letting fabricated user attribution persist.
        if (
            task_snapshot != _NO_USER_TASK_SENTINEL
            or re.search(r"\bUser\s+asked\s*:", summary, re.IGNORECASE)
        ):
            raise RuntimeError(
                "Context compression summary invented user attribution for a "
                "session with no user-authored turns"
            )

    @classmethod
    def _is_context_summary_message(cls, message: Any) -> bool:
        """Return True for summary handoff messages by metadata or content."""
        if not isinstance(message, dict):
            return False
        return cls._has_compressed_summary_metadata(
            message
        ) or cls._is_context_summary_content(message.get("content"))


# fmt: on


# Bind the godfile's constants only after this mixin exists. This preserves
# both import orders: importing this module first lets context_compressor import
# the already-defined class, while the normal godfile import has constants ready.
from agent.context_compressor import (  # noqa: E402
    COMPRESSED_SUMMARY_METADATA_KEY,
    COMPRESSION_CONTINUATION_USER_CONTENT,
    HISTORICAL_TASK_HEADING,
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    _HISTORICAL_SUMMARY_PREFIXES,
    _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
    _MERGED_SUMMARY_DELIMITER,
    _NO_USER_TASK_SENTINEL,
    _SUMMARY_END_MARKER,
)

__all__ = ["ContextCompressorSummaryKernelMixin"]
