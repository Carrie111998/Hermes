"""Native OpenAI Responses server-side compaction — gpt-5.6 on direct OpenAI routes only.

OpenAI's Responses API supports server-side compaction: include
``context_management=[{"type": "compaction", "compact_threshold": N}]`` in a
``/v1/responses`` request and, when the rendered input crosses N tokens, the
server summarizes older context into an opaque ``compaction`` output item
(``encrypted_content``, sealed to the issuing endpoint). Replaying that item
as an input item on later requests stands in for the pruned history, so the
model keeps long-horizon recall without the client ever seeing a summary.
Docs: https://developers.openai.com/api/docs/guides/compaction

Hermes' support is deliberately narrow (live verification, Aug 2026):

* **gpt-5.6 family only.** gpt-5.6 and its variants compact correctly.
  Sending the field to gpt-5.1 / gpt-5.2 reliably fails server-side —
  HTTP 500 on the blocking path and a permanent stall on the streaming
  path (90s watchdog x 3 retries = a dead turn). There is no structured
  "unsupported" rejection to downgrade on, so the only safe gate is an
  explicit model-family check.
* **Direct OpenAI routes only:** api.openai.com (API key) or the ChatGPT
  Codex backend (subscription OAuth). Every other Responses surface
  (xAI, GitHub/Copilot, relays, local servers) never sees the field —
  most would 400 on the unknown parameter, and none can mint or decrypt
  the compaction blob.

Ownership model: an eligible native route/model owns automatic compaction.
Hermes' local summarizer is suppressed until ownership transfers because of
a structured rejection, malformed checkpoint, explicit manual compression,
or hard-boundary starvation. Ownership is route-scoped and persisted in the
session's existing ``model_config`` JSON; opaque checkpoints continue to ride
the existing ``codex_reasoning_items`` sidecar.

This module stays free of transport/adapter dependencies so the transport,
adapter, and conversation loop can share the gate without import cycles. The
two exceptions — ``agent.context_compressor`` and ``agent.message_content`` —
sit below this module in the dependency graph (neither imports
``native_compaction``), so importing their provenance/text primitives here
introduces no cycle.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from agent.context_compressor import is_compaction_summary_message
from agent.message_content import flatten_message_text

logger = logging.getLogger(__name__)

# Native compaction fires this many tokens below the local compressor's
# trigger so the server always gets the first shot at compaction.
LOCAL_TRIGGER_SAFETY_MARGIN = 8_192

DEFAULT_COMPACT_THRESHOLD = 200_000

# Model-family gate. Substring match on the lowercased model id so dated
# snapshots (gpt-5.6-2026-07-xx) and variants (gpt-5.6-mini) stay eligible.
_ELIGIBLE_MODEL_MARKER = "gpt-5.6"

NATIVE_OWNERSHIP_METADATA_KEY = "native_compaction_ownership"
NATIVE_OWNERSHIP_VERSION = 1
NATIVE_HARD_EMERGENCY_RESERVE_TOKENS = 8_192

OWNER_NATIVE = "native"
OWNER_LOCAL = "local"
PHASE_AWAITING_CHECKPOINT = "awaiting_checkpoint"
PHASE_CHECKPOINT_ACCEPTED = "checkpoint_accepted"
PHASE_LOCAL_FALLBACK = "local_fallback"

_LOCAL_FALLBACK_REASONS = frozenset(
    {
        "structured_rejection",
        "malformed_checkpoint",
        "hard_emergency_no_checkpoint",
        "manual_compression",
    }
)


def is_native_compaction_model(model: Optional[str]) -> bool:
    """True when the model is in the gpt-5.6 family."""
    return _ELIGIBLE_MODEL_MARKER in (model or "").lower()


def is_direct_openai_route(
    base_url: Optional[str],
    *,
    is_codex_backend: bool = False,
) -> bool:
    """True for api.openai.com or the ChatGPT Codex backend — nothing else."""
    if is_codex_backend:
        return True
    try:
        hostname = (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return False
    return hostname == "api.openai.com"


def canonical_responses_endpoint(base_url: Any) -> str:
    """Return a stable, credential-free endpoint identity."""
    try:
        parsed = urlsplit(str(base_url or ""))
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if not parsed.scheme or not hostname:
        return ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)}
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{authority}{path}"


def _responses_route_flags(agent: Any):
    """Use the canonical Responses route classifier owned by the adapter."""
    from agent.codex_responses_adapter import classify_responses_route

    return classify_responses_route(agent)


def native_compaction_route_key(agent: Any) -> str:
    """Stable route/model key for session-scoped ownership."""
    flags = _responses_route_flags(agent)
    route_kind = "codex" if flags.is_codex_backend else "api"
    return "|".join(
        (
            str(getattr(agent, "api_mode", None) or ""),
            str(getattr(agent, "provider", None) or "").lower(),
            canonical_responses_endpoint(getattr(agent, "base_url", None)),
            str(getattr(agent, "model", None) or "").lower(),
            route_kind,
        )
    )


def native_compaction_route_eligible(
    agent: Any,
    *,
    is_codex_backend: Optional[bool] = None,
    is_xai_responses: Optional[bool] = None,
    is_github_responses: Optional[bool] = None,
) -> bool:
    """Whether the current route/model owns automatic native compaction."""
    flags = _responses_route_flags(agent)
    codex_backend = (
        flags.is_codex_backend
        if is_codex_backend is None
        else bool(is_codex_backend)
    )
    xai_responses = (
        flags.is_xai_responses
        if is_xai_responses is None
        else bool(is_xai_responses)
    )
    github_responses = (
        flags.is_github_responses
        if is_github_responses is None
        else bool(is_github_responses)
    )
    if getattr(agent, "api_mode", "codex_responses") != "codex_responses":
        return False
    if not bool(getattr(agent, "codex_responses_native_compaction", False)):
        return False
    if not bool(getattr(agent, "compression_enabled", True)):
        return False
    if getattr(agent, "compression_checkpoint_required", False) is True:
        return False
    if xai_responses or github_responses:
        return False
    if not is_native_compaction_model(getattr(agent, "model", None)):
        return False
    return is_direct_openai_route(
        getattr(agent, "base_url", None), is_codex_backend=codex_backend
    )


def _checkpoint_items(messages: Any) -> List[Dict[str, Any]]:
    checkpoints: List[Dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict):
            continue
        sidecar = message.get("codex_reasoning_items")
        for item in sidecar if isinstance(sidecar, list) else ():
            if isinstance(item, dict) and item.get("type") == "compaction":
                checkpoints.append(item)
    return checkpoints


def is_usable_compaction_checkpoint(item: Any) -> bool:
    """A replayable checkpoint has non-empty ciphertext and a good status."""
    status = item.get("status") if isinstance(item, dict) else None
    return (
        isinstance(item, dict)
        and item.get("type") == "compaction"
        and isinstance(item.get("encrypted_content"), str)
        and bool(item["encrypted_content"].strip())
        and not (
            isinstance(status, str)
            and status.strip().lower()
            in {"failed", "incomplete", "cancelled", "canceled"}
        )
    )


def has_usable_compaction_checkpoint(items: Any) -> bool:
    return any(
        is_usable_compaction_checkpoint(item)
        for item in (items if isinstance(items, list) else ())
    )


def _current_native_issuer_kind(agent: Any) -> str:
    flags = _responses_route_flags(agent)
    if flags.is_codex_backend:
        return "codex_backend"
    endpoint = canonical_responses_endpoint(getattr(agent, "base_url", None))
    return f"other:{endpoint}" if endpoint else "other"


def _checkpoint_digest(item: Dict[str, Any]) -> str:
    return hashlib.sha256(item["encrypted_content"].encode("utf-8")).hexdigest()


def _compatible_checkpoint_items(
    agent: Any,
    messages: Any,
    *,
    known_digests: Any = (),
) -> List[Dict[str, Any]]:
    """Return usable checkpoints authorized by this exact route/model."""
    current_issuer = _current_native_issuer_kind(agent)
    known = {
        digest
        for digest in (known_digests if isinstance(known_digests, list) else ())
        if isinstance(digest, str)
    }
    compatible = []
    for item in _checkpoint_items(messages):
        if not is_usable_compaction_checkpoint(item):
            continue
        if _checkpoint_digest(item) not in known:
            continue
        issuer = item.get("_issuer_kind")
        if issuer is None or issuer == current_issuer:
            compatible.append(item)
    return compatible


def _default_native_state(route_key: str) -> Dict[str, Any]:
    return {
        "version": NATIVE_OWNERSHIP_VERSION,
        "route_key": route_key,
        "owner": OWNER_NATIVE,
        "phase": PHASE_AWAITING_CHECKPOINT,
        "reason": "eligible_route",
        "checkpoint_digests": [],
        "latest_request_generation": 0,
        "latest_request_tokens": None,
        "checkpoint_generation": None,
        "episode_start_generation": None,
        "episode_start_tokens": None,
    }


def _ownership_default_document() -> Dict[str, Any]:
    return {"version": NATIVE_OWNERSHIP_VERSION, "routes": {}}


def _cache_ownership_document(agent: Any, document: Dict[str, Any]) -> None:
    session_id = str(getattr(agent, "session_id", None) or "")
    agent._native_compaction_ownership_cache = {
        "session_id": session_id,
        "document": document,
    }
    initial = getattr(agent, "_session_init_model_config", None)
    if isinstance(initial, dict):
        updated = dict(initial)
        updated[NATIVE_OWNERSHIP_METADATA_KEY] = document
        agent._session_init_model_config = updated


def _load_ownership_document(agent: Any) -> Dict[str, Any]:
    session_id = str(getattr(agent, "session_id", None) or "")
    document = _ownership_default_document()
    cached = getattr(agent, "_native_compaction_ownership_cache", None)
    cached_document = None
    if isinstance(cached, dict) and cached.get("session_id") == session_id:
        candidate = cached.get("document")
        if isinstance(candidate, dict) and isinstance(candidate.get("routes"), dict):
            cached_document = candidate
    session_db = getattr(agent, "_session_db", None)
    getter = getattr(session_db, "get_session_model_config_value", None)
    if session_id and callable(getter) and not getattr(agent, "_persist_disabled", False):
        try:
            persisted = getter(session_id, NATIVE_OWNERSHIP_METADATA_KEY, None)
            if isinstance(persisted, dict) and isinstance(persisted.get("routes"), dict):
                document = persisted
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.warning("native ownership metadata read failed", exc_info=True)
    elif cached_document is not None:
        document = cached_document

    # A failed atomic write leaves the database at an older revision. Local
    # ownership is one-way, so that older native row must never weaken a
    # fail-closed transition already observed by this agent.
    if cached_document is not None:
        cached_local = {
            key: dict(state)
            for key, state in cached_document["routes"].items()
            if isinstance(state, dict) and state.get("owner") == OWNER_LOCAL
        }
        if cached_local:
            routes = dict(document.get("routes", {}))
            for key, state in cached_local.items():
                persisted = routes.get(key)
                if not isinstance(persisted, dict) or persisted.get("owner") != OWNER_LOCAL:
                    routes[key] = state
            document = {**document, "routes": routes}
    _cache_ownership_document(agent, document)
    return document


def _persist_ownership_document(agent: Any, document: Dict[str, Any]) -> None:
    _cache_ownership_document(agent, document)
    session_id = str(getattr(agent, "session_id", None) or "")
    session_db = getattr(agent, "_session_db", None)
    setter = getattr(session_db, "patch_session_model_config", None)
    if not session_id or not callable(setter) or getattr(agent, "_persist_disabled", False):
        return
    try:
        ensure = getattr(agent, "_ensure_db_session", None)
        if callable(ensure):
            ensure()
        setter(session_id, {NATIVE_OWNERSHIP_METADATA_KEY: document})
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        logger.warning("native ownership metadata write failed", exc_info=True)


def _mutate_ownership_route(agent: Any, route_key: str, mutator) -> Dict[str, Any]:
    """Merge one route atomically so local fallback can never flip back."""
    session_id = str(getattr(agent, "session_id", None) or "")
    session_db = getattr(agent, "_session_db", None)
    atomic = getattr(session_db, "mutate_session_model_config_value", None)

    def update_document(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict) and isinstance(raw.get("routes"), dict):
            document = dict(raw)
            document["routes"] = dict(raw["routes"])
        else:
            document = _ownership_default_document()
        document["version"] = NATIVE_OWNERSHIP_VERSION
        prior = document["routes"].get(route_key)
        updated = mutator(dict(prior) if isinstance(prior, dict) else None)
        if isinstance(updated, dict):
            updated["version"] = NATIVE_OWNERSHIP_VERSION
            updated["route_key"] = route_key
            document["routes"][route_key] = updated
        return document

    if (
        session_id
        and callable(atomic)
        and not getattr(agent, "_persist_disabled", False)
    ):
        try:
            ensure = getattr(agent, "_ensure_db_session", None)
            if callable(ensure):
                ensure()
            document = atomic(
                session_id,
                NATIVE_OWNERSHIP_METADATA_KEY,
                update_document,
                _ownership_default_document(),
            )
            if not isinstance(document, dict):
                document = _ownership_default_document()
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            logger.warning("native ownership metadata atomic write failed", exc_info=True)
            document = update_document(_load_ownership_document(agent))
    else:
        document = update_document(_load_ownership_document(agent))
        _persist_ownership_document(agent, document)

    _cache_ownership_document(agent, document)
    state = document.get("routes", {}).get(route_key)
    return dict(state) if isinstance(state, dict) else _default_native_state(route_key)


def native_compaction_ownership(agent: Any, messages: Any = None) -> Dict[str, Any]:
    """Return the monotonic ownership record for the current route/model."""
    route_key = native_compaction_route_key(agent)
    if not native_compaction_route_eligible(agent):
        state = {
            "version": NATIVE_OWNERSHIP_VERSION,
            "route_key": route_key,
            "owner": OWNER_LOCAL,
            "phase": PHASE_LOCAL_FALLBACK,
            "reason": "native_ineligible",
            "checkpoint_digests": [],
        }
        agent.native_compaction_ownership_state = dict(state)
        return state

    document = _load_ownership_document(agent)
    state = document.get("routes", {}).get(route_key)
    if not isinstance(state, dict):
        state = _default_native_state(route_key)
    compatible = _compatible_checkpoint_items(
        agent,
        messages,
        known_digests=state.get("checkpoint_digests", []),
    )
    if (
        state.get("owner") == OWNER_NATIVE
        and compatible
        and state.get("phase") != PHASE_CHECKPOINT_ACCEPTED
    ):
        def accept_existing(prior):
            if not isinstance(prior, dict) or prior.get("owner") != OWNER_NATIVE:
                return prior
            updated = dict(prior)
            updated["phase"] = PHASE_CHECKPOINT_ACCEPTED
            updated["reason"] = "usable_checkpoint"
            updated["checkpoint_digests"] = list(
                dict.fromkeys(
                    [
                        *updated.get("checkpoint_digests", []),
                        *(_checkpoint_digest(item) for item in compatible),
                    ]
                )
            )[-16:]
            return updated

        state = _mutate_ownership_route(agent, route_key, accept_existing)
    agent.native_compaction_ownership_state = dict(state)
    return state


def transition_native_compaction_to_local(agent: Any, reason: str) -> Dict[str, Any]:
    """Transfer this route to durable local ownership exactly once."""
    if reason not in _LOCAL_FALLBACK_REASONS:
        raise ValueError(f"unsupported native compaction fallback reason: {reason}")
    route_key = native_compaction_route_key(agent)
    current = native_compaction_ownership(agent)
    if current.get("owner") == OWNER_LOCAL:
        return current

    def transfer(prior):
        if isinstance(prior, dict) and prior.get("owner") == OWNER_LOCAL:
            return prior
        previous = prior or {}
        return {
            "owner": OWNER_LOCAL,
            "phase": PHASE_LOCAL_FALLBACK,
            "reason": reason,
            "checkpoint_digests": list(previous.get("checkpoint_digests", [])),
            "latest_request_generation": int(
                previous.get("latest_request_generation", 0) or 0
            ),
            "checkpoint_generation": previous.get("checkpoint_generation"),
        }

    state = _mutate_ownership_route(agent, route_key, transfer)
    agent.native_compaction_ownership_state = dict(state)
    return state


def begin_native_compaction_request(
    agent: Any, request_tokens: Optional[int] = None
) -> Optional[int]:
    """Allocate the current route's next monotonic request generation."""
    if not native_compaction_route_eligible(agent):
        return None
    route_key = native_compaction_route_key(agent)

    def advance(prior):
        state = dict(prior) if isinstance(prior, dict) else _default_native_state(route_key)
        if state.get("owner") == OWNER_LOCAL:
            return state
        generation = state.get("latest_request_generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = 0
        state["latest_request_generation"] = generation + 1
        try:
            state["latest_request_tokens"] = max(0, int(request_tokens or 0))
        except (TypeError, ValueError):
            state["latest_request_tokens"] = 0
        return state

    state = _mutate_ownership_route(agent, route_key, advance)
    if state.get("owner") != OWNER_NATIVE:
        return None
    generation = state.get("latest_request_generation")
    return generation if isinstance(generation, int) else None


def observe_native_compaction_response(
    agent: Any,
    response: Any,
    *,
    request_generation: Optional[int] = None,
) -> Dict[str, Any]:
    """Accept only a fresh, usable checkpoint from the current route."""
    if not native_compaction_route_eligible(agent):
        return native_compaction_ownership(agent)
    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    output_items = list(output) if isinstance(output, list) else []
    compactions = []
    for raw in output_items:
        item = raw if isinstance(raw, dict) else getattr(raw, "__dict__", {})
        if isinstance(item, dict) and item.get("type") == "compaction":
            compactions.append(item)
    if not compactions:
        return native_compaction_ownership(agent)

    observed = native_compaction_ownership(agent)
    if (
        request_generation is not None
        and request_generation != observed.get("latest_request_generation")
    ):
        state = observed
        accepted_digests: set[str] = set()
    else:
        usable = [item for item in compactions if is_usable_compaction_checkpoint(item)]
        if not usable:
            accepted_digests = set()
            state = transition_native_compaction_to_local(agent, "malformed_checkpoint")
        else:
            route_key = native_compaction_route_key(agent)
            response_digests = [_checkpoint_digest(item) for item in usable]
            accepted_digests = set()

            def accept_response(prior):
                state = dict(prior) if isinstance(prior, dict) else _default_native_state(route_key)
                if state.get("owner") == OWNER_LOCAL:
                    return state
                latest = state.get("latest_request_generation", 0)
                generation = latest if request_generation is None else request_generation
                if generation != latest:
                    return state
                digests = list(state.get("checkpoint_digests", []))
                for digest in response_digests:
                    if digest not in digests:
                        digests.append(digest)
                        accepted_digests.add(digest)
                if not accepted_digests:
                    return state
                request_tokens = state.get("latest_request_tokens")
                if not isinstance(request_tokens, int) or isinstance(request_tokens, bool):
                    request_tokens = None
                state.update(
                    owner=OWNER_NATIVE,
                    phase=PHASE_CHECKPOINT_ACCEPTED,
                    reason="usable_checkpoint",
                    checkpoint_digests=digests[-16:],
                    checkpoint_generation=generation,
                    episode_start_generation=generation,
                    episode_start_tokens=request_tokens,
                )
                return state

            state = _mutate_ownership_route(agent, route_key, accept_response)
            if state.get("owner") == OWNER_LOCAL:
                accepted_digests.clear()

    filtered = []
    for raw in output_items:
        item = raw if isinstance(raw, dict) else getattr(raw, "__dict__", {})
        if not (isinstance(item, dict) and item.get("type") == "compaction"):
            filtered.append(raw)
        elif is_usable_compaction_checkpoint(item) and _checkpoint_digest(item) in accepted_digests:
            filtered.append(raw)
    if len(filtered) != len(output_items):
        if isinstance(response, dict):
            response["output"] = filtered
        else:
            response.output = filtered
    agent.native_compaction_ownership_state = dict(state)
    return state


def native_hard_emergency_boundary(agent: Any) -> Optional[int]:
    compressor = getattr(agent, "context_compressor", None)
    context_length = getattr(compressor, "context_length", None)
    if (
        not isinstance(context_length, int)
        or isinstance(context_length, bool)
        or context_length <= 0
    ):
        return None
    return max(1_024, context_length - NATIVE_HARD_EMERGENCY_RESERVE_TOKENS)


def suppress_automatic_local_compaction(
    agent: Any,
    messages: Any,
    approx_tokens: Optional[int] = None,
) -> bool:
    """True while native owns this episode; transfer at the hard boundary."""
    state = native_compaction_ownership(agent, messages)
    if state.get("owner") != OWNER_NATIVE:
        return False
    boundary = native_hard_emergency_boundary(agent)
    if boundary is None:
        return True
    if approx_tokens is None:
        from agent.model_metadata import estimate_messages_tokens_rough

        approx_tokens = estimate_messages_tokens_rough(messages)
    try:
        current_tokens = max(0, int(approx_tokens or 0))
    except (TypeError, ValueError):
        current_tokens = 0

    at_boundary = current_tokens >= boundary
    compatible_checkpoint_present = bool(
        _compatible_checkpoint_items(
            agent,
            messages,
            known_digests=state.get("checkpoint_digests", []),
        )
    )
    if (
        state.get("phase") == PHASE_CHECKPOINT_ACCEPTED
        and compatible_checkpoint_present
    ):
        episode_start = state.get("episode_start_tokens")
        checkpoint_generation = state.get("checkpoint_generation")
        if (
            isinstance(episode_start, int)
            and not isinstance(episode_start, bool)
            and state.get("episode_start_generation") == checkpoint_generation
        ):
            at_boundary = max(0, current_tokens - episode_start) >= boundary
    if at_boundary:
        transition_native_compaction_to_local(agent, "hard_emergency_no_checkpoint")
        return False
    return True


def resolve_compact_threshold(
    configured_threshold: Any,
    local_trigger_tokens: Any = None,
) -> int:
    """Clamp the configured native threshold below the local compressor trigger.

    Without the clamp a native threshold above the local trigger would let the
    local summarizer fire first every time, making native compaction dead
    config. ``local_trigger_tokens`` is ``ContextCompressor.threshold_tokens``
    when a compressor is attached, else None.
    """
    try:
        configured = int(configured_threshold)
    except (TypeError, ValueError):
        configured = DEFAULT_COMPACT_THRESHOLD
    if isinstance(configured_threshold, bool) or configured <= 0:
        configured = DEFAULT_COMPACT_THRESHOLD

    local = None
    try:
        if local_trigger_tokens is not None and not isinstance(local_trigger_tokens, bool):
            local = int(local_trigger_tokens)
    except (TypeError, ValueError):
        local = None
    if local is None or local <= 0:
        return configured

    if local > LOCAL_TRIGGER_SAFETY_MARGIN:
        upper = local - LOCAL_TRIGGER_SAFETY_MARGIN
    else:
        upper = max(1_024, int(local * 0.8))
    return max(1_024, min(configured, upper))


_checkpoint_suppression_logged = False


def _warn_native_compaction_suppressed_by_checkpoint_gate() -> None:
    """Log once per process that the checkpoint gate suppresses native compaction.

    The suppression itself is re-evaluated per request; only the log line is
    deduplicated so a long session does not repeat it on every API call.
    """
    global _checkpoint_suppression_logged
    if _checkpoint_suppression_logged:
        return
    _checkpoint_suppression_logged = True
    logger.warning(
        "compression.checkpoint_required is enabled: server-side native "
        "compaction (context_management) is disabled for this agent so the "
        "checkpoint-aware Hermes compressor stays authoritative."
    )


def native_compaction_context_management(
    agent: Any,
    *,
    is_codex_backend: bool,
    is_xai_responses: bool = False,
    is_github_responses: bool = False,
    messages: Any = None,
) -> Optional[List[Dict[str, Any]]]:
    """Return the ``context_management`` payload for this request, or None.

    None means "do not send the field" — the request is byte-identical to
    pre-feature behavior. All gates and route-scoped ownership are re-checked
    per request, so model/provider switches and durable fallback transitions
    take effect on the next call.
    """
    if not native_compaction_route_eligible(
        agent,
        is_codex_backend=is_codex_backend,
        is_xai_responses=is_xai_responses,
        is_github_responses=is_github_responses,
    ):
        if getattr(agent, "compression_checkpoint_required", False) is True:
            _warn_native_compaction_suppressed_by_checkpoint_gate()
        return None
    if native_compaction_ownership(agent, messages).get("owner") != OWNER_NATIVE:
        return None

    compressor = getattr(agent, "context_compressor", None)
    threshold = resolve_compact_threshold(
        getattr(agent, "codex_responses_compact_threshold", DEFAULT_COMPACT_THRESHOLD),
        getattr(compressor, "threshold_tokens", None) if compressor is not None else None,
    )
    return [{"type": "compaction", "compact_threshold": threshold}]


# Retention budget for plaintext user messages carried across a native
# compaction boundary (mirrors Codex CLI's RETAINED_MESSAGE_TOKEN_BUDGET).
# Live verification (Aug 2026, gpt-5.6 @ api.openai.com): the server renders
RETAINED_USER_MESSAGE_TOKEN_BUDGET = 64_000

# Retention budget for local compression summary messages carried across a native
# compaction boundary to prevent summary token inflation.
RETAINED_SUMMARY_TOKEN_BUDGET = 32_000


def _approx_tokens(text: str) -> int:
    """Cheap chars//4 token estimate — same shape Codex uses for retention."""
    return max(1, len(text) // 4)


def _extract_item_text(item: Any) -> Optional[str]:
    """Extract measurable text from string, list content, output_text, or nested metadata text.

    Returns None when the item carries no measurable text.
    Handles string content, multipart lists (input_text/text/output_text), and fallback keys.
    """
    if not isinstance(item, dict):
        return None

    content = item.get("content")
    if content is None and "output_text" in item:
        content = item.get("output_text")

    if isinstance(content, str):
        return content if content.strip() else None

    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    parts.append(part.strip())
            elif isinstance(part, dict):
                part_text = part.get("text") or part.get("input_text") or part.get("output_text")
                if isinstance(part_text, str) and part_text.strip():
                    parts.append(part_text.strip())
                part_meta = part.get("metadata")
                if isinstance(part_meta, dict) and isinstance(part_meta.get("text"), str):
                    if part_meta["text"].strip():
                        parts.append(part_meta["text"].strip())
        text = " ".join(parts)
        return text if text.strip() else None

    return None


def _is_summary_item(item: Any) -> bool:
    """True when *item* is a canonical Hermes compression-summary message.

    Delegates entirely to
    ``agent.context_compressor.is_compaction_summary_message`` — the single
    authoritative provenance check already used by every other summary
    consumer (memory providers, frontends, the compactor itself). It prefers
    the exact, truthy ``COMPRESSED_SUMMARY_METADATA_KEY`` marker and falls
    back to the canonical prefix classifier (``SUMMARY_PREFIX`` /
    ``LEGACY_SUMMARY_PREFIX`` / historical prefixes, including the
    merge-into-tail shape) for the case where the underscore-prefixed key
    was already stripped by a wire sanitizer.

    Deliberately NOT a second heuristic: no arbitrary underscore-key scan, no
    inference from a falsy or unrelated metadata key, and no matching on
    ad-hoc content headings like ``"## Summary"`` in ordinary text — any of
    those can promote a normal user/assistant message (or adversarial
    content) to durable retained history (#90975 review).
    """
    return is_compaction_summary_message(item)


def prune_pre_checkpoint_items(
    items: List[Dict[str, Any]],
    retained_user_token_budget: int = RETAINED_USER_MESSAGE_TOKEN_BUDGET,
    retained_summary_token_budget: int = RETAINED_SUMMARY_TOKEN_BUDGET,
    enable_summary_retention: bool = True,
    item_sources: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Restructure Responses input around the newest compaction checkpoint.

    The server drops every input item that precedes a replayed ``compaction``
    item (live-verified Aug 2026), so sending pre-checkpoint history is dead
    weight AND silently erases the user's plaintext asks — including any
    local-compression summary the agent already produced, which previously
    vanished here because it carries ``role="assistant"``, not ``"user"``
    (#90975). When a checkpoint is present, rebuild the wire as::

        [checkpoint run] + [retained user & summary messages (newest-first budget)] + [post]

    - The NEWEST contiguous run of checkpoints wins.
    - Retained user messages are kept verbatim within
      ``retained_user_token_budget``; the boundary message is head-truncated
      when it only partially fits (string content only) — goals are usually
      stated up front, so the head is the valuable end.
    - Compression summary messages (``_is_summary_item``, the canonical
      ``agent.context_compressor`` provenance check) are retained whole
      within ``retained_summary_token_budget``. A summary is never
      byte/character-sliced: Hermes summaries carry structural framing
      (handoff prefix, end marker, merge-into-tail delimiters) that a blind
      slice can corrupt, so one that doesn't fit whole is dropped instead.
      A summary already retained once (identical text) is never duplicated,
      so repeated checkpoints stay idempotent.
    - ``enable_summary_retention`` is a function-level override (used by
      tests and callers that need the pre-#90975 behavior back); it is not
      wired to a user-facing config surface.
    - Original relative chronological order between user messages and
      summaries is preserved.
    - ``item_sources`` (optional, parallel to ``items``) is the raw chat
      message each Responses item was converted from. By the time a summary
      reaches this function as a converted ``item`` it can already be lossy:
      a merge-into-tail tool-result carrier becomes a typed
      ``function_call_output`` (no ``content``/``role`` survives the
      conversion at all), and a merge-into-tail assistant carrier can be
      shadowed by a stale exact ``codex_message_items`` replay captured
      before the merge rewrote its content. When a source is provided and is
      itself a canonical summary carrier (``is_compaction_summary_message``),
      its content is read directly from the source — never from the
      converted item — and it is retained as a synthesized
      ``role="assistant"`` message regardless of what shape the original
      item took. Without ``item_sources`` (default), retention only sees
      what survived conversion, matching pre-#90976 behavior (#90976).
    """
    if not isinstance(items, list) or not items:
        return items

    last_cp = None
    for i, item in enumerate(items):
        if is_usable_compaction_checkpoint(item):
            last_cp = i
    if last_cp is None:
        return items

    # Extend backwards over the contiguous run ending at last_cp.
    first_cp = last_cp
    while (
        first_cp > 0
        and is_usable_compaction_checkpoint(items[first_cp - 1])
    ):
        first_cp -= 1

    pre = items[:first_cp]
    checkpoint_run = items[first_cp : last_cp + 1]
    post = items[last_cp + 1 :]

    if isinstance(item_sources, list) and len(item_sources) == len(items):
        pre_sources: List[Any] = item_sources[:first_cp]
    else:
        pre_sources = [None] * len(pre)

    retained_reversed: List[Dict[str, Any]] = []
    user_remaining = max(0, int(retained_user_token_budget))
    summary_remaining = max(0, int(retained_summary_token_budget))
    seen_summary_texts: set = set()

    def _try_retain_summary(text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Check budget/dedup/cost for a summary; return cost info or None."""
        if not text or summary_remaining <= 0 or text in seen_summary_texts:
            return None
        cost = _approx_tokens(text)
        if cost > summary_remaining:
            # Never byte-slice a summary's structural framing — drop it
            # whole rather than corrupt the handoff prefix / end marker.
            return None
        seen_summary_texts.add(text)
        return {"cost": cost}

    for item, source in zip(reversed(pre), reversed(pre_sources)):
        if not isinstance(item, dict):
            continue

        # Canonical source-based summary detection: reads the ORIGINAL chat
        # message's own content, so it sees past a lossy conversion (a
        # typed `function_call_output` wrapper, or a stale exact-replay
        # message) that erased the summary from `item` itself (#90976).
        # This is never a heuristic promotion of arbitrary item content —
        # it only fires when the source message itself is a canonical,
        # provenance-tagged summary carrier.
        if enable_summary_retention and isinstance(source, dict) and _is_summary_item(source):
            text = flatten_message_text(source.get("content")) if isinstance(source, dict) else ""
            text = text if text.strip() else None
            result = _try_retain_summary(text)
            if result:
                _src_role = source.get("role")
                retained_reversed.append({
                    "role": _src_role if _src_role in ("user", "assistant") else "assistant",
                    "content": text,
                })
                summary_remaining -= result["cost"]
            continue

        # Skip typed non-message items (function_call_output etc. never
        # carry role=user or a summary flag, but stay defensive about
        # future shapes).
        if "type" in item and item.get("type") != "message":
            continue

        is_summary = enable_summary_retention and _is_summary_item(item)
        is_user = item.get("role") == "user"

        if not is_user and not is_summary:
            continue

        text = _extract_item_text(item)
        if text is None:
            continue
        # Image-only user messages have empty text but non-empty content —
        # main retains them at 1-token cost (images count as zero, matching
        # Codex's retention accounting). Don't skip them just because text
        # is falsy.
        if not text and not is_user:
            continue

        if is_summary:
            result = _try_retain_summary(text)
            if result:
                retained_reversed.append(item)
                summary_remaining -= result["cost"]
        elif is_user:
            if user_remaining <= 0:
                continue
            cost = _approx_tokens(text)
            if cost <= user_remaining:
                retained_reversed.append(item)
                user_remaining -= cost
            elif isinstance(item.get("content"), str):
                truncated = dict(item)
                truncated["content"] = item["content"][: user_remaining * 4]
                if truncated["content"].strip():
                    retained_reversed.append(truncated)
                user_remaining = 0

    retained_ordered = list(reversed(retained_reversed))
    result = checkpoint_run + retained_ordered + post

    logger.debug(
        "Pruned pre-checkpoint items: %d input -> %d retained (user_rem=%d, summary_rem=%d)",
        len(items),
        len(result),
        user_remaining,
        summary_remaining,
    )

    return result


def is_native_compaction_rejection(error: Any, status_code: Any = None) -> bool:
    """True when a provider error is a STRUCTURED rejection of the
    context_management field.

    Used by the conversation loop's one-shot recovery: transfer this route to
    local ownership, strip the field, and retry. Matching is deliberately
    narrow — a transient 5xx/timeout whose body merely ECHOES the request
    (and therefore contains the field name) must NOT permanently downgrade
    native compaction for the route (#82777).

    Two conditions are always required:

    * ``status_code`` parses to 400, or the message explicitly embeds the
      SDK-style ``Error code: 400`` marker, and
    * the error text names ``context_management`` / ``compact_threshold``
      alongside rejection language ("unknown", "unsupported", "invalid",
      "unexpected", "not permitted"...). A bare field-name echo without
      rejection language does not match.
    """
    text = str(error or "").lower()
    if "context_management" not in text and "compact_threshold" not in text:
        return False
    parsed_status = None
    if status_code is not None and not isinstance(status_code, bool):
        try:
            parsed_status = int(status_code)
        except (TypeError, ValueError):
            pass
    if parsed_status is None:
        if re.search(
            r"(?:\btimeout(?:error)?\b|\btimed\s+out\b|\bconnection\s+reset\b|"
            r"\bhttp\s+5\d\d\b|\b5xx\b)",
            text,
        ):
            return False
        embedded = re.search(r"\berror\s+code\s*:\s*(\d{3})\b", text)
        if embedded is not None:
            parsed_status = int(embedded.group(1))
    if parsed_status != 400:
        return False
    rejection_markers = (
        "unknown", "unsupported", "invalid", "unexpected", "not permitted",
        "not allowed", "unrecognized", "extra field", "no such", "bad request",
        "not supported",
    )
    return any(marker in text for marker in rejection_markers)


def has_compaction_checkpoint(items: Any) -> bool:
    """Does this ``codex_reasoning_items`` sidecar carry a compaction checkpoint?

    A ``type: "compaction"`` item is the server-side stand-in for history that
    has already been pruned — cumulative context, not per-turn reasoning. It
    rides the same sidecar as ordinary reasoning items, so anything that
    rewrites or discards that sidecar (or the message carrying it) has to ask
    this question first: the checkpoint exists in exactly one place, and the
    request that loses it loses the compacted history with it.
    """
    return any(
        isinstance(item, dict) and item.get("type") == "compaction"
        for item in (items if isinstance(items, list) else ())
    )


def merge_interim_reasoning_items(
    prior_items: Any,
    new_items: Any,
) -> List[Dict[str, Any]]:
    """Merge ``codex_reasoning_items`` across Codex incomplete-continuation
    dedup, preserving native compaction checkpoints.

    The incomplete-retry path updates a visually-duplicate interim assistant
    message in place with the newer response's replay payload. A checkpoint
    captured on the EARLIER response is a cumulative context carrier the
    continuation won't re-emit (the replayed checkpoint keeps the server
    render under threshold), so a blind overwrite drops the only copy and the
    next request balloons back to full history. Rule: newer items win, but
    prior checkpoints are prepended unless the newer payload carries its own.
    """
    kept_checkpoints = [
        item
        for item in (prior_items if isinstance(prior_items, list) else [])
        if is_usable_compaction_checkpoint(item)
    ]
    new_list = list(new_items) if isinstance(new_items, list) else []
    if has_usable_compaction_checkpoint(new_list) or not kept_checkpoints:
        return new_list
    return kept_checkpoints + new_list
