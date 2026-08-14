"""Config-gated spill of oversized tool results to session-scoped files.

When ``tools.spill.enabled`` is true and a tool result's UTF-8 size exceeds
``tools.spill.max_inline_bytes``, the FULL text is written once to a
session-scoped file under ``HERMES_HOME/sessions/spill/<session>/`` and the
inline message becomes a bounded head/tail preview plus a locator notice::

    (N bytes omitted. Full result stored at: /path/to/file)

The model recovers the full content by calling ``read_file`` on the path.

Design rules (mirroring the dsh spill-policy reference implementation):

1. **The notice's byte cost is reserved INSIDE the cap.** The replacement is
   ``preview + "\\n\\n" + notice``; the preview budget is
   ``cap - len(notice at worst-case omitted count) - 2`` (the 2 is the join),
   and the final replacement is re-checked against the cap before it is
   returned. A replacement that would exceed the cap is abandoned and the
   original stays inline — the policy NEVER emits something larger than the
   advertised cap.
2. **Best-effort storage.** A write failure (permissions, ENOSPC, missing
   HERMES_HOME) logs a warning and keeps the result inline. A spill failure
   must never hide a tool result.
3. **Read tools are skipped.** ``read_file`` (and siblings) never spill —
   otherwise the model's recovery path would itself spill and loop
   (read → spill → read again).
4. **The transform happens exactly once, at write time.** This module is
   invoked where the tool result string becomes a conversation message; the
   stored message is already the preview+notice form, so the transcript
   between steps stays byte-identical (no re-spill on later requests — a
   prompt-cache-safe property, per AGENTS.md).
5. **Default off.** ``enabled: false`` is the conservative default; the
   legacy char-based persistence (``tools.tool_result_storage``) remains the
   always-on safety net.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

#: Tools whose purpose is reading files. Spilling their results would create a
#: read → spill → read-again loop, because the model recovers spilled content
#: via ``read_file``. Keep the set deliberately small and conservative.
READ_TOOLS: frozenset[str] = frozenset({"read_file"})

#: Cap (UTF-8 bytes) used when ``max_inline_bytes`` is not configured. Matches
#: the legacy char-based default (100_000) so the two layers align.
DEFAULT_MAX_INLINE_BYTES: int = 100_000

#: Subdirectory under ``HERMES_HOME`` for session artifacts (existing
#: convention: ``agent.logs_dir = hermes_home / "sessions"``).
_SESSIONS_DIR_NAME = "sessions"
#: Spill artifact root under the sessions dir.
_SPILL_DIR_NAME = "spill"

#: Path-segment sanitization, mirroring ``tools.tool_result_storage``.
_UNSAFE_SEGMENT_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_SEGMENT_LEN = 80
_MAX_FILENAME_STEM = 120


@dataclass(frozen=True)
class SpillConfig:
    """Resolved, validated spill configuration for tool result bounding."""

    enabled: bool = False
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES

    @classmethod
    def from_raw(cls, raw: Any) -> "SpillConfig":
        """Build a config from a raw dict / bool / None.

        ``False``/``None``/missing → disabled (the conservative default).
        Numeric fields are clamped to sane ranges instead of raising, so a
        typo in user config cannot break tool execution.
        """
        if raw is True:
            return cls(enabled=True)
        if raw is False or raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls()

        enabled = bool(raw.get("enabled", False))
        cap = _safe_int(raw.get("max_inline_bytes"), DEFAULT_MAX_INLINE_BYTES)
        cap = max(1, min(cap, 50_000_000))
        return cls(enabled=enabled, max_inline_bytes=cap)

    def resolve(self) -> "SpillConfig":
        """Return self; kept for API symmetry with budget resolution helpers."""
        return self


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> SpillConfig:
    """Load ``tools.spill`` from the user config file (best-effort)."""
    try:
        from hermes_cli.config import load_config as _load

        cfg = _load() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        if not isinstance(tools_cfg, dict):
            tools_cfg = {}
        return SpillConfig.from_raw(tools_cfg.get("spill"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to load spill config: %s", exc)
        return SpillConfig()


# ---------------------------------------------------------------------------
# Byte-safe text helpers
# ---------------------------------------------------------------------------


def _utf8_len(text: str) -> int:
    """UTF-8 byte length of ``text``."""
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes, never splitting
    a multibyte character. ``max_bytes`` <= 0 yields ``""``."""
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[:max_bytes]
    # ``cut`` is a prefix of valid UTF-8, so the ONLY invalid part is a
    # possibly-truncated final character; back off byte-by-byte until it
    # decodes (at most one multibyte char's width of iterations).
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _preview_head_tail(text: str, budget_bytes: int) -> Tuple[str, int]:
    """Split ``text`` into a head/tail preview within ``budget_bytes``.

    head gets ``ceil(budget/2)`` bytes, tail gets ``floor(budget/2)`` bytes
    (mirrors the TextRetainer headTail split). Returns ``(preview, omitted)``
    where ``omitted`` is the number of UTF-8 bytes NOT kept inline.
    """
    if budget_bytes <= 0:
        return "", _utf8_len(text)
    total = _utf8_len(text)
    head_bytes = math.ceil(budget_bytes / 2)
    tail_bytes = math.floor(budget_bytes / 2)
    head = _truncate_utf8(text, head_bytes)
    kept_head = _utf8_len(head)
    tail = _truncate_utf8(text[::-1], tail_bytes)[::-1]
    kept = kept_head + _utf8_len(tail)
    if kept >= total:
        return text, 0
    preview = head + tail
    # Defensive: the byte-safe truncations guarantee kept <= budget; kept can
    # never exceed total (head/tail are disjoint ends of the same string).
    return preview, total - kept


# ---------------------------------------------------------------------------
# Notice + storage
# ---------------------------------------------------------------------------


def _spill_notice(omitted_bytes: int, path: str) -> str:
    """The locator notice appended to a spilled preview.

    Format is fixed and model-facing: ``(N bytes omitted. Full result stored
    at: <path>)``. ``path`` is the absolute spill file path the model can pass
    to ``read_file``.
    """
    return f"({omitted_bytes} bytes omitted. Full result stored at: {path})"


def _safe_segment(raw: str, fallback: str = "segment") -> str:
    """Sanitize an untrusted string (session id, tool name, tool use id) into
    one traversal-safe path segment. Mirrors ``_safe_result_filename`` from
    ``tools.tool_result_storage``: unsafe chars become ``_``, whole-segment
    dots are neutralized, and overlong stems get a sha256 digest suffix."""
    raw_str = str(raw or "")
    safe = _UNSAFE_SEGMENT_CHARS.sub("_", raw_str).strip("._-")
    if not safe:
        return fallback
    if len(safe) > _MAX_SEGMENT_LEN:
        digest = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:_MAX_SEGMENT_LEN].rstrip('._-') or fallback}_{digest}"
    return safe


def spill_dir(session_id: str, hermes_home: Optional[Path] = None) -> Path:
    """The session-scoped spill directory.

    ``<HERMES_HOME>/sessions/spill/<safe-session-id>/`` — follows the existing
    ``hermes_home / "sessions"`` artifact convention (``agent.logs_dir``).
    The directory is created lazily by the writer.
    """
    from hermes_constants import get_hermes_home

    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / _SESSIONS_DIR_NAME / _SPILL_DIR_NAME / _safe_segment(session_id, "session")


def spill_path(
    session_id: str,
    tool_name: str,
    tool_use_id: str,
    hermes_home: Optional[Path] = None,
) -> Path:
    """The deterministic spill file path for one tool call.

    Deterministic (derived from the call id, not random) so re-processing the
    same call overwrites the same file — the transform is idempotent and no
    orphans accumulate. Tool name and call id are both sanitized to single
    safe segments, so a model-controlled id can never traverse the tree.
    """
    name = _safe_segment(tool_name, "tool")
    call = _safe_segment(tool_use_id, "result")
    stem = f"{name}_{call}"
    if len(stem) > _MAX_FILENAME_STEM:
        digest = hashlib.sha256(f"{tool_name}|{tool_use_id}".encode("utf-8")).hexdigest()[:12]
        stem = f"{stem[:_MAX_FILENAME_STEM].rstrip('._-') or 'result'}_{digest}"
    return spill_dir(session_id, hermes_home) / f"{stem}.txt"


def _write_spill_file(content: str, path: Path) -> bool:
    """Best-effort local write of the FULL result. Returns True on success;
    on any failure logs a warning and returns False (caller keeps inline)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("tool-spill: could not write spill file %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def maybe_spill_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    session_id: Optional[str] = None,
    config: Optional[SpillConfig] = None,
    hermes_home: Optional[Path] = None,
) -> str:
    """Spill an oversized tool result to a session-scoped file.

    Returns the (possibly replaced) string to store as the tool message
    content. The transform is applied once, here, at write time — the stored
    message is already the final form, so the transcript between steps is
    byte-identical and no later request re-spills it.

    Best-effort: disabled config, a read tool, a missing session id, a write
    failure, or an over-cap replacement all keep ``content`` unchanged (with
    a warning log), never an error.
    """
    cfg = config if config is not None else load_config()
    if not cfg.enabled:
        return content
    if tool_name in READ_TOOLS:
        return content

    total_bytes = _utf8_len(content)
    cap = cfg.max_inline_bytes
    if total_bytes <= cap:
        return content

    if not session_id:
        logger.warning("tool-spill: no session id for %s; keeping inline content", tool_name)
        return content

    path = spill_path(session_id, tool_name, tool_use_id, hermes_home=hermes_home)
    if not _write_spill_file(content, path):
        return content

    # Reserve the notice's byte cost INSIDE the cap so the replacement
    # (preview + "\n\n" + notice) never exceeds it. The reservation prices
    # the notice at the worst-case omitted count (the full byte total): its
    # digit count bounds the real count's, so the reserved size is a safe
    # upper bound and the final notice is never longer than reserved.
    reserve = _utf8_len(_spill_notice(total_bytes, str(path))) + 2
    preview_budget = max(0, cap - reserve)
    preview_text, omitted = _preview_head_tail(content, preview_budget)
    notice = _spill_notice(omitted, str(path))
    replaced = f"{preview_text}\n\n{notice}" if preview_text else notice

    # Invariant: never emit a replacement larger than the cap. When the notice
    # alone exceeds the cap (tiny cap or a long spill root), there is no
    # within-cap replacement — keep the inline content. A within-cap
    # replacement is always smaller than the original (which is > cap by the
    # entry condition), so this one check subsumes "not smaller than the
    # original". The already-written spill file is a harmless orphan.
    if _utf8_len(replaced) > cap:
        logger.warning(
            "tool-spill: replacement for %s exceeds max_inline_bytes; keeping inline content",
            tool_name,
        )
        return content

    logger.info(
        "tool-spill: spilled %s result (%d bytes -> %s)",
        tool_name, total_bytes, path,
    )
    return replaced
