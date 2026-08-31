

def resolve_provider_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    raw_codex: bool = False,
    explicit_base_url: str = None,
    explicit_api_key: str = None,
    api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
    task: Optional[str] = None,
    allow_provider_fallback: bool = True,
) -> Tuple[Optional[Any], Optional[str]]:
    """Central router: given a provider name and optional model, return a
    configured client with the correct auth, base URL, and API format.

    The returned client always exposes ``.chat.completions.create()`` — for
    Codex/Responses API providers, an adapter handles the translation
    transparently.

    Args:
        provider: Provider identifier.  One of:
            "openrouter", "nous", "openai-codex" (or "codex"),
            "zai", "kimi-coding", "minimax", "minimax-cn",
            "custom" (OPENAI_BASE_URL + OPENAI_API_KEY),
            "auto" (full auto-detection chain).
        model: Model slug override.  If None, uses the provider's default
               auxiliary model.
        async_mode: If True, return an async-compatible client.
        raw_codex: If True, return a raw OpenAI client for Codex providers
            instead of wrapping in CodexAuxiliaryClient.  Use this when
            the caller needs direct access to responses.stream() (e.g.,
            the main agent loop).
        explicit_base_url: Optional direct OpenAI-compatible endpoint.
        explicit_api_key: Optional API key paired with explicit_base_url.
        allow_provider_fallback: If False, an explicit endpoint may use only
            explicit_api_key; it must not borrow ambient/provider-wide keys.
        api_mode: API mode override.  One of "chat_completions",
            "codex_responses", or None (auto-detect).  When set to
            "codex_responses", the client is wrapped in
            CodexAuxiliaryClient to route through the Responses API.

    Returns:
        (client, resolved_model) or (None, None) if auth is unavailable.
    """
    _validate_proxy_env_urls()
    # Preserve the original provider name before alias normalization so a
    # user-declared ``custom_providers`` entry whose name coincidentally
    # matches a built-in alias (e.g. user names their custom provider "kimi"
    # which aliases to "kimi-coding") is still reachable via the named-custom
    # branch below.
    original_provider = (provider or "").strip().lower()
    # Normalise aliases
    provider = _normalize_aux_provider(provider)

    # MoA virtual provider chokepoint: "moa" is not a real HTTP provider —
    # its acting model is the preset's aggregator slot. The two resolver
    # layers above (_resolve_auto, _resolve_task_provider_model) already
    # unwrap their own paths, but callers that route here directly (vision
    # auto-detect, _try_main_agent_model_fallback, get_available_vision_backends,
    # plugin code) would otherwise dead-end in the unknown-provider branch.
    # ``model`` carries the preset name for moa calls; when the preset can't
    # be resolved we leave the call untouched and let the normal
    # missing-provider handling produce its diagnostic.
    if provider == "moa":
        _agg_provider, _agg_model = _resolve_moa_aggregator(model)
        if _agg_provider and _agg_model:
            original_provider = _agg_provider.strip().lower()
            provider = _normalize_aux_provider(_agg_provider)
            model = _agg_model
            # The moa:// facade endpoint and placeholder key belong to the
            # virtual runtime, not the aggregator's real provider.
            if explicit_base_url and str(explicit_base_url).lower().startswith("moa://"):
                explicit_base_url = None
                explicit_api_key = None

    # Universal model-resolution fallback for concrete providers. ``auto`` is
    # intentionally excluded: `_resolve_auto(main_runtime=...)` returns the
    # model paired with the provider it actually selected. Pre-filling an auto
    # call from `_read_main_model()` can leak a stale process-global runtime
    # into a different provider (for example Claude model slug on Codex OAuth)
    # and override that correctly resolved model.
    #
    # Concrete provider resolution order:
    #
    #   1. ``model`` argument (caller knew what they wanted)
    #   2. Provider's catalog default — cheap/fast model the provider
    #      registered via ``ProviderProfile.default_aux_model`` or the
    #      legacy ``_API_KEY_PROVIDER_AUX_MODELS_FALLBACK`` dict.  Empty
    #      string for OAuth-gated providers (openai-codex, xai-oauth)
    #      whose accepted-model lists drift on the backend, so we don't
    #      pin a default that can silently rot.
    #   3. User's main model from ``model.model`` in config.yaml.  This is
    #      the load-bearing step for OAuth providers: an xai-oauth user
    #      with grok-4.3 configured gets grok-4.3 for title generation
    #      instead of silently dropping to whatever Step-2 fallback (#31845).
    #      When the main provider is MoA, ``_read_main_model_for_aux()``
    #      substitutes the preset's aggregator model — the preset NAME is
    #      never a valid wire model id, so unset aux models default to the
    #      preset's acting model instead.
    #
    # Each provider branch below sees a non-empty ``model`` whenever the
    # user has *anything* configured — no provider-specific empty-model
    # guards needed.  When the user has NOTHING configured (fresh install,
    # main_model also empty), the branches still hit their own
    # missing-credentials returns and ``_resolve_auto`` falls through to
    # the Step-2 chain as before.
    #
    # Prefer explicit caller model, then provider-scoped aux model, then main model.
    # Do NOT pre-fill a blank ``auto`` request from the config/main default here.
    # ``auto`` has its own main-runtime resolver below; pre-filling first can pair
    # a stale configured model with a live fallback provider (e.g. Claude model
    # sent to Codex after the main lane fell back to gpt-5.5). Let _resolve_auto()
    # return the actual current runtime model when the caller did not explicitly
    # request one. (# compression-current-model)
    #
    # Nous + vision is the one carve-out: the branch below resolves its model
    # from the Portal's tier-aware vision recommendation (``_try_nous(vision=
    # True)``), and ``final_model = model or default`` means anything pre-filled
    # here wins over that. The main chat model is routinely text-only (e.g. a
    # ``:free`` chat SKU), so pre-filling it sends the image to a model that
    # cannot accept one and the Portal 404s. Leave ``model`` unset and let the
    # Portal slot through; only an explicit caller model may override it.
    _nous_portal_vision = provider == "nous" and is_vision
    if not model and provider != "auto" and not _nous_portal_vision:
        model = _get_aux_model_for_provider(provider) or _read_main_model_for_aux() or model

    def _needs_codex_wrap(client_obj, base_url_str: str, model_str: str) -> bool:
        """Decide if a plain OpenAI client should be wrapped for Responses API.

        Returns True when api_mode is explicitly "codex_responses", or when
        auto-detection (api.openai.com + codex-family model) suggests it.
        Already-wrapped clients (CodexAuxiliaryClient) are skipped.
        """
        if isinstance(client_obj, CodexAuxiliaryClient):
            return False
        if raw_codex:
            return False
        if provider == "actual":
            return True
        if api_mode == "codex_responses":
            return True
        # Auto-detect: api.openai.com + codex model name pattern
        if api_mode and api_mode != "codex_responses":
            return False  # explicit non-codex mode
        if base_url_hostname(base_url_str) == "api.openai.com":
            model_lower = (model_str or "").lower()
            if "codex" in model_lower:
                return True
        return False

    def _wrap_if_needed(client_obj, final_model_str: str, base_url_str: str = "",
                        api_key_str: str = ""):
        """Wrap a plain OpenAI client in the correct transport adapter.

        Handles two cases:
        - ``CodexAuxiliaryClient`` when the endpoint needs the Responses API
          (explicit ``api_mode=codex_responses`` or api.openai.com + codex
          model name).
        - ``AnthropicAuxiliaryClient`` when the endpoint speaks Anthropic
          Messages (explicit ``api_mode=anthropic_messages``, any ``/anthropic``
          suffix, ``api.kimi.com/coding``, or ``api.anthropic.com``).

        Clients that are already specialized wrappers pass through unchanged.
        """
        if _needs_codex_wrap(client_obj, base_url_str, final_model_str):
            logger.debug(
                "resolve_provider_client: wrapping client in CodexAuxiliaryClient "
                "(api_mode=%s, model=%s, base_url=%s)",
                api_mode or "auto-detected", final_model_str,
                base_url_str[:60] if base_url_str else "")
            return CodexAuxiliaryClient(client_obj, final_model_str)
        # Anthropic-wire endpoints: rewrap plain OpenAI clients so
        # chat.completions.create() is translated to /v1/messages.
        return _maybe_wrap_anthropic(
            client_obj, final_model_str, api_key_str, base_url_str, api_mode,
        )

    # ── Auto: try all providers in priority order ────────────────────
    if provider == "auto":
        client, resolved, effective_provider = _resolve_auto_route(
            main_runtime=main_runtime,
            task=task,
        )
        if client is None:
            return None, None
        # When auto-detection lands on a non-OpenRouter provider (e.g. a
        # local server), an OpenRouter-formatted model override like
        # "google/gemini-3-flash-preview" won't work.  Drop it and use
        # the provider's own default model instead.
        if model and "/" in model and resolved and "/" not in resolved:
            logger.debug(
                "Dropping OpenRouter-format model %r for non-OpenRouter "
                "auxiliary provider (using %r instead)", model, resolved)
            model = None
        final_model = model or resolved
        routed_client, routed_model = (
            _to_async_client(client, final_model, is_vision=is_vision)
            if async_mode else (client, final_model)
        )
        _tag_effective_provider(routed_client, effective_provider)
        return routed_client, routed_model

    # ── OpenRouter ───────────────────────────────────────────
    if provider == "openrouter":
        client, default = _try_openrouter(
            explicit_api_key=explicit_api_key,
            model=model,
        )
        if client is None:
            logger.warning(
                "resolve_provider_client: openrouter requested but %s",
                _describe_openrouter_unavailable(model=model),
            )
            return None, None
        final_model = _normalize_resolved_model(model or default, provider)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    # ── Nous Portal (OAuth) ──────────────────────────────────────────
    if provider == "nous":
        # Detect vision tasks: caller flag (strict vision backend), explicit
        # model override from _PROVIDER_VISION_MODELS, or a known vision id.
        _is_vision = (
            is_vision
            or model in _PROVIDER_VISION_MODELS.values()
            or (model or "").strip().lower() == "mimo-v2-omni"
        )
        client, default = _try_nous(vision=_is_vision)
        if client is None:
            logger.warning("resolve_provider_client: nous requested "
                           "but Nous Portal not configured (run: hermes auth)")
            return None, None
        final_model = _normalize_resolved_model(model or default, provider)
        # Dual-wire: anthropic/* → /v1/messages, everything else stays on
        # /chat/completions. Derive from the catalog id (not a stale
        # api_mode=chat_completions) so aux matches the main agent.
        from hermes_cli.providers import nous_api_mode

        portal_mode = nous_api_mode(final_model)
        api_key_str = str(getattr(client, "api_key", "") or "")
        base_url_str = str(getattr(client, "base_url", "") or "")
        client = _maybe_wrap_anthropic(
            client, final_model, api_key_str, base_url_str, portal_mode,
        )
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    # ── OpenAI Codex (OAuth → Responses API) ─────────────────────────
    if provider == "openai-codex":
        if not model:
            logger.warning(
                "resolve_provider_client: openai-codex requested without a "
                "model; pass model explicitly (e.g. model.model in config.yaml "
                "or auxiliary.<task>.model for per-task aux routing)."
            )
            return None, None
        if raw_codex:
            # Return the raw OpenAI client for callers that need direct
            # access to responses.stream() (e.g., the main agent loop).
            codex_token = _read_codex_access_token()
            if not codex_token:
                logger.warning("resolve_provider_client: openai-codex requested "
                               "but no Codex OAuth token found (run: hermes model)")
                return None, None
            final_model = _normalize_resolved_model(model, provider)
            raw_client = _create_openai_client(
                api_key=codex_token,
                base_url=_CODEX_AUX_BASE_URL,
                default_headers=_codex_cloudflare_headers(codex_token),
            )
            return (raw_client, final_model)
        # Standard path: wrap in CodexAuxiliaryClient adapter
        client, default = _build_codex_client(model)
        if client is None:
            logger.warning("resolve_provider_client: openai-codex requested "
                           "but no Codex OAuth token found (run: hermes model)")
            return None, None
        final_model = _normalize_resolved_model(model or default, provider)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    # ── xAI Grok OAuth (device code → Responses API) ───────────────
    # Without this branch, an xai-oauth main provider falls through to the
    # generic ``oauth_external`` arm below and returns ``(None, None)``,
    # silently re-routing every auxiliary task (compression, web extract,
    # session search, curator, etc.) to whatever Step-2 fallback the user
    # has configured.  Users on xAI Grok OAuth would then see surprise
    # OpenRouter / Nous bills for side tasks they thought were running on
    # their xAI subscription.
    if provider == "xai-oauth":
        client, default = _build_xai_oauth_aux_client(model)
        if client is None:
            logger.warning(
                "resolve_provider_client: xai-oauth requested but no xAI "
                "OAuth token found (run: hermes model -> xAI Grok OAuth — SuperGrok / Premium+)"
            )
            return None, None
        final_model = _normalize_resolved_model(model or default, provider)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    # ── Custom endpoint (OPENAI_BASE_URL + OPENAI_API_KEY) ───────────
    if provider == "custom":
        custom_base = ""
        custom_key = ""
        # Base passed to _wrap_if_needed for the Anthropic-wrap decision.  It
        # normally equals custom_base, but anthropic_messages talks to the
        # /anthropic surface directly, so it must keep the raw /anthropic base
        # while the plain OpenAI client (created from custom_base below, and the
        # OpenAI-wire fallback taken when the anthropic SDK is unavailable) still
        # uses the /v1-rewritten base so it never lands on
        # /anthropic/chat/completions.  Empty means "use custom_base". See #16254.
        wrap_base = ""
        if explicit_base_url:
            custom_base = _to_openai_base_url(explicit_base_url).strip()
            if api_mode == "anthropic_messages":
                wrap_base = (explicit_base_url or "").strip().rstrip("/")
            if explicit_api_key:
                custom_key = explicit_api_key.strip()
            elif allow_provider_fallback:
                custom_key = (
                    _scoped_key_env("OPENAI_API_KEY")
                    or _read_main_api_key_if_same_host(custom_base)
                    or "no-key-required"  # local servers don't need auth
                )
            else:
                # An entry-local credential source was declared but did not
                # resolve. Never substitute the process/profile-wide key or
                # an inferred same-host main credential for that entry.
                logger.debug(
                    "resolve_provider_client: explicit custom endpoint has "
                    "no usable entry-local credential"
                )
                return None, None
            if not custom_base:
                logger.warning(
                    "resolve_provider_client: explicit custom endpoint requested "
                    "but base_url is empty"
                )
                return None, None
        elif main_runtime:
            # When main_runtime carries a concrete base_url + api_key for a
            # named custom provider (custom:<name>), use it directly instead
            # of re-resolving from the bare "custom" provider name.
            # Re-resolution loses the provider name and falls back to
            # OpenRouter or a wrong API-key provider — the main agent already
            # solved this, we just need to reuse its answer. (#45472)
            _main_base = str(main_runtime.get("base_url") or "").strip().rstrip("/")
            _main_key = str(main_runtime.get("api_key") or "").strip()
            if _main_base and _main_key:
                custom_base = _main_base
                custom_key = _main_key
        if custom_base and custom_key:
            final_model = _normalize_resolved_model(
                model or (main_runtime.get("model") if main_runtime else None) or "gpt-4o-mini",
                provider,
            )
            extra = {}
            _clean_base, _dq = _extract_url_query_params(custom_base)
            if _dq:
                extra["default_query"] = _dq
            if base_url_host_matches(custom_base, "api.kimi.com"):
                extra["default_headers"] = {"User-Agent": "claude-code/0.1.0"}
            elif base_url_host_matches(custom_base, "githubcopilot.com"):
                from hermes_cli.copilot_auth import copilot_request_headers
                extra["default_headers"] = copilot_request_headers(
                    is_agent_turn=True, is_vision=is_vision
                )
            elif base_url_host_matches(custom_base, "integrate.api.nvidia.com"):
                extra["default_headers"] = build_nvidia_nim_headers(custom_base)
            else:
                # Fall back to profile.default_headers for providers that
                # declare client-level attribution headers on their profile.
                try:
                    from providers import get_provider_profile as _gpf_custom
                    _ph_custom = _gpf_custom(provider)
                    if _ph_custom and _ph_custom.default_headers:
                        extra["default_headers"] = dict(_ph_custom.default_headers)
                except Exception:
                    pass
            _merged_custom = _apply_user_default_headers(extra.get("default_headers"))
            if _merged_custom:
                extra["default_headers"] = _merged_custom
            client = _create_openai_client(api_key=custom_key, base_url=_clean_base, **extra)
            client = _wrap_if_needed(client, final_model, wrap_base or custom_base, custom_key)
            return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                    else (client, final_model))
        # Try custom first, then API-key providers (Codex excluded here:
        # falling through to Codex with no model is a stale-constant trap).
        for try_fn in (_try_custom_endpoint, _resolve_api_key_provider):
            client, default = try_fn()
            if client is not None:
                final_model = _normalize_resolved_model(model or default, provider)
                _cbase = str(getattr(client, "base_url", "") or "")
                # ``client.api_key`` may be a callable (Azure Foundry Entra
                # bearer provider). Pass empty string for the wrapper-detection
                # path — wrapping decisions are based on base_url + api_mode.
                _raw_ckey = getattr(client, "api_key", "")
                _ckey = "" if (callable(_raw_ckey) and not isinstance(_raw_ckey, str)) else str(_raw_ckey or "")
                client = _wrap_if_needed(client, final_model, _cbase, _ckey)
                return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                        else (client, final_model))
        logger.warning("resolve_provider_client: custom/main requested "
                       "but no endpoint credentials found")
        return None, None

    # ── Named custom providers (config.yaml providers dict / custom_providers list) ───
    try:
        from hermes_cli.runtime_provider import _get_named_custom_provider
        # When the raw requested name is an alias (``kimi`` → ``kimi-coding``)
        # and the user defined a ``custom_providers`` entry under that alias
        # name, the custom entry is the intended target — the built-in alias
        # rewriting would otherwise hijack the request.  Only preferred when
        # the raw name is an alias (not a canonical provider name) so custom
        # entries that coincidentally match a canonical provider (e.g. ``nous``)
        # still defer to the built-in per `_get_named_custom_provider`'s guard.
        custom_entry = None
        if original_provider and original_provider != provider:
            custom_entry = _get_named_custom_provider(original_provider)
        if custom_entry is None:
            custom_entry = _get_named_custom_provider(provider)
        if custom_entry:
            custom_base = (custom_entry.get("base_url") or "").strip()
            custom_key = (custom_entry.get("api_key") or "").strip()
            custom_key_env = (custom_entry.get("key_env") or custom_entry.get("api_key_env") or "").strip()
            if not custom_key and custom_key_env:
                custom_key = _scoped_key_env(custom_key_env)
            # Auxiliary tasks resolve named custom providers here rather than
            # through _resolve_named_custom_runtime, so key_cmd has to be
            # honoured on both paths at matching precedence: otherwise the main
            # agent turn works while every auxiliary call (title generation,
            # compression, vision, embedding) 401s on the placeholder below.
            custom_key_cmd = str(custom_entry.get("key_cmd", "") or "").strip()
            if custom_key_cmd:
                from agent.command_token_source import build_command_token_provider
                custom_key = build_command_token_provider(
                    custom_key_cmd, custom_entry.get("name") or provider
                ) or custom_key
            custom_key = custom_key or "no-key-required"
            if custom_key == "no-key-required":
                logger.warning(
                    "resolve_provider_client: named custom provider %r has no resolvable "
                    "api_key — request will be sent with placeholder no-key-required "
                    "and will 401 on auth-required endpoints",
                    custom_entry.get("name") or provider,
                )
            # An explicit per-task api_mode override (from _resolve_task_provider_model)
            # wins; otherwise fall back to what the provider entry declared.
            entry_api_mode = (api_mode or custom_entry.get("api_mode") or "").strip()
            if custom_base:
                final_model = _normalize_resolved_model(
                    model
                    or custom_entry.get("model")
                    or (main_runtime.get("model") if main_runtime else None)
                    or _read_main_model_for_aux()
                    or "gpt-4o-mini",
                    provider,
                )
                # anthropic_messages talks to the /anthropic surface directly;
                # OpenAI-wire paths (chat_completions / codex_responses) need the
                # /v1 equivalent.  Rewrite only on the OpenAI-wire path so the
                # Anthropic fallback SDK still sees the original URL.
                if entry_api_mode == "anthropic_messages":
                    openai_base = custom_base
                    raw_base_for_wrap = custom_base
                else:
                    openai_base = _to_openai_base_url(custom_base)
                    raw_base_for_wrap = custom_base
                _clean_base2, _dq2 = _extract_url_query_params(openai_base)
                _extra2 = {"default_query": _dq2} if _dq2 else {}
                _headers2 = _apply_user_default_headers(_extra2.get("default_headers"))
                if _headers2:
                    _extra2["default_headers"] = _headers2
                logger.debug(
                    "resolve_provider_client: named custom provider %r (%s, api_mode=%s)",
                    provider, final_model, entry_api_mode or "chat_completions")
                # anthropic_messages: route through the Anthropic Messages API
                # via AnthropicAuxiliaryClient. Mirrors the anonymous-custom
                # branch in _try_custom_endpoint(). See #15033.
                if entry_api_mode == "anthropic_messages":
                    try:
                        from agent.anthropic_adapter import build_anthropic_client
                        real_client = build_anthropic_client(custom_key, custom_base)
                    except ImportError:
                        logger.warning(
                            "Named custom provider %r declares api_mode="
                            "anthropic_messages but the anthropic SDK is not "
                            "installed — falling back to OpenAI-wire.",
                            provider,
                        )
                        # Fallback went OpenAI-wire after all — redo the query
                        # extraction against the rewritten /v1 URL.
                        _fallback_base = _to_openai_base_url(custom_base)
                        _fb_clean, _fb_dq = _extract_url_query_params(_fallback_base)
                        _fb_extra = {"default_query": _fb_dq} if _fb_dq else {}
                        _fb_headers = _apply_user_default_headers(_fb_extra.get("default_headers"))
                        if _fb_headers:
                            _fb_extra["default_headers"] = _fb_headers
                        client = _create_openai_client(api_key=custom_key, base_url=_fb_clean, **_fb_extra)
                        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                                else (client, final_model))
                    sync_anthropic = AnthropicAuxiliaryClient(
                        real_client, final_model, custom_key, custom_base, is_oauth=False,
                    )
                    if async_mode:
                        return AsyncAnthropicAuxiliaryClient(sync_anthropic), final_model
                    return sync_anthropic, final_model
                client = _create_openai_client(api_key=custom_key, base_url=_clean_base2, **_extra2)
                # codex_responses or inherited auto-detect (via _wrap_if_needed).
                # _wrap_if_needed reads the closed-over `api_mode` (the task-level
                # override). Named-provider entry api_mode=codex_responses also
                # flows through here.
                if entry_api_mode == "codex_responses" and not isinstance(
                    client, CodexAuxiliaryClient
                ):
                    client = CodexAuxiliaryClient(client, final_model)
                else:
                    client = _wrap_if_needed(client, final_model, raw_base_for_wrap, custom_key)
                return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                        else (client, final_model))
            logger.warning(
                "resolve_provider_client: named custom provider %r has no base_url",
                provider)
            return None, None
    except ImportError:
        pass

    # ── Azure Foundry (delegates to runtime resolver for auth_mode-aware routing) ─
    #
    # The generic PROVIDER_REGISTRY path below uses
    # ``resolve_api_key_provider_credentials`` which only knows about the
    # static ``AZURE_FOUNDRY_API_KEY`` env var. That misses two important
    # cases for the ``azure-foundry`` provider:
    #
    #   1. ``model.auth_mode: entra_id`` — no static key exists; we need
    #      a callable bearer-token provider from ``azure_identity_adapter``.
    #   2. Non-default ``model.base_url`` (Foundry projects path) — the
    #      env-var-only resolver doesn't apply config-yaml-driven URL
    #      overrides.
    #
    # Delegate to the same runtime resolver the main agent uses so
    # auxiliary tasks (title generation, compression, vision, embedding,
    # session search) inherit the user's full Azure config.
    if provider == "azure-foundry":
        client, default_model = _try_azure_foundry(
            model=model,
            explicit_api_key=explicit_api_key,
            explicit_base_url=explicit_base_url,
            api_mode=api_mode,
        )
        if client is None:
            logger.warning(
                "resolve_provider_client: azure-foundry requested but "
                "runtime resolution failed (run: hermes doctor for "
                "diagnostics)"
            )
            return None, None
        final_model = _normalize_resolved_model(model or default_model, provider)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    # ── API-key providers from PROVIDER_REGISTRY ─────────────────────
    try:
        from hermes_cli.auth import (
            PROVIDER_REGISTRY,
            resolve_api_key_provider_credentials,
            resolve_external_process_provider_credentials,
        )
    except ImportError:
        logger.debug("hermes_cli.auth not available for provider %s", provider)
        return None, None

    pconfig = PROVIDER_REGISTRY.get(provider)
    if pconfig is None:
        # Demoted from logger.warning to debug; dedup keyed by provider name
        # so the first occurrence surfaces but repeated retries stay silent.
        if provider not in _LOGGED_UNKNOWN_PROVIDER_KEYS:
            _LOGGED_UNKNOWN_PROVIDER_KEYS.add(provider)
            logger.debug("resolve_provider_client: unknown provider %r", provider)
        return None, None

    if pconfig.auth_type == "api_key":
        if provider == "anthropic":
            client, default_model = _try_anthropic(explicit_api_key=explicit_api_key)
            if client is None:
                logger.warning("resolve_provider_client: anthropic requested but no Anthropic credentials found")
                return None, None
            final_model = _normalize_resolved_model(model or default_model, provider)
            return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode else (client, final_model))

        creds = resolve_api_key_provider_credentials(provider)
        api_key = str(creds.get("api_key", "")).strip()
        # Honour an explicit api_key override (e.g. from a fallback_model entry
        # or a custom_providers entry) so callers that pass an explicit
        # credential can authenticate against endpoints where no built-in
        # credential is registered for this provider alias.
        if explicit_api_key:
            api_key = explicit_api_key.strip() or api_key
        raw_base_url = str(creds.get("base_url", "")).strip().rstrip("/") or pconfig.inference_base_url
        if explicit_base_url:
            raw_base_url = explicit_base_url.strip().rstrip("/")
        # OpenCode Zen free tier (*-free slugs): served anonymously on the
        # Zen relay only — no credential needed, and any unknown bearer
        # (including a Go subscription key) is rejected. Route through the
        # keyless Zen runtime regardless of configured OpenCode credentials.
        try:
            from hermes_cli.models import opencode_zen_free_runtime as _oc_free_rt
            _free_rt = _oc_free_rt(provider, model)
        except Exception:
            _free_rt = None
        if _free_rt is not None:
            api_key = _free_rt["api_key"]
            raw_base_url = str(_free_rt["base_url"]).rstrip("/")
        if provider == "actual":
            try:
                from hermes_cli.auth import (
                    ACTUAL_LOCAL_NOAUTH_PLACEHOLDER,
                    is_actual_local_base_url,
                    normalize_actual_base_url,
                )

                raw_base_url = normalize_actual_base_url(raw_base_url)
                if not api_key and is_actual_local_base_url(raw_base_url):
                    api_key = ACTUAL_LOCAL_NOAUTH_PLACEHOLDER
            except Exception:
                pass
        if not api_key:
            tried_sources = list(pconfig.api_key_env_vars)
            if provider == "copilot":
                tried_sources.append("gh auth token")
            logger.debug("resolve_provider_client: provider %s has no API "
                         "key configured (tried: %s)",
                         provider, ", ".join(tried_sources))
            return None, None

        base_url = _to_openai_base_url(raw_base_url)
        # Honour an explicit base_url override from the caller — used when a
        # fallback_model entry (or custom_providers lookup) routes through a
        # built-in provider name but targets a user-specified endpoint.
        if explicit_base_url:
            base_url = _to_openai_base_url(explicit_base_url.strip().rstrip("/"))

        default_model = _get_aux_model_for_provider(provider)
        final_model = _normalize_resolved_model(model or default_model, provider)

        if provider == "gemini":
            from agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url

            if is_native_gemini_base_url(base_url):
                client = GeminiNativeClient(api_key=api_key, base_url=base_url)
                logger.debug("resolve_provider_client: %s (%s)", provider, final_model)
                return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                        else (client, final_model))

        # Provider-specific headers
        headers = {}
        if base_url_host_matches(base_url, "api.kimi.com"):
            headers["User-Agent"] = "claude-code/0.1.0"
        elif base_url_host_matches(base_url, "githubcopilot.com"):
            from hermes_cli.copilot_auth import copilot_request_headers

            headers.update(copilot_request_headers(
                is_agent_turn=True, is_vision=is_vision
            ))
        elif base_url_host_matches(base_url, "integrate.api.nvidia.com"):
            headers.update(build_nvidia_nim_headers(base_url))
        elif base_url_host_matches(base_url, "x.ai"):
            from tools.xai_http import hermes_xai_default_headers

            headers.update(hermes_xai_default_headers())
        else:
            # Fall back to profile.default_headers for providers that declare
            # client-level attribution headers on their profile (e.g. GMI
            # User-Agent for traffic identification, Vercel AI Gateway
            # Referer/Title for analytics).
            try:
                from providers import get_provider_profile as _gpf_main
                _ph_main = _gpf_main(provider)
                if _ph_main and _ph_main.default_headers:
                    headers.update(_ph_main.default_headers)
            except Exception:
                pass
        _merged_main = _apply_user_default_headers(headers)
        if _merged_main:
            headers = _merged_main
        client = _create_openai_client(api_key=api_key, base_url=base_url,
                        **({"default_headers": headers} if headers else {}))

        # Copilot GPT-5+ models (except gpt-5-mini) require the Responses
        # API — they are not accessible via /chat/completions.  Wrap the
        # plain client in CodexAuxiliaryClient so call_llm() transparently
        # routes through responses.stream().
        if provider == "copilot" and final_model and not raw_codex:
            try:
                from hermes_cli.models import _should_use_copilot_responses_api
                if _should_use_copilot_responses_api(final_model):
                    logger.debug(
                        "resolve_provider_client: copilot model %s needs "
                        "Responses API — wrapping with CodexAuxiliaryClient",
                        final_model)
                    client = CodexAuxiliaryClient(client, final_model)
            except ImportError:
                pass

        # Honor api_mode for any API-key provider (e.g. direct OpenAI with
        # codex-family models).  The copilot-specific wrapping above handles
        # copilot; this covers the general case (#6800).  Also rewraps
        # Anthropic-wire endpoints (Kimi Coding Plan api.kimi.com/coding,
        # /anthropic-suffixed gateways) so named providers like kimi-coding
        # land on the right transport without needing per-provider branches.
        client = _wrap_if_needed(client, final_model, raw_base_url, api_key)

        logger.debug("resolve_provider_client: %s (%s)", provider, final_model)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    if pconfig.auth_type == "external_process":
        creds = resolve_external_process_provider_credentials(provider)
        final_model = _normalize_resolved_model(
            model
            or (main_runtime.get("model") if main_runtime else None)
            or _read_main_model_for_aux(),
            provider,
        )
        if provider == "copilot-acp":
            api_key = str(creds.get("api_key", "")).strip()
            base_url = str(creds.get("base_url", "")).strip()
            command = str(creds.get("command", "")).strip() or None
            args = list(creds.get("args") or [])
            if not final_model:
                logger.warning(
                    "resolve_provider_client: copilot-acp requested but no model "
                    "was provided or configured"
                )
                return None, None
            if not api_key or not base_url:
                logger.warning(
                    "resolve_provider_client: copilot-acp requested but external "
                    "process credentials are incomplete"
                )
                return None, None
            from agent.copilot_acp_client import CopilotACPClient

            client = CopilotACPClient(
                api_key=api_key,
                base_url=base_url,
                command=command,
                args=args,
            )
            logger.debug("resolve_provider_client: %s (%s)", provider, final_model)
            return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                    else (client, final_model))
        if provider not in _LOGGED_UNSUPPORTED_EXTPROC_KEYS:
            _LOGGED_UNSUPPORTED_EXTPROC_KEYS.add(provider)
            logger.debug("resolve_provider_client: external-process provider %s not "
                         "directly supported", provider)
        return None, None

    elif pconfig.auth_type == "vertex":
        # Google Vertex AI — Gemini via the OpenAI-compatible endpoint with an
        # OAuth2 bearer token (NOT a static key). We build a standard OpenAI
        # client pointed at the runtime-computed Vertex base_url with a fresh
        # token; no custom SDK or message translation needed.
        try:
            from agent.vertex_adapter import get_vertex_config, has_vertex_credentials
        except ImportError:
            logger.warning("resolve_provider_client: vertex requested but "
                           "google-auth not installed")
            return None, None

        if not has_vertex_credentials():
            logger.debug("resolve_provider_client: vertex requested but "
                         "no GCP credentials found")
            return None, None

        token, base_url = get_vertex_config()
        if not token or not base_url:
            logger.warning("resolve_provider_client: vertex requested but "
                           "could not mint token / resolve project")
            return None, None

        default_model = "google/gemini-3-flash-preview"
        final_model = _normalize_resolved_model(model or default_model, provider)
        try:
            # Alias the import: a bare `from openai import OpenAI` here would
            # make `OpenAI` function-local and shadow the module-level lazy
            # proxy for every other branch of this function (breaking both the
            # Bedrock Mantle branch below and patch("agent.auxiliary_client.OpenAI")).
            from openai import OpenAI as _VertexOpenAI
            client = _VertexOpenAI(api_key=token, base_url=base_url)
        except Exception as exc:
            logger.warning("resolve_provider_client: cannot create Vertex "
                           "client: %s", exc)
            return None, None
        logger.debug("resolve_provider_client: vertex (%s)", final_model)
        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    elif pconfig.auth_type == "aws_sdk":
        # AWS SDK providers (Bedrock) — Claude models use the Anthropic Bedrock
        # SDK (prompt caching, thinking); OpenAI models (GPT-5.5/5.6) use
        # Bedrock Mantle's OpenAI Responses endpoint; all other models use the
        # Converse API.
        try:
            from agent.bedrock_adapter import (
                has_aws_credentials,
                is_anthropic_bedrock_model,
                resolve_bedrock_runtime_region,
                is_openai_bedrock_model,
                bedrock_openai_base_url,
                resolve_bedrock_bearer_token,
                configure_bedrock_openai_client_kwargs,
            )
            from agent.anthropic_adapter import build_anthropic_bedrock_client
        except ImportError:
            logger.warning("resolve_provider_client: bedrock requested but "
                           "boto3, httpx/openai, or anthropic SDK not installed")
            return None, None

        if not has_aws_credentials():
            logger.debug("resolve_provider_client: bedrock requested but "
                         "no AWS credentials found")
            return None, None

        # Region must match the main runtime's resolution (bedrock.region in
        # config.yaml first, then env/profile) — see review on #53880/#65076:
        # a bare resolve_bedrock_region() here let auxiliary calls (compression,
        # memory, vision) leave the primary runtime's configured region.
        region = resolve_bedrock_runtime_region()
        default_model = "anthropic.claude-haiku-4-5-20251001-v1:0"
        final_model = _normalize_resolved_model(model or default_model, provider) or default_model

        if is_openai_bedrock_model(final_model):
            # NOTE: no local `from openai import OpenAI` here — the module-level
            # lazy proxy (see top of file) must stay visible so tests can
            # patch("agent.auxiliary_client.OpenAI", ...).
            bearer = resolve_bedrock_bearer_token()
            mantle_base_url = bedrock_openai_base_url(region)
            client_kwargs: Dict[str, Any] = {
                "api_key": bearer or "aws-sdk",
                "base_url": mantle_base_url,
            }
            configure_bedrock_openai_client_kwargs(client_kwargs)
            client = OpenAI(**client_kwargs)
            logger.debug("resolve_provider_client: bedrock-openai (%s, %s)", final_model, region)
            if raw_codex:
                return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                        else (client, final_model))
            wrapped = CodexAuxiliaryClient(client, final_model)
            return (_to_async_client(wrapped, final_model, is_vision=is_vision) if async_mode
                    else (wrapped, final_model))

        base_url = f"https://bedrock-runtime.{region}.amazonaws.com"

        if is_anthropic_bedrock_model(final_model):
            try:
                real_client = build_anthropic_bedrock_client(region)
            except ImportError as exc:
                logger.warning("resolve_provider_client: cannot create Bedrock "
                               "client: %s", exc)
                return None, None
            client = AnthropicAuxiliaryClient(
                real_client, final_model, api_key="aws-sdk",
                base_url=base_url,
            )
            logger.debug("resolve_provider_client: bedrock anthropic (%s, %s)",
                         final_model, region)
        else:
            client = BedrockAuxiliaryClient(region, final_model)
            logger.debug("resolve_provider_client: bedrock converse (%s, %s)",
                         final_model, region)

        return (_to_async_client(client, final_model, is_vision=is_vision) if async_mode
                else (client, final_model))

    elif pconfig.auth_type in {"oauth_device_code", "oauth_external"}:
        # OAuth providers — route through their specific try functions
        if provider == "nous":
            return resolve_provider_client("nous", model, async_mode)
        if provider == "openai-codex":
            return resolve_provider_client("openai-codex", model, async_mode)
        if provider == "xai-oauth":
            return resolve_provider_client("xai-oauth", model, async_mode)
        # Other OAuth providers not directly supported
        if provider not in _LOGGED_UNSUPPORTED_OAUTH_KEYS:
            _LOGGED_UNSUPPORTED_OAUTH_KEYS.add(provider)
            logger.debug("resolve_provider_client: OAuth provider %s not "
                         "directly supported, try 'auto'", provider)
        return None, None

    # Demoted from logger.warning to debug; dedup keyed on (auth_type,
    # provider) so the first occurrence surfaces (real schema-drift bug) but
    # per-call retries stay silent.
    _auth_dedup_key = (pconfig.auth_type, provider)
    if _auth_dedup_key not in _LOGGED_UNHANDLED_AUTHTYPE_KEYS:
        _LOGGED_UNHANDLED_AUTHTYPE_KEYS.add(_auth_dedup_key)
        logger.debug("resolve_provider_client: unhandled auth_type %s for %s",
                     pconfig.auth_type, provider)
    return None, None


# ── Public API ──────────────────────────────────────────────────────────────

def get_text_auxiliary_client(
    task: str = "",
    *,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[OpenAI], Optional[str]]:
    """Return (client, default_model_slug) for text-only auxiliary tasks.

    Args:
        task: Optional task name ("compression", "skills_hub") to check
              for a task-specific provider override.

    Callers may override the returned model via config.yaml
    (e.g. auxiliary.compression.model, auxiliary.skills_hub.model).
    """
    provider, model, base_url, api_key, api_mode = _resolve_task_provider_model(task or None)
    return resolve_provider_client(
        provider,
        model=model,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
        api_mode=api_mode,
        main_runtime=main_runtime,
    )


def get_async_text_auxiliary_client(task: str = "", *, main_runtime: Optional[Dict[str, Any]] = None):
    """Return (async_client, model_slug) for async consumers.

    For standard providers returns (AsyncOpenAI, model). For Codex returns
    (AsyncCodexAuxiliaryClient, model) which wraps the Responses API.
    Returns (None, None) when no provider is available.
    """
    provider, model, base_url, api_key, api_mode = _resolve_task_provider_model(task or None)
    return resolve_provider_client(
        provider,
        model=model,
        async_mode=True,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
        api_mode=api_mode,
        main_runtime=main_runtime,
    )


_VISION_AUTO_PROVIDER_ORDER = (
    "openrouter",
    "nous",
    "deepinfra",
)


def _main_model_supports_vision(provider: str, model: Optional[str]) -> bool:
    """Return True when ``provider``/``model`` is known to accept image input.

    Used by the vision auto-detect chain to skip the user's main provider
    when it's known to be text-only (e.g. DeepSeek, gpt-oss without vision).
    Without this guard, ``resolve_vision_provider_client(provider="auto")``
    would happily return the main-provider client and any subsequent image
    payload would surface as a cryptic provider-side error
    (``unknown variant `image_url`, expected `text```, #31179).

    Returns True when capability lookup is unknown — preserves the historical
    behaviour of attempting the call, so providers we haven't catalogued yet
    don't silently regress to text-only.
    """
    try:
        from agent.image_routing import _lookup_supports_vision
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return True
    try:
        supports = _lookup_supports_vision(provider, model, load_config_readonly())
    except Exception:  # pragma: no cover - defensive
        return True
    if supports is None:
        # No capability data — keep current behaviour and let the call attempt
        # happen rather than silently skipping. This avoids false-positive
        # skips for new/custom providers.
        return True
    return bool(supports)


def _normalize_vision_provider(provider: Optional[str]) -> str:
    return _normalize_aux_provider(provider)


def _resolve_strict_vision_backend(
    provider: str,
    model: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    provider = _normalize_vision_provider(provider)
    if provider == "copilot":
        return resolve_provider_client("copilot", model, is_vision=True)
    if provider == "openrouter":
        return _try_openrouter(model=model)
    if provider == "nous":
        # Must go through resolve_provider_client so anthropic/* vision
        # recommendations wrap onto /v1/messages — _try_nous alone returns
        # a bare OpenAI client and the call 404s.
        return resolve_provider_client("nous", model, is_vision=True)
    if provider == "openai-codex":
        # Route through resolve_provider_client so the caller's explicit
        # model is used.  There is no safe default Codex model (shifting
        # allow-list); callers must specify via auxiliary.<task>.model.
        return resolve_provider_client("openai-codex", model, is_vision=True)
    if provider == "anthropic":
        return _try_anthropic()
    if provider == "deepinfra":
        # DeepInfra exposes vision-capable models (Llama-4 Scout/Maverick,
        # Qwen3-VL, Gemma 3, Gemini) on the same OpenAI-compatible endpoint
        # as its chat models. The default is discovered live via the profile's
        # default_vision_model() hook (key-gated, chat-surface + vision tag) so
        # we don't pin a hardcoded id that may rot when DeepInfra retires a
        # model, and this module stays provider-agnostic.
        vision_model = model or _resolve_provider_vision_default("deepinfra")
        if not vision_model:
            logger.debug(
                "Vision auto-detect: deepinfra catalog unreachable or "
                "returned no vision-tagged models — skipping"
            )
            return None, None
        return resolve_provider_client("deepinfra", vision_model, is_vision=True)
    if provider == "custom":
        return _try_custom_endpoint()
    return None, None


def _strict_vision_backend_available(provider: str) -> bool:
    return _resolve_strict_vision_backend(provider)[0] is not None


def get_available_vision_backends() -> List[str]:
    """Return the currently available vision backends in auto-selection order.

    Order: active provider → OpenRouter → Nous → stop.  This is the single
    source of truth for setup, tool gating, and runtime auto-routing of
    vision tasks.
    """
    available: List[str] = []
    # 1. Active provider — if the user configured a provider, try it first.
    main_provider = _read_main_provider()
    if main_provider and main_provider not in {"auto", ""}:
        if main_provider in _VISION_AUTO_PROVIDER_ORDER:
            if _strict_vision_backend_available(main_provider):
                available.append(main_provider)
        else:
            client, _ = resolve_provider_client(main_provider, _read_main_model())
            if client is not None:
                available.append(main_provider)
    # 2. OpenRouter, 3. Nous — skip if already covered by main provider.
    for p in _VISION_AUTO_PROVIDER_ORDER:
        if p not in available and _strict_vision_backend_available(p):
            available.append(p)
    return available


def resolve_vision_provider_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    async_mode: bool = False,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    """Resolve the client actually used for vision tasks.

    Direct endpoint overrides take precedence over provider selection. Explicit
    provider overrides still use the generic provider router for non-standard
    backends, so users can intentionally force experimental providers. Auto mode
    stays conservative and only tries vision backends known to work today.
    """
    runtime = _normalize_main_runtime(main_runtime)
    requested, resolved_model, resolved_base_url, resolved_api_key, resolved_api_mode = _resolve_task_provider_model(
        "vision", provider, model, base_url, api_key
    )
    requested = _normalize_vision_provider(requested)

    def _finalize(resolved_provider: str, sync_client: Any, default_model: Optional[str]):
        if sync_client is None:
            return resolved_provider, None, None
        final_model = resolved_model or default_model
        if async_mode:
            async_client, async_model = _to_async_client(sync_client, final_model, is_vision=True)
            return resolved_provider, async_client, async_model
        return resolved_provider, sync_client, final_model

    if resolved_base_url:
        provider_for_base_override = (
            requested if requested and requested not in {"", "auto"} else "custom"
        )
        client, final_model = resolve_provider_client(
            provider_for_base_override,
            model=resolved_model,
            async_mode=async_mode,
            explicit_base_url=resolved_base_url,
            explicit_api_key=resolved_api_key,
            api_mode=resolved_api_mode,
            main_runtime=runtime,
        )
        if client is None:
            return provider_for_base_override, None, None
        return provider_for_base_override, client, final_model

    if requested == "auto":
        # Vision auto-detection order:
        #   1. User's main provider + main model (including aggregators).
        #      _PROVIDER_VISION_MODELS provides per-provider vision model
        #      overrides when the provider has a dedicated multimodal model
        #      that differs from the chat model (e.g. xiaomi → mimo-v2-omni,
        #      zai → glm-5v-turbo). DeepInfra is similar but resolves its
        #      default vision model live from the catalog (see
        #      :func:`_resolve_provider_vision_default`). Nous is the
        #      exception: it has a dedicated strict vision backend with
        #      tier-aware defaults, so it must not fall through to the
        #      user's text chat model here.
        #   2. OpenRouter (vision-capable aggregator fallback)
        #   3. Nous Portal (vision-capable aggregator fallback)
        #   4. DeepInfra   (OpenAI-compatible; vision model discovered
        #                   live from the catalog — tried when
        #                   DEEPINFRA_API_KEY is set)
        #   5. Stop
        main_provider = str(runtime.get("provider") or _read_main_provider())
        main_model = str(runtime.get("model") or _read_main_model())
        if main_provider.strip().lower() == "moa":
            # MoA virtual provider: main_model is a preset NAME, and every
            # capability probe below (_PROVIDERS_WITHOUT_VISION,
            # _main_model_supports_vision, _resolve_provider_vision_default)
            # would run against a provider/model pair that doesn't exist on
            # any wire. Unwrap to the preset's aggregator slot first so the
            # checks and the eventual client target the real acting model.
            _agg_provider, _agg_model = _resolve_moa_aggregator(main_model)
            if _agg_provider and _agg_model:
                main_provider, main_model = _agg_provider, _agg_model
                # Drop the moa:// facade endpoint from the runtime view used
                # below — it belongs to the virtual provider, not the
                # aggregator's real provider.
                runtime = dict(runtime)
                runtime["base_url"] = ""
                runtime["api_key"] = ""
                runtime["api_mode"] = ""
        if main_provider and main_provider not in {"auto", "", "moa"}:
            # A provider-specific vision default wins over the user's chat model:
            # static overrides (xiaomi/zai) and catalog-backed discovery (the
            # DeepInfra profile hook) both yield a *known* vision-capable model,
            # whereas the pinned chat model is usually NOT multimodal (e.g. the
            # DeepSeek-V4-Flash default) and _main_model_supports_vision can't be
            # trusted to catch that. Only fall back to the chat model when no
            # provider default is available (catalog unreachable).
            provider_vision_default = _resolve_provider_vision_default(main_provider)
            vision_model = provider_vision_default or main_model
            if main_provider == "nous":
                # Nous resolves its vision model from the Portal's tier-aware
                # recommended-models slots inside _try_nous(vision=True).
                # Passing the chat model here overrides that pick, so a
                # text-only chat default (e.g. a `:free` chat SKU) receives the
                # image and the upstream rejects it with a 404. Only an
                # explicit auxiliary.vision.model may override the Portal.
                sync_client, default_model = _resolve_strict_vision_backend(
                    main_provider, resolved_model or provider_vision_default
                )
                if sync_client is not None:
                    logger.info(
                        "Vision auto-detect: using main provider %s (%s)",
                        main_provider, default_model or resolved_model or main_model,
                    )
                    return _finalize(main_provider, sync_client, default_model)
            elif main_provider in _PROVIDERS_WITHOUT_VISION:
                # Kimi Coding Plan's /coding endpoint (Anthropic Messages wire)
                # does not accept image input — Kimi's own docs say "Current
                # model does not support image input, switch to a model with
                # image_in capability" and vision lives on the separate Kimi
                # Platform (api.moonshot.ai). Skip the main provider and fall
                # through to the aggregator chain instead of returning a
                # client that will 404 on every vision request (#17076).
                logger.debug(
                    "Vision auto-detect: skipping main provider %s (no "
                    "vision support) — falling through to aggregator chain",
                    main_provider,
                )
            elif not _main_model_supports_vision(main_provider, vision_model):
                # The main model is known to be text-only (e.g. DeepSeek V4,
                # gpt-oss-120b without vision). Building a client and sending
                # an image would produce a cryptic provider-side error like
                # ``unknown variant `image_url`, expected `text``` (#31179).
                # Fall through to the aggregator chain instead.
                #
                # Only log the provider name (not the model) — mirrors the
                # sibling _PROVIDERS_WITHOUT_VISION branch above, and avoids
                # CodeQL py/clear-text-logging-sensitive-data heuristic false
                # positives on multi-value interpolations.
                logger.debug(
                    "Vision auto-detect: skipping main provider %s "
                    "(reports no vision capability) — falling through to "
                    "aggregator chain",
                    main_provider,
                )
            else:
                # Custom endpoints (``custom`` / ``custom:<name>``) carry no
                # built-in base_url/api_key — resolve_provider_client("custom")
                # would return None ("no endpoint credentials found") and the
                # whole chain would fall through to the aggregators, breaking
                # vision for every user on a custom provider that has no
                # separate ``auxiliary.vision`` block.  Recover the live main
                # endpoint that ``set_runtime_main()`` recorded for this turn so
                # Step 1 can build a working client.
                rpc_base_url = None
                rpc_api_key = None
                rpc_api_mode = resolved_api_mode
                if main_provider == "custom" or main_provider.startswith("custom:"):
                    runtime_base_url = runtime.get("base_url")
                    if runtime_base_url:
                        rpc_base_url = runtime_base_url
                        rpc_api_key = runtime.get("api_key") or None
                        rpc_api_mode = (
                            resolved_api_mode
                            or runtime.get("api_mode")
                            or None
                        )
                    else:
                        # No live runtime recorded (non-gateway caller): fall
                        # back to resolving the configured custom endpoint.
                        custom_base, custom_key, custom_mode = _resolve_custom_runtime()
                        if custom_base:
                            rpc_base_url = custom_base
                            rpc_api_key = custom_key
                            rpc_api_mode = resolved_api_mode or custom_mode or None
                rpc_client, rpc_model = resolve_provider_client(
                    main_provider, vision_model,
                    api_mode=rpc_api_mode,
                    explicit_base_url=rpc_base_url,
                    explicit_api_key=rpc_api_key,
                    main_runtime=runtime,
                    is_vision=True)
                if rpc_client is not None:
                    logger.info(
                        "Vision auto-detect: using main provider %s (%s)",
                        main_provider, rpc_model or vision_model,
                    )
                    return _finalize(
                        main_provider, rpc_client, rpc_model or vision_model)

        # Fall back through aggregators (uses their dedicated vision model,
        # not the user's main model) when main provider has no client.
        for candidate in _VISION_AUTO_PROVIDER_ORDER:
            if candidate == main_provider:
                continue  # already tried above
            sync_client, default_model = _resolve_strict_vision_backend(candidate)
            if sync_client is not None:
                return _finalize(candidate, sync_client, default_model)

        logger.debug("Auxiliary vision client: none available")
        return None, None, None

    if requested in _VISION_AUTO_PROVIDER_ORDER:
        sync_client, default_model = _resolve_strict_vision_backend(
            requested, resolved_model
        )
        return _finalize(requested, sync_client, default_model)

    # ZAI vision models must use the OpenAI-compatible endpoint, not the
    # Anthropic-compatible one (which may be the main-runtime default).
    # The Anthropic wire rejects max_tokens on multimodal calls (error 1210),
    # while the OpenAI wire handles it correctly.
    if requested == "zai" and not resolved_base_url:
        zai_openai_urls = [
            "https://open.bigmodel.cn/api/paas/v4",
            "https://api.z.ai/api/paas/v4",
        ]
        for _zai_url in zai_openai_urls:
            client, final_model = _get_cached_client(
                requested, resolved_model, async_mode,
                base_url=_zai_url,
                api_key=resolved_api_key or None,
                api_mode="chat_completions",
                main_runtime=runtime,
                is_vision=True,
            )
            if client is not None:
                return _finalize(requested, client, final_model)
        # Fallback: try without explicit base_url (old behavior)
        client, final_model = _get_cached_client(requested, resolved_model, async_mode,
                                                 api_mode=resolved_api_mode,
                                                 main_runtime=runtime,
                                                 is_vision=True)
        if client is None:
            return requested, None, None
        return requested, client, final_model

    client, final_model = _get_cached_client(requested, resolved_model, async_mode,
                                             api_mode=resolved_api_mode,
                                             main_runtime=runtime,
                                             is_vision=True)
    if client is None:
        return requested, None, None
    return requested, client, final_model


def get_auxiliary_extra_body() -> dict:
    """Return extra_body kwargs for auxiliary API calls.

    Includes Nous Portal product tags when the auxiliary client is backed
    by Nous Portal. Returns empty dict otherwise.
    """
    return _nous_extra_body() if auxiliary_is_nous else {}


def auxiliary_max_tokens_param(value: int, *, model: Optional[str] = None) -> dict:
    """Return the correct max tokens kwarg for the auxiliary client's provider.

    OpenRouter and local models use 'max_tokens'. Direct OpenAI with newer
    models (gpt-4o, gpt-4.1, gpt-5+, o-series) requires 'max_completion_tokens'.
    The Codex adapter translates max_tokens internally, so we use max_tokens
    for it as well. Pass ``model`` so third-party OpenAI-compatible endpoints
    fronting the newer families are also recognised — URL-only detection
    misses the case where a custom base URL serves e.g. ``gpt-5.4``.
    """
    custom_base = _current_custom_base_url()
    or_key = _scoped_key_env("OPENROUTER_API_KEY")
    # Use max_completion_tokens for direct OpenAI-compatible providers that reject
    # max_tokens on newer GPT-4o/o-series/GPT-5-style models.
    _custom_host = base_url_hostname(custom_base) or ""
    if (not or_key
            and _read_nous_auth() is None
            and (
                _custom_host == "api.openai.com"
                or _custom_host == "api.githubcopilot.com"
                or _custom_host.endswith(".githubcopilot.com")
            )):
        return {"max_completion_tokens": value}
    # ...and for any caller serving a newer OpenAI-family model by name.
    if model_forces_max_completion_tokens(model):
        return {"max_completion_tokens": value}
    return {"max_tokens": value}


# ── Centralized LLM Call API ────────────────────────────────────────────────
#
# call_llm() and async_call_llm() own the full request lifecycle:
#   1. Resolve provider + model from task config (or explicit args)
#   2. Get or create a cached client for that provider
#   3. Format request args for the provider + model (max_tokens handling, etc.)
#   4. Make the API call
#   5. Return the response
#
# Every auxiliary LLM consumer should use these instead of manually
# constructing clients and calling .chat.completions.create().

# Client cache: (provider, async_mode, base_url, api_key, api_mode, runtime_key) -> (client, default_model, loop)
# NOTE: loop identity is NOT part of the key.  On async cache hits we check
# whether the cached loop is the *current* loop; if not, the stale entry is
# replaced in-place.  This bounds cache growth to one entry per unique
# provider config rather than one per (config × event-loop), which previously
# caused unbounded fd accumulation in long-running gateway processes (#10200).
_client_cache: Dict[tuple, tuple] = {}
_client_cache_lock = threading.Lock()
_CLIENT_CACHE_MAX_SIZE = 64  # safety belt — evict oldest when exceeded


class _CallableCacheDiscriminator:
    """Hash a credential callback by identity without exposing its state."""

    __slots__ = ("_callback",)

    def __init__(self, callback: Any) -> None:
        # Retain the callback so its id cannot be reused while cached.
        self._callback = callback

    def __hash__(self) -> int:
        return id(self._callback)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _CallableCacheDiscriminator)
            and self._callback is other._callback
        )

    def __repr__(self) -> str:
        return "<callable-api-key>"


def _runtime_cache_discriminator(field: str, value: Any) -> Any:
    """Return a hashable, secret-safe runtime cache-key component."""
    if field == "api_key" and callable(value):
        return _CallableCacheDiscriminator(value)
    if field == "api_key" and isinstance(value, str) and value:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).digest()
        return ("api-key-digest", digest)
    return value


def _client_cache_key(
    provider: str,
    *,
    async_mode: bool,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    api_mode: Optional[str] = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
    task: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple:
    runtime = _normalize_main_runtime(main_runtime)
    runtime_key = tuple(
        _runtime_cache_discriminator(field, runtime.get(field, ""))
        for field in _MAIN_RUNTIME_FIELDS
    ) if provider == "auto" else ()
    # `auto` can now resolve through task-specific or main fallback policy,
    # so the task participates in the cache key. Non-auto providers keep the
    # old cache shape because the explicit provider/model tuple is sufficient.
    task_key = (
        (task or "", _task_prefers_fast_model(task))
        if provider == "auto"
        else ""
    )
    pool_hint = _pool_cache_hint(provider, main_runtime=main_runtime)
    # The model MUST participate in the key. Two concurrent auxiliary calls to
    # the SAME provider/base_url/key but DIFFERENT models (e.g. a MoA reference
    # fan-out running opus + gpt-5.5 in parallel threads) would otherwise share
    # one cache entry. On a cache MISS both build a client for the same key; the
    # second's _store_cached_client sees the first as the "old" entry and CLOSES
    # it — while the first call is still mid-request on it — yielding a spurious
    # APIConnectionError that fails the sibling advisor (root cause of the run2
    # double-advisor "Connection error" collapse). Keying on model gives each
    # model its own client, so concurrent fan-out calls never cross-close.
    model_key = model or runtime.get("model", "")
    api_key_key = _runtime_cache_discriminator("api_key", api_key or "")
    return (provider, async_mode, base_url or "", api_key_key, api_mode or "", runtime_key, is_vision, task_key, pool_hint, model_key)


def _store_cached_client(cache_key: tuple, client: Any, default_model: Optional[str], *, bound_loop: Any = None) -> None:
    if isinstance(client, _AuxProbeClientStub):
        # Probe stubs must never enter the cache — a runtime caller would
        # receive a non-functional client on the next cache hit.
        return
    with _client_cache_lock:
        old_entry = _client_cache.get(cache_key)
        if old_entry is not None and old_entry[0] is not client:
            _close_cached_client(old_entry[0])
        _client_cache[cache_key] = (client, default_model, bound_loop)


def _refresh_nous_auxiliary_client(
    *,
    cache_provider: str,
    model: Optional[str],
    async_mode: bool,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    api_mode: Optional[str] = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
) -> Tuple[Optional[Any], Optional[str]]:
    """Refresh Nous runtime creds, rebuild the client, and replace the cache entry."""
    runtime = _resolve_nous_runtime_api(force_refresh=True)
    if runtime is None:
        return None, model

    fresh_key, fresh_base_url = runtime
    sync_client = _create_openai_client(api_key=fresh_key, base_url=fresh_base_url)
    final_model = model

    current_loop = None
    if async_mode:
        try:
            import asyncio as _aio
            current_loop = _aio.get_event_loop()
        except RuntimeError:
            pass
        client, final_model = _to_async_client(sync_client, final_model or "", is_vision=is_vision)
    else:
        client = sync_client

    cache_key = _client_cache_key(
        cache_provider,
        async_mode=async_mode,
        base_url=base_url,
        api_key=api_key,
        api_mode=api_mode,
        main_runtime=main_runtime,
        is_vision=is_vision,
        model=final_model,
    )
    _store_cached_client(cache_key, client, final_model, bound_loop=current_loop)
    return client, final_model


def neuter_async_httpx_del() -> None:
    """Monkey-patch ``AsyncHttpxClientWrapper.__del__`` to be a no-op.

    The OpenAI SDK's ``AsyncHttpxClientWrapper.__del__`` schedules
    ``self.aclose()`` via ``asyncio.get_running_loop().create_task()``.
    When an ``AsyncOpenAI`` client is garbage-collected while
    prompt_toolkit's event loop is running (the common CLI idle state),
    the ``aclose()`` task runs on prompt_toolkit's loop but the
    underlying TCP transport is bound to a *different* loop (the worker
    thread's loop that the client was originally created on).  If that
    loop is closed or its thread is dead, the transport's
    ``self._loop.call_soon()`` raises ``RuntimeError("Event loop is
    closed")``, which prompt_toolkit surfaces as "Unhandled exception
    in event loop ... Press ENTER to continue...".

    Neutering ``__del__`` is safe because:
    - Cached clients are explicitly cleaned via ``_force_close_async_httpx``
      on stale-loop detection and ``shutdown_cached_clients`` on exit.
    - Uncached clients' TCP connections are cleaned up by the OS when the
      process exits.
    - The OpenAI SDK itself marks this as a TODO (``# TODO(someday):
      support non asyncio runtimes here``).

    Call this once at CLI startup, before any ``AsyncOpenAI`` clients are
    created.
    """
    try:
        from openai._base_client import AsyncHttpxClientWrapper
        AsyncHttpxClientWrapper.__del__ = lambda self: None  # type: ignore[assignment]
    except (ImportError, AttributeError):
        pass  # Graceful degradation if the SDK changes its internals


def _force_close_async_httpx(client: Any) -> None:
    """Mark the httpx AsyncClient inside an AsyncOpenAI client as closed.

    This prevents ``AsyncHttpxClientWrapper.__del__`` from scheduling
    ``aclose()`` on a (potentially closed) event loop, which causes
    ``RuntimeError: Event loop is closed`` → prompt_toolkit's
    "Press ENTER to continue..." handler.

    We intentionally do NOT run the full async close path — the
    connections will be dropped by the OS when the process exits.
    """
    try:
        from httpx._client import ClientState
        inner = getattr(client, "_client", None)
        if inner is not None and not getattr(inner, "is_closed", True):
            inner._state = ClientState.CLOSED
    except Exception:
        pass


def _schedule_async_close(close_result: Any, client: Any) -> None:
    """Finish an async close without leaking an unawaited coroutine."""
    async def _await_close() -> None:
        try:
            await close_result
        except Exception:
            pass
        finally:
            _force_close_async_httpx(client)

    runner = _await_close()
    try:
        import asyncio as _aio

        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            _aio.run(runner)
        else:
            task = loop.create_task(runner)

            def _consume(completed_task) -> None:
                try:
                    completed_task.exception()
                except BaseException:
                    pass

            task.add_done_callback(_consume)
            runner = None
    except Exception:
        if runner is not None:
            try:
                runner.close()
            except Exception:
                pass
        _force_close_async_httpx(client)


def _close_cached_client(client: Any, *, close_async: bool = False) -> None:
    """Close one cached client, awaiting async transports only when safe."""
    if client is None:
        return
    close_fn = getattr(client, "close", None)
    if not callable(close_fn):
        _force_close_async_httpx(client)
        return
    try:
        close_result = close_fn()
    except Exception:
        _force_close_async_httpx(client)
        return
    if inspect.isawaitable(close_result):
        if close_async:
            _schedule_async_close(close_result, client)
        else:
            # Do not await a client owned by another live event loop.
            # Closing the coroutine avoids an unawaited-coroutine warning;
            # the transport is still neutered for safe eventual GC.
            try:
                close_result.close()
            except Exception:
                pass
            _force_close_async_httpx(client)
        return
    _force_close_async_httpx(client)


def shutdown_cached_clients() -> None:
    """Close all cached clients (sync and async) to prevent event-loop errors.

    Call this during CLI shutdown, *before* the event loop is closed, to
    avoid ``AsyncHttpxClientWrapper.__del__`` raising on a dead loop.

    Snapshot and clear the cache under the lock, then close transports outside
    it. Async transport shutdown may block while an owner loop drains; holding
    the global cache lock during that wait stalls unrelated auxiliary callers
    and can turn teardown into a process-wide lock convoy.
    """
    with _client_cache_lock:
        clients = [
            (entry[0], entry[2])
            for entry in _client_cache.values()
            if entry[0] is not None
        ]
        _client_cache.clear()
    try:
        import asyncio as _aio

        running_loop = _aio.get_running_loop()
    except RuntimeError:
        running_loop = None
    for client, owner_loop in clients:
        # A live foreign loop owns its async transport. Calling its coroutine
        # on this thread can bind/close sockets from the wrong loop; neuter it
        # and let that owner finish teardown. Closed loops are safe to drain
        # locally, and the current loop can await its own client.
        close_async = owner_loop is not None and (
            owner_loop.is_closed() or owner_loop is running_loop
        )
        _close_cached_client(client, close_async=close_async)


def cleanup_stale_async_clients() -> None:
    """Force-close cached async clients whose event loop is closed.

    Call this after each agent turn to proactively clean up stale clients
    before GC can trigger ``AsyncHttpxClientWrapper.__del__`` on them.
    This is defense-in-depth — the primary fix is ``neuter_async_httpx_del``
    which disables ``__del__`` entirely.
    """
    stale_clients = []
    with _client_cache_lock:
        stale_keys = []
        for key, entry in _client_cache.items():
            client, _default, cached_loop = entry
            if cached_loop is not None and cached_loop.is_closed():
                stale_keys.append(key)
                stale_clients.append(client)
        for key in stale_keys:
            del _client_cache[key]
    for client in stale_clients:
        _close_cached_client(client, close_async=True)


def _is_openrouter_client(client: Any) -> bool:
    for obj in (client, getattr(client, "_client", None), getattr(client, "client", None)):
        if obj and base_url_host_matches(str(getattr(obj, "base_url", "") or ""), "openrouter.ai"):
            return True
    return False


def _cached_client_accepts_slash_models(client: Any, cached_default: Optional[str]) -> bool:
    """Best-effort check for cached clients that accept ``vendor/model`` IDs."""
    if _is_openrouter_client(client):
        return True
    return bool(cached_default and "/" in cached_default)


def _compat_model(client: Any, model: Optional[str], cached_default: Optional[str]) -> Optional[str]:
    """Keep slash-bearing model IDs only for cached clients that support them.

    Mirrors the guard in resolve_provider_client() which is skipped on cache hits.
    """
    if model and "/" in model and not _cached_client_accepts_slash_models(client, cached_default):
        return cached_default
    return model or cached_default
