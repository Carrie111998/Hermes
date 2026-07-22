"""Automatic handoff packet generation for repeated context compression.

This module deliberately builds on the current SessionDB handoff primitives
instead of rotating agent sessions itself.  A compression threshold can now
produce a redacted markdown handoff packet and, when explicitly configured,
mark the current durable session as pending handoff to a gateway platform.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_HANDOFF_ARTIFACT_DIR = "handoffs"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def configure_auto_handoff_on_compression(agent: Any, agent_config: dict[str, Any]) -> None:
    """Populate agent attributes from ``agent.auto_handoff_on_compression``.

    Bad config is tolerated: this feature must never prevent agent startup or
    normal compression.
    """
    raw = agent_config.get("auto_handoff_on_compression", {}) if isinstance(agent_config, dict) else {}
    if raw is True:
        raw = {"enabled": True}
    if not isinstance(raw, dict):
        raw = {}

    agent._auto_handoff_on_compression_enabled = _truthy(raw.get("enabled", False))
    try:
        agent._auto_handoff_after_compressions = max(
            1, int(raw.get("after_compressions", 2) or 2)
        )
    except (TypeError, ValueError):
        agent._auto_handoff_after_compressions = 2
    try:
        agent._auto_handoff_max_auto_handoffs = max(
            0, int(raw.get("max_auto_handoffs", 1) or 0)
        )
    except (TypeError, ValueError):
        agent._auto_handoff_max_auto_handoffs = 1

    mode = str(raw.get("mode", "packet") or "packet").strip().lower()
    # Legacy aliases from the pre-refactor branch.  ``fresh_session`` is
    # intentionally not replayed: current Hermes has durable SessionDB handoff
    # and in-place compaction primitives, so this refactor avoids ad-hoc child
    # session rotation during compression.
    if mode in {"prompt_user", "artifact", "packet"}:
        mode = "packet"
    elif mode in {"platform", "request_handoff", "handoff"}:
        mode = "platform"
    else:
        logger.warning(
            "Invalid agent.auto_handoff_on_compression.mode=%r — using packet",
            mode,
        )
        mode = "packet"
    agent._auto_handoff_mode = mode
    agent._auto_handoff_platform = str(raw.get("platform", "") or "").strip().lower()
    agent._auto_handoff_artifact_dir = str(
        raw.get("handoff_artifact_dir", DEFAULT_HANDOFF_ARTIFACT_DIR)
        or DEFAULT_HANDOFF_ARTIFACT_DIR
    )
    agent._auto_handoff_count = 0


def _handoff_safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "session").strip("-._")
    return slug[:80] or "session"


def _resolve_handoff_dir(raw_dir: str) -> Path:
    """Resolve a handoff artifact directory under active HERMES_HOME."""
    raw = (raw_dir or DEFAULT_HANDOFF_ARTIFACT_DIR).strip()
    path = Path(raw).expanduser()
    hermes_home = get_hermes_home().expanduser().resolve()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        parts = path.parts
        if parts and parts[0] == ".hermes":
            resolved = hermes_home.joinpath(*parts[1:]).resolve()
        else:
            resolved = (hermes_home / path).resolve()
    try:
        resolved.relative_to(hermes_home)
    except ValueError as exc:
        raise ValueError("handoff_artifact_dir must resolve under HERMES_HOME") from exc
    return resolved


def _truncate(value: Any, limit: int = 4000) -> str:
    text = redact_sensitive_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit:,} chars]"


def _git_snapshot() -> dict[str, str]:
    """Best-effort git metadata for a handoff packet; never raises."""
    cwd = Path.cwd()

    def _run(args: list[str], limit: int = 3000) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        return _truncate(proc.stdout.strip(), limit)

    return {
        "workspace": str(cwd),
        "branch": _run(["branch", "--show-current"], 500),
        "status": _run(["status", "--short"]),
        "recent_commits": _run(["log", "-3", "--oneline"]),
    }


def _todo_lines(agent: Any) -> list[str]:
    store = getattr(agent, "_todo_store", None)
    if store is None or not hasattr(store, "read"):
        return ["- No active todo snapshot available."]
    try:
        items = store.read() or []
    except Exception:
        items = []
    if not items:
        return ["- No active todos recorded."]
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = _truncate(item.get("status", "pending"), 80)
        item_id = _truncate(item.get("id", "?"), 120)
        content = _truncate(item.get("content", ""), 1000)
        lines.append(f"- [{status}] {item_id}: {content}")
    return lines or ["- No active todos recorded."]


def _message_lines(messages: list[dict[str, Any]], *, max_messages: int = 8) -> list[str]:
    if not messages:
        return ["- No compressed context messages available."]
    selected = messages[-max_messages:]
    lines: list[str] = []
    omitted = max(0, len(messages) - len(selected))
    if omitted:
        lines.append(
            f"- Omitted {omitted} older compressed message(s); inspect the prior session transcript if needed."
        )
    for idx, msg in enumerate(selected, start=max(0, len(messages) - len(selected)) + 1):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = _truncate(msg.get("content", ""), 1800).replace("\n", "\n  ")
        lines.append(f"- Message {idx} ({role}):\n  {content}")
    return lines or ["- No compressed context messages available."]


def build_compression_handoff_packet(
    agent: Any,
    compressed_messages: list[dict[str, Any]],
    *,
    approx_tokens: Optional[int] = None,
    trigger_description: str = "repeated context compression crossed the configured handoff threshold",
) -> str:
    """Build a compact, redacted markdown packet for a human or future session."""
    session_id = str(getattr(agent, "session_id", "") or "unknown")
    old_title = ""
    session_db = getattr(agent, "_session_db", None)
    if session_db is not None:
        try:
            old_title = session_db.get_session_title(session_id) or ""
        except Exception:
            old_title = ""
    git = _git_snapshot()
    compression_count = getattr(getattr(agent, "context_compressor", None), "compression_count", 0)
    lines = [
        "# Hermes compression handoff packet",
        "",
        f"Generated because {trigger_description}.",
        "Use this packet as context only; verify live repo/system state before editing or taking external actions.",
        "",
        "## Session",
        f"- Session ID: `{_truncate(session_id, 200)}`",
        f"- Title: {_truncate(old_title or '(untitled)', 500)}",
        f"- Platform: `{_truncate(getattr(agent, 'platform', '') or 'cli', 120)}`",
        f"- Model/provider: `{_truncate(getattr(agent, 'model', '') or '?', 200)}` / `{_truncate(getattr(agent, 'provider', '') or '?', 120)}`",
        f"- Compression count: {compression_count}",
        f"- Approx tokens before compression: {approx_tokens:,}" if approx_tokens else "- Approx tokens before compression: unknown",
        "",
        "## Workspace snapshot",
        f"- Path: `{_truncate(git['workspace'], 1000)}`",
        f"- Branch: `{_truncate(git['branch'] or '(unknown/non-git)', 500)}`",
        "- `git status --short`:",
        "```text",
        _truncate(git["status"] or "(clean or unavailable)", 3000),
        "```",
        "- Recent commits:",
        "```text",
        _truncate(git["recent_commits"] or "(unavailable)", 3000),
        "```",
        "",
        "## Active todos",
        *_todo_lines(agent),
        "",
        "## Compressed context tail",
        *_message_lines(compressed_messages),
        "",
        "## Recommended restart discipline",
        "- Verify `git status --short` and inspect referenced files before editing.",
        "- Continue the in-progress todo first; do not repeat completed work unless verification shows it is missing.",
        "- Ask before destructive actions, protected/default branch writes, or externally visible changes unless already approved in the live session.",
    ]
    return redact_sensitive_text("\n".join(lines).rstrip() + "\n")


def write_compression_handoff_packet(agent: Any, packet: str) -> Optional[Path]:
    try:
        directory = _resolve_handoff_dir(getattr(agent, "_auto_handoff_artifact_dir", DEFAULT_HANDOFF_ARTIFACT_DIR))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = (
            f"{_handoff_safe_slug(getattr(agent, 'session_id', '') or 'session')}"
            f"-{timestamp}-handoff.md"
        )
        path = directory / filename
        # Refuse to overwrite an existing path and create the packet private
        # from the first byte. This avoids timestamp-collision clobbering and a
        # world-readable intermediate file on permissive umasks.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(redact_sensitive_text(packet))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path
    except Exception as exc:
        logger.warning("Could not write compression handoff packet: %s", exc)
        return None


def _threshold_reached(agent: Any) -> bool:
    if not getattr(agent, "_auto_handoff_on_compression_enabled", False):
        return False
    try:
        max_handoffs = int(getattr(agent, "_auto_handoff_max_auto_handoffs", 1) or 0)
    except (TypeError, ValueError):
        max_handoffs = 1
    if max_handoffs <= 0:
        return False
    try:
        used = int(getattr(agent, "_auto_handoff_count", 0) or 0)
    except (TypeError, ValueError):
        used = 0
    if used >= max_handoffs:
        return False
    try:
        after = max(1, int(getattr(agent, "_auto_handoff_after_compressions", 2) or 2))
    except (TypeError, ValueError):
        after = 2
    try:
        count = int(getattr(getattr(agent, "context_compressor", None), "compression_count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    return count >= after


def maybe_trigger_compression_handoff(
    agent: Any,
    compressed_messages: list[dict[str, Any]],
    *,
    approx_tokens: Optional[int] = None,
) -> Optional[Path]:
    """Create a handoff artifact and optionally request platform handoff.

    Returns the artifact path when a packet was written; returns ``None`` when
    disabled, below threshold, bounded out, or write failed.  This function is
    fail-open: compression continues even if handoff setup fails.
    """
    if not _threshold_reached(agent):
        return None

    agent._auto_handoff_count = int(getattr(agent, "_auto_handoff_count", 0) or 0) + 1
    packet = build_compression_handoff_packet(
        agent,
        compressed_messages,
        approx_tokens=approx_tokens,
    )
    artifact_path = write_compression_handoff_packet(agent, packet)

    mode = str(getattr(agent, "_auto_handoff_mode", "packet") or "packet").lower()
    platform = str(getattr(agent, "_auto_handoff_platform", "") or "").strip().lower()
    requested = False
    if mode == "platform" and platform:
        session_db = getattr(agent, "_session_db", None)
        request_handoff = getattr(session_db, "request_handoff", None)
        if callable(request_handoff):
            try:
                requested = bool(request_handoff(getattr(agent, "session_id", ""), platform))
            except Exception as exc:
                logger.warning("Compression handoff request failed: %s", exc)
        if not requested:
            try:
                agent._emit_warning(
                    f"⚠ Compression handoff packet written, but automatic handoff to {platform} could not be queued."
                )
            except Exception:
                pass

    try:
        suffix = f" Handoff to {platform} queued." if requested else ""
        location = f" Packet: {artifact_path}" if artifact_path else " Packet write failed."
        agent._emit_status(
            "⚠ Repeated compression reached the auto-handoff threshold."
            f"{location}{suffix}"
        )
    except Exception:
        pass
    return artifact_path
