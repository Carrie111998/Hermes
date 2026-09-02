"""Per-turn routing override: actuate a plugin's routing decision.

The ``pre_llm_call`` plugin hook can inject context into a turn, but it cannot
change *which model answers it* — by the time the hook fires, the turn's model
and client are already fixed (``restore_primary_runtime`` has run at the top of
the loop). A plugin can therefore decide "this turn is cheap" and have no way to
act on it.

This module is the small interface extension that closes that gap. A
``pre_llm_call`` hook result may carry a ``route`` key::

    {"context": "...optional injected text...",
     "route": {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"}}

``model`` is required. ``provider`` and the optional ``base_url`` / ``api_key`` /
``api_mode`` fields are forwarded to the agent's existing ``switch_model``.
``turn_context`` extracts the first well-formed override and applies it right
after the hook fires, before the turn's first API call is assembled.

Nothing here decides *when* to route. That policy belongs entirely to a plugin;
core only provides the actuation path.

── Turn scoping (the load-bearing detail) ──
A proactive route is a PER-TURN decision, exactly like reactive fallback — it
must not pin the session to the routed model. We reuse the agent's existing,
tested ``switch_model`` (atomic client rebuild with rollback), which persists the
swap into ``_primary_runtime``. We then scope it with a DEDICATED flag rather
than by overloading reactive-fallback state:

  1. Stash the pre-swap ``_primary_runtime`` in ``_routing_override_saved_primary``.
  2. Set ``_routing_override_active = True``.
  3. Leave ``_primary_runtime`` pointing at the ROUTED runtime for the turn, and
     do not touch ``_fallback_activated``.

``restore_primary_runtime`` reverts the swap at the top of the next turn, in a
dedicated block that runs BEFORE the ``_fallback_activated`` and rate-limit
cooldown gates, so a routed turn always reverts regardless of cooldown state.

The dedicated flag is not incidental — reusing the reactive-fallback fields for
scoping produced three distinct defects during review:

  - Scoping via ``_rate_limited_until`` / ``_fallback_activated`` let a routed
    turn LEAK past its turn whenever a cooldown gate skipped restoration.
  - Pointing ``_primary_runtime`` at the pre-route model made in-turn transient
    recovery rebuild the *pre-route* client mid-routed-turn, jumping tiers.
  - Pre-arming ``_fallback_activated`` corrupted reactive cooldown accounting, so
    a 429 from the routed model armed no cooldown.

Keeping ``_primary_runtime`` == the routed runtime and using a dedicated flag
fixes all three: reactive fallback continues to work normally UNDERNEATH a routed
turn, treating the routed runtime as its "primary".

── Known limitation: the system prompt's identity line ──
The cached system prompt (including its ``Model:`` / ``Provider:`` identity
lines) is assembled earlier in ``build_turn_context`` than the ``pre_llm_call``
hook fires, and the turn's effective prompt is captured into a local before the
hook runs. A routed turn therefore ships the *pre-route* identity line, so the
routed model will misreport which model it is if asked during that turn. Reactive
failover solves the equivalent problem with ``rewrite_prompt_model_identity`` plus
``_sync_failover_system_message``, which run inside the call-block retry loop.
Wiring the same reconciliation into the proactive path means reaching into the
turn's prompt/compression bookkeeping, which is deliberately out of scope for this
interface addition. Everything else about the turn — client, credentials, endpoint,
wire format, reasoning config — is the routed model's.

── Fail-safe ──
Any error (malformed override, ``switch_model`` raising on a bad key or network
fault) leaves the agent untouched and returns ``False``. A routing miss must
degrade to "answer on the configured primary", never break the turn.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Optional endpoint/credential fields forwarded into switch_model alongside
# model/provider.
_PASSTHROUGH_FIELDS = ("base_url", "api_key", "api_mode")


def extract_routing_override(results: Iterable[Any]) -> Optional[dict]:
    """Return the first well-formed ``route`` override from hook results, or None.

    A well-formed override is a dict with a non-empty ``model``. First match wins
    (deterministic, and consistent with how ``pre_llm_call`` context parts are
    consumed in plugin registration order).
    """
    if not results:
        return None
    for r in results:
        if not isinstance(r, dict):
            continue
        route = r.get("route")
        if isinstance(route, dict) and str(route.get("model") or "").strip():
            return route
    return None


def apply_routing_override(agent: Any, override: Optional[dict]) -> bool:
    """Actuate a routing override for THIS turn. Return True iff a swap happened.

    Turn-scoped and fail-safe (see the module docstring). No-op (returns False)
    when the override is empty or already matches the live model/provider.
    """
    if not override or not isinstance(override, dict):
        return False

    target_model = str(override.get("model") or "").strip()
    if not target_model:
        return False
    target_provider = str(override.get("provider") or "").strip()

    cur_model = str(getattr(agent, "model", "") or "").strip()
    cur_provider = str(getattr(agent, "provider", "") or "").strip()
    # Avoid churn: nothing to do if we are already on the target.
    if target_model == cur_model and (
        not target_provider or target_provider == cur_provider
    ):
        return False

    # Stash the configured-primary runtime so restore_primary_runtime can revert
    # the swap next turn. switch_model overwrites _primary_runtime with the routed
    # runtime — we deliberately KEEP that (so in-turn recovery paths stay on the
    # routed model) and use the stash plus a dedicated flag for the revert, NOT
    # the reactive-fallback state.
    primary_snapshot = getattr(agent, "_primary_runtime", None)

    kwargs = {k: override[k] for k in _PASSTHROUGH_FIELDS if override.get(k)}

    # When the route switches provider but carries no explicit base_url, resolve
    # the new provider's canonical endpoint up front.
    #
    # ``switch_model`` deliberately REFUSES a cross-provider switch that carries
    # no resolved base_url — it raises rather than keep the previous provider's
    # endpoint (agent/agent_runtime_helpers.py, the ``elif old_norm_provider !=
    # new_norm_provider: raise ValueError`` branch; see #47828). Its existing
    # callers go through ``model_switch.switch_model()``, which resolves the URL
    # first. A plugin route does not, and the natural override a plugin writes is
    # ``{"model": ..., "provider": ...}`` with no base_url. Without this
    # pre-resolution such a route would never take effect: the ValueError is
    # caught by the fail-safe below and the turn silently stays on the configured
    # primary.
    #
    # Fail-safe: a resolver error must not break the turn. We fall through and
    # let switch_model decide — a same-provider route still switches; a
    # cross-provider one is refused and the turn keeps its configured primary.
    if (
        "base_url" not in kwargs
        and target_provider
        and target_provider.strip().lower() != cur_provider.strip().lower()
    ):
        try:
            from agent.auxiliary_client import resolve_provider_client

            _client, _ = resolve_provider_client(target_provider, model=target_model)
            if _client is not None:
                _resolved_base = str(getattr(_client, "base_url", "") or "").strip()
                if _resolved_base:
                    kwargs["base_url"] = _resolved_base
                if "api_key" not in kwargs:
                    _resolved_key = getattr(_client, "api_key", None)
                    if _resolved_key:
                        kwargs["api_key"] = _resolved_key
        except Exception:  # noqa: BLE001 — never break the turn on a resolve miss
            logger.debug(
                "routing override: could not pre-resolve endpoint for %s; "
                "switch_model will fall back to provider defaults",
                target_provider,
                exc_info=True,
            )

    try:
        agent.switch_model(target_model, target_provider, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a routing miss must never break the turn
        logger.warning(
            "routing override: switch to %s/%s failed (%s); staying on %s/%s",
            target_model,
            target_provider or "?",
            exc,
            cur_model,
            cur_provider,
        )
        return False

    # Mark the override turn-scoped with a DEDICATED flag. Do NOT restore the
    # pre-route snapshot into _primary_runtime (keep it == the routed runtime) and
    # do NOT arm _fallback_activated (that would corrupt reactive cooldown
    # accounting).
    try:
        # Only the FIRST apply of a turn snapshots. A second apply without an
        # intervening restore would otherwise stash the first route's own runtime
        # as the "pristine" pre-turn state, so the route would survive the revert.
        # The single in-tree call site applies once per turn, but the snapshot is
        # the whole safety property.
        #
        # ``is not True`` deliberately, not falsiness: the flag is set to exactly
        # True here and cleared to False by the revert, so anything else means "no
        # snapshot has been taken" rather than "a routed turn is live".
        if getattr(agent, "_routing_override_active", False) is not True:
            agent._routing_override_saved_primary = primary_snapshot
        agent._routing_override_active = True
    except Exception:  # noqa: BLE001 — best-effort scoping; swap already applied
        logger.debug("routing override: could not scope override to turn", exc_info=True)

    logger.info(
        "routing override: routed this turn to %s/%s (was %s/%s)",
        target_model,
        target_provider or "?",
        cur_model,
        cur_provider,
    )
    return True
