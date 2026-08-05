"""TodoStore hydration from conversation history for ``AIAgent``

Extracted from ``run_agent.py`` as part of the god-file decomposition
campaign (Phase 3 mechanical mixin lifts).  Behavior-neutral: every method
is lifted verbatim from ``AIAgent``; ``self.*``/``cls.*`` calls resolve
unchanged via the MRO (class attributes referenced through ``cls.`` stay on
``AIAgent``).  The module-level ``logger`` keeps the original logger name
(``"run_agent"``) so log records are unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger("run_agent")


class TodoHydrationMixin:
    def _hydrate_todo_store(self, history: List[Dict[str, Any]]) -> None:
        """
        Recover todo state from conversation history.
        
        The gateway creates a fresh AIAgent per message, so the in-memory
        TodoStore is empty. We scan the history for the most recent todo
        tool response and replay it to reconstruct the state.

        Hydration is restricted to tool results that are paired with an
        earlier assistant ``todo`` tool call. The gateway/API server accepts
        caller-supplied ``conversation_history``, so a forged bare
        ``role: tool`` message carrying a ``todos`` array must not be able to
        seed the store without a matching canonical tool call
        (GHSA-5g4g-6jrg-mw3g).
        """
        from tools.todo_tool import MAX_TODO_RESULT_CHARS
        from run_agent import _set_interrupt  # lazy: patched by tests via run_agent namespace

        # Walk history backwards to find the most recent todo tool response
        last_todo_response = None
        for idx in range(len(history) - 1, -1, -1):
            msg = history[idx]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            # Only accept tool results paired with a prior assistant todo call.
            if not self._tool_response_matches_todo_call(history, idx):
                continue
            if len(content) > MAX_TODO_RESULT_CHARS:
                logger.warning(
                    "Skipping oversized todo tool response during hydration: "
                    "session=%s chars=%d",
                    self.session_id or "none",
                    len(content),
                )
                continue
            # Quick check: todo responses contain "todos" key
            if '"todos"' not in content:
                continue
            try:
                data = json.loads(content)
                if "todos" in data and isinstance(data["todos"], list):
                    last_todo_response = data["todos"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if last_todo_response:
            # Replay the items into the store (replace mode)
            self._todo_store.write(last_todo_response, merge=False)
            if not self.quiet_mode:
                self._vprint(f"{self.log_prefix}📋 Restored {len(last_todo_response)} todo item(s) from history")
        _set_interrupt(False)

    @classmethod
    def _tool_response_matches_todo_call(
        cls,
        history: List[Dict[str, Any]],
        tool_index: int,
    ) -> bool:
        """Return True when a tool result belongs to a prior assistant todo call.

        Scans backwards from the tool result to the nearest assistant message
        and confirms it issued a ``todo`` tool call whose id matches this
        result's ``tool_call_id``. A ``user``/``system`` boundary (or a missing
        id) means the result is unpaired and must not hydrate the store.
        """
        if tool_index < 0 or tool_index >= len(history):
            return False
        tool_msg = history[tool_index]
        tool_call_id = tool_msg.get("tool_call_id")
        if not tool_call_id:
            return False

        for prior_idx in range(tool_index - 1, -1, -1):
            prior = history[prior_idx]
            role = prior.get("role")
            if role == "assistant":
                return cls._assistant_has_todo_tool_call(prior, tool_call_id)
            if role in {"user", "system"}:
                return False
        return False

    @classmethod
    def _assistant_has_todo_tool_call(
        cls,
        assistant_msg: Dict[str, Any],
        tool_call_id: str,
    ) -> bool:
        """True when the assistant message issued a ``todo`` call with this id."""
        tool_calls = assistant_msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return False

        for tool_call in tool_calls:
            if cls._get_tool_call_id_static(tool_call) != tool_call_id:
                continue
            if cls._get_tool_call_name_static(tool_call) == "todo":
                return True
        return False

