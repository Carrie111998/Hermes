"""Typed, privacy-bounded terminal carriers for webhook agent runs."""

from __future__ import annotations

from enum import Enum
from typing import Any


class WebhookTerminalOutcome(str, Enum):
    """Terminal agent outcomes that may become durable outbound effects."""

    ERROR = "error"


_TERMINAL_NOTICES = {
    WebhookTerminalOutcome.ERROR: (
        "Webhook processing failed before a final response was produced."
    ),
}


def terminal_outcome_notice(outcome: WebhookTerminalOutcome) -> str:
    """Return stable user text without provider errors, payloads, or secrets."""

    if not isinstance(outcome, WebhookTerminalOutcome):
        raise TypeError("webhook terminal outcome must be typed")
    return _TERMINAL_NOTICES[outcome]


def terminal_outcome_carrier(outcome: WebhookTerminalOutcome) -> dict[str, Any]:
    """Return the exact JSON carrier persisted beside the terminal notice."""

    if not isinstance(outcome, WebhookTerminalOutcome):
        raise TypeError("webhook terminal outcome must be typed")
    return {
        "v": 1,
        "kind": "terminal_outcome",
        "outcome": outcome.value,
    }
