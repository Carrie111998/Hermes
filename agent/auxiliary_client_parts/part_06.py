

def _get_cached_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    base_url: str = None,
    api_key: str = None,
    api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
    task: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Get or create a cached client for the given provider.

    Async clients (AsyncOpenAI) use httpx.AsyncClient internally, which
    binds to the event loop that was current when the client was created.
    Using such a client on a *different* loop causes deadlocks or
    RuntimeError.  To prevent cross-loop issues, the cache validates on
    every async hit that the cached loop is the *current, open* loop.
    If the loop changed (e.g. a new gateway worker-thread loop), the stale
    entry is replaced in-place rather than creating an additional entry.

    This keeps cache size bounded to one entry per unique provider config,
    preventing the fd-exhaustion that previously occurred in long-running
    gateways where recycled worker threads created unbounded entries (#10200).
    """
    # Resolve the current event loop for async clients so we can validate
    # cached entries.  Loop identity is NOT in the cache key — instead we
    # check at hit time whether the cached loop is still current and open.
    # This prevents unbounded cache growth from recycled worker-thread loops
    # while still guaranteeing we never reuse a client on the wrong loop
    # (which causes deadlocks, see #2681).
    current_loop = None
    if async_mode:
        try:
            import asyncio as _aio
            current_loop = _aio.get_event_loop()
        except RuntimeError:
            pass
    runtime = _normalize_main_runtime(main_runtime)
    cache_key = _client_cache_key(
        provider,
        async_mode=async_mode,
        base_url=base_url,
        api_key=api_key,
        api_mode=api_mode,
        main_runtime=main_runtime,
        is_vision=is_vision,
        task=task,
        model=model,
    )
    with _client_cache_lock:
        if cache_key in _client_cache:
            cached_client, cached_default, cached_loop = _client_cache[cache_key]
            if async_mode:
                # Validate: the cached client must be bound to the CURRENT,
                # OPEN loop.  If the loop changed or was closed, the httpx
                # transport inside is dead — force-close and replace.
                loop_ok = (
                    cached_loop is not None
                    and cached_loop is current_loop
                    and not cached_loop.is_closed()
                )
                if loop_ok:
                    effective = _compat_model(cached_client, model, cached_default)
                    return cached_client, effective
                # Stale — evict and fall through to create a new client.
                # Only a client whose owner loop is closed may be awaited from
                # this thread; a live foreign loop remains force-neutered.
                owner_loop_closed = (
                    cached_loop is not None and cached_loop.is_closed()
                )
                _close_cached_client(cached_client, close_async=owner_loop_closed)
                del _client_cache[cache_key]
            else:
                effective = _compat_model(cached_client, model, cached_default)
                return cached_client, effective
    # Build outside the lock.
    # For pool-backed api_key providers, derive the active API key from the
    # pool entry rather than from env vars.  resolve_api_key_provider_credentials
    # always prefers env vars (first-entry bias), which bypasses pool rotation:
    # after key #1 is marked exhausted the retry would still get key #1 from
    # the env var and fail again, causing the retry2_err handler to mark key #2.
    effective_api_key = api_key
    if not effective_api_key:
        _pe = _peek_pool_entry(_normalize_aux_provider(provider))
        if _pe is not None:
            _pk = _pool_runtime_api_key(_pe)
            if _pk:
                effective_api_key = _pk
    client, default_model = resolve_provider_client(
        provider,
        model,
        async_mode,
        explicit_base_url=base_url,
        explicit_api_key=effective_api_key,
        api_mode=api_mode,
        main_runtime=runtime,
        is_vision=is_vision,
        task=task,
    )
    if client is not None:
        # For async clients, remember which loop they were created on so we
        # can detect stale entries later.
        bound_loop = current_loop
        with _client_cache_lock:
            if cache_key not in _client_cache:
                # Safety belt: if the cache has grown beyond the max, evict
                # the oldest entries (FIFO — dict preserves insertion order).
                # Do not close an evicted client here: another caller may be
                # mid-request with the object it obtained from this cache.
                # Dropping the cache reference lets normal refcount/GC cleanup
                # happen after in-flight users release it.
                while len(_client_cache) >= _CLIENT_CACHE_MAX_SIZE:
                    evict_key = next(iter(_client_cache))
                    del _client_cache[evict_key]
                _client_cache[cache_key] = (client, default_model, bound_loop)
            else:
                built_client = client
                client, default_model, _ = _client_cache[cache_key]
                # This concurrently built loser was never exposed to a caller,
                # so it is safe to close immediately.
                _close_cached_client(built_client, close_async=async_mode)
    return client, model or default_model


# Aliases that target direct REST APIs not modeled as first-class providers
# in PROVIDER_REGISTRY. Used for ``auxiliary.<task>.provider`` so users can
# write the obvious name and have it resolve to a working ``custom`` endpoint
# without needing to know our internal provider IDs.
#
# Why these specifically: PROVIDER_REGISTRY has ``openai-codex`` (OAuth) and
# ``custom`` (manual base_url + OPENAI_API_KEY) but no plain ``openai`` for
# direct API-key access. Users predictably type ``provider: openai`` and
# expect it to use OPENAI_API_KEY against api.openai.com. Previously this
# silently fell back to the user's main provider, sending OpenAI model names
# to e.g. DeepSeek and producing cryptic ``unknown variant 'image_url'``
# errors (issue #31179).
_AUX_DIRECT_API_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
}


def _resolve_task_provider_model(
    task: str = None,
    provider: str = None,
    model: str = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Determine provider + model for a call.

    Priority:
      1. Explicit provider/model/base_url/api_key args (always win)
      2. Config file (auxiliary.{task}.provider/model/base_url)
      3. "auto" (full auto-detection chain)

    Returns (provider, model, base_url, api_key, api_mode) where model may
    be None (use provider default). A bare base_url is treated as custom, but
    a first-class provider plus base_url keeps the provider identity so its
    auth, transport, and request-shaping behavior still apply. api_mode is one
    of "chat_completions", "codex_responses", or None (auto-detect).
    """
    cfg_provider = None
    cfg_model = None
    cfg_base_url = None
    cfg_api_key = None
    cfg_api_mode = None

    if task:
        task_config = _get_auxiliary_task_config(task)
        cfg_provider = str(task_config.get("provider", "")).strip() or None
        cfg_model = str(task_config.get("model", "")).strip() or None
        cfg_base_url = str(task_config.get("base_url", "")).strip() or None
        cfg_api_key = str(task_config.get("api_key", "")).strip() or None
        # Resolve key_env → env var when api_key is not set directly
        if not cfg_api_key:
            cfg_key_env = str(
                task_config.get("key_env") or task_config.get("api_key_env") or ""
            ).strip()
            if cfg_key_env:
                cfg_api_key = _scoped_key_env(cfg_key_env) or None
        cfg_api_mode = str(task_config.get("api_mode", "")).strip() or None

    # 'auto' is a sentinel meaning "inherit from main runtime / auto-detect", not
    # a literal model id. Without this, a config of `auxiliary.<task>.model: auto`
    # propagates the literal string "auto" to the wire, where the provider returns
    # a 200 OK with an error-text body (e.g. "the model 'auto' does not exist"),
    # which downstream consumers like ContextCompressor accept as the task output.
    # The provider-side 'auto' is handled in _resolve_auto() via main_runtime
    # fallback, so dropping cfg_model to None here lets that path do its job.
    #
    # The explicit `model` kwarg needs the identical normalization: MoA slots
    # (agent/moa_loop.py's _slot_runtime) forward a preset's `model:` field as
    # this explicit argument rather than through auxiliary.<task> config, so a
    # user-configured `model: auto` on a MoA reference/aggregator slot reaches
    # this function here, not as cfg_model. Only normalizing cfg_model let that
    # literal "auto" slip through via `model or cfg_model` below.
    if model and model.lower() == "auto":
        model = None
    if cfg_model and cfg_model.lower() == "auto":
        cfg_model = None

    resolved_model = model or cfg_model
    resolved_api_mode = cfg_api_mode

    # MoA virtual provider: an *explicit* `provider: moa` override (either the
    # caller-passed `provider` arg or `auxiliary.<task>.provider` in
    # config.yaml) reaches this function directly — it never goes through
    # _resolve_auto(), which only unwraps the *implicit* "main provider is
    # moa" case (#53827). Left as-is, "moa" is returned verbatim and
    # resolve_provider_client() looks it up in PROVIDER_REGISTRY (which has
    # no "moa" entry — it's not a real HTTP provider), falls to the
    # unknown-provider dead end, and call_llm surfaces a nonsensical
    # "MOA_API_KEY environment variable" error for a provider that was never
    # meant to be reached over the wire. Auxiliary tasks don't need the
    # reference fan-out — resolve to the preset's aggregator slot instead,
    # exactly like the implicit path does (shared helper: _resolve_moa_aggregator).
    def _unwrap_moa_provider(prov: str, mdl: Optional[str]) -> Tuple[str, Optional[str]]:
        if prov.strip().lower() != "moa":
            return prov, mdl
        agg_provider, agg_model = _resolve_moa_aggregator(mdl)
        if agg_provider and agg_model:
            return agg_provider, agg_model
        return prov, mdl

    if provider and str(provider).strip().lower() == "moa":
        provider, resolved_model = _unwrap_moa_provider(provider, resolved_model)
        # The moa:// virtual endpoint (if any explicit base_url/api_key was
        # passed alongside provider="moa") belongs to the facade, not the
        # aggregator's real provider — drop it so the aggregator resolves
        # through its own provider credentials, mirroring _resolve_auto().
        if provider and provider.lower() != "moa":
            base_url = None
            api_key = None
    elif cfg_provider and str(cfg_provider).strip().lower() == "moa":
        cfg_provider, cfg_model = _unwrap_moa_provider(cfg_provider, resolved_model)
        if cfg_provider and cfg_provider.lower() != "moa":
            resolved_model = cfg_model
            cfg_base_url = None
            cfg_api_key = None

    # Convenience aliases for direct API-key endpoints that aren't first-class
    # providers (e.g. ``provider: openai`` → custom + api.openai.com/v1).
    # Applied to both explicit args and config-derived values. When the user
    # has already supplied a base_url we keep their endpoint but still rewrite
    # the provider to ``custom`` so resolution doesn't hit the
    # PROVIDER_REGISTRY-only path (which has no ``openai`` entry).
    def _expand_direct_api_alias(prov: Optional[str], existing_base: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not prov:
            return prov, existing_base
        target_base = _AUX_DIRECT_API_BASE_URLS.get(prov.strip().lower())
        if target_base is None:
            return prov, existing_base
        return "custom", existing_base or target_base

    def _preserve_provider_with_base_url(prov: Optional[str]) -> bool:
        normalized = str(prov or "").strip().lower()
        if normalized in {"", "auto", "custom"} or normalized.startswith("custom:"):
            return False
        try:
            from hermes_cli.providers import get_provider

            return get_provider(normalized) is not None
        except Exception:
            # Keep the high-risk provider-backed routes safe even if provider
            # catalog loading is unavailable during early import/test paths.
            return normalized in {
                "anthropic",
                "copilot",
                "copilot-acp",
                "minimax-oauth",
                "nous",
                "openai-codex",
                "qwen-oauth",
                "xai-oauth",
            }

    if provider:
        provider, base_url = _expand_direct_api_alias(provider, base_url)
    if cfg_provider:
        cfg_provider, cfg_base_url = _expand_direct_api_alias(cfg_provider, cfg_base_url)

    # An explicit provider arg without an explicit base_url must not bypass
    # the task's configured endpoint: adopt auxiliary.<task>.base_url/api_key
    # when the config targets the same provider (or names none), so the
    # early `if provider:` return below carries the configured endpoint
    # instead of falling through to main-runtime resolution (#58515).
    # An explicit "auto" is excluded — it means "inherit / auto-detect" and
    # must keep flowing through the existing auto-resolution chain.
    if provider and provider != "auto" and not base_url and cfg_base_url and cfg_provider in (None, provider):
        base_url = cfg_base_url
        if not api_key:
            api_key = cfg_api_key

    if base_url and _preserve_provider_with_base_url(provider):
        return provider, resolved_model, base_url, api_key, resolved_api_mode
    if base_url:
        return "custom", resolved_model, base_url, api_key, resolved_api_mode
    if provider:
        return provider, resolved_model, base_url, api_key, resolved_api_mode

    if task:
        # Config.yaml is the primary source for per-task overrides.
        if cfg_base_url and cfg_api_key:
            # Both base_url and api_key explicitly set → custom endpoint.
            return "custom", resolved_model, cfg_base_url, cfg_api_key, resolved_api_mode
        if cfg_base_url and cfg_provider and cfg_provider != "auto":
            # base_url set without api_key but with a known provider — use
            # the provider so it can resolve credentials from env vars
            # (e.g. OPENROUTER_API_KEY) instead of locking into "custom".
            return cfg_provider, resolved_model, cfg_base_url, None, resolved_api_mode
        if cfg_provider and cfg_provider != "auto":
            return cfg_provider, resolved_model, cfg_base_url, cfg_api_key, resolved_api_mode

        return "auto", resolved_model, None, None, resolved_api_mode

    return "auto", resolved_model, None, None, resolved_api_mode


_DEFAULT_AUX_TIMEOUT = 30.0

# Compression summarises large conversation histories; a reasoning auxiliary
# model (e.g. Codex / GPT-5.5) can legitimately take longer than the default
# ``auxiliary.compression.timeout`` (120 s), causing the stream to time out and
# the compressor to fall back to the deterministic context marker (#54915).
# This is a bounded *floor* applied only to config-derived compression timeouts
# — it does not affect other auxiliary tasks and does not override an explicit
# per-call ``timeout=``.  A floor is harmless for fast compression models
# (they finish before the deadline) and is a minimum, so a higher config value
# is kept unchanged.
_COMPRESSION_TIMEOUT_FLOOR_SECONDS = 300.0


def _get_auxiliary_task_config(task: str) -> Dict[str, Any]:
    """Return the config dict for auxiliary.<task>, or {} when unavailable.

    For plugin-registered auxiliary tasks (see
    :meth:`hermes_cli.plugins.PluginContext.register_auxiliary_task`) the
    plugin's declared *defaults* are layered underneath the user's config
    so an unconfigured plugin task still works:

        plugin defaults  ←  config.yaml auxiliary.<task>  (user wins)

    Built-in tasks ignore this path (their defaults live in DEFAULT_CONFIG).
    """
    if not task:
        return {}
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except ImportError:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get(task, {}) if isinstance(aux, dict) else {}
    if not isinstance(task_config, dict):
        task_config = {}

    # Layer plugin-declared defaults underneath user config so
    # ctx.register_auxiliary_task(defaults={...}) takes effect without
    # forcing the user to write config.yaml entries.
    try:
        from hermes_cli.plugins import get_plugin_auxiliary_tasks
        for _entry in get_plugin_auxiliary_tasks():
            if _entry.get("key") == task:
                _defaults = _entry.get("defaults") or {}
                if isinstance(_defaults, dict):
                    merged = dict(_defaults)
                    merged.update(task_config)
                    return merged
                break
    except Exception:
        # Plugin discovery failure must not break aux task config reads.
        pass

    return task_config


class CompressionFastLane(NamedTuple):
    """Explicit, non-reasoning compression route safe for a bounded summary."""

    certified_non_reasoning: bool
    max_tokens: Optional[int]
    reasoning_config: Optional[Dict[str, Any]]


def _fast_lane_config_fields(
    config: Dict[str, Any],
) -> tuple[str, str, bool, Optional[int]]:
    """Extract the fast-lane certification fields from one task config.

    Returns ``(provider, model, non_reasoning, cap)``:

    - ``provider``/``model``: normalized (stripped; provider lowercased).
    - ``non_reasoning``: True only when ``reasoning_effort`` EXPLICITLY
      disables thinking. Delegates to ``parse_reasoning_effort`` so every
      spelling users can write in config.yaml (``none``, ``false``,
      ``disabled``, YAML boolean ``false``) certifies identically —
      ``_get_task_extra_body`` already uses the same parser to disable
      reasoning, and the two predicates must not disagree. Empty/unset
      (provider default) is NOT non-reasoning.
    - ``cap``: positive int from ``max_output_tokens``, else None.
      Booleans are config drift, never a cap (``int(True) == 1``).
    """
    from hermes_constants import parse_reasoning_effort

    provider = str(config.get("provider") or "").strip().lower()
    model = str(config.get("model") or "").strip()
    parsed_effort = parse_reasoning_effort(config.get("reasoning_effort"))
    non_reasoning = parsed_effort is not None and parsed_effort.get("enabled") is False
    raw_cap = config.get("max_output_tokens")
    try:
        cap = 0 if isinstance(raw_cap, bool) else int(raw_cap or 0)
    except (TypeError, ValueError):
        cap = 0
    return provider, model, non_reasoning, (cap if cap > 0 else None)


def resolve_compression_fast_lane(
    actual_provider: str,
    actual_model: Optional[str],
    *,
    requested_provider: Optional[str] = None,
    requested_model: Optional[str] = None,
    route_config: Optional[Dict[str, Any]] = None,
) -> CompressionFastLane:
    """Certify the opt-in fast lane against one already-resolved route.

    A cap is safe only when the operator has selected a concrete auxiliary
    provider/model, explicitly certified it as non-reasoning, and that exact
    route is the one Hermes will call. A requested model covers a compressor
    summary-model override. Auto/inherited and drifted routes stay uncapped.
    """
    config = (
        route_config
        if route_config is not None
        else _get_auxiliary_task_config("compression")
    )
    cfg_provider, cfg_model, non_reasoning, cap = _fast_lane_config_fields(config)
    provider = str(requested_provider or "").strip().lower() or cfg_provider
    model = str(requested_model or "").strip() or cfg_model
    explicit_route = provider not in {"", "auto"} and model.lower() not in {"", "auto"}
    provider_matches = _normalize_aux_provider(
        _fallback_provider_from_label(str(actual_provider or ""))
    ) == _normalize_aux_provider(provider)
    model_matches = str(actual_model or "").strip().lower() == model.lower()
    certified = explicit_route and provider_matches and model_matches and non_reasoning
    if not certified:
        return CompressionFastLane(False, None, None)
    return CompressionFastLane(
        True,
        cap,
        {"enabled": False, "effort": "none"},
    )


def _compression_config_claims_fast_lane(config: Dict[str, Any]) -> bool:
    """Whether task config declares fast-only controls that cannot leak."""
    provider, model, non_reasoning, cap = _fast_lane_config_fields(config)
    return (
        provider not in {"", "auto"}
        and model.lower() not in {"", "auto"}
        and non_reasoning
        and cap is not None
    )


def _compression_fast_lane_controls(
    task: str | None,
    *,
    actual_provider: str,
    actual_model: str | None,
    requested_provider: str | None,
    requested_model: str | None,
    route_config: Dict[str, Any],
    leak_guard_config: Dict[str, Any],
    max_tokens: int | None,
    extra_body: Dict[str, Any],
) -> tuple[int | None, Dict[str, Any]]:
    """Apply the certified compression controls to one resolved route."""
    if task != "compression" or max_tokens is not None:
        return max_tokens, extra_body
    body = dict(extra_body)
    lane = resolve_compression_fast_lane(
        actual_provider,
        actual_model,
        requested_provider=requested_provider,
        requested_model=requested_model,
        route_config=route_config,
    )
    if lane.reasoning_config is not None:
        if "reasoning" not in body:
            body["reasoning"] = lane.reasoning_config
    elif _compression_config_claims_fast_lane(leak_guard_config):
        body.pop("reasoning", None)
    return lane.max_tokens, body


def _get_task_timeout(task: str, default: float = _DEFAULT_AUX_TIMEOUT) -> float:
    """Read timeout from auxiliary.{task}.timeout in config, falling back to *default*."""
    if not task:
        return default
    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("timeout")
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return default


def _effective_aux_timeout(task: str, timeout: Optional[float]) -> float:
    """Resolve the effective timeout for an auxiliary LLM call.

    Uses the caller-provided ``timeout`` when given; otherwise reads
    ``auxiliary.{task}.timeout`` from config via :func:`_get_task_timeout`.
    For the ``compression`` task only, applies a bounded floor so a reasoning
    model summarising a large context is not cut off by the default timeout
    (#54915).  The floor is intentionally skipped when the caller passes an
    explicit ``timeout=`` — explicit per-call deadlines are always honoured —
    and it is a minimum (``max``), so a config value already above it is kept.
    """
    effective = timeout if timeout is not None else _get_task_timeout(task)
    if timeout is None and task == "compression":
        effective = max(effective, _COMPRESSION_TIMEOUT_FLOOR_SECONDS)
    return effective


def _get_task_extra_body(task: str) -> Dict[str, Any]:
    """Read auxiliary.<task>.extra_body and return a shallow copy when valid.

    Also folds in ``auxiliary.<task>.reasoning_effort`` as an
    ``extra_body.reasoning`` config dict ({"enabled": ..., "effort": ...})
    when set. An explicit ``extra_body.reasoning`` in config wins over the
    ``reasoning_effort`` shorthand (it is the more specific wire control).
    Downstream, each wire already translates ``extra_body.reasoning``:
    chat.completions passes it through, the Codex Responses adapter maps it
    to top-level ``reasoning``/``include``, and the Anthropic auxiliary
    client maps it to ``build_anthropic_kwargs(reasoning_config=...)``.

    MoA tasks are excluded by design: reasoning depth for MoA is a per-slot
    setting in the MoA preset (``moa.presets.<name>.reference_models[].
    reasoning_effort`` / ``aggregator.reasoning_effort``), not an
    auxiliary-task knob — an ensemble-wide value would override the
    per-slot ones.
    """
    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("extra_body")
    result = dict(raw) if isinstance(raw, dict) else {}
    if "reasoning" not in result:
        effort = task_config.get("reasoning_effort")
        if effort is not None and effort != "":
            if task in ("moa_reference", "moa_aggregator"):
                logger.warning(
                    "auxiliary.%s.reasoning_effort is not supported — MoA "
                    "reasoning depth is per-slot: set reasoning_effort on the "
                    "preset's reference_models entries / aggregator instead "
                    "(moa.presets.<name>...). Ignoring.",
                    task,
                )
                return result
            from hermes_constants import parse_reasoning_effort
            parsed = parse_reasoning_effort(effort)
            if parsed is not None:
                result["reasoning"] = parsed
            else:
                logger.warning(
                    "auxiliary.%s.reasoning_effort %r is not a valid level "
                    "(none, minimal, low, medium, high, xhigh, max, ultra) — ignoring",
                    task, effort,
                )
    return result


# ---------------------------------------------------------------------------
# Per-task concurrency limiting (#23324)
# ---------------------------------------------------------------------------
# Background auxiliary work (title generation, context compression, etc.) can
# spawn unbounded concurrent LLM calls when many sessions are active. During
# provider incidents each call also retries / fans out across the fallback
# chain, multiplying request volume on already-degraded endpoints. A per-task
# semaphore caps in-flight calls so retry amplification stays bounded.

_aux_sync_semaphores: Dict[str, Tuple[int, threading.BoundedSemaphore]] = {}
_aux_async_semaphores: Dict[Tuple[str, int], Tuple[int, Any]] = {}
_aux_sem_lock = threading.Lock()


def _get_task_max_concurrency(task: Optional[str]) -> Optional[int]:
    """Return ``auxiliary.<task>.max_concurrency`` as a positive int, or None."""
    if not task or task == "vision":
        # Vision already uses this key for its encode/resize CPU worker pool;
        # its LLM calls deliberately remain concurrent.
        return None
    raw = _get_auxiliary_task_config(task).get("max_concurrency")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _acquire_sync_aux_semaphore(task: Optional[str]) -> Optional[threading.BoundedSemaphore]:
    """Get a per-task sync semaphore, rebuilding it after a config change."""
    limit = _get_task_max_concurrency(task)
    if limit is None:
        return None
    with _aux_sem_lock:
        entry = _aux_sync_semaphores.get(task)
        if entry is None or entry[0] != limit:
            semaphore = threading.BoundedSemaphore(limit)
            _aux_sync_semaphores[task] = (limit, semaphore)
            return semaphore
        return entry[1]


def _acquire_async_aux_semaphore(task: Optional[str]):
    """Get a per-task, per-event-loop async semaphore after config lookup."""
    limit = _get_task_max_concurrency(task)
    if limit is None:
        return None
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    key = (task, id(loop))
    with _aux_sem_lock:
        entry = _aux_async_semaphores.get(key)
        if entry is None or entry[0] != limit:
            semaphore = asyncio.Semaphore(limit)
            _aux_async_semaphores[key] = (limit, semaphore)
            return semaphore
        return entry[1]


def _reset_aux_semaphores() -> None:
    """Drop cached semaphores (test helper)."""
    with _aux_sem_lock:
        _aux_sync_semaphores.clear()
        _aux_async_semaphores.clear()


# ---------------------------------------------------------------------------
# Anthropic-compatible endpoint detection + image block conversion
# ---------------------------------------------------------------------------

# Providers that use Anthropic-compatible endpoints (via OpenAI SDK wrapper).
# Their image content blocks must use Anthropic format, not OpenAI format.
_ANTHROPIC_COMPAT_PROVIDERS = frozenset({"minimax", "minimax-oauth", "minimax-cn"})


def _is_anthropic_compat_endpoint(provider: str, base_url: str) -> bool:
    """Detect if an endpoint expects Anthropic-format content blocks.

    Returns True for known Anthropic-compatible providers (MiniMax) and
    any endpoint whose URL contains ``/anthropic`` in the path.
    """
    if provider in _ANTHROPIC_COMPAT_PROVIDERS:
        return True
    url_lower = (base_url or "").lower()
    return "/anthropic" in url_lower


def _convert_openai_images_to_anthropic(messages: list) -> list:
    """Convert OpenAI ``image_url``/``video_url`` blocks to Anthropic format.

    Converts:
    - ``image_url`` blocks to Anthropic ``image`` blocks
    - ``video_url`` blocks to Anthropic ``video`` blocks (MiniMax M3 compat)

    Only touches messages that have list-type content with ``image_url`` or
    ``video_url`` blocks; plain text messages pass through unchanged.
    """
    converted = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            converted.append(msg)
            continue
        new_content = []
        changed = False
        for block in content:
            if block.get("type") == "image_url":
                image_url_val = (block.get("image_url") or {}).get("url", "")
                if image_url_val.startswith("data:"):
                    # Parse data URI: data:<media_type>;base64,<data>
                    header, _, b64data = image_url_val.partition(",")
                    media_type = "image/png"
                    if ":" in header and ";" in header:
                        media_type = header.split(":", 1)[1].split(";", 1)[0]
                    new_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64data,
                        },
                    })
                else:
                    # URL-based image
                    new_content.append({
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": image_url_val,
                        },
                    })
                changed = True
            elif block.get("type") == "video_url":
                # MiniMax's Anthropic-compatible endpoint expects a "video"
                # block (not OpenAI's "video_url", and not "input_video").
                # See https://platform.minimax.io/docs/api-reference/text-anthropic-api
                # — the Messages-field table lists type="video" (M3 only,
                # URL/base64/mm_file://). The source shape mirrors the "image"
                # block: base64 → {type:"base64", media_type, data}, URL →
                # {type:"url", url}.
                video_url_val = (block.get("video_url") or {}).get("url", "")
                if video_url_val.startswith("data:"):
                    # Parse data URI: data:<media_type>;base64,<data>
                    header, _, b64data = video_url_val.partition(",")
                    media_type = "video/mp4"
                    if ":" in header and ";" in header:
                        media_type = header.split(":", 1)[1].split(";", 1)[0]
                    new_content.append({
                        "type": "video",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64data,
                        },
                    })
                else:
                    # URL-based video
                    new_content.append({
                        "type": "video",
                        "source": {
                            "type": "url",
                            "url": video_url_val,
                        },
                    })
                changed = True
            else:
                new_content.append(block)
        converted.append({**msg, "content": new_content} if changed else msg)
    return converted


_PROFILE_REASONING_KEYS = {
    "reasoning",
    "reasoning_effort",
    "thinking",
    "thinking_config",
    "thinkingconfig",
    "thinking_budget",
    "thinkingbudget",
    "enable_thinking",
    "think",
    "verbosity",
}


def _contains_profile_reasoning_fields(value: Any) -> bool:
    """Return whether a profile payload contains a reasoning wire control."""
    if not isinstance(value, dict):
        return False
    for key, nested in value.items():
        normalized = str(key).strip().lower()
        if normalized in _PROFILE_REASONING_KEYS:
            return True
        if _contains_profile_reasoning_fields(nested):
            return True
    return False


def _build_call_kwargs(
    provider: str,
    model: str,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list] = None,
    timeout: float = 30.0,
    extra_body: Optional[dict] = None,
    reasoning_config: Optional[dict] = None,
    base_url: Optional[str] = None,
    task: Optional[str] = None,
) -> dict:
    """Build kwargs for .chat.completions.create() with model/provider adjustments."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "timeout": timeout,
    }

    fixed_temperature = _fixed_temperature_for_model(model, base_url)
    if fixed_temperature is OMIT_TEMPERATURE:
        temperature = None  # strip — let server choose
    elif fixed_temperature is not None:
        temperature = fixed_temperature

    # Opus 4.7+ rejects any non-default temperature/top_p/top_k — silently
    # drop here so auxiliary callers that hardcode temperature (e.g. 0 on
    # structured-JSON extraction) don't 400 the moment
    # the aux model is flipped to 4.7.
    if temperature is not None:
        from agent.anthropic_adapter import _forbids_sampling_params
        if _forbids_sampling_params(model):
            temperature = None

    if temperature is not None:
        kwargs["temperature"] = temperature

    if max_tokens is not None:
        # We do NOT cap output by default. Most chat-completions providers treat
        # an omitted max_tokens as "use the model's max output", which is what we
        # want for auxiliary tasks (compression summaries, titles, vision, etc.) —
        # an explicit cap only risks truncating a summary or 400-ing on providers
        # that reject the parameter outright (e.g. GitHub Copilot / newer OpenAI
        # GPT-5 models require max_completion_tokens, not max_tokens; ZAI vision
        # models reject it entirely with error 1210). Omitting it sidesteps all of
        # those wire-format quirks at once.
        #
        # The one exception is the Anthropic Messages wire (MiniMax and any
        # ``/anthropic`` endpoint reached through the OpenAI SDK wrapper), where
        # max_tokens is a MANDATORY field — omitting it is a hard 400. Keep it only
        # there.
        #
        # NVIDIA NIM (integrate.api.nvidia.com and local NIM endpoints) is a
        # second exception: some models—notably minimaxai/minimax-m3—return HTTP
        # 200 with an empty choices[] payload when max_tokens is omitted. The main
        # NVIDIA chat path already sends an output cap via the provider profile;
        # preserve it on the auxiliary path too.
        _effective_base = base_url or (
            _current_custom_base_url() if provider == "custom" else ""
        )
        _provider_norm = str(provider or "").strip().lower()
        _is_nvidia_nim = (
            _provider_norm in {"nvidia", "nvidia-nim", "nim", "build-nvidia", "nemotron"}
            or base_url_host_matches(_effective_base, "integrate.api.nvidia.com")
        )
        _is_moa = bool(task) and str(task) == "moa_reference"
        # Gemini's native generateContent maps max_tokens → maxOutputTokens and,
        # when it is omitted, applies a fixed 65,535-token ceiling rather than
        # "the model's full budget" (see gemini_native_adapter.build_gemini_request).
        # So an explicit cap is both safe and the ONLY way to honor it here —
        # dropping max_tokens silently makes MoA's reference_max_tokens a no-op
        # for gemini advisors (they run effectively uncapped).
        _is_gemini_native = _provider_norm in {
            "gemini", "google", "google-gemini", "google-ai-studio",
        }
        if not _is_gemini_native and _effective_base:
            try:
                from agent.gemini_native_adapter import is_native_gemini_base_url
                _is_gemini_native = is_native_gemini_base_url(_effective_base)
            except Exception:
                pass
        _nous_on_messages = False
        if _provider_norm in {"nous", "nous-portal", "nousresearch"}:
            from hermes_cli.providers import nous_api_mode

            _nous_on_messages = nous_api_mode(model) == "anthropic_messages"
        if (
            _is_anthropic_compat_endpoint(provider, _effective_base)
            or _nous_on_messages
            or _is_nvidia_nim
            or _is_moa
            or _is_gemini_native
        ):
            # Use auxiliary_max_tokens_param() so models that require
            # max_completion_tokens (GPT-5 family, Copilot) get the right
            # parameter name instead of a hardcoded max_tokens that 400s.
            kwargs.update(auxiliary_max_tokens_param(max_tokens, model=model))

    if tools:
        # Defensive dedup: providers like Google Vertex, Azure, and Bedrock
        # reject requests with duplicate tool names (HTTP 400).  The upstream
        # injection paths (run_agent.py) already dedup, but this guard
        # converts a hard API failure into a warning if an upstream regression
        # reintroduces duplicates.  See: #18478
        _seen: set = set()
        _deduped: list = []
        for _t in tools:
            _tname = (_t.get("function") or {}).get("name", "")
            if _tname and _tname in _seen:
                logger.warning(
                    "_build_call_kwargs: duplicate tool name '%s' removed "
                    "(provider=%s model=%s)",
                    _tname, provider, model,
                )
                continue
            if _tname:
                _seen.add(_tname)
            _deduped.append(_t)
        kwargs["tools"] = _deduped

    # Build provider-aware reasoning kwargs through the same profile hooks used
    # by the standard chat-completions transport. Some providers require
    # top-level controls (Kimi/custom ``reasoning_effort``), others use nested
    # body fields (Gemini ``thinking_config``), and OpenRouter/Nous use
    # ``extra_body.reasoning``. Profiles are the source of truth for those wire
    # shapes. Providers without a reasoning-aware profile retain the generic
    # ``extra_body.reasoning`` fallback used by Codex-compatible adapters.
    effective_base = base_url or (
        _current_custom_base_url() if provider == "custom" else ""
    )
    profile_body: Dict[str, Any] = {}
    profile_reasoning_extra: Dict[str, Any] = {}
    profile_top_level: Dict[str, Any] = {}
    profile_handles_reasoning = False
    try:
        from providers import get_provider_profile
        from providers.base import ProviderProfile

        profile = get_provider_profile(str(provider or "").strip().lower())
        if profile is not None:
            profile_body = profile.build_extra_body(
                model=model,
                base_url=effective_base,
                reasoning_config=reasoning_config,
            ) or {}
            profile_reasoning_extra, profile_top_level = (
                profile.build_api_kwargs_extras(
                    reasoning_config=reasoning_config,
                    supports_reasoning=reasoning_config is not None,
                    model=model,
                    base_url=effective_base,
                )
            )
            profile_reasoning_extra = profile_reasoning_extra or {}
            profile_top_level = profile_top_level or {}
            profile_handles_reasoning = (
                type(profile).build_api_kwargs_extras
                is not ProviderProfile.build_api_kwargs_extras
                or _contains_profile_reasoning_fields(profile_body)
                or _contains_profile_reasoning_fields(profile_reasoning_extra)
                or _contains_profile_reasoning_fields(profile_top_level)
            )
    except Exception as exc:
        logger.debug(
            "_build_call_kwargs: provider profile projection failed for %s: %s",
            provider,
            exc,
        )

    kwargs.update(profile_top_level)
    merged_extra = dict(extra_body or {})
    merged_extra.update(profile_body)
    merged_extra.update(profile_reasoning_extra)
    if (
        reasoning_config
        and isinstance(reasoning_config, dict)
        and not profile_handles_reasoning
    ):
        if reasoning_config.get("enabled") is False:
            merged_extra["reasoning"] = {"enabled": False}
        else:
            effort = reasoning_config.get("effort") or "medium"
            merged_extra["reasoning"] = {"enabled": True, "effort": effort}
    # Portal product tags + sticky session_id. The provider profile usually
    # supplies both; this fallback covers profile-load failures and alias
    # spellings the profile lookup might miss. session_id keeps aux
    # compression/title/vision calls on the same upstream instance as the
    # main turn (cache warmth) — tags alone are not enough on /v1/messages.
    _provider_for_portal = str(provider or "").strip().lower()
    if _provider_for_portal in {"nous", "nous-portal", "nousresearch"}:
        if "tags" not in merged_extra:
            merged_extra["tags"] = _nous_portal_tags()
        if "session_id" not in merged_extra:
            try:
                from agent.portal_tags import get_conversation_context

                sticky_key = get_conversation_context()
            except Exception:
                sticky_key = None
            if sticky_key:
                merged_extra["session_id"] = sticky_key
    if merged_extra:
        kwargs["extra_body"] = merged_extra

    # Anthropic Messages adapters translate Hermes reasoning into native
    # ``thinking`` via a private kwarg (and strip OpenAI-shaped
    # ``extra_body.reasoning``). Do not expose this private kwarg to ordinary
    # OpenAI-compatible SDK clients, which would reject it. Portal Claude is
    # dual-wire — include it when the catalog id selects /v1/messages.
    if reasoning_config and isinstance(reasoning_config, dict):
        provider_norm = str(provider or "").strip().lower()
        effective_base = base_url or ""
        _nous_on_messages = False
        if provider_norm in {"nous", "nous-portal", "nousresearch"}:
            from hermes_cli.providers import nous_api_mode

            _nous_on_messages = nous_api_mode(model) == "anthropic_messages"
        if (
            provider_norm == "anthropic"
            or _nous_on_messages
            or _endpoint_speaks_anthropic_messages(effective_base)
            or _is_anthropic_compat_endpoint(provider_norm, effective_base)
        ):
            kwargs["_reasoning_config"] = dict(reasoning_config)

    return kwargs


def _validate_llm_response(
    response: Any,
    task: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Any:
    """Validate that an LLM response has the expected .choices[0].message shape.

    Fails fast with a clear error instead of letting malformed payloads
    propagate to downstream consumers where they crash with misleading
    AttributeError (e.g. "'str' object has no attribute 'choices'").

    See #7264.

    Also the single accounting chokepoint for auxiliary usage: every
    successful non-streaming aux response passes through here exactly once,
    so token usage is recorded against the ambient session context published
    by the agent loop (``agent.aux_accounting``, issue #23270). Recording is
    best-effort and never affects validation. *provider*/*base_url* are
    optional accounting hints — fallback-path calls omit them and the row
    keeps the model (read from the response itself) with an empty route.
    """
    if response is None:
        raise RuntimeError(
            f"Auxiliary {task or 'call'}: LLM returned None response"
        )
    from agent.aux_accounting import record_aux_usage
    record_aux_usage(response, task, provider=provider, base_url=base_url)
    # Allow SimpleNamespace responses from adapters (CodexAuxiliaryClient,
    # AnthropicAuxiliaryClient) — they have .choices[0].message.
    try:
        choices = response.choices
        if not choices or not hasattr(choices[0], "message"):
            raise AttributeError("missing choices[0].message")
    except (AttributeError, TypeError, IndexError) as exc:
        recovered = _recover_aux_response_message(response)
        if recovered is not None:
            _record_relay_auxiliary_response_model(response)
            _complete_relay_auxiliary_call()
            return recovered
        response_type = type(response).__name__
        response_preview = str(response)[:120]
        raise RuntimeError(
            f"Auxiliary {task or 'call'}: LLM returned invalid response "
            f"(type={response_type}): {response_preview!r}. "
            f"Expected object with .choices[0].message — check provider "
            f"adapter or custom endpoint compatibility."
        ) from exc
    _record_relay_auxiliary_response_model(response)
    _complete_relay_auxiliary_call()
    return response


def _complete_relay_auxiliary_call(*, outcome: str = "success") -> None:
    """Close one auxiliary logical call after acceptance or terminal failure."""
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    from agent import relay_llm

    relay_llm.complete_logical_call(
        str(context.get("request_id") or ""),
        outcome=outcome,
        model_name=str(context.get("model") or "unknown"),
        provider_name=str(context.get("provider") or "auxiliary"),
        response_model_name=context.get("response_model"),
    )


def _record_relay_auxiliary_response_model(response: Any) -> None:
    """Retain the provider-reported model for terminal route attribution."""
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    if isinstance(response, dict):
        model = response.get("model")
    else:
        model = getattr(response, "model", None)
    if isinstance(model, str) and model.strip():
        context["response_model"] = model


def _fail_relay_auxiliary_call() -> None:
    """Close a terminally failed call without replacing its original error."""
    try:
        _complete_relay_auxiliary_call(outcome="failed")
    except Exception:
        logger.warning(
            "Relay auxiliary failure finalization failed",
            exc_info=True,
        )


def _recover_aux_response_message(response: Any) -> Optional[Any]:
    """Synthesize chat-completions shape from Responses-style text fields.

    Auxiliary callers consume ``choices[0].message``.  Some compatible
    endpoints return text outside ``choices`` (for example ``output_text`` or
    ``output`` items).  Preserve that response before declaring it malformed.
    """
    text = _extract_aux_response_text(response)
    if not text:
        return None

    choice = SimpleNamespace(
        message=SimpleNamespace(content=text),
        finish_reason=getattr(response, "finish_reason", None) or "stop",
    )
    try:
        response.choices = [choice]
        return response
    except Exception:
        return SimpleNamespace(
            id=getattr(response, "id", ""),
            model=getattr(response, "model", ""),
            object=getattr(response, "object", "chat.completion"),
            choices=[choice],
            usage=getattr(response, "usage", None),
        )


def _extract_aux_response_text(response: Any) -> str:
    output_text = _obj_get(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = _obj_get(response, "output")
    if not isinstance(output, list):
        return ""

    parts: List[str] = []
    for item in output:
        item_type = _obj_get(item, "type")
        if item_type and item_type != "message":
            continue
        for part in (_obj_get(item, "content") or []):
            part_type = _obj_get(part, "type")
            if part_type in {"output_text", "text", None}:
                text = _obj_get(part, "text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    value = getattr(obj, key, default)
    if value is default and isinstance(obj, dict):
        value = obj.get(key, default)
    return value


# ── Streamed aggregation for progress-hooked auxiliary calls ─────────────
# When a forward-progress hook is installed (aux_progress_hook — today only
# by context compression), the primary chat.completions attempt is upgraded
# to a streamed request that is aggregated back into a complete response.
# Two effects, both deliberate:
#   1. The configured ``timeout`` becomes an INTER-CHUNK idle timeout instead
#      of a total budget (httpx applies the read timeout per stream read), so
#      a slow-but-generating summary model is never killed mid-generation
#      while tokens are moving — only a genuinely silent connection dies.
#   2. Every arriving chunk ticks the progress hook, letting outer watchdogs
#      (gateway session hygiene) extend their deadlines on liveness instead
#      of guessing with a fixed wall clock.
# A total ceiling still bounds the pathological 1-token-per-idle-window
# stream; see _aux_stream_total_ceiling().

_AUX_STREAM_CEILING_FLOOR_SECONDS = 600.0
_AUX_STREAM_CEILING_MULTIPLIER = 4.0


def _aux_stream_total_ceiling(effective_timeout: Optional[float]) -> float:
    """Absolute wall-clock bound for a progress-hooked streamed aux call.

    Generous by design — the idle timeout is the real guard; this only stops
    a degenerate stream that trickles one token per idle window forever.
    """
    try:
        timeout = float(effective_timeout) if effective_timeout is not None else 0.0
    except (TypeError, ValueError):
        timeout = 0.0
    return max(_AUX_STREAM_CEILING_FLOOR_SECONDS,
               _AUX_STREAM_CEILING_MULTIPLIER * timeout)


def _client_streams_internally(client: Any) -> bool:
    """Wire adapters that consume a stream inside .create() already tick the
    progress hook themselves (Codex per SSE event, Anthropic per stream
    event); Bedrock's Converse shim cannot stream at all. None of them
    accept chat-completions ``stream=True`` semantics from us."""
    return isinstance(client, (
        CodexAuxiliaryClient,
        AnthropicAuxiliaryClient,
        BedrockAuxiliaryClient,
    ))


def _is_streaming_rejected_error(exc: Exception) -> bool:
    """Provider explicitly refused a streamed chat.completions request."""
    err = str(exc).lower()
    if "stream_options" in err:
        return True
    return "stream" in err and (
        "not supported" in err
        or "unsupported" in err
        or "not allowed" in err
        or "disabled" in err
    )


def _provider_requires_stream(provider: str, base_url: Optional[str]) -> bool:
    """Detect providers that only accept streaming (non-stream = HTTP 400).

    Some OpenAI-compatible endpoints reject non-streaming chat requests
    outright — e.g. Tencent Copilot returns
    ``{"code": 11101, "msg": "Non-stream chat request is currently not
    supported"}``. The main conversation loop already streams, so interactive
    chat works; auxiliary tasks (title generation, compression, web extract)
    used the non-streaming path and failed on every call. When this returns
    True the auxiliary client sends ``stream=True`` and aggregates the chunks
    itself (see :func:`_aggregate_chat_stream`). Credit @kudi88 (PR #60686).

    Beyond the known-host list, users can mark ANY custom endpoint as
    stream-only via ``auxiliary.stream_only_base_urls`` in config.yaml
    (list of substrings matched against the endpoint URL).
    """
    _url = str(base_url or "").lower()
    if not _url:
        return False
    # Tencent Copilot — "Non-stream chat request is currently not supported"
    if base_url_host_matches(_url, "copilot.tencent.com"):
        return True
    try:
        from hermes_cli.config import load_config
        aux_cfg = (load_config() or {}).get("auxiliary", {})
        markers = aux_cfg.get("stream_only_base_urls") or []
        if isinstance(markers, (list, tuple)):
            for marker in markers:
                if isinstance(marker, str) and marker.strip() and marker.strip().lower() in _url:
                    return True
    except Exception:
        # Config read is best-effort; never break an aux call over it.
        pass
    return False


def _create_with_progress(
    client: Any,
    kwargs: Dict[str, Any],
    task: Optional[str] = None,
    *,
    force_stream: bool = False,
) -> Any:
    """chat.completions.create() that streams when a progress hook is active
    or the provider only accepts streamed requests.

    Behavior is byte-for-byte identical to a plain ``create(**kwargs)`` when
    neither trigger applies (every existing caller/task) or when the client's
    wire adapter streams internally. With a hook + a chunk-capable client,
    the request is sent with ``stream=True`` and aggregated, ticking the hook
    only for substantive chunks. The configured ``timeout`` acts per stream
    read (idle) rather than as a total budget, and outer liveness watchdogs see
    tokens moving. ``force_stream=True`` (stream-only providers such as Tencent
    Copilot — credit @kudi88, PR #60686) takes the same streamed path even
    without a hook. Providers that reject the streamed request fall back to
    the plain non-streaming call — except under ``force_stream``, where a
    stream-only provider rejects the plain call by definition, so the
    original error is surfaced to the normal recovery chains instead.
    """
    _notify_aux_dispatch()
    _notify_aux_progress()  # Preserve the watchdog's historical dispatch tick.
    if (not _aux_progress_active() and not force_stream) or _client_streams_internally(client):
        response = client.chat.completions.create(**kwargs)
        if not _client_streams_internally(client):
            _notify_aux_provider_response()
        return response

    total_ceiling = _aux_stream_total_ceiling(kwargs.get("timeout"))
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    stream_kwargs["stream_options"] = {"include_usage": True}
    try:
        chunks = client.chat.completions.create(**stream_kwargs)
    except Exception as exc:
        # Genuine provider failures (auth, credit, rate limit, network) are
        # not streaming's fault — surface them unchanged so the existing
        # recovery chains (credential refresh, pool rotation, provider
        # fallback) see the same error they would on a plain call.
        if (
            force_stream
            or _is_transient_transport_error(exc)
            or _is_auth_error(exc)
            or _is_payment_error(exc)
            or _is_rate_limit_error(exc)
        ):
            raise
        # Anything else may be a streaming-specific rejection (explicit
        # "stream not supported", stream_options 400, or an idiosyncratic
        # 4xx). Retry non-streaming once; if the request itself is bad the
        # plain call reproduces the real error for the normal except-chains.
        logger.debug(
            "Auxiliary %s: streamed request failed (%s); retrying "
            "non-streaming", task or "call", exc,
        )
        _notify_aux_dispatch()
        response = client.chat.completions.create(**kwargs)
        _notify_aux_provider_response()
        return response

    # Some shims (MoA virtual provider under quiet mode, defensive adapters)
    # return a complete response even when stream=True was requested. A
    # complete response object carries the full summary payload, so it counts
    # as provider response progress (TTFP) and forward progress alike.
    if hasattr(chunks, "choices"):
        _notify_aux_provider_response()
        return chunks
    return _aggregate_chat_stream(
        chunks, model=str(kwargs.get("model") or ""), total_ceiling=total_ceiling,
    )


def _aggregate_chat_stream(
    chunks: Any,
    *,
    model: str = "",
    total_ceiling: Optional[float] = None,
) -> Any:
    """Consume a chat.completions chunk stream into a complete response.

    Ticks the thread-local aux progress hook only for non-empty content,
    reasoning, or tool-call fragments. Raises
    TimeoutError when *total_ceiling* seconds elapse before the stream
    finishes — phrased with "timed out" so existing timeout classification
    (``_is_timeout_error``) treats it exactly like a request timeout.
    Accumulation is shared with the async mirror via
    :class:`_ChatStreamAccumulator`.
    """
    acc = _ChatStreamAccumulator(model=model, total_ceiling=total_ceiling)
    try:
        for chunk in chunks:
            acc.feed(chunk)
    finally:
        close_fn = getattr(chunks, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    return acc.finish()


class _ChatStreamAccumulator:
    """Shared per-chunk accumulation for sync and async stream aggregation.

    Mirrors :func:`_aggregate_chat_stream`'s chunk handling so the async
    consumer below cannot drift from the sync one (same content/reasoning/
    tool-call delta reassembly, same "timed out" ceiling phrasing).
    """

    def __init__(self, model: str = "", total_ceiling: Optional[float] = None):
        self._started = time.monotonic()
        self._total_ceiling = total_ceiling
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls_acc: Dict[int, Dict[str, Any]] = {}
        self.finish_reason = None
        self.usage = None
        self.resp_id = ""
        self.resp_model = model or ""

    def feed(self, chunk: Any) -> None:
        # Every provider frame records transport-level timing (TTFP
        # telemetry, first-frame-wins); only a substantive payload below
        # ticks the forward-progress hook that keeps compression alive.
        _notify_aux_timing_response()
        made_progress = False
        if (
            self._total_ceiling is not None
            and (time.monotonic() - self._started) >= self._total_ceiling
        ):
            raise TimeoutError(
                f"Auxiliary streamed call timed out after {self._total_ceiling:.0f}s "
                "total ceiling (stream still open but over budget)"
            )
        self.resp_id = getattr(chunk, "id", None) or self.resp_id
        self.resp_model = getattr(chunk, "model", None) or self.resp_model
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage:
            self.usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None) or self.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        piece = getattr(delta, "content", None)
        if piece:
            self.content_parts.append(piece)
            made_progress = True
        reasoning_piece = (
            getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
        )
        if reasoning_piece and isinstance(reasoning_piece, str):
            self.reasoning_parts.append(reasoning_piece)
            made_progress = True
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc, "index", 0) or 0
            acc = self.tool_calls_acc.setdefault(
                idx, {"id": "", "name": "", "arguments": []}
            )
            tool_fragment = False
            if getattr(tc, "id", None):
                acc["id"] = tc.id
                tool_fragment = True
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    acc["name"] = fn.name
                    tool_fragment = True
                if getattr(fn, "arguments", None):
                    acc["arguments"].append(fn.arguments)
                    tool_fragment = True
            made_progress = made_progress or tool_fragment

        if made_progress:
            _notify_aux_progress()

    def finish(self) -> Any:
        tool_calls = None
        if self.tool_calls_acc:
            tool_calls = [
                SimpleNamespace(
                    id=acc["id"],
                    type="function",
                    function=SimpleNamespace(
                        name=acc["name"],
                        arguments="".join(acc["arguments"]),
                    ),
                )
                for _idx, acc in sorted(self.tool_calls_acc.items())
            ]
        message = SimpleNamespace(
            role="assistant",
            content="".join(self.content_parts),
            tool_calls=tool_calls,
            reasoning="".join(self.reasoning_parts) or None,
        )
        choice = SimpleNamespace(
            index=0,
            message=message,
            finish_reason=self.finish_reason or "stop",
        )
        return SimpleNamespace(
            id=self.resp_id,
            model=self.resp_model,
            object="chat.completion",
            choices=[choice],
            usage=self.usage,
        )


async def _aggregate_chat_stream_async(
    chunks: Any,
    *,
    model: str = "",
    total_ceiling: Optional[float] = None,
) -> Any:
    """Async mirror of :func:`_aggregate_chat_stream` (``async for`` consumer).

    The AsyncOpenAI stream contract is an async iterator — consuming it with
    the sync helper raises. Same accumulation and ceiling semantics via
    :class:`_ChatStreamAccumulator`.
    """
    acc = _ChatStreamAccumulator(model=model, total_ceiling=total_ceiling)
    try:
        async for chunk in chunks:
            acc.feed(chunk)
    finally:
        close_fn = getattr(chunks, "close", None) or getattr(chunks, "aclose", None)
        if callable(close_fn):
            try:
                result = close_fn()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
    return acc.finish()


async def _acreate_with_stream(
    client: Any,
    kwargs: Dict[str, Any],
    task: Optional[str] = None,
) -> Any:
    """Async chat.completions.create() for stream-only providers.

    Sends ``stream=True`` and aggregates the async chunk stream into a
    complete response (credit @kudi88, PR #60686 — async contract fixed to
    ``async for`` and tool-call deltas preserved per sweeper review).
    """
    total_ceiling = _aux_stream_total_ceiling(kwargs.get("timeout"))
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    stream_kwargs["stream_options"] = {"include_usage": True}
    chunks = await client.chat.completions.create(**stream_kwargs)
    # Defensive: shims may hand back a complete response despite stream=True.
    if hasattr(chunks, "choices"):
        return chunks
    return await _aggregate_chat_stream_async(
        chunks, model=str(kwargs.get("model") or ""), total_ceiling=total_ceiling,
    )


@_relay_auxiliary_call
def call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
    reasoning_config: Optional[dict] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    api_mode: str = None,
    stream: bool = False,
    stream_options: dict = None,
    route_info: Optional[Dict[str, str]] = None,
    latency_info: Optional[Dict[str, int]] = None,
) -> Any:
    """Run an auxiliary LLM request, applying the configured task limit."""
    queue_started_at = time.monotonic()
    semaphore = _acquire_sync_aux_semaphore(task)
    if semaphore is not None:
        semaphore.acquire()
    request_started_at = time.monotonic()
    if latency_info is not None:
        latency_info["queue_wait_ms"] = max(
            0, int((request_started_at - queue_started_at) * 1000)
        )
    prior_progress_hook = getattr(_aux_progress, "hook", None)

    def _timed_response() -> None:
        if latency_info is not None and "time_to_first_progress_ms" not in latency_info:
            latency_info["time_to_first_progress_ms"] = max(
                0, int((time.monotonic() - request_started_at) * 1000)
            )

    def _timed_dispatch() -> None:
        if latency_info is not None and "provider_dispatch_ms" not in latency_info:
            latency_info["provider_dispatch_ms"] = max(
                0, int((time.monotonic() - request_started_at) * 1000)
            )

    try:
        with (
            aux_progress_hook(
                prior_progress_hook
                if callable(prior_progress_hook)
                else ((lambda: None) if latency_info is not None else None)
            ),
            _aux_timing_hook(_aux_dispatch, _timed_dispatch),
            _aux_timing_hook(_aux_provider_response, _timed_response),
        ):
            response = _call_llm_impl(
                task=task,
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                main_runtime=main_runtime,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                timeout=timeout,
                extra_body=extra_body,
                reasoning_config=reasoning_config,
                extra_headers=extra_headers,
                api_mode=api_mode,
                stream=stream,
                stream_options=stream_options,
                route_info=route_info,
            )
        if stream and semaphore is not None:
            stream_semaphore = semaphore
            semaphore = None
            return _release_sync_semaphore_after_stream(response, stream_semaphore)
        return response
    finally:
        if latency_info is not None:
            latency_info["summary_generation_ms"] = max(
                0, int((time.monotonic() - request_started_at) * 1000)
            )
        if semaphore is not None:
            semaphore.release()


def _release_sync_semaphore_after_stream(
    stream: Any, semaphore: threading.BoundedSemaphore,
):
    """Release a permit only after a streaming response is consumed or closed."""
    try:
        yield from stream
    finally:
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        finally:
            semaphore.release()
