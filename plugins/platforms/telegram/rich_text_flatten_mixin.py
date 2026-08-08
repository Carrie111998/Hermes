"""Rich-message plaintext flattening methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c11 cluster: best-effort plaintext extraction from Bot API rich-message reply payloads (inline nodes and blocks), used to reconstruct reply text the Telegram server did not echo as plain text.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (none) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``RichTextFlattenMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

from typing import Any, List, Optional

class RichTextFlattenMixin:
    """Rich-message plaintext flattening cluster lifted verbatim from ``TelegramAdapter``."""

    @classmethod
    def _flatten_rich_inline_text(cls, value: Any) -> str:
        """Best-effort plaintext flattener for Bot API rich-message inline nodes."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(cls._flatten_rich_inline_text(item) for item in value)
        if isinstance(value, dict):
            text = value.get("text")
            if text is not None:
                return cls._flatten_rich_inline_text(text)
            children = value.get("children")
            if children is not None:
                return cls._flatten_rich_inline_text(children)
        return ""

    @classmethod
    def _flatten_rich_blocks(cls, blocks: Any) -> str:
        """Best-effort plaintext flattener for Bot API rich-message blocks."""
        if not isinstance(blocks, list):
            return ""

        lines: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")
            if block_type == "list":
                for item in block.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    item_text = cls._flatten_rich_blocks(item.get("blocks"))
                    if not item_text:
                        continue
                    label = item.get("label")
                    item_lines = item_text.splitlines()
                    if not item_lines:
                        continue
                    first_line = item_lines[0]
                    if label:
                        first_line = f"{label} {first_line}".strip()
                    lines.append(first_line)
                    lines.extend(item_lines[1:])
                continue

            text = cls._flatten_rich_inline_text(block.get("text"))
            if text:
                lines.extend(text.splitlines())

        return "\n".join(line.rstrip() for line in lines if line)

    @classmethod
    def _extract_rich_reply_text(cls, reply_to_message: Any) -> Optional[str]:
        """Return plaintext echoed by Telegram's rich_message reply payload."""
        try:
            api_kwargs = getattr(reply_to_message, "api_kwargs", None)
            getter = getattr(api_kwargs, "get", None)
            if not callable(getter):
                return None
            rich_message = getter("rich_message")
            rich_getter = getattr(rich_message, "get", None)
            if not callable(rich_getter):
                return None
            text = cls._flatten_rich_blocks(rich_getter("blocks")).strip()
            return text or None
        except Exception:
            return None
