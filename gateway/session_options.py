"""Structured, silent runtime options for messaging-gateway sessions.

Host UIs use this instead of injecting human-facing slash commands. The
gateway remains the single owner of provider resolution, capability checks,
session state, and restart persistence.

Two primitives defined here are shared with the user-facing slash commands
(``/model``, ``/reasoning``, ``/fast``) so the host API and the user never
disagree about a session's runtime options:

* :func:`session_admission_lock` — one ``asyncio.Lock`` per session key.
  ``GatewayRunner._handle_message`` holds it across its idle->running claim
  (no await inside, so the uncontended hot path never yields), the slash
  write-through holds it across persist + live mutation, and
  :func:`apply_gateway_session_options` holds it across busy check + durable
  commit + live mutation. A turn therefore cannot be admitted *between* the
  API observing "idle" and the API committing, and two writers cannot
  interleave their persist/mutate steps.
* :func:`commit_session_runtime_options` — the single durable-first
  write-through: persist the complete snapshot via the async session store,
  then (and only then) move live ``SessionState``. A failed durable write
  raises with live state untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Mapping

from gateway.session import sanitize_model_override
from gateway.session_state import SERVICE_TIER_UNSET

logger = logging.getLogger(__name__)

# "caller did not pass this field" for the partial-patch primitive below.
UNSET = object()


def _reasoning_label(value: dict | None) -> str | None:
    if value is None:
        return None
    if value.get("enabled") is False:
        return "none"
    return str(value.get("effort") or "medium")


def _rejected(code: str, error: str) -> dict[str, Any]:
    return {"status": "rejected", "code": code, "error": error}


def _busy_result() -> dict[str, Any]:
    return _rejected(
        "session_busy",
        "session options can only change while the session is idle",
    )


# ---------------------------------------------------------------------------
# Per-session admission exclusion
# ---------------------------------------------------------------------------


def session_admission_lock(runner: Any, session_key: str) -> asyncio.Lock:
    """Return the per-session lock shared by turn admission and option writes.

    One lock per session key, created lazily and never evicted (same lifetime
    as ``GatewayRunner._sessions``). ``__dict__`` access keeps bare
    ``object.__new__`` test runners working.
    """
    locks = runner.__dict__.get("_session_options_locks")
    if locks is None:
        locks = {}
        runner._session_options_locks = locks
    lock = locks.get(session_key)
    if lock is None:
        lock = locks[session_key] = asyncio.Lock()
    return lock


def session_admission_lock_held(runner: Any, session_key: str) -> bool:
    """True while a runtime-options write or a turn claim owns ``session_key``.

    Synchronous probe for the one idle->running claimer that cannot await the
    lock (the boot-resume pre-claim); it must skip the session for that pass
    instead of claiming underneath an in-flight commit.
    """
    locks = runner.__dict__.get("_session_options_locks") or {}
    lock = locks.get(session_key)
    return lock is not None and lock.locked()


# ---------------------------------------------------------------------------
# Single durable-first write-through
# ---------------------------------------------------------------------------


def _durable_tier(live_tier: Any) -> str | None:
    """Map the live tri-state (UNSET / None / "priority") to the durable one."""
    if live_tier is SERVICE_TIER_UNSET:
        return None
    return "priority" if live_tier == "priority" else "normal"


def _runtime_options_signature(conversation: Any) -> tuple:
    """Comparable snapshot of the live triple (credentials stripped)."""
    reasoning = conversation.reasoning_override
    return (
        sanitize_model_override(conversation.model_override),
        dict(reasoning) if isinstance(reasoning, dict) else None,
        conversation.service_tier_override,
    )


async def commit_session_runtime_options(
    runner: Any,
    session_key: str,
    *,
    model_override: Any = UNSET,
    reasoning_override: Any = UNSET,
    service_tier_override: Any = UNSET,
    durable: bool = True,
    require_routing_entry: bool = False,
) -> bool:
    """Durable-first write-through for per-session runtime options.

    Every session-runtime mutation — the structured host API and the
    ``/model``, ``/reasoning`` and ``/fast`` slash commands — funnels through
    here under :func:`session_admission_lock`, so memory and disk cannot
    disagree: the complete snapshot is persisted FIRST and live
    ``SessionState`` only moves once the store commit succeeded. A failed
    durable write propagates to the caller with live state untouched, so a
    command fails loudly instead of leaving this process on a configuration a
    restart would silently discard.

    Omitted keyword arguments keep their current live value (partial patch).
    ``service_tier_override`` uses the live encoding (``"priority"``,
    ``None`` = explicit normal, ``SERVICE_TIER_UNSET`` = inherit).

    Returns ``False`` when the store has no routing entry for ``session_key``
    yet. Slash commands can run before the entry exists (first message of a
    chat), so by default the override then stays process-local exactly as it
    did before persistence existed; ``require_routing_entry=True`` (host API,
    which creates the entry first) leaves live state untouched instead.

    ``durable=False`` is reserved for ``/model --once``: the one-turn override
    is live-only by contract (#29923). While such an override is live, any
    durable write made by a sibling path (e.g. ``/reasoning``) persists the
    PRE-once model from the restore snapshot, never the one-turn model.
    """
    async with session_admission_lock(runner, session_key):
        return await _commit_session_runtime_options_locked(
            runner,
            session_key,
            model_override=model_override,
            reasoning_override=reasoning_override,
            service_tier_override=service_tier_override,
            durable=durable,
            require_routing_entry=require_routing_entry,
        )


async def _commit_session_runtime_options_locked(
    runner: Any,
    session_key: str,
    *,
    model_override: Any = UNSET,
    reasoning_override: Any = UNSET,
    service_tier_override: Any = UNSET,
    durable: bool = True,
    require_routing_entry: bool = False,
) -> bool:
    """Body of :func:`commit_session_runtime_options`; caller holds the lock."""
    runner._rehydrate_session_runtime_options(session_key)
    conversation = runner._session_state(session_key).conversation
    new_model = (
        conversation.model_override if model_override is UNSET else model_override
    )
    new_reasoning = (
        conversation.reasoning_override
        if reasoning_override is UNSET
        else reasoning_override
    )
    new_tier = (
        conversation.service_tier_override
        if service_tier_override is UNSET
        else service_tier_override
    )

    durable_model = new_model
    if durable and model_override is UNSET and conversation.one_turn_restore:
        # A /model --once override is live: persist what the post-turn restore
        # will put back, never the one-turn model.
        snapshot = conversation.one_turn_restore
        durable_model = (
            dict(snapshot.get("override") or {})
            if snapshot.get("had_override")
            else None
        )

    persisted = True
    store = getattr(runner, "session_store", None)
    if durable and callable(getattr(store, "set_runtime_options", None)):
        # Off-loop via the async facade. Raises on a failed save (the store
        # rolls its own entry back); False when no routing entry exists yet.
        persisted = bool(
            await runner.async_session_store.set_runtime_options(
                session_key,
                model_override=durable_model,
                reasoning_override=new_reasoning,
                service_tier_override=_durable_tier(new_tier),
            )
        )
        if not persisted and require_routing_entry:
            return False

    conversation.model_override = new_model
    conversation.reasoning_override = new_reasoning
    conversation.service_tier_override = new_tier
    if durable and model_override is not UNSET:
        # A durable model commit supersedes any pending one-turn restore; the
        # old snapshot would otherwise revert this model after the next turn.
        conversation.one_turn_restore = None
    return persisted


# ---------------------------------------------------------------------------
# Structured host API
# ---------------------------------------------------------------------------


async def apply_gateway_session_options(
    runner: Any,
    source: Any,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and atomically apply per-session model/reasoning/fast state."""
    allowed = {
        "model",
        "provider",
        "reasoning_effort",
        "fast",
        "confirm_model_selection",
        "initial",
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        return _rejected(
            "invalid_options", f"unknown session option(s): {', '.join(unknown)}"
        )

    normalized_source = await asyncio.to_thread(
        runner._normalize_source_for_session_key, source
    )
    session_key = runner._session_key_for_source(normalized_source)
    if not session_key:
        return _rejected(
            "invalid_session", "session source did not resolve to a session key"
        )
    # Cheap early-out only; the authoritative busy check runs under the
    # admission lock in _apply_scoped, right before the durable commit.
    if runner._is_session_running(session_key):
        return _busy_result()

    profile_scope = contextlib.nullcontext()
    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        from gateway.run import _profile_runtime_scope

        profile_scope = _profile_runtime_scope(
            runner._resolve_profile_home_for_source(normalized_source)
        )
    with profile_scope:
        return await _apply_scoped(runner, normalized_source, session_key, options)


async def _apply_scoped(
    runner: Any,
    source: Any,
    session_key: str,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    from gateway.run import _load_gateway_config, _resolve_gateway_model
    from hermes_cli.model_selection_guards import combined_selection_warning
    from hermes_cli.model_switch import switch_model
    from hermes_cli.models import resolve_fast_mode_overrides
    from hermes_constants import parse_reasoning_effort

    if "provider" in options and "model" not in options:
        return _rejected("invalid_options", "provider requires model")

    runner._rehydrate_session_runtime_options(session_key)
    state = runner._session_state(session_key)
    # Validation below runs OUTSIDE the admission lock (model resolution can
    # hit the network) against this snapshot of the live triple; the commit
    # re-reads live state under the lock and refuses to overwrite anything
    # that moved in between (a user slash command stays authoritative).
    expected = _runtime_options_signature(state.conversation)
    current_model, current_runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
    )
    current_provider = str(current_runtime.get("provider") or "openrouter")
    current_base_url = str(current_runtime.get("base_url") or "")
    current_api_key = str(current_runtime.get("api_key") or "")

    effective_model = current_model
    effective_provider = current_provider
    model_warning = ""
    # Requested values in the LIVE encoding, only for fields named in options.
    patch: dict[str, Any] = {}

    if "model" in options:
        requested_model = str(options.get("model") or "").strip()
        requested_provider = str(options.get("provider") or "").strip()
        if not requested_model:
            if requested_provider:
                return _rejected("invalid_options", "provider requires model")
            patch["model_override"] = None
            user_config = _load_gateway_config()
            effective_model = _resolve_gateway_model(user_config)
            effective_provider = str(
                ((user_config.get("model") or {}).get("provider")
                 if isinstance(user_config.get("model"), dict) else "")
                or current_provider
            )
        else:
            user_config = _load_gateway_config()
            user_providers = user_config.get("providers")
            if not isinstance(user_providers, dict):
                user_providers = {}
            try:
                from hermes_cli.config import get_compatible_custom_providers

                custom_providers = get_compatible_custom_providers(user_config)
            except Exception:
                custom_providers = user_config.get("custom_providers")
            result = await asyncio.to_thread(
                switch_model,
                raw_input=requested_model,
                current_provider=current_provider,
                current_model=current_model,
                current_base_url=current_base_url,
                current_api_key=current_api_key,
                is_global=False,
                explicit_provider=requested_provider,
                user_providers=user_providers,
                custom_providers=custom_providers,
            )
            if not result.success:
                return _rejected(
                    "model_rejected", result.error_message or "model switch failed"
                )
            selection_warning = await asyncio.to_thread(
                combined_selection_warning,
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or current_base_url,
                api_key=result.api_key or current_api_key,
                model_info=result.model_info,
            )
            if selection_warning is not None and not bool(
                options.get("confirm_model_selection")
            ):
                return {
                    "status": "confirmation_required",
                    "code": "model_confirmation_required",
                    "title": selection_warning.title,
                    "message": selection_warning.message,
                }
            patch["model_override"] = {
                "model": result.new_model,
                "provider": result.target_provider,
                "api_key": result.api_key,
                "base_url": result.base_url,
                "api_mode": result.api_mode,
            }
            effective_model = result.new_model
            effective_provider = result.target_provider
            model_warning = str(result.warning_message or "")

    if "reasoning_effort" in options:
        effort = str(options.get("reasoning_effort") or "").strip().lower()
        if not effort:
            patch["reasoning_override"] = None
        else:
            reasoning_override = parse_reasoning_effort(effort)
            if reasoning_override is None:
                return _rejected(
                    "reasoning_rejected", f"unsupported reasoning effort: {effort}"
                )
            patch["reasoning_override"] = reasoning_override

    if "fast" in options:
        requested_fast = options.get("fast")
        if not isinstance(requested_fast, bool):
            return _rejected("fast_rejected", "fast must be a boolean")
        if requested_fast:
            if resolve_fast_mode_overrides(effective_model) is None:
                return _rejected(
                    "fast_unsupported", "fast mode is not available for this model"
                )
            patch["service_tier_override"] = "priority"
        else:
            patch["service_tier_override"] = None  # explicit normal

    def _accepted(applied: list[str], reasoning: Any, tier: Any) -> dict[str, Any]:
        return {
            "status": "accepted",
            "session_key": session_key,
            "applied": applied,
            "effective": {
                "model": effective_model,
                "provider": effective_provider,
                "reasoning_effort": _reasoning_label(reasoning),
                "fast": tier == "priority",
            },
            **({"warning": model_warning} if model_warning else {}),
        }

    if not patch:
        return _accepted(
            [], state.conversation.reasoning_override,
            state.conversation.service_tier_override,
        )

    # Commit. Everything from the busy check to the live mutation runs under
    # the per-session admission lock that _handle_message also takes around
    # its idle->running claim, so no turn can be admitted in between and no
    # other writer can interleave its persist/mutate steps with ours.
    async with session_admission_lock(runner, session_key):
        if runner._is_session_running(session_key):
            return _busy_result()
        # Create/resolve the routing entry only after every requested value
        # has passed validation.
        entry = await runner.async_session_store.get_or_create_session(source)
        conversation = state.conversation
        if getattr(entry, "was_auto_reset", False):
            # The session crossed an idle/daily boundary: consume the flag so
            # the next message's cleanup does not wipe what we are about to
            # store (#48031), and clear the previous conversation's scope NOW
            # so it cannot leak into the fresh session either (#58403).
            # Note: a rejection below (fast_unsupported) still consumes this
            # boundary -- the next inbound message would have cleared the same
            # scope anyway, so nothing the caller could observe is lost.
            entry.was_auto_reset = False
            runner._clear_conversation_scope(session_key, reason="auto_reset")
            runner._evict_cached_agent(session_key)
            if "model_override" not in patch:
                # The previous override is gone: re-derive what the fresh
                # session will actually run so the fast check and the reply
                # describe that model, not the one we validated against.
                effective_model, runtime = runner._resolve_session_agent_runtime(
                    source=source, session_key=session_key
                )
                effective_provider = str(runtime.get("provider") or "openrouter")
                if (
                    patch.get("service_tier_override") == "priority"
                    and resolve_fast_mode_overrides(effective_model) is None
                ):
                    return _rejected(
                        "fast_unsupported",
                        "fast mode is not available for this model",
                    )
        elif _runtime_options_signature(conversation) != expected:
            return _rejected(
                "conflict",
                "session runtime options changed while the request was "
                "being validated; re-read and retry",
            )
        # Every idle->running claim that can await takes this lock. The one
        # claimer that cannot (the synchronous boot-resume pre-claim in
        # GatewayRunner._schedule_resume_pending_sessions) probes the lock and skips
        # the session instead; this in-lock re-read is defence-in-depth for
        # that single site, not a substitute for the lock.
        if runner._is_session_running(session_key):
            return _busy_result()

        # Patch onto the (possibly just cleared) live triple; only fields whose
        # value actually changes are applied, so re-asserting the current
        # options is a no-op (no durable write, no agent eviction, no note).
        base = {
            "model_override": conversation.model_override,
            "reasoning_override": conversation.reasoning_override,
            "service_tier_override": conversation.service_tier_override,
        }
        new = {**base, **patch}
        changed = {
            name for name, value in patch.items() if base[name] != value
        }
        changed_fields = [
            label
            for label, name in (
                ("model", "model_override"),
                ("reasoning_effort", "reasoning_override"),
                ("fast", "service_tier_override"),
            )
            if name in changed
        ]
        if not changed:
            return _accepted(
                [], new["reasoning_override"], new["service_tier_override"]
            )

        persisted = await _commit_session_runtime_options_locked(
            runner,
            session_key,
            require_routing_entry=True,
            **{name: new[name] for name in changed},
        )
        if not persisted:
            return _rejected(
                "session_missing",
                "session disappeared while applying runtime options",
            )
        state.persistent.runtime_options_rehydrated = True
        runner._evict_cached_agent(session_key)

        model_switched = "model_override" in changed and (
            effective_model != current_model
            or effective_provider != current_provider
        )
        if model_switched and not bool(options.get("initial")):
            from hermes_cli.model_switch import format_model_for_display

            if not hasattr(runner, "_pending_model_notes"):
                runner._pending_model_notes = {}
            runner._pending_model_notes[session_key] = (
                f"[Note: model was just switched from "
                f"{format_model_for_display(current_model)} to "
                f"{format_model_for_display(effective_model)} via "
                f"{effective_provider}. Adjust your self-identification accordingly.]"
            )

    session_db = getattr(runner, "_session_db", None)
    if session_db is not None and new["model_override"] is not None:
        try:
            entry = await runner.async_session_store.lookup_by_session_key(session_key)
            if entry is not None:
                await session_db.update_session_model(
                    entry.session_id,
                    effective_model,
                    provider=effective_provider,
                )
        except Exception:
            logger.debug("Failed to mirror structured model option", exc_info=True)

    return _accepted(
        changed_fields, new["reasoning_override"], new["service_tier_override"]
    )
