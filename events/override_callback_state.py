"""In-memory token -> override-target map for Telegram rate-limit reroute buttons.

``events/override_buttons.py::buttons_for()`` builds an OPAQUE callback
token (the event's ``event_id``, byte-truncated -- see that module's
docstring) precisely so a Telegram tap can never name an arbitrary model
through ``callback_data``. Something has to remember what that token
actually points at -- this module is that "something".

Why in-memory, not file-backed (unlike ``events/model_override.py`` and
``events/rate_limit_signal.py``): the buttons are only ever meaningful to
the same long-lived gateway process that sent them out -- a fresh cron
process never receives a Telegram callback -- so there is no cross-process
coherence problem to solve here. This mirrors
``plugins/platforms/telegram/adapter.py``'s own ``_slash_confirm_state`` /
``_approval_state`` / ``_clarify_state``, which are plain in-process dicts
for the identical reason.

Populated by ``events/subscribers/telegram_notifier.py`` right after it
calls ``buttons_for()`` — that module is deliberately pure/stateless (its
own docstring says so, and it needs to stay unit-testable without
python-telegram-bot installed), so it does not record anything itself.
The notifier is the one place downstream of it that has both the token
(embedded identically in every button on the same alert -- see
``buttons_for``) AND the event payload (provider/model/fallback_provider/
fallback_model) needed to populate this map.

Consumed by ``plugins/platforms/telegram/adapter.py``'s
``_handle_callback_query`` (the ``rl:`` branch), which pops an entry the
first time its token is used -- popping BEFORE acting is what makes a
double-tap idempotent, and what guarantees the model that gets diverted is
always resolved from OUR OWN state, never from anything a tap could put on
the wire.

No TTL / expiry sweep here: entries are removed by ``pop()`` on first use.
A never-tapped entry is a few hundred bytes and is bounded in practice by
how often MODEL_RATE_LIMITED alerts fire -- not worth a background reaper
for this phase.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

# How long a recorded token stays actionable.
#
# Without a bound, a never-tapped alert stays fully tappable for as long as the
# gateway lives, and a tap on a days-old alert writes a LIVE 6h override for an
# episode that resolved long ago. `set_override` re-checks whether the TARGET is
# currently limited, but nothing re-checks whether the ORIGINAL still is -- so a
# stale tap is a silent, real reroute of healthy traffic.
#
# 12h comfortably outlives a genuine outage the operator might act on after a
# night's sleep, while ensuring a forgotten button cannot act next week.
# adapter.py::_notify_clarify_expired is the in-repo precedent for expiring a
# stale prompt.
_TOKEN_TTL_SECONDS = 12 * 60 * 60

# token -> {"provider", "model", "replacement_provider", "replacement_model",
#           "recorded_at"}
_state: Dict[str, Dict[str, str]] = {}


def record(
    token: str,
    *,
    provider: str,
    model: str,
    replacement_provider: str,
    replacement_model: str,
) -> None:
    """Remember what a callback token refers to.

    Called once, when the buttons are sent. This does overwrite any prior
    entry for the same token, but that branch is not something a re-alert
    on the same rate-limit episode ever exercises: ``buttons_for()``
    derives the token from ``event.event_id``, and ``Event.create`` assigns
    a fresh ``uuid4`` per event, so every MODEL_RATE_LIMITED alert --
    including a re-alert on the same episode -- gets its own unique token
    and its own entry here. Two records would only ever collide on the
    same token if something reused an event_id, which nothing does today;
    the overwrite is a harmless default for that hypothetical, not a
    mechanism anything currently relies on.
    """
    _reap_expired()
    _state[str(token)] = {
        "provider": provider or "",
        "model": model or "",
        "replacement_provider": replacement_provider or "",
        "replacement_model": replacement_model or "",
        "recorded_at": str(time.monotonic()),
    }


def pop(token: str) -> Optional[Dict[str, str]]:
    """Consume and return the entry for ``token``.

    Returns ``None`` for a token that was never recorded OR was already
    consumed by a prior tap -- the two cases are indistinguishable on
    purpose, and both must resolve to "already resolved, do nothing" at
    the call site.

    A token older than ``_TOKEN_TTL_SECONDS`` is treated as never-recorded:
    acting on a stale alert would write a live override for an episode that
    has almost certainly resolved.
    """
    _reap_expired()
    entry = _state.pop(str(token), None)
    if entry is None:
        return None
    if _is_expired(entry):
        return None
    return entry


def _is_expired(entry: Dict[str, str]) -> bool:
    """Whether a recorded entry has aged past the TTL.

    An unparseable or missing ``recorded_at`` counts as EXPIRED rather than
    fresh: a token we cannot date is one we cannot vouch for, and refusing it
    costs one re-tap while honouring it could reroute live traffic.
    """
    try:
        recorded_at = float(entry.get("recorded_at", ""))
    except (TypeError, ValueError):
        return True
    return (time.monotonic() - recorded_at) >= _TOKEN_TTL_SECONDS


def _reap_expired() -> None:
    """Drop aged entries so the map cannot grow for the gateway's lifetime."""
    for token in [t for t, e in _state.items() if _is_expired(e)]:
        _state.pop(token, None)


def reset() -> None:
    """Test hook: drop all entries."""
    _state.clear()
