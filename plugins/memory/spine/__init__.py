"""Spine — Hermes Memory Provider v2 (plugins/memory/spine).

Implements the MemoryProvider ABC with canonical JSONL logs, a derived
SQLite FTS5+vec index, and four agent tools (remember, recall, reflect, forget).
Three loops (observer, consolidation, activation manifest) run via plugin hooks.

Spec: memory-system-v2.1.0-spec.md
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

__version__ = "2.1.0"


def _send_secrets_alert(target: str, text: str) -> None:
    """Best-effort real Telegram send for a secrets-block alert.

    Previously this path only did `print(...)`, which looked like a real
    notification but just wrote to whatever process's stdout happened to be
    calling this hook — the user would never actually see it. `target` is
    "telegram:<chat_id>[:<thread_id>]".

    This hook fires synchronously from inside the live agent's call chain,
    which may already be running inside an event loop — schedule on it if so,
    otherwise run one directly.
    """
    parts = target.split(":")
    if len(parts) < 2 or parts[0] != "telegram":
        logger.warning("Unrecognized secrets-alert target %r — not sent", target)
        return
    chat_id, thread_id = parts[1], (parts[2] if len(parts) > 2 else None)

    import hermes_cli.gateway as gateway_mod
    from tools.send_message_tool import _send_telegram

    token = gateway_mod.get_env_value("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        logger.warning("No TELEGRAM_BOT_TOKEN available — secrets alert not sent")
        return

    async def _send():
        await _send_telegram(token, chat_id, text, thread_id=thread_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_send())
    else:
        asyncio.run(_send())


class SpineProvider(MemoryProvider):
    """Hermes Memory v2 — "The Sleeping Brain" provider."""

    # ------------------------------------------------------------------
    # MemoryProvider ABC
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "spine"

    def is_available(self) -> bool:
        """Spine is local-only — always available if config is present."""
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Read config, open index, warm embedder if needed."""
        from .config import load_spine_config

        self._session_id = session_id
        self._config = load_spine_config(kwargs.get("hermes_home", ""))
        logger.info("Spine initialized — session=%s", session_id)

    def system_prompt_block(self) -> str:
        """Spine contributes no static system prompt text."""
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return the spine tool schemas."""
        from .tools import REMEMBER_SCHEMA, RECALL_SCHEMA, RECALL_AT_SCHEMA, REFLECT_SCHEMA, FORGET_SCHEMA, EXPLAIN_SCHEMA

        return [REMEMBER_SCHEMA, RECALL_SCHEMA, RECALL_AT_SCHEMA, REFLECT_SCHEMA, FORGET_SCHEMA, EXPLAIN_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch tool call to the appropriate handler."""
        from .tools import handle_remember, handle_recall, handle_recall_at, handle_reflect, handle_forget, handle_explain

        handlers = {
            "remember": handle_remember,
            "recall": handle_recall,
            "recall_at": handle_recall_at,
            "reflect": handle_reflect,
            "forget": handle_forget,
            "explain": handle_explain,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return '{"error": "Unknown tool: ' + tool_name + '"}'
        return handler(args, config=self._config)

    def shutdown(self) -> None:
        """Unload embedder, close index."""
        from .embedder import unload_embedder

        unload_embedder()
        logger.info("Spine shutdown complete.")

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Inject activation manifest on first turn (spec §5.3, HANDOFF-REVIEW P1-4).

        Uses the MemoryProvider ABC on_turn_start hook (confirmed present).
        Injects exactly once per session via seen-set; re-injects after reset.
        """
        if not hasattr(self, "_manifest_shown"):
            self._manifest_shown = False

        if not self._manifest_shown:
            from .loops import build_activation_manifest
            self._last_manifest = build_activation_manifest(self._config)
            self._manifest_shown = True
            # Inject via system prompt block for this turn
            manifest = self._last_manifest
            self._manifest_text = (
                f"\n[ACTIVATION MANIFEST — session start]\n"
                f"Profile: {manifest.get('active_profile', 'default')}\n"
                f"Skills: {', '.join(manifest.get('relevant_skills', []))}\n"
                f"**Cross-topic reminder:** you have cross-topic context access across this conversation.\n"
                f"Rules: {manifest.get('rule_checklist', '')}\n"
                f"Recent memory: {manifest.get('recalled_top_6', 'No observations yet.')}\n"
            )
            logger.info("Activation manifest injected (turn %d)", turn_number)

    def on_session_reset(self, **kwargs) -> None:
        """Clear tracking state on /reset — manifest re-shown next session."""
        self._manifest_shown = False
        self._last_manifest = None
        self._manifest_text = None

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Observer pass — extract durable observations (spec §5.1)."""
        from .loops import run_observer

        run_observer(messages, config=self._config)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Post-hoc secrets scan on builtin memory writes (HANDOFF-REVIEW P1-6).

        Builtin memory() bypasses spine's remember() gate. This hook scans
        every builtin write and strips secrets from MEMORY.md if found.
        Not ideal (secret lands briefly), but the safest plug-compatible fix.
        """
        from .secrets_detector import detect_secrets
        from .config import SpineConfig

        result = detect_secrets(content)
        if result.get("blocked"):
            logger.warning("Builtin memory write blocked by secrets detector: %s", result.get("reason"))
            # Strip the line from MEMORY.md — match the line's actual content
            # exactly (lstrip("-*") only matters for bullet-style entries;
            # verified 2026-07-30 that real MEMORY.md is actually §-separated
            # free-text paragraphs, not bullets — harmless no-op against that
            # format, kept for any bullet-style entries that do occur), not
            # "any line containing this as a substring anywhere" — the old
            # substring check could delete unrelated lines that merely
            # happened to contain the same short fragment (e.g. a recurring
            # URL) elsewhere in the file.
            import os as _os
            mem_path = _os.path.expanduser("~/.hermes/memories/MEMORY.md")
            if _os.path.exists(mem_path):
                with open(mem_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                target_content = content.strip()
                cleaned = [
                    l for l in lines
                    if l.strip().lstrip("-*").strip() != target_content
                ]
                with open(mem_path, "w", encoding="utf-8") as f:
                    f.writelines(cleaned)
                logger.info("Stripped secret-bearing content from MEMORY.md")
            # Alert — actually send it, don't just log locally
            from .config import load_spine_config
            target_ch = "telegram:-1004389869012:11"
            try:
                cfg = load_spine_config()
                target_ch = getattr(cfg, "report_target", target_ch)
                _send_secrets_alert(
                    target_ch,
                    f"🔒 Secrets alert: builtin memory write blocked — {result.get('reason')}",
                )
            except Exception:
                logger.warning("Failed to send secrets alert to %s", target_ch)
