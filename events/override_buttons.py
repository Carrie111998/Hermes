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

# Longest callback_data prefix is "rl:dismiss:" (11 bytes); Telegram caps
# callback_data at 64 bytes TOTAL. 48 leaves that prefix 5 bytes of slack
# while comfortably clearing a normal uuid4 event_id (36 chars) untouched --
# only a pathological event_id (see the token comment below) is ever
# actually truncated.
#
# The bound is in BYTES, not characters. A character slice looks equivalent
# and is not: an event_id of multi-byte characters ("🎉" * 5000, which
# Event.from_dict will happily accept from a stored row) survives a 48-CHAR
# slice as 192 bytes, producing ~203-byte callback_data. Telegram rejects
# that with a BadRequest, which re-raises out of _send_telegram and drops the
# ENTIRE ALERT -- the exact failure this bound exists to prevent, reintroduced
# by the more natural-looking slice. Verified during review by reproduction.
_MAX_TOKEN_BYTES = 48


def buttons_for(event: Any) -> Optional[List[List[dict]]]:
    """Build the inline-button spec for a MODEL_RATE_LIMITED event.

    Returns ``None`` when no buttons apply (unroutable detector, or an
    outcome that doesn't warrant a control), otherwise a list of button
    rows suitable for ``InlineKeyboardMarkup`` construction downstream.
    """
    payload = getattr(event, "payload", None) or {}

    # Normalized the same way as the other two payload["detector"]/["outcome"]
    # consumers (events.routing_policy:463, whatsapp_escalator.py:417) so all
    # three can never disagree if a producer ever varies case.
    detector = (payload.get("detector") or "").strip().lower()
    if detector != "runtime":
        return None

    outcome = (payload.get("outcome") or "").strip().lower()
    if outcome not in _BUTTON_OUTCOMES:
        return None

    # Bounded so an over-long event_id (Event.from_dict accepts whatever id
    # a row carries; only Event.create's uuid4 is guaranteed short) can't
    # push callback_data past Telegram's 64-byte cap. InlineKeyboardButton
    # would construct fine on an oversized token and fail later as a
    # BadRequest at send_message -- which re-raises and drops the entire
    # alert, not just the buttons. A truncated-but-present token still
    # round-trips through the (later) callback handler's lookup; if it
    # doesn't resolve, that tap just no-ops -- strictly better than losing
    # the message.
    token = getattr(event, "event_id", None) or "unknown"
    # Byte-safe truncation: slice the UTF-8 encoding, then decode with
    # errors="ignore" so a cut landing mid-codepoint drops that partial
    # character rather than raising. Never slice the str directly -- see
    # _MAX_TOKEN_BYTES above for why that silently fails to bound anything.
    token = str(token).encode("utf-8")[:_MAX_TOKEN_BYTES].decode("utf-8", "ignore")

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
