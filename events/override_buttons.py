"""Inline-button spec builder for model-rate-limit reroute notifications.

Returns a serializable button spec -- a list of rows of
``{"label": str, "callback_data": str}`` dicts -- rather than a
``telegram.InlineKeyboardMarkup``. That keeps this module importable and
unit-testable without python-telegram-bot installed; conversion to the real
Telegram type happens in exactly one place,
``tools.send_message_tool._send_telegram`` (the one place in the send path
that already imports telegram types).

Design rulings (2026-08-14 -- encode them here, do not re-derive):

- Buttons ONLY when ``payload["detector"] == "runtime"``. The
  credential-pool detector keys episodes on ``"{provider}:pool"`` and the
  Nous detector on ``"nous/nous-portal"`` -- neither is a routable model
  slug, so an override written from a tap on one of those alerts could
  never match anything. A control that looks real but does nothing is
  worse than no control.
- ``outcome == "diverted"`` -> three buttons: one-tap divert naming the
  fallback that absorbed the traffic, "Choose model...", "Dismiss".
- ``outcome in {"chain_exhausted", "no_fallback"}`` -> "Choose model..." and
  "Dismiss" ONLY. No one-tap: by definition every configured fallback is
  already rate-limited, so any suggestion would divert into another dead
  model.
- ``outcome == "recovered"`` -> no buttons.
- ``callback_data`` is ``rl:<action>:<token>`` -- an OPAQUE token only,
  never the model name. Telegram caps callback_data at 64 bytes, and a tap
  must not be able to name an arbitrary model; the token is resolved
  server-side by the callback handler (a later task) rather than carrying
  the model slug itself.
"""

from __future__ import annotations

from typing import Any, List, Optional

# outcomes that ever produce buttons when the detector is routable.
_BUTTON_OUTCOMES = frozenset({"diverted", "chain_exhausted", "no_fallback"})


def buttons_for(event: Any) -> Optional[List[List[dict]]]:
    """Build the inline-button spec for a MODEL_RATE_LIMITED event.

    Returns ``None`` when no buttons apply (unroutable detector, or an
    outcome that doesn't warrant a control), otherwise a list of button
    rows suitable for ``InlineKeyboardMarkup`` construction downstream.
    """
    payload = getattr(event, "payload", None) or {}

    if payload.get("detector") != "runtime":
        return None

    outcome = payload.get("outcome")
    if outcome not in _BUTTON_OUTCOMES:
        return None

    token = getattr(event, "event_id", None) or "unknown"

    choose = {"label": "Choose model…", "callback_data": f"rl:choose:{token}"}
    dismiss = {"label": "Dismiss", "callback_data": f"rl:dismiss:{token}"}

    if outcome == "diverted":
        fallback_model = payload.get("fallback_model") or "fallback"
        divert = {
            "label": f"Divert 6h → {fallback_model}",
            "callback_data": f"rl:divert:{token}",
        }
        return [[divert, choose, dismiss]]

    # chain_exhausted / no_fallback: no one-tap, every fallback is dead too.
    return [[choose, dismiss]]
