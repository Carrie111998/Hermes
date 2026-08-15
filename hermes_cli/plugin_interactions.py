"""Generic plugin interaction helpers for rich command replies and callbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "PluginCallbackResult",
    "PluginInlineButton",
    "PluginInteractionReply",
    "RESERVED_TELEGRAM_CALLBACK_PREFIXES",
    "coerce_plugin_command_text",
    "is_reserved_telegram_callback",
    "plugin_interaction_send_metadata",
    "validate_callback_data",
]

# Core Telegram inline callbacks — plugins must never register or intercept these.
RESERVED_TELEGRAM_CALLBACK_PREFIXES = (
    "mp:",
    "mpg:",
    "mpv:",
    "mm:",
    "mc:",
    "mb",
    "mx:",
    "mg:",
    "cp:",
    "gt:",
    "ea:",
    "sc:",
    "cl:",
    "update_prompt:",
)


def is_reserved_telegram_callback(data: str) -> bool:
    value = (data or "").strip()
    return any(
        value.startswith(prefix) or (prefix.endswith(":") and value == prefix[:-1])
        for prefix in RESERVED_TELEGRAM_CALLBACK_PREFIXES
    )


@dataclass(frozen=True)
class PluginInlineButton:
    label: str
    callback_data: str


@dataclass(frozen=True)
class PluginInteractionReply:
    text: str
    buttons: tuple[tuple[PluginInlineButton, ...], ...] = ()
    parse_mode: str = "plain"


@dataclass(frozen=True)
class PluginCallbackResult:
    answer_text: str = ""
    delete_message: bool = False
    edit_text: str | None = None


def validate_callback_data(data: str, *, max_len: int = 64) -> str:
    value = (data or "").strip()
    if not value:
        raise ValueError("callback_data must not be empty")
    if len(value.encode("utf-8")) > max_len:
        raise ValueError(f"callback_data exceeds {max_len} bytes")
    return value


def coerce_plugin_command_text(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, PluginInteractionReply):
        return result.text
    return str(result)


def plugin_interaction_send_metadata(result: Any) -> Mapping[str, Any]:
    if not isinstance(result, PluginInteractionReply):
        return {}
    metadata: dict[str, Any] = {}
    if result.parse_mode and result.parse_mode != "plain":
        metadata["plugin_parse_mode"] = result.parse_mode
    if result.buttons:
        metadata["plugin_inline_keyboard"] = [
            [{"text": button.label, "callback_data": button.callback_data} for button in row]
            for row in result.buttons
        ]
    return metadata


TelegramCallbackHandler = Callable[..., Any]
