"""Shared interactive-prompt formatting cores for platform adapters.

Extracted verbatim from ``gateway/platforms/base.py`` (godfile decomposition
wave 1, shard s3, cluster c13: ``_truncate_preview``, ``_ea_escape``, ``_format_exec_approval``, ``_format_choice_page``).  The mixin is a base of
``BasePlatformAdapter``; the ``_EA_*`` template class attrs these methods read stay on
``BasePlatformAdapter`` (adapters override them) and resolve via MRO.
"""

from __future__ import annotations

from typing import Any, Dict


class PromptFormattingMixin:
    @staticmethod
    def _truncate_preview(text: str, budget: int, suffix: str = "...") -> str:
        """Truncate ``text`` to ``budget`` chars, appending ``suffix`` when cut.

        The shared ``x[:budget] + "..." if len(x) > budget else x`` idiom used
        by every adapter's approval/confirm preview construction.
        """
        text = str(text or "")
        return text[:budget] + suffix if len(text) > budget else text

    def _ea_escape(self, text: str) -> str:
        """Escape hook applied to the command preview and reason text.

        Default is pass-through; HTML-mode platforms (Telegram) override.
        """
        return text

    def _format_exec_approval(
        self,
        command: str,
        description: str = "dangerous command",
        smart_denied: bool = False,
    ) -> str:
        """Shared formatting core for exec-approval prompt text.

        Assembles ``_EA_HEADER`` + fenced command preview (truncated to
        ``_EA_CMD_BUDGET``) + ``_EA_REASON_LABEL`` + description, plus
        ``_EA_SMART_DENY_LINE`` when ``smart_denied``. Button construction
        stays platform-local; adapters with additional trailing instructions
        (e.g. reaction legends) append them to this core.
        """
        cmd_preview = self._truncate_preview(str(command or ""), self._EA_CMD_BUDGET)
        text = (
            f"{self._EA_HEADER}"
            f"{self._EA_CODE_OPEN}{self._ea_escape(cmd_preview)}{self._EA_CODE_CLOSE}"
            f"{self._EA_REASON_LABEL}{self._ea_escape(description)}"
        )
        if smart_denied:
            text += self._EA_SMART_DENY_LINE
        return text

    @staticmethod
    def _format_choice_page(
        options: list,
        page: int,
        per_page: int,
    ) -> "tuple[list, Dict[str, Any]]":
        """Shared pagination core for picker keyboards/menus.

        Clamps ``page`` into range, slices ``options`` for that page and
        returns ``(page_options, meta)`` where ``meta`` carries ``page``,
        ``total_pages``, ``start``, ``end``, ``total`` and ``page_info`` —
        the `` (N–M of T)`` suffix text (empty when everything fits on one
        page). Option/button rendering stays platform-local.
        """
        total = len(options)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        start = page * per_page
        end = min(start + per_page, total)
        page_info = f" ({start + 1}–{end} of {total})" if total_pages > 1 else ""
        meta: Dict[str, Any] = {
            "page": page,
            "total_pages": total_pages,
            "start": start,
            "end": end,
            "total": total,
            "page_info": page_info,
        }
        return options[start:end], meta

