"""Runtime override support for ``pre_llm_call`` plugin hooks.

A plugin may return ``{"runtime_override": {...}}`` from ``pre_llm_call`` to
proactively override the LLM API call parameters for the current turn:

    {"context": "recalled text...",            # existing behavior, unchanged
     "runtime_override": {
         "model": "gpt-5.6",
         "provider": "openai",
         "base_url": "https://api.openai.com/v1",
         "api_key": "sk-...",
         "api_mode": "chat_completions",
         "system_prompt": "You are ...",
     }}

Contract (mirrors the ``pre_failover_decision`` redirect contract):

* ``redirect`` is *error-driven*: it is applied by the retry/failover machinery
  only after an API call has failed.  ``runtime_override`` is *proactive*: it is
  applied before the first API call of the turn.  The two do not conflict —
  redirect rewrites identity on the failover path, runtime_override rewrites
  identity on the primary path.
* The override is ephemeral and turn-scoped: it lives on ``agent._runtime_override``,
  is re-resolved on every turn prologue, is never persisted to the session DB and
  is never injected into the user message / session history.
* Unsupported keys are logged with a one-line warning and ignored (never crash).
*
* TRUST IMPLICATION: a plugin that returns ``runtime_override`` gains
* prompt-routing authority — overriding ``base_url`` (plus optional
* ``api_key``) redirects every subsequent LLM call, carrying the entire
* session context, to an arbitrary endpoint.  Installing a plugin therefore
* grants it this power; only install plugins you trust.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Keys a plugin may override.  Anything else is logged and ignored.
#: ``system_prompt`` is intentionally NOT supported: it is the prompt-cache
#: prefix (byte-stable for the life of a conversation), so overriding it would
#: invalidate the cache and drop the core instructions. Model/provider routing
#: does not need it; persona switching needs a separate cache-safe design.
RUNTIME_OVERRIDE_KEYS = frozenset(
    {"model", "provider", "base_url", "api_key", "api_mode"}
)

#: All supported keys are plain non-empty strings.
_STRING_KEYS = frozenset(
    {"model", "provider", "base_url", "api_key", "api_mode"}
)

#: Known wire protocols.  Unknown api_mode values are rejected (logged+ignored)
#: instead of being forwarded into the call path.
KNOWN_API_MODES = frozenset(
    {"chat_completions", "anthropic_messages", "codex_responses", "bedrock_converse"}
)

#: Identity keys whose change makes the effective route different from the
#: pre-override route.  A route change refreshes provider-derived state the
#: same way ``switch_model`` / ``_try_activate_fallback`` do.
_ROUTE_KEYS = frozenset({"model", "provider", "base_url", "api_mode"})


def validate_runtime_override(overrides: Any) -> Dict[str, str]:
    """Type-check + filter a plugin-provided ``runtime_override`` dict.

    Returns only the supported, correctly-typed keys.  Unsupported keys and
    wrong-typed values are logged with a one-line warning and dropped, so a
    misbehaving plugin can never crash the turn.
    """
    if not isinstance(overrides, dict):
        logger.warning(
            "pre_llm_call runtime_override ignored: expected dict, got %s",
            type(overrides).__name__,
        )
        return {}
    valid: Dict[str, str] = {}
    for key, value in overrides.items():
        if key not in RUNTIME_OVERRIDE_KEYS:
            logger.warning(
                "pre_llm_call runtime_override: unsupported key %r ignored "
                "(supported: %s)",
                key,
                ", ".join(sorted(RUNTIME_OVERRIDE_KEYS)),
            )
            continue
        if key in _STRING_KEYS and (not isinstance(value, str) or not value.strip()):
            logger.warning(
                "pre_llm_call runtime_override: key %r must be a non-empty "
                "string, ignored",
                key,
            )
            continue
        if key == "api_mode" and value not in KNOWN_API_MODES:
            logger.warning(
                "pre_llm_call runtime_override: unsupported api_mode %r "
                "(known: %s), ignored",
                value,
                ", ".join(sorted(KNOWN_API_MODES)),
            )
            continue
        valid[key] = value.strip() if isinstance(value, str) else value
    return valid


def _refresh_derived_route_state(agent: Any, overrides: Dict[str, str]) -> None:
    """Refresh provider-derived state for the overridden route.

    Mirrors what the canonical route switch (``switch_model`` /
    ``_try_activate_fallback``) does when the active route changes: the
    switched-to provider's ``request_overrides`` (``extra_body``) replaces the
    previous provider's, and ``runtime_capabilities`` is re-resolved for the
    new model/endpoint.  Both are best-effort — a resolution failure must never
    crash the turn; the scope snapshot still restores the original values on
    exit.
    """
    try:
        from agent.agent_runtime_helpers import (
            _apply_switched_provider_request_overrides,
        )

        _apply_switched_provider_request_overrides(
            agent, str(overrides.get("provider") or agent.provider)
        )
    except Exception as _ro_exc:  # noqa: BLE001
        logger.debug(
            "runtime_override: request_overrides refresh failed (%s); "
            "keeping previous value for the scope",
            _ro_exc,
        )
    try:
        from agent.native_compaction import resolve_native_compaction_capabilities

        agent.runtime_capabilities = resolve_native_compaction_capabilities(
            model=getattr(agent, "model", "") or "",
            base_url=getattr(agent, "base_url", "") or "",
            provider=getattr(agent, "provider", "") or "",
            is_codex_backend=(
                (getattr(agent, "provider", "") or "").strip().lower()
                == "openai-codex"
            ),
        )
    except Exception as _cap_exc:  # noqa: BLE001
        logger.debug(
            "runtime_override: runtime_capabilities refresh failed (%s); "
            "keeping previous value for the scope",
            _cap_exc,
        )


class _RuntimeOverrideScope:
    """Context manager that temporarily applies an override to ``agent``.

    The override is an atomic route transaction, not a second, narrower route
    mutation primitive: it snapshots and restores every route-owned datum the
    canonical switch (``switch_model`` / ``_try_activate_fallback``) manages,
    so the effective route stays consistent through request construction,
    request middleware, ``pre_api_request``, wire execution, and response
    handling.

    On ``api_mode`` change the transport cache is invalidated (mirroring
    ``switch_model`` / ``agent_init``), so no transport warmed for the previous
    mode leaks into the overridden wire path.  ``base_url`` is a property whose
    setter refreshes ``_base_url_lower`` / ``_base_url_hostname``, so plain
    assignment keeps the derived host-matching state consistent.

    Precedence with the error-driven failover path: a proactive override owns
    only the primary attempt.  If ``_try_activate_fallback`` succeeds while the
    scope is open, the fallback supersedes the override.  Supersession is an
    EXPLICIT handoff, never inferred: the fallback call site invokes
    ``consume_runtime_override(agent)``, which finds this scope (registered as
    ``agent._active_runtime_override_scope`` in ``__enter__``) and calls
    ``supersede()``.  ``__exit__`` then sees the ``_superseded`` flag and skips
    the route-identity restore (which would clobber the freshly activated
    fallback); the caller has already cleared ``agent._runtime_override`` so
    retries stay on the fallback route.
    """

    _ATTRS = ("model", "provider", "api_mode", "api_key")
    _MISSING = object()
    # Route-owned derived state the wire path reads.  Snapshot/restore the
    # whole set unconditionally so untouched fields are no-ops and changed
    # fields revert atomically on exit.
    #
    # EDGE-2 (codex_responses): the codebase holds NO per-route Codex client
    # state to snapshot — there is no ``_codex_client`` / ``_codex_session_id``
    # attribute anywhere.  The only agent-held Codex state is ``_codex_session``
    # (the lazy app-server session used exclusively by the ``codex_app_server``
    # api_mode, which is a separate execution path — ``_run_codex_app_server_turn``
    # — that never enters this scope, and is not a valid override api_mode),
    # plus turn-scoped counters (``_codex_incomplete_retries``) and transient
    # per-call stream timestamps.  The ``codex_responses`` wire is fully covered
    # by the OpenAI-wire ``client`` + ``_transport_cache`` snapshots below.
    _DERIVED_ATTRS = (
        "requested_provider",
        "request_overrides",
        "client",
        "_anthropic_client",
        "_is_anthropic_oauth",
        "_reasoning_echo_flag",
        "runtime_capabilities",
        "_credential_pool",
        "_credential_pool_entry_id",
    )

    def __init__(self, agent: Any, overrides: Dict[str, str]) -> None:
        self.agent = agent
        self.overrides = overrides
        self._snapshot: Dict[str, Any] = {}
        self._client_kwargs_snapshot: Optional[Dict[str, Any]] = None
        self._transport_cache_snapshot: Optional[Dict[str, Any]] = None
        self._superseded = False

    def __enter__(self) -> "_RuntimeOverrideScope":
        agent = self.agent
        # ── Snapshot phase ─────────────────────────────────────────────
        # NOTE: agent._runtime_override is the canonical source.
        # TurnContext.runtime_override is derived from it at construction
        # time.  The call path in conversation_loop.py reads only the
        # agent attribute; keep that as the single point of truth.
        ov = self.overrides
        # EDGE-4: the merged override may pair an api_mode contributed by one
        # plugin with an api_key contributed by another (later hooks win per
        # key).  Cross-validate the pairing before anything is applied and
        # drop a provably mismatched credential so it never ships to the
        # wrong wire.  ``ov`` IS ``agent._runtime_override``, so the dropped
        # key does not linger in the canonical source either.
        _drop_mode_conflicting_api_key(ov)
        for name in self._ATTRS:
            if name in ov:
                self._snapshot[name] = getattr(agent, name, self._MISSING)
        if "base_url" in ov:
            self._snapshot["base_url"] = getattr(agent, "base_url", self._MISSING)
        for name in self._DERIVED_ATTRS:
            self._snapshot[name] = getattr(agent, name, self._MISSING)
        # request_overrides is replaced wholesale on activation, never mutated
        # in place — a shallow copy is enough to make the restore exact.
        if isinstance(self._snapshot.get("request_overrides"), dict):
            self._snapshot["request_overrides"] = dict(
                self._snapshot["request_overrides"]
            )
        # _client_kwargs feeds the per-request OpenAI-wire client.  Snapshot a
        # shallow copy so in-place mutation is reversible.
        ck = getattr(agent, "_client_kwargs", None)
        if isinstance(ck, dict):
            self._client_kwargs_snapshot = dict(ck)
            self._snapshot["_client_kwargs"] = ck
        for name, ov_key in (
            ("_anthropic_api_key", "api_key"),
            ("_anthropic_base_url", "base_url"),
        ):
            # Snapshot unconditionally (with _MISSING when absent): a route
            # whose pre-override mode never built an anthropic client has no
            # _anthropic_* attrs, and an override that switches the wire to
            # anthropic_messages must be able to create them (restore deletes
            # them again).
            if ov_key in ov:
                self._snapshot[name] = getattr(agent, name, self._MISSING)
        # Transport cache: snapshot the content so exit can restore the
        # pre-override cache instead of leaving an override-mode transport in
        # the agent's per-mode cache.
        _tc = getattr(agent, "_transport_cache", self._MISSING)
        if isinstance(_tc, dict):
            self._transport_cache_snapshot = dict(_tc)
            self._snapshot["_transport_cache"] = _tc

        # ── Activation phase ───────────────────────────────────────────
        for name in self._ATTRS:
            if name in ov:
                # Normalize before storing: the emptiness check below strips,
                # but the stored value must be stripped too or " gpt-5.6 "
                # flows into agent.model and onto the wire.
                val = str(ov[name]).strip()
                if not val:
                    logger.warning("runtime_override: empty value for %r ignored", name)
                    continue
                ov[name] = val
                setattr(agent, name, ov[name])
        if "base_url" in ov:
            agent.base_url = str(ov["base_url"]).strip().rstrip("/")
        if "provider" in ov:
            agent.requested_provider = ov["provider"]
        if "api_mode" in ov and isinstance(_tc, dict):
            # The overridden wire may need a different transport — drop the
            # eagerly-warmed cache so _get_transport() builds the mode's
            # transport fresh (mirrors switch_model / agent_init).
            _tc.clear()
        if _ROUTE_KEYS.intersection(ov):
            _refresh_derived_route_state(agent, ov)
        if "api_key" in ov and isinstance(ck, dict):
            ck["api_key"] = ov["api_key"]
        if "base_url" in ov and isinstance(ck, dict):
            ck["base_url"] = str(ov["base_url"]).strip().rstrip("/")
        if "_anthropic_api_key" in self._snapshot and "api_key" in ov:
            agent._anthropic_api_key = ov["api_key"]
        if "_anthropic_base_url" in self._snapshot and "base_url" in ov:
            agent._anthropic_base_url = str(ov["base_url"]).strip().rstrip("/")
        if "api_key" in ov:
            # A plugin-supplied key is a static bearer, never an OAuth flow.
            agent._is_anthropic_oauth = False

        # Register as the agent's active scope so the fallback handoff
        # (consume_runtime_override) can find and supersede this scope.
        # Outer-wins: the wire-time safety-net scope (Scope 2 in
        # conversation_loop.py) is created INSIDE this attempt scope and must
        # not steal the registration — the fallback sites all run outside the
        # inner scope, so superseding the outermost attempt scope is always
        # correct.  An inner scope therefore registers only when no scope is
        # registered, and unregisters only when it holds the registration.
        if getattr(agent, "_active_runtime_override_scope", None) is None:
            agent._active_runtime_override_scope = self
        return self

    def supersede(self) -> None:
        """Explicitly hand the route to the fallback chain.

        Marks the scope superseded so ``__exit__`` skips the route-identity
        restore (which would clobber the freshly activated fallback), and
        clears ``agent._runtime_override`` so no retry iteration re-applies
        the failed override.  Called by ``consume_runtime_override`` from the
        ``_try_activate_fallback`` success sites.
        """
        self._superseded = True
        self.agent._runtime_override = {}

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        agent = self.agent
        try:
            if self._superseded:
                # The fallback chain took ownership of the route mid-scope:
                # ``try_activate_fallback`` replaced every route-owned datum
                # (model/provider/base_url/api_mode/client/request_overrides/
                # runtime_capabilities/credential pool) and rebuilt
                # ``_client_kwargs`` and the transport cache for the fallback
                # route.  Restoring any of those here would clobber the
                # fallback — BUG-4: this is intentional, the fallback path
                # owns them now.  The ONE exception is ``_is_anthropic_oauth``:
                # the override unconditionally forced it False (static-key
                # assumption) and the fallback path re-derives it only for
                # anthropic_messages fallbacks, so the scope must undo that
                # forced datum or the agent's OAuth state stays False until a
                # later /model re-snapshot bakes it in permanently (LEAK-2).
                _oauth = self._snapshot.get("_is_anthropic_oauth", self._MISSING)
                if _oauth is self._MISSING:
                    try:
                        delattr(agent, "_is_anthropic_oauth")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    try:
                        agent._is_anthropic_oauth = _oauth
                    except Exception:  # noqa: BLE001
                        pass
                return
            for name, value in self._snapshot.items():
                if name in ("_client_kwargs", "_transport_cache"):
                    continue  # restored from the snapshots below
                if value is self._MISSING:
                    # Attribute did not exist before the override — don't
                    # fabricate it (tests build bare agents via __new__).
                    try:
                        delattr(agent, name)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                try:
                    setattr(agent, name, value)
                except Exception:  # noqa: BLE001 — restore must never raise
                    pass
            if self._client_kwargs_snapshot is not None:
                ck = getattr(agent, "_client_kwargs", None)
                if isinstance(ck, dict):
                    ck.clear()
                    ck.update(self._client_kwargs_snapshot)
            if self._transport_cache_snapshot is not None:
                tc = getattr(agent, "_transport_cache", None)
                if isinstance(tc, dict):
                    tc.clear()
                    tc.update(self._transport_cache_snapshot)
        finally:
            # Unregister on every exit path (normal restore AND superseded
            # skip), and only when this scope holds the registration.
            if getattr(agent, "_active_runtime_override_scope", None) is self:
                agent._active_runtime_override_scope = None


def _drop_mode_conflicting_api_key(overrides: Dict[str, str]) -> None:
    """Cross-validate ``api_key`` against ``api_mode`` in a MERGED override.

    Multiple plugins' ``runtime_override`` dicts are merged per-key (later
    hooks win), so the surviving ``api_mode`` may come from one plugin while
    the surviving ``api_key`` was contributed by another plugin whose route
    intent was the other wire family.  Shipping the mismatched credential
    sends, e.g., an OpenAI-style key to the Anthropic Messages wire (auth
    failure with a confusing error) or an ``sk-ant-*`` key to a
    chat_completions endpoint.

    Detection is deliberately narrow and acts ONLY on a provable family
    mismatch: ``sk-ant-*`` (Anthropic's prefix) on a non-anthropic wire, or a
    vendor-unambiguous OpenAI-family prefix (``sk-proj-`` / ``sk-svcacct-`` /
    ``sk-or-``) on the anthropic wire.  Generic ``sk-...`` keys and
    third-party anthropic-compatible endpoint keys (MiniMax, Bedrock, proxies
    — none follow Anthropic's prefix convention) carry no provable signal and
    are left untouched, so legitimate credentials are never dropped.  On
    mismatch the ``api_key`` is dropped (in place, since ``overrides`` is
    ``agent._runtime_override``) with a logged warning; the wire then uses the
    agent's own credential for that mode instead of a guaranteed-wrong one.
    """
    api_key = overrides.get("api_key")
    api_mode = overrides.get("api_mode")
    if not api_key or not api_mode:
        return
    mismatched = (
        api_mode != "anthropic_messages"
        and api_key.startswith("sk-ant-")
    ) or (
        api_mode == "anthropic_messages"
        and api_key.startswith(("sk-proj-", "sk-svcacct-", "sk-or-"))
    )
    if mismatched:
        logger.warning(
            "runtime_override: api_key %r… does not match api_mode %r "
            "(cross-plugin merge conflict); dropping the key so the wrong "
            "wire never receives it",
            api_key[:12], api_mode,
        )
        overrides.pop("api_key", None)


def apply_runtime_override(agent: Any, overrides: Dict[str, str]) -> "_RuntimeOverrideScope":
    """Return a context manager that applies ``overrides`` to ``agent``."""
    return _RuntimeOverrideScope(agent, overrides)


def consume_runtime_override(agent: Any) -> None:
    """Explicit supersede handoff: the fallback chain took ownership of the route.

    A proactive override owns only the primary attempt: once
    ``_try_activate_fallback`` succeeds, the fallback route supersedes it for
    the remainder of the logical request, so the next retry iteration must not
    re-enter the route that just failed.  Every ``_try_activate_fallback``
    success site in conversation_loop.py calls this on success.

    When an override scope is active (``agent._active_runtime_override_scope``),
    this marks it superseded and clears ``agent._runtime_override``; when no
    scope is active (exception-driven fallbacks run after the attempt's scope
    already restored the agent) it clears the turn-scoped override directly so
    the current request stays on the fallback route.  None-safe: a bare agent
    without the registration attribute never raises.
    """
    scope = getattr(agent, "_active_runtime_override_scope", None)
    if scope is not None:
        scope.supersede()
        return
    try:
        agent._runtime_override = {}
    except AttributeError:
        pass  # bare test agent without the attribute
