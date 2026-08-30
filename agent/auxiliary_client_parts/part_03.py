_RUNTIME_MAIN_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("auxiliary_runtime_main", default=None)
)

_RELAY_AUX_CALL_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("auxiliary_relay_call", default=None)
)


def _relay_auxiliary_call(callback):
    """Give every physical retry in one auxiliary call a shared Relay identity."""

    @functools.wraps(callback)
    def wrapped(*args, **kwargs):
        task = args[0] if args else kwargs.get("task")
        token = _RELAY_AUX_CALL_CONTEXT.set({
            "task": str(task or "unknown"),
            "request_id": f"aux-{uuid.uuid4().hex}",
            "attempt_count": 0,
            "provider": "",
            "model": "",
            "response_model": None,
            "api_mode": "chat_completions",
        })
        try:
            return callback(*args, **kwargs)
        except BaseException:
            _fail_relay_auxiliary_call()
            raise
        finally:
            _RELAY_AUX_CALL_CONTEXT.reset(token)

    return wrapped


def _relay_auxiliary_call_async(callback):
    """Async counterpart to :func:`_relay_auxiliary_call`."""

    @functools.wraps(callback)
    async def wrapped(*args, **kwargs):
        task = args[0] if args else kwargs.get("task")
        token = _RELAY_AUX_CALL_CONTEXT.set({
            "task": str(task or "unknown"),
            "request_id": f"aux-{uuid.uuid4().hex}",
            "attempt_count": 0,
            "provider": "",
            "model": "",
            "response_model": None,
            "api_mode": "chat_completions",
        })
        try:
            return await callback(*args, **kwargs)
        except BaseException:
            _fail_relay_auxiliary_call()
            raise
        finally:
            _RELAY_AUX_CALL_CONTEXT.reset(token)

    return wrapped


def _set_relay_auxiliary_route(
    provider: str | None,
    model: str | None,
    api_mode: str | None,
) -> None:
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    context["provider"] = str(provider or "auxiliary")
    context["model"] = str(model or "unknown")
    context["response_model"] = None
    context["api_mode"] = str(api_mode or "chat_completions")


def _record_route_info(
    route_info: Optional[Dict[str, str]],
    provider: Optional[str],
    model: Optional[str],
) -> None:
    """Expose the concrete route selected for one auxiliary call."""
    if route_info is not None:
        route_info["provider"] = provider or "auto"
        route_info["model"] = model or "default"


def _relay_auxiliary_metadata(
    *,
    provider: str | None = None,
    api_mode: str | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return None
    attempt_count = int(context.get("attempt_count") or 0)
    context["attempt_count"] = attempt_count + 1
    provider_name = str(provider or context.get("provider") or "auxiliary")
    model_name = str(context.get("model") or "unknown")
    return provider_name, model_name, {
        "api_mode": str(api_mode or context.get("api_mode") or "chat_completions"),
        "api_request_id": str(context["request_id"]),
        "call_role": f"auxiliary:{context['task']}",
        "retry_count": attempt_count,
        "auxiliary_task": str(context["task"]),
    }


def _relay_sync_completion(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str | None = None,
    api_mode: str | None = None,
    create: Callable[[dict[str, Any]], Any] | None = None,
) -> Any:
    callback = create or (lambda request: client.chat.completions.create(**request))
    route = _relay_auxiliary_metadata(provider=provider, api_mode=api_mode)
    # Protected compression calls isolate only the provider callback and stream
    # aggregation.  The owning thread remains free to unwind its lease/DB
    # transaction on hard cancel without touching the process-shared client.
    if route is None:
        return _run_protected_sync_provider_call(callback, kwargs)
    provider_name, fallback_model, metadata = route
    from agent import relay_llm

    return relay_llm.execute_current(
        kwargs,
        lambda request: _run_protected_sync_provider_call(callback, request),
        name=provider_name,
        model_name=str(kwargs.get("model") or fallback_model),
        metadata=metadata,
        defer_logical_completion=True,
    )


async def _relay_async_completion(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str | None = None,
    api_mode: str | None = None,
    create: Callable[[dict[str, Any]], Any] | None = None,
) -> Any:
    callback = create or (lambda request: client.chat.completions.create(**request))
    route = _relay_auxiliary_metadata(provider=provider, api_mode=api_mode)
    if route is None:
        return await callback(kwargs)
    provider_name, fallback_model, metadata = route
    from agent import relay_llm

    return await relay_llm.execute_current_async(
        kwargs,
        callback,
        name=provider_name,
        model_name=str(kwargs.get("model") or fallback_model),
        metadata=metadata,
        defer_logical_completion=True,
    )


def _relay_sync_stream(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str | None = None,
    api_mode: str | None = None,
) -> Any:
    route = _relay_auxiliary_metadata(provider=provider, api_mode=api_mode)
    if route is None:
        return client.chat.completions.create(**kwargs)
    provider_name, fallback_model, metadata = route
    from agent import relay_llm

    return relay_llm.stream_current(
        kwargs,
        lambda request: client.chat.completions.create(**request),
        name=provider_name,
        model_name=str(kwargs.get("model") or fallback_model),
        finalizer=dict,
        metadata=metadata,
        completed_response_predicate=lambda value: hasattr(value, "choices"),
    )
_RUNTIME_MAIN_COMPAT_SNAPSHOT: Tuple[Any, ...] = ("", "", "", "", "", "")
_RUNTIME_MAIN_COMPAT_LOCK = threading.Lock()


def _compat_runtime_main() -> Optional[Dict[str, Any]]:
    """Expose deliberately patched legacy globals in a single main context.

    ``set_runtime_main`` mirrors values into the old module attributes for
    introspection, but those mirrors must never become runtime inputs. A direct
    patch is recognized only when it differs from the mirrored snapshot and
    only on the main thread, keeping concurrent session workers isolated.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    values = (
        _RUNTIME_MAIN_PROVIDER,
        _RUNTIME_MAIN_MODEL,
        _RUNTIME_MAIN_BASE_URL,
        _RUNTIME_MAIN_API_KEY,
        _RUNTIME_MAIN_API_MODE,
        _RUNTIME_MAIN_AUTH_MODE,
    )
    if values == _RUNTIME_MAIN_COMPAT_SNAPSHOT:
        return None
    return dict(zip(_MAIN_RUNTIME_FIELDS, values))


def _runtime_main_value(field: str) -> Any:
    """Read one runtime field through context-local/controlled legacy state."""
    runtime = _RUNTIME_MAIN_CONTEXT.get()
    if runtime is None:
        runtime = _compat_runtime_main()
    if isinstance(runtime, dict):
        value = runtime.get(field)
        if value:
            return value
    return ""


def set_runtime_main(
    provider: str,
    model: str,
    *,
    requested_provider: str = "",
    base_url: str = "",
    api_key: Any = "",
    api_mode: str = "",
    auth_mode: str = "",
    session_id: str = "",
    cache_scope: str = "",
) -> contextvars.Token:
    """Record the current context's live main runtime for auxiliary routing.

    Context-local state prevents concurrent gateway sessions from overwriting
    one another while retaining compatibility mirrors for legacy readers.

    ``cache_scope`` is the rotation-stable logical cache scope (compression-
    lineage root — agent/prompt_cache_scope.py) resolved once per turn by
    turn_context; auxiliary Responses calls prefer it over ``session_id``
    for prompt_cache_key derivation (#79017).
    """
    global _RUNTIME_MAIN_PROVIDER, _RUNTIME_MAIN_MODEL
    global _RUNTIME_MAIN_BASE_URL, _RUNTIME_MAIN_API_KEY, _RUNTIME_MAIN_API_MODE
    global _RUNTIME_MAIN_AUTH_MODE, _RUNTIME_MAIN_COMPAT_SNAPSHOT
    runtime = {
        "provider": (provider or "").strip().lower(),
        "requested_provider": (requested_provider or "").strip().lower(),
        "model": (model or "").strip(),
        "base_url": (base_url or "").strip(),
        "api_key": (
            api_key.strip()
            if isinstance(api_key, str)
            else api_key if callable(api_key) else ""
        ),
        "api_mode": (api_mode or "").strip(),
        "auth_mode": (auth_mode or "").strip().lower(),
        "session_id": (session_id or "").strip(),
        "cache_scope": (cache_scope or "").strip(),
    }
    # Publish authoritative context before updating locked compatibility
    # mirrors; concurrent sessions never read those mirrors at runtime.
    token = _RUNTIME_MAIN_CONTEXT.set(runtime)
    with _RUNTIME_MAIN_COMPAT_LOCK:
        (
            _RUNTIME_MAIN_PROVIDER,
            _RUNTIME_MAIN_MODEL,
            _RUNTIME_MAIN_BASE_URL,
            _RUNTIME_MAIN_API_KEY,
            _RUNTIME_MAIN_API_MODE,
            _RUNTIME_MAIN_AUTH_MODE,
        ) = (runtime[field] for field in _MAIN_RUNTIME_FIELDS)
        _RUNTIME_MAIN_COMPAT_SNAPSHOT = tuple(
            runtime[field] for field in _MAIN_RUNTIME_FIELDS
        )
    return token


def reset_runtime_main(token: contextvars.Token) -> None:
    """Restore the runtime binding that preceded one scoped turn."""
    if token is None:
        return
    try:
        _RUNTIME_MAIN_CONTEXT.reset(token)
    except (RuntimeError, ValueError):
        # A token cannot be reset from another copied Context. Background
        # workers inherit values, not ownership of the parent's token.
        pass


@contextlib.contextmanager
def scoped_runtime_main(main_runtime: Optional[Dict[str, Any]]):
    """Temporarily bind an explicit runtime without touching legacy mirrors."""
    runtime = _normalize_main_runtime(main_runtime)
    token = _RUNTIME_MAIN_CONTEXT.set(runtime or None)
    try:
        yield runtime
    finally:
        _RUNTIME_MAIN_CONTEXT.reset(token)


def clear_runtime_main() -> None:
    """Clear the runtime override in the current context."""
    global _RUNTIME_MAIN_PROVIDER, _RUNTIME_MAIN_MODEL
    global _RUNTIME_MAIN_BASE_URL, _RUNTIME_MAIN_API_KEY, _RUNTIME_MAIN_API_MODE
    global _RUNTIME_MAIN_AUTH_MODE, _RUNTIME_MAIN_COMPAT_SNAPSHOT
    _RUNTIME_MAIN_CONTEXT.set(None)
    with _RUNTIME_MAIN_COMPAT_LOCK:
        _RUNTIME_MAIN_PROVIDER = ""
        _RUNTIME_MAIN_MODEL = ""
        _RUNTIME_MAIN_BASE_URL = ""
        _RUNTIME_MAIN_API_KEY = ""
        _RUNTIME_MAIN_API_MODE = ""
        _RUNTIME_MAIN_AUTH_MODE = ""
        _RUNTIME_MAIN_COMPAT_SNAPSHOT = ("", "", "", "", "", "")


def _resolve_custom_runtime() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve the active custom/main endpoint the same way the main CLI does.

    This covers both env-driven OPENAI_BASE_URL setups and config-saved custom
    endpoints where the base URL lives in config.yaml instead of the live
    environment.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="custom")
    except Exception as exc:
        logger.debug("Auxiliary client: custom runtime resolution failed: %s", exc)
        runtime = None

    if not isinstance(runtime, dict):
        openai_base = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
        openai_key = _scoped_key_env("OPENAI_API_KEY")
        if not openai_base:
            return None, None, None
        runtime = {
            "base_url": openai_base,
            "api_key": openai_key,
        }

    custom_base = runtime.get("base_url")
    custom_key = runtime.get("api_key")
    custom_mode = runtime.get("api_mode")
    if not isinstance(custom_base, str) or not custom_base.strip():
        return None, None, None

    custom_base = custom_base.strip().rstrip("/")
    if base_url_host_matches(custom_base, "openrouter.ai"):
        # requested='custom' falls back to OpenRouter when no custom endpoint is
        # configured. Treat that as "no custom endpoint" for auxiliary routing.
        return None, None, None

    # Local servers (Ollama, llama.cpp, vLLM, LM Studio) don't require auth.
    # Use a placeholder key — the OpenAI SDK requires a non-empty string but
    # local servers ignore the Authorization header.  Same fix as cli.py
    # _ensure_runtime_credentials() (PR #2556).
    if not isinstance(custom_key, str) or not custom_key.strip():
        custom_key = "no-key-required"

    if not isinstance(custom_mode, str) or not custom_mode.strip():
        custom_mode = None

    return custom_base, custom_key.strip(), custom_mode


def _current_custom_base_url() -> str:
    custom_base, _, _ = _resolve_custom_runtime()
    return custom_base or ""


def _validate_proxy_env_urls() -> None:
    """Fail fast with a clear error when proxy env vars have malformed URLs.

    Common cause: shell config (e.g. .zshrc) with a typo like
    ``export HTTP_PROXY=http://127.0.0.1:6153export NEXT_VAR=...``
    which concatenates 'export' into the port number.  Without this
    check the OpenAI/httpx client raises a cryptic ``Invalid port``
    error that doesn't name the offending env var.
    """
    from urllib.parse import urlparse

    normalize_proxy_env_vars()

    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
            if parsed.scheme:
                _ = parsed.port          # raises ValueError for e.g. '6153export'
        except ValueError as exc:
            raise RuntimeError(
                f"Malformed proxy environment variable {key}={value!r}. "
                "Fix or unset your proxy settings and try again."
            ) from exc


def _validate_base_url(base_url: str) -> None:
    """Reject obviously broken custom endpoint URLs before they reach httpx."""
    from urllib.parse import urlparse

    candidate = str(base_url or "").strip()
    if not candidate or candidate.startswith("acp://"):
        return
    try:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            _ = parsed.port              # raises ValueError for malformed ports
    except ValueError as exc:
        raise RuntimeError(
            f"Malformed custom endpoint URL: {candidate!r}. "
            "Run `hermes setup` or `hermes model` and enter a valid http(s) base URL."
        ) from exc


def _try_custom_endpoint() -> Tuple[Optional[Any], Optional[str]]:
    runtime = _resolve_custom_runtime()
    if len(runtime) == 2:
        custom_base, custom_key = runtime
        custom_mode = None
    else:
        custom_base, custom_key, custom_mode = runtime
    if not custom_base or not custom_key:
        return None, None
    if custom_base.lower().startswith(_CODEX_AUX_BASE_URL.lower()):
        return None, None
    model = _read_main_model_for_aux() or "gpt-4o-mini"
    logger.debug("Auxiliary client: custom endpoint (%s, api_mode=%s)", model, custom_mode or "chat_completions")
    _clean_base, _dq = _extract_url_query_params(custom_base)
    _extra = {"default_query": _dq} if _dq else {}
    # User-configured model.default_headers override the SDK's identifying
    # headers (User-Agent: OpenAI/Python ..., X-Stainless-*) on this custom
    # endpoint's auxiliary calls too — matching the main agent client so the
    # whole session reaches a gateway/WAF that rejects the SDK fingerprint. (#40033)
    _custom_headers = _apply_user_default_headers(None)
    if _custom_headers:
        _extra["default_headers"] = _custom_headers
    if custom_mode == "codex_responses":
        real_client = _create_openai_client(api_key=custom_key, base_url=_clean_base, **_extra)
        return CodexAuxiliaryClient(real_client, model), model
    if custom_mode == "anthropic_messages":
        # Third-party Anthropic-compatible gateway (MiniMax, Zhipu GLM,
        # LiteLLM proxies, etc.).  Must NEVER be treated as OAuth —
        # Anthropic OAuth claims only apply to api.anthropic.com.
        try:
            from agent.anthropic_adapter import build_anthropic_client
            real_client = build_anthropic_client(custom_key, custom_base)
        except ImportError:
            logger.warning(
                "Custom endpoint declares api_mode=anthropic_messages but the "
                "anthropic SDK is not installed — falling back to OpenAI-wire."
            )
            return _create_openai_client(api_key=custom_key, base_url=_clean_base, **_extra), model
        return (
            AnthropicAuxiliaryClient(real_client, model, custom_key, custom_base, is_oauth=False),
            model,
        )
    # URL-based anthropic detection for custom endpoints that didn't set
    # api_mode explicitly (e.g. kimi.com/coding reached via custom config).
    _fallback_client = _create_openai_client(api_key=custom_key, base_url=_clean_base, **_extra)
    _fallback_client = _maybe_wrap_anthropic(
        _fallback_client, model, custom_key, custom_base, custom_mode,
    )
    return _fallback_client, model


def _build_xai_oauth_aux_client(model: str) -> Tuple[Optional[Any], Optional[str]]:
    """Build a CodexAuxiliaryClient for an xAI Grok OAuth-authenticated session.

    xAI's ``/v1/responses`` endpoint speaks the OpenAI Responses API, so we
    wrap a plain ``OpenAI`` client in ``CodexAuxiliaryClient`` to translate
    ``chat.completions.create()`` calls into ``responses.stream()`` requests.

    The caller must pass an explicit model — pinning a default for Grok
    would silently rot when xAI's allowlist drifts.  Returns ``(None, None)``
    when the user has not authenticated with xAI Grok OAuth.
    """
    if not model:
        logger.warning(
            "Auxiliary client: xai-oauth requested without a model; "
            "pass model explicitly (auxiliary.<task>.model in config.yaml)."
        )
        return None, None
    resolved = _resolve_xai_oauth_for_aux()
    if resolved is None:
        return None, None
    api_key, base_url = resolved
    logger.debug("Auxiliary client: xAI OAuth (%s via Responses API)", model)
    from tools.xai_http import hermes_xai_default_headers

    real_client = _create_openai_client(
        api_key=api_key,
        base_url=base_url,
        default_headers=hermes_xai_default_headers(),
    )
    return CodexAuxiliaryClient(real_client, model), model


def _build_codex_client(model: str) -> Tuple[Optional[Any], Optional[str]]:
    """Build a CodexAuxiliaryClient for an explicitly-requested model.

    There is no auto-selection of the Codex model: the ChatGPT-account
    Codex endpoint's accepted model list is an undocumented, drifting
    allow-list, so any hardcoded default we pick goes stale.  The caller
    is responsible for passing the model (e.g. from the user's own
    ``model.model`` or ``auxiliary.<task>.model`` config).

    Returns (None, None) when no Codex OAuth token is available.
    """
    if not model:
        logger.warning(
            "Auxiliary client: openai-codex requested without a model; "
            "pass model explicitly (auxiliary.<task>.model in config.yaml)."
        )
        return None, None
    pool_present, entry = _select_pool_entry("openai-codex")
    if pool_present:
        codex_token = _pool_runtime_api_key(entry)
        if codex_token:
            base_url = _pool_runtime_base_url(entry, _CODEX_AUX_BASE_URL) or _CODEX_AUX_BASE_URL
        else:
            codex_token = _read_codex_access_token()
            if not codex_token:
                return None, None
            base_url = _CODEX_AUX_BASE_URL
    else:
        codex_token = _read_codex_access_token()
        if not codex_token:
            return None, None
        base_url = _CODEX_AUX_BASE_URL
    logger.debug("Auxiliary client: Codex OAuth (%s via Responses API)", model)
    real_client = _create_openai_client(
        api_key=codex_token,
        base_url=base_url,
        default_headers=_codex_cloudflare_headers(codex_token, base_url=base_url),
    )
    return CodexAuxiliaryClient(real_client, model), model


def _try_azure_foundry(
    *,
    model: Optional[str] = None,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve an Azure Foundry auxiliary client via the runtime resolver.

    Mirrors the ``_try_anthropic`` / ``_try_nous`` shape but delegates to
    :func:`hermes_cli.runtime_provider._resolve_azure_foundry_runtime` —
    the same resolver the main agent uses — so:

    * ``auth_mode: api_key`` (default) gets the static
      ``AZURE_FOUNDRY_API_KEY`` string.
    * ``auth_mode: entra_id`` gets a callable bearer-token provider
      (``Callable[[], str]`` from
      :mod:`agent.azure_identity_adapter`).
    * Per-model ``api_mode`` auto-routing for GPT-5.x / o-series /
      codex models works.
    * ``model.entra.{tenant_id,client_id,authority,scope}`` config
      fields propagate.
    * Non-default ``model.base_url`` overrides are honored.

    The OpenAI SDK accepts both shapes for ``api_key`` so the caller
    can forward the result without coercion.

    Returns ``(client, model)`` or ``(None, None)`` on failure.
    """
    try:
        from hermes_cli.runtime_provider import _resolve_azure_foundry_runtime
        from hermes_cli.auth import AuthError
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return None, None

    try:
        cfg = load_config_readonly()
        model_cfg = cfg.get("model") if isinstance(cfg, dict) else {}
        if not isinstance(model_cfg, dict):
            model_cfg = {}
    except Exception:
        model_cfg = {}

    try:
        runtime = _resolve_azure_foundry_runtime(
            requested_provider="azure-foundry",
            model_cfg=model_cfg,
            explicit_api_key=explicit_api_key,
            explicit_base_url=explicit_base_url,
            target_model=model,
        )
    except AuthError as exc:
        logger.debug("Auxiliary azure-foundry: %s", exc)
        return None, None
    except Exception as exc:
        logger.debug("Auxiliary azure-foundry runtime error: %s", exc)
        return None, None

    api_key = runtime.get("api_key")
    base_url = str(runtime.get("base_url", "") or "")
    runtime_api_mode = api_mode or runtime.get("api_mode") or "chat_completions"

    # Empty-string check on api_key here would be wrong for callable
    # token providers (callables are truthy and non-empty by definition).
    # Bail only when api_key is None / empty string.
    _has_key = bool(api_key) if not callable(api_key) else True
    if not _has_key or not base_url:
        return None, None

    final_model = _normalize_resolved_model(
        model or str(model_cfg.get("default") or ""),
        "azure-foundry",
    )
    if not final_model:
        # No fallback aux model for Azure — the user must have a
        # deployment name. Surface that as "no client" so the auto
        # chain falls through to the next provider rather than 404ing.
        logger.debug(
            "Auxiliary azure-foundry: no model resolved (model=%r, default=%r)",
            model, model_cfg.get("default"),
        )
        return None, None

    # Azure pre-v1 endpoints sometimes carry api-version query params
    # in the base URL; the OpenAI SDK drops them when joining paths,
    # so lift them out and pass via default_query.
    extra: Dict[str, Any] = {}
    _clean_base, _dq = _extract_url_query_params(base_url)
    if _dq:
        extra["default_query"] = _dq

    client = _create_openai_client(api_key=api_key, base_url=_clean_base, **extra)

    if runtime_api_mode == "codex_responses":
        # GPT-5.x / o-series / codex models on Azure Foundry are
        # Responses-API-only — wrap so chat.completions.create() is
        # translated to /responses behind the scenes.
        return CodexAuxiliaryClient(client, final_model), final_model

    if runtime_api_mode == "anthropic_messages":
        # Forward ``api_key`` verbatim — for static keys it's a string,
        # for Entra ID it's a callable. ``_maybe_wrap_anthropic`` →
        # ``build_anthropic_client`` detects the callable and installs
        # the bearer-injecting httpx hook.
        return _maybe_wrap_anthropic(
            client, final_model, api_key,
            base_url, runtime_api_mode,
        ), final_model

    # chat_completions — return the plain OpenAI client.
    return client, final_model


def _try_anthropic(explicit_api_key: str = None) -> Tuple[Optional[Any], Optional[str]]:
    try:
        from agent.anthropic_adapter import build_anthropic_client, resolve_anthropic_token
    except ImportError:
        return None, None

    pool_present, entry = _select_pool_entry("anthropic")
    if pool_present and entry is not None:
        token = explicit_api_key or _pool_runtime_api_key(entry)
    else:
        # Pool absent, OR pool present but no usable entry (expired token +
        # stale refresh_token, all entries exhausted, etc). Fall through to the
        # legacy resolver instead of hard-failing: a temporarily dead pool
        # entry must not wedge auxiliary tasks when a valid standalone
        # credential (ANTHROPIC_TOKEN, credentials file, API key) exists. This
        # matches the openrouter and codex paths, which already fall back to
        # their env/auth-store credential on (True, None). Without this, the
        # goal judge and every other Anthropic-routed side channel died with
        # "no auxiliary client configured" while the main session stayed
        # healthy (it resolves the env token directly).
        entry = None
        token = explicit_api_key or resolve_anthropic_token()
    if not token:
        return None, None

    # Allow base URL override from config.yaml model.base_url, but only when:
    #   1. the configured provider is anthropic (otherwise a non-Anthropic
    #      base_url, e.g. Codex endpoint, would leak into Anthropic requests), AND
    #   2. the override URL actually points at an Anthropic-compatible endpoint.
    # Without gate (2), operators who route main-session traffic through a
    # non-Anthropic provider that accepts Anthropic-format requests (e.g.
    # OpenRouter at openrouter.ai/api/v1, with provider=anthropic in config.yaml)
    # would have every auxiliary side-channel call (memory extractors,
    # reflection, vision, title generation) 401 from the foreign host —
    # see issue #52608.
    base_url = _pool_runtime_base_url(entry, _ANTHROPIC_DEFAULT_BASE_URL) if pool_present else _ANTHROPIC_DEFAULT_BASE_URL
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, dict):
            cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
            if cfg_provider == "anthropic":
                cfg_base_url = (model_cfg.get("base_url") or "").strip().rstrip("/")
                if cfg_base_url and _is_anthropic_compatible_host(cfg_base_url):
                    base_url = cfg_base_url
    except Exception:
        pass

    from agent.anthropic_adapter import _is_oauth_token
    is_oauth = _is_oauth_token(token)
    model = _get_aux_model_for_provider("anthropic") or "claude-haiku-4-5-20251001"
    if _aux_probe_active():
        # Availability probe — token + SDK adapter import resolved; skip
        # real client construction.
        return _AuxProbeClientStub(api_key="", base_url=base_url), model
    logger.debug("Auxiliary client: Anthropic native (%s) at %s (oauth=%s)", model, base_url, is_oauth)
    try:
        real_client = build_anthropic_client(token, base_url)
    except ImportError:
        # The anthropic_adapter module imports fine but the SDK itself is
        # missing — build_anthropic_client raises ImportError at call time
        # when _anthropic_sdk is None.  Treat as unavailable.
        return None, None
    return AnthropicAuxiliaryClient(real_client, model, token, base_url, is_oauth=is_oauth), model


_AUTO_PROVIDER_LABELS = {
    "_try_openrouter": "openrouter",
    "_try_nous": "nous",
    "_try_custom_endpoint": "local/custom",
    "_resolve_api_key_provider": "api-key",
}

_MAIN_RUNTIME_FIELDS = ("provider", "model", "base_url", "api_key", "api_mode", "auth_mode")
_MAIN_RUNTIME_CONTEXT_FIELDS = _MAIN_RUNTIME_FIELDS + ("requested_provider",)


def _normalize_main_runtime(main_runtime: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a sanitized copy of a live main-runtime override.

    Most fields are stripped strings. ``api_key`` may legitimately be a
    zero-arg callable (Azure Foundry Entra ID token provider) — preserve
    those as-is so auxiliary clients inherit the same authentication
    surface as the main agent. The OpenAI SDK accepts ``Callable[[], str]``
    for ``api_key`` and calls it before every request.
    """
    if main_runtime is None:
        # Context-local state is inherited by tool worker wrappers while
        # remaining isolated across concurrent gateway sessions. Never fall
        # back to compatibility mirrors here: another session may have written
        # them most recently, which would leak its endpoint/key into this call.
        main_runtime = _RUNTIME_MAIN_CONTEXT.get()
        if main_runtime is None:
            main_runtime = _compat_runtime_main()
    if not isinstance(main_runtime, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for field in _MAIN_RUNTIME_CONTEXT_FIELDS:
        value = main_runtime.get(field)
        # Preserve a callable api_key (Entra ID bearer provider) unchanged.
        if field == "api_key" and callable(value) and not isinstance(value, str):
            normalized[field] = value
            continue
        if isinstance(value, str) and value.strip():
            normalized[field] = value.strip()
    for identity_field in ("provider", "requested_provider"):
        identity = normalized.get(identity_field)
        if isinstance(identity, str):
            normalized[identity_field] = identity.lower()
    return normalized


def _get_provider_chain() -> List[tuple]:
    """Return the ordered provider detection chain.

    Built at call time (not module level) so that test patches
    on the ``_try_*`` functions are picked up correctly.

    NOTE: ``openai-codex`` is deliberately NOT in this chain.  The
    ChatGPT-account Codex endpoint only accepts a shifting, undocumented
    allow-list of model IDs, so falling back to it with a guessed model
    fails more often than not.  Codex is used only when the user's main
    provider *is* openai-codex (see Step 1 of ``_resolve_auto``) or when
    a caller explicitly requests it with a model.
    """
    return [
        ("openrouter", _try_openrouter),
        ("nous", _try_nous),
        ("local/custom", _try_custom_endpoint),
        ("api-key", _resolve_api_key_provider),
    ]


# ── Auxiliary "recently 402'd" unhealthy-provider cache ────────────────────
#
# When an auxiliary provider returns HTTP 402 (Payment Required / credit
# exhaustion), retrying it on every subsequent aux call is wasteful — the
# provider stays depleted for hours or days, but the chain re-tries it as
# the FIRST entry on every compression/title-gen/session-search call,
# burns ~1 RTT, gets 402 again, then falls back. On a long Discord/LCM
# session that adds up to dozens of doomed 402s.
#
# Solution: when ANY caller observes a payment error against a provider,
# mark it unhealthy for ``_AUX_UNHEALTHY_TTL_SECONDS``. ``_resolve_auto``
# Step-2 and ``_try_payment_fallback`` both consult this cache and skip
# unhealthy entries (logging once per skip-reason so the user sees what
# happened). Entries auto-expire so a topped-up account recovers without
# manual intervention.
#
# Failure isolation: the cache is in-process only. A second hermes
# process won't inherit the unhealthy mark — that's intentional, since
# the user might be running two profiles with different OpenRouter keys.

_AUX_UNHEALTHY_TTL_SECONDS = 600  # 10 minutes
_aux_unhealthy_until: Dict[str, float] = {}
_aux_unhealthy_logged_at: Dict[str, float] = {}

# Map provider names that show up in resolved_provider / explicit-config
# back to the chain labels used by _get_provider_chain(). Keep in sync
# with the alias map in _try_payment_fallback below.
_AUX_UNHEALTHY_LABEL_ALIASES = {
    "openrouter": "openrouter",
    "nous": "nous",
    "custom": "local/custom",
    "local/custom": "local/custom",
    "openai-codex": "openai-codex",
    "codex": "openai-codex",
}


def _normalize_chain_label(provider: str) -> str:
    """Normalize a resolved_provider value to a chain label used by
    ``_get_provider_chain()``. Falls back to the lowercased input for
    direct API-key providers (deepseek, alibaba, minimax, etc.) which
    each report their own provider name from the api-key chain.
    """
    if not provider:
        return ""
    p = str(provider).strip().lower()
    return _AUX_UNHEALTHY_LABEL_ALIASES.get(p, p)


def _mark_provider_unhealthy(provider: str, ttl: Optional[float] = None) -> None:
    """Mark ``provider`` as recently-402'd, hidden from chain iteration
    until the TTL expires. Called from the payment-fallback branches in
    ``call_llm`` and ``acall_llm`` after a confirmed payment error.
    """
    label = _normalize_chain_label(provider)
    if not label:
        return
    expires_at = time.time() + (ttl if ttl is not None else _AUX_UNHEALTHY_TTL_SECONDS)
    _aux_unhealthy_until[label] = expires_at
    logger.warning(
        "Auxiliary: marking %s unhealthy for %ds (payment / credit error). "
        "Subsequent auxiliary calls will skip it until %s.",
        label,
        int(ttl if ttl is not None else _AUX_UNHEALTHY_TTL_SECONDS),
        time.strftime("%H:%M:%S", time.localtime(expires_at)),
    )


def _is_provider_unhealthy(label: str) -> bool:
    """True iff ``label`` is in the unhealthy cache and the TTL hasn't expired.
    Lazily evicts expired entries so the cache stays small.
    """
    if not label:
        return False
    expires_at = _aux_unhealthy_until.get(label)
    if expires_at is None:
        return False
    if time.time() >= expires_at:
        _aux_unhealthy_until.pop(label, None)
        _aux_unhealthy_logged_at.pop(label, None)
        return False
    return True


def _log_skip_unhealthy(label: str, task: Optional[str] = None) -> None:
    """Emit a single info-level log per minute when we skip an unhealthy
    provider. Avoids spamming the log on bursty sessions while still
    giving the user a trail.
    """
    now = time.time()
    last = _aux_unhealthy_logged_at.get(label, 0.0)
    if now - last >= 60:
        _aux_unhealthy_logged_at[label] = now
        expires_at = _aux_unhealthy_until.get(label, now)
        logger.info(
            "Auxiliary %s: skipping %s (recently returned payment error, retry in %ds)",
            task or "call", label, max(0, int(expires_at - now)),
        )


def _reset_aux_unhealthy_cache() -> None:
    """Clear the unhealthy cache. Used by tests and by a future explicit
    user trigger (e.g. ``hermes config aux reset``)."""
    _aux_unhealthy_until.clear()
    _aux_unhealthy_logged_at.clear()


def _is_payment_error(exc: Exception) -> bool:
    """Detect payment/credit/quota exhaustion errors.

    Returns True for HTTP 402 (Payment Required) and for 429/other errors
    whose message indicates billing exhaustion or daily quota exhaustion
    rather than transient rate limiting.

    Daily token quota errors (e.g. Bedrock "Too many tokens per day",
    Vertex AI "quota exceeded") are functionally equivalent to credit
    exhaustion — the provider cannot serve the request until the quota
    resets — and should trigger the same provider-fallback logic.
    """
    status = getattr(exc, "status_code", None)
    if status == 402:
        return True
    err_lower = str(exc).lower()
    # OpenRouter and other providers include "credits" or "afford" in 402 bodies,
    # but sometimes wrap them in 429 or other codes.
    # Daily quota exhaustion from Bedrock, Vertex AI, and similar providers
    # uses different language but is semantically identical to credit exhaustion.
    if status in {402, 403, 404, 429, None}:
        if any(kw in err_lower for kw in (
            "credits", "insufficient funds",
            "can only afford", "billing",
            "payment required",
            "out of funds", "run out of funds",
            "balance_depleted", "no usable credits",
            "model_not_supported_on_free_tier",
            "not available on the free tier",
            "requires a subscription", "upgrade for access",
            "upgrade for higher limits", "reached your session usage limit",
            # Daily / monthly / weekly quota exhaustion keywords
            "quota exceeded", "quota_exceeded",
            "too many tokens per day", "daily limit",
            "tokens per day", "daily quota",
            "resource exhausted",  # Vertex AI / gRPC quota errors
            "weekly usage limit", "weekly limit",  # OpenCode Go weekly subscription cap
        )):
            return True
    return False


def _nous_portal_account_has_fresh_paid_access() -> bool:
    """Return True only when the fresh Nous account API says paid access is allowed."""
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        account_info = get_nous_portal_account_info(force_fresh=True)
        return account_info.paid_service_access is True
    except Exception as exc:
        logger.debug("Auxiliary Nous paid-entitlement refresh check failed: %s", exc)
        return False


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect rate-limit errors that warrant provider fallback.

    Returns True for HTTP 429 errors whose message indicates rate limiting
    (as opposed to billing/quota exhaustion, which _is_payment_error handles).
    Also catches OpenAI SDK RateLimitError instances that may not set
    .status_code on the exception object.
    """
    status = getattr(exc, "status_code", None)
    err_lower = str(exc).lower()

    # OpenAI SDK's RateLimitError sometimes omits .status_code —
    # detect by class name so we don't miss these.  (PR #8023 pattern)
    if type(exc).__name__ == "RateLimitError":
        return True

    if status == 429:
        # Distinguish rate-limit from billing: billing keywords are handled
        # by _is_payment_error, everything else on 429 is a rate limit.
        if any(kw in err_lower for kw in (
            "rate limit", "rate_limit", "too many requests",
            "try again", "retry after", "resets in",
        )):
            return True
        # Generic 429 without billing keywords = likely a rate limit
        if not any(kw in err_lower for kw in (
            "credits", "insufficient funds", "billing",
            "payment required", "can only afford",
            "out of funds", "run out of funds",
            "balance_depleted", "no usable credits",
            "model_not_supported_on_free_tier",
            "not available on the free tier",
        )):
            return True
    return False


def _is_transient_transport_error(exc: Exception) -> bool:
    """Return True for a one-off transport blip worth retrying ON the
    same provider before any provider/model fallback.

    Covers connection/streaming-close errors (via the canonical
    ``_is_connection_error`` detector, shared so the two cannot drift) plus a
    pure 5xx/408 HTTP status. Deliberately narrow: this is the "retry the
    same target once" gate, distinct from ``_is_payment_error`` /
    ``_is_auth_error`` / ``_is_rate_limit_error`` which the except-chain
    handles by switching provider, refreshing creds, or rotating the pool.
    """
    return _failure_scope_is_transient_transport_error(exc)


_DEFAULT_TRANSIENT_RETRIES = 2
# Base for exponential backoff between transient retries (seconds). Overridable
# so tests can zero it out and not sleep real wall-clock time.
_TRANSIENT_RETRY_BACKOFF_BASE = 1.0


def _transient_retry_count() -> int:
    """Number of same-provider retries for a transient transport blip.

    Read from ``auxiliary.transient_retries`` in config.yaml (default 2 →
    3 total attempts). Clamped to [0, 6] to bound worst-case wall time. A
    connection blip to a pinned auxiliary target (e.g. a MoA reference
    advisor) has no meaningful provider fallback, so a couple of retries with
    backoff is the difference between recovering and silently losing the call.
    Best-effort: any config-read failure falls back to the default.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        val = cfg_get(load_config(), "auxiliary", "transient_retries")
        if val is None:
            return _DEFAULT_TRANSIENT_RETRIES
        n = int(val)
        return max(0, min(n, 6))
    except Exception:
        return _DEFAULT_TRANSIENT_RETRIES


def _is_auth_error(exc: Exception) -> bool:
    """Detect auth failures that should trigger provider-specific refresh."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    err_lower = str(exc).lower()
    if "error code: 401" in err_lower or "authenticationerror" in type(exc).__name__.lower():
        return True
    # xAI returns HTTP 403 with "unauthenticated:bad-credentials" when an OAuth2
    # access token has expired or is invalid — semantically a 401 auth failure,
    # even though the status code is 403 (PermissionDenied).
    if status == 403 and "bad-credentials" in err_lower:
        return True
    if "unauthenticated" in err_lower and "bad-credentials" in err_lower:
        return True
    return False


def _is_unsupported_parameter_error(exc: Exception, param: str) -> bool:
    """Detect provider 400s for an unsupported request parameter.

    Different OpenAI-compatible endpoints phrase the same class of error a few
    ways: ``Unsupported parameter: X``, ``unsupported_parameter`` with a
    ``param`` field, ``X is not supported``, ``unknown parameter: X``,
    ``unrecognized request argument: X``.  We match on both the parameter
    name and a generic "unsupported/unknown/unrecognized parameter" marker so
    call sites can reactively retry without the offending key instead of
    surfacing a noisy auxiliary failure.

    Generalizes the temperature-specific detector that originally shipped
    with PR #15621 so the same retry strategy can cover ``max_tokens``,
    ``seed``, ``top_p``, and any future quirk. Credit @nicholasrae (PR #15416)
    for the generalization pattern.
    """
    param_lower = (param or "").lower()
    if not param_lower:
        return False
    err_lower = str(exc).lower()
    if param_lower not in err_lower:
        return False
    return any(marker in err_lower for marker in (
        "unsupported parameter",
        "unsupported_parameter",
        "not supported",
        "does not support",
        "unknown parameter",
        "unrecognized request argument",
        "unrecognized parameter",
        "invalid parameter",
    ))


def _is_unsupported_temperature_error(exc: Exception) -> bool:
    """Back-compat wrapper: detect API errors where the model rejects ``temperature``.

    Delegates to :func:`_is_unsupported_parameter_error`; kept as a separate
    public symbol because existing tests and call sites import it by name.
    """
    return _is_unsupported_parameter_error(exc, "temperature")


def _is_structured_output_rejection(exc: Exception) -> bool:
    """Detect provider 400s that reject the structured-output request field.

    One predicate covers the field on both wires, because both come from the
    same caller-supplied ``response_format``:

    - OpenAI wire: the provider rejects ``response_format`` itself. vLLM
      gateways translate the field into ``guided_grammar`` and fail when the
      grammar backend is absent (``compile_grammar_error: No module named
      'xgrammar'``, #82816). Other endpoints answer ``This response_format
      type is unavailable now``.
    - Anthropic wire: the adapter translates ``response_format`` into
      ``output_config.format``. Gateways that predate structured outputs
      (the documented case is the ``bedrock-mantle`` Messages endpoint)
      reject that field: ``output_config: Extra inputs are not permitted``.

    Callers tolerate an unconstrained reply — the title prompt demands bare
    JSON and ``_extract_title_text`` has a loose-JSON fallback — so the right
    reaction is one retry without the field, not a hard failure.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and status not in {400, 422}:
        return False
    err_lower = str(exc).lower()
    # vLLM grammar-backend failures name the translated parameter, not ours.
    if "guided_grammar" in err_lower or "xgrammar" in err_lower or (
        "compile_grammar_error" in err_lower
    ):
        return True
    if "extra inputs are not permitted" in err_lower and (
        "response_format" in err_lower or "output_config" in err_lower
    ):
        return True
    if "response_format" in err_lower and "unavailable" in err_lower:
        return True
    return (
        _is_unsupported_parameter_error(exc, "response_format")
        or _is_unsupported_parameter_error(exc, "output_config")
    )


def _without_structured_output_format(kwargs: dict) -> Optional[dict]:
    """Copy *kwargs* without any ``response_format`` request field.

    Removes the top-level kwarg and the ``extra_body`` entry. Returns None
    when the kwargs carry no such field, so call sites do not retry a
    request that the removal did not change.
    """
    changed = False
    retry_kwargs = dict(kwargs)
    if retry_kwargs.pop("response_format", None) is not None:
        changed = True
    extra_body = retry_kwargs.get("extra_body")
    if isinstance(extra_body, dict) and "response_format" in extra_body:
        remaining = {
            k: v for k, v in extra_body.items() if k != "response_format"
        }
        if remaining:
            retry_kwargs["extra_body"] = remaining
        else:
            retry_kwargs.pop("extra_body", None)
        changed = True
    return retry_kwargs if changed else None


def _is_model_not_found_error(exc: Exception) -> bool:
    """Detect "the requested model doesn't exist" errors (404 / invalid model).

    This fires when a resolved model name is no longer served by the endpoint
    — most commonly when a long-lived process pinned a Portal-recommended model
    that has since been dropped from the Nous → OpenRouter catalog. The Nous
    proxy returns 404 with a body like::

        Model 'gpt-5.4-mini' not found. The requested model does not exist
        in our configuration or OpenRouter catalog.

    Distinct from :func:`_is_payment_error` (which also matches some 404s for
    free-tier/credit language) — this one keys on "does not exist / not found /
    not a valid model" phrasing, and explicitly excludes the billing keywords
    that the payment path already owns so the two predicates don't overlap.
    """
    status = getattr(exc, "status_code", None)
    err_lower = str(exc).lower()
    # Billing/quota 404s belong to _is_payment_error — don't claim them here.
    if any(kw in err_lower for kw in (
        "credits", "insufficient funds", "billing", "out of funds",
        "balance_depleted", "no usable credits", "free tier", "free-tier",
        "not available on the free tier",
    )):
        return False
    if status not in {404, 400, None}:
        return False
    return any(kw in err_lower for kw in (
        "model does not exist",
        "does not exist in our configuration",
        "openrouter catalog",
        "is not a valid model",
        "no such model",
        "model not found",
        "the model `",            # OpenAI-style: "The model `X` does not exist"
        "model_not_found",
        "unknown model",
    ))


def _is_model_incompatible_error(exc: Exception) -> bool:
    """Detect "this route cannot serve this model" 400s (capability mismatch).

    Distinct from :func:`_is_model_not_found_error` (the model does not exist
    anywhere): here the model name is valid but the *current provider/account*
    is structurally unable to run it. The canonical case is a configured
    fallback that cannot run the main model — e.g. an ``openai-codex`` /
    ChatGPT-account fallback asked to compress a ``glm-5.2`` conversation::

        Error code: 400 - {'detail': "The 'glm-5.2' model is not supported
        when using Codex with a ChatGPT account."}

    The candidate authenticates fine and builds a client, so the auth and
    payment predicates don't fire and the call would otherwise raise and
    abort the whole auxiliary task (commonly compression — which then drops
    middle turns and churns the session, destroying the prompt cache).
    Treating it as a fallback-worthy capability error lets the chain skip the
    incapable route and continue to the next candidate, mirroring the
    context-window feasibility screen (#52392).

    Billing/quota 400s belong to :func:`_is_payment_error`; "model does not
    exist" 400s belong to :func:`_is_model_not_found_error`. This predicate
    explicitly excludes both so the three don't overlap.
    """
    status = getattr(exc, "status_code", None)
    if status not in {400, None}:
        return False
    err_lower = str(exc).lower()
    # Not-found 400s ("invalid model ID", "model does not exist") are owned by
    # _is_model_not_found_error. Billing/free-tier 400s are owned by the
    # payment path — key on the billing keywords directly here rather than
    # calling _is_payment_error(), because that predicate is status-gated
    # ({402,403,404,429,None}) and would not recognise a 400-coded billing
    # body, letting it leak into this capability bucket.
    if _is_model_not_found_error(exc):
        return False
    if any(kw in err_lower for kw in (
        "credits", "insufficient funds", "billing", "out of funds",
        "balance_depleted", "no usable credits", "payment required",
        "free tier", "free-tier", "not available on the free tier",
        "model_not_supported_on_free_tier", "quota",
    )):
        return False
    return any(kw in err_lower for kw in (
        "is not supported when using",   # codex/ChatGPT-account model gating
        "model is not supported",
        "not supported with this",
        "not supported for this account",
        "model_not_supported",
        "does not support this model",
        "unsupported model",
    ))


def _is_invalid_aux_response_error(exc: Exception) -> bool:
    """Detect provider responses that authenticated but cannot serve aux shape.

    Some OpenAI-compatible routes return HTTP 200 with an empty/malformed
    ChatCompletion instead of a normal provider error.  That is still a
    provider/model capability failure for auxiliary tasks: downstream callers
    need ``choices[0].message`` and should be able to continue through the
    same fallback path as explicit model-incompatibility errors.
    """
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return (
        "auxiliary " in msg
        and "llm returned invalid response" in msg
        and "choices[0].message" in msg
    )


def _evict_cached_clients(provider: str) -> None:
    """Drop cached auxiliary clients for a provider so fresh creds are used."""
    normalized = _normalize_aux_provider(provider)
    with _client_cache_lock:
        stale_keys = [
            key for key in _client_cache
            if _normalize_aux_provider(str(key[0])) == normalized
        ]
        for key in stale_keys:
            client = _client_cache.get(key, (None, None, None))[0]
            if client is not None:
                _close_cached_client(client)
            _client_cache.pop(key, None)


def _evict_cached_client_instance(target: Any) -> bool:
    """Drop the cache entry whose stored client is *target*.

    Used when a specific cached client has been poisoned (closed httpx
    transport after a timeout, broken streaming session, etc.) so the next
    auxiliary call rebuilds rather than reusing the dead instance.

    Walks both sync and async wrappers (``CodexAuxiliaryClient``,
    ``AnthropicAuxiliaryClient``, ``AsyncCodexAuxiliaryClient``, etc.) via
    their ``_real_client`` attribute so a timeout that closes the underlying
    ``OpenAI`` (or native provider) client evicts every cached shim that
    exposed it. Async wrappers must mirror their sync sibling's
    ``_real_client`` for this to work — otherwise the sync entry is evicted
    but the async entry survives and keeps reusing the dead transport.

    Returns True when at least one entry was evicted.
    """
    if target is None:
        return False
    evicted = False
    with _client_cache_lock:
        for key in list(_client_cache.keys()):
            entry = _client_cache.get(key)
            if entry is None:
                continue
            cached = entry[0]
            if cached is None:
                continue
            real = getattr(cached, "_real_client", None)
            if cached is target or real is target:
                del _client_cache[key]
                evicted = True
    return evicted


def _pool_cache_hint(
    provider: str,
    *,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a stable cache discriminator for pooled providers."""
    normalized = _normalize_aux_provider(provider)
    if normalized == "auto":
        runtime = _normalize_main_runtime(main_runtime)
        normalized = _normalize_aux_provider(runtime.get("provider") or _read_main_provider())
    if normalized in {"", "auto", "custom"}:
        return ""
    entry = _peek_pool_entry(normalized)
    if entry is None:
        return ""
    entry_id = str(getattr(entry, "id", "") or "").strip()
    if not entry_id:
        return ""
    return f"{normalized}:{entry_id}"


def _pool_error_context(exc: Exception) -> Dict[str, Any]:
    status = getattr(exc, "status_code", None)
    payload: Dict[str, Any] = {"message": str(exc)}
    if status is not None:
        payload["status_code"] = status
    return payload


def _recoverable_pool_provider(
    resolved_provider: str,
    client: Any,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Infer which provider pool can recover the current auxiliary client."""
    normalized = _normalize_aux_provider(resolved_provider)
    if normalized not in {"", "auto", "custom"}:
        return normalized
    base = str(getattr(client, "base_url", "") or "")
    if base_url_host_matches(base, "chatgpt.com"):
        return "openai-codex"
    if base_url_host_matches(base, "openrouter.ai"):
        return "openrouter"
    if base_url_host_matches(base, "inference-api.nousresearch.com"):
        return "nous"
    if base_url_host_matches(base, "api.anthropic.com"):
        return "anthropic"
    if base_url_host_matches(base, "githubcopilot.com"):
        return "copilot"
    if base_url_host_matches(base, "api.kimi.com"):
        return "kimi-coding"
    if base_url_host_matches(base, "api.x.ai"):
        return "xai-oauth"
    # For api_key providers not in the hardcoded list (e.g. opencode-go), match
    # the client base URL against all registered api_key providers so that
    # credential-pool rotation works for any provider the user configured.
    if main_runtime:
        rt = _normalize_main_runtime(main_runtime)
        rt_provider = rt.get("provider", "")
        if rt_provider and rt_provider not in {"", "auto", "custom"}:
            try:
                from hermes_cli.auth import PROVIDER_REGISTRY
                pconfig = PROVIDER_REGISTRY.get(rt_provider)
                if pconfig and getattr(pconfig, "auth_type", None) == "api_key":
                    rt_base = str(getattr(pconfig, "inference_base_url", "") or "").rstrip("/")
                    if rt_base and base_url_host_matches(base, base_url_hostname(rt_base)):
                        return rt_provider
            except Exception:
                pass
    return None


def _recover_provider_pool(provider: str, exc: Exception, *, failed_api_key: str = "") -> bool:
    """Try same-provider credential-pool recovery for auxiliary calls.

    ``failed_api_key`` is the API key that was actually used for the failing
    request.  Passing it lets mark_exhausted_and_rotate identify the correct
    pool entry even when another process has already rotated the pool (which
    would leave current() as None, causing the wrong entry to be marked).
    """
    normalized = _normalize_aux_provider(provider)
    try:
        pool = load_pool(normalized)
    except Exception as load_exc:
        logger.debug("Auxiliary client: could not load pool for %s recovery: %s", normalized, load_exc)
        return False
    if not pool or not pool.has_credentials():
        return False

    status_code = getattr(exc, "status_code", None)
    error_context = _pool_error_context(exc)
    hint = failed_api_key or None

    if _is_auth_error(exc):
        refreshed = pool.try_refresh_current()
        if refreshed is not None:
            _evict_cached_clients(normalized)
            return True
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=status_code if status_code is not None else 401,
            error_context=error_context,
            api_key_hint=hint,
        )
        if next_entry is not None:
            _evict_cached_clients(normalized)
            return True
        return False

    if _is_payment_error(exc) or _is_rate_limit_error(exc):
        fallback_status = 402 if _is_payment_error(exc) else 429
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=status_code if status_code is not None else fallback_status,
            error_context=error_context,
            api_key_hint=hint,
        )
        if next_entry is not None:
            _evict_cached_clients(normalized)
            return True
    return False


def _retry_same_provider_sync(
    *,
    task: Optional[str],
    resolved_provider: str,
    resolved_model: Optional[str],
    resolved_base_url: Optional[str],
    resolved_api_key: Optional[str],
    resolved_api_mode: Optional[str],
    main_runtime: Optional[Dict[str, Any]],
    final_model: Optional[str],
    messages: list,
    temperature: Optional[float],
    max_tokens: Optional[int],
    tools: Optional[list],
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: Optional[dict],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    if task == "vision":
        effective_provider, retry_client, retry_model = resolve_vision_provider_client(
            provider=resolved_provider,
            model=final_model,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            async_mode=False,
        )
    else:
        retry_client, retry_model = _get_cached_client(
            resolved_provider,
            resolved_model,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            api_mode=resolved_api_mode,
            main_runtime=main_runtime,
        )
        effective_provider = _effective_provider_for_client(
            retry_client, resolved_provider,
        )
    if retry_client is None:
        raise RuntimeError(
            f"Auxiliary {task or 'call'}: provider {resolved_provider} could not be rebuilt after recovery"
        )

    retry_base = str(getattr(retry_client, "base_url", "") or "")
    retry_kwargs = _build_call_kwargs(
        effective_provider or resolved_provider,
        retry_model or final_model,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        timeout=effective_timeout,
        extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=retry_base or resolved_base_url,
        task=task,
    )
    # Preserve per-request attribution headers (e.g. Copilot's
    # ``x-initiator: user``) across the rebuilt-client retry — dropping them
    # here would let a recovery retry silently lose capability gating (#60293).
    if extra_headers:
        retry_kwargs["extra_headers"] = dict(extra_headers)
    if _is_anthropic_compat_endpoint(resolved_provider, retry_base):
        retry_kwargs["messages"] = _convert_openai_images_to_anthropic(retry_kwargs["messages"])
    return _validate_llm_response(
        _relay_sync_completion(
            retry_client,
            retry_kwargs,
            provider=resolved_provider,
            api_mode=resolved_api_mode,
        ),
        task,
    )


async def _retry_same_provider_async(
    *,
    task: Optional[str],
    resolved_provider: str,
    resolved_model: Optional[str],
    resolved_base_url: Optional[str],
    resolved_api_key: Optional[str],
    resolved_api_mode: Optional[str],
    final_model: Optional[str],
    messages: list,
    temperature: Optional[float],
    max_tokens: Optional[int],
    tools: Optional[list],
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: Optional[dict],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    if task == "vision":
        effective_provider, retry_client, retry_model = resolve_vision_provider_client(
            provider=resolved_provider,
            model=final_model,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            async_mode=True,
        )
    else:
        retry_client, retry_model = _get_cached_client(
            resolved_provider,
            resolved_model,
            async_mode=True,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            api_mode=resolved_api_mode,
        )
        effective_provider = _effective_provider_for_client(
            retry_client, resolved_provider,
        )
    if retry_client is None:
        raise RuntimeError(
            f"Auxiliary {task or 'call'}: provider {resolved_provider} could not be rebuilt after recovery"
        )

    retry_base = str(getattr(retry_client, "base_url", "") or "")
    retry_kwargs = _build_call_kwargs(
        effective_provider or resolved_provider,
        retry_model or final_model,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        timeout=effective_timeout,
        extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=retry_base or resolved_base_url,
        task=task,
    )
    # Preserve per-request attribution headers across the rebuilt-client
    # retry — see the sync variant above (#60293).
    if extra_headers:
        retry_kwargs["extra_headers"] = dict(extra_headers)
    if _is_anthropic_compat_endpoint(resolved_provider, retry_base):
        retry_kwargs["messages"] = _convert_openai_images_to_anthropic(retry_kwargs["messages"])
    return _validate_llm_response(
        await _relay_async_completion(
            retry_client,
            retry_kwargs,
            provider=resolved_provider,
            api_mode=resolved_api_mode,
        ),
        task,
    )


def _refresh_provider_credentials(provider: str) -> bool:
    """Refresh short-lived credentials for OAuth-backed auxiliary providers."""
    normalized = _normalize_aux_provider(provider)
    try:
        if normalized == "copilot":
            from hermes_cli.copilot_auth import (
                _jwt_cache,
                _token_fingerprint,
                exchange_copilot_token,
                resolve_copilot_token,
            )

            raw_token, _source = resolve_copilot_token()
            if not str(raw_token or "").strip():
                return False
            _jwt_cache.pop(_token_fingerprint(raw_token), None)
            exchange_copilot_token(raw_token)
            _evict_cached_clients(normalized)
            return True
        if normalized == "openai-codex":
            from hermes_cli.auth import resolve_codex_runtime_credentials

            creds = resolve_codex_runtime_credentials(force_refresh=True)
            if not str(creds.get("api_key", "") or "").strip():
                return False
            _evict_cached_clients(normalized)
            return True
        if normalized == "nous":
            from hermes_cli.auth import resolve_nous_runtime_credentials

            creds = resolve_nous_runtime_credentials(
                timeout_seconds=env_float("HERMES_NOUS_TIMEOUT_SECONDS", 15),
                force_refresh=True,
            )
            if not str(creds.get("api_key", "") or "").strip():
                return False
            _evict_cached_clients(normalized)
            return True
        if normalized == "anthropic":
            from agent.anthropic_credentials import read_claude_code_credentials, _refresh_oauth_token, resolve_anthropic_token

            creds = read_claude_code_credentials()
            token = _refresh_oauth_token(creds) if isinstance(creds, dict) and creds.get("refreshToken") else None
            if not str(token or "").strip():
                token = resolve_anthropic_token()
            if not str(token or "").strip():
                return False
            _evict_cached_clients(normalized)
            return True
        if normalized == "xai-oauth":
            # Preference: pool-level refresh (uses refresh_token from pool entry),
            # then fall back to singleton auth-store resolver.
            pool = load_pool(normalized)
            if pool and pool.has_credentials():
                # Ensure a current entry is selected before trying to refresh.
                pool.select()
                refreshed = pool.try_refresh_current()
                if refreshed is not None and str(getattr(refreshed, "runtime_api_key", "") or "").strip():
                    _evict_cached_clients(normalized)
                    return True
            from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

            creds = resolve_xai_oauth_runtime_credentials(force_refresh=True)
            if not str(creds.get("api_key", "") or "").strip():
                return False
            _evict_cached_clients(normalized)
            return True
        if normalized == "vertex":
            # Mirrors run_agent.py's _try_refresh_vertex_client_credentials
            # for the main conversation loop. Without this branch, an
            # auxiliary Vertex client (vision, title generation, reflection,
            # context compression, ...) that 401s on its ~1h token expiry
            # falls through to the final `return False` below: the stale
            # client is never evicted from _client_cache (whose cache key
            # ignores the rotating bearer token), so every subsequent
            # auxiliary Vertex call keeps 401ing until process restart.
            from agent.vertex_adapter import get_vertex_config

            token, base_url = get_vertex_config()
            if not isinstance(token, str) or not token.strip():
                return False
            if not isinstance(base_url, str) or not base_url.strip():
                return False
            _evict_cached_clients(normalized)
            return True
    except Exception as exc:
        logger.debug("Auxiliary provider credential refresh failed for %s: %s", normalized, exc)
        return False
    return False


def _auth_refresh_provider_for_route(
    resolved_provider: Optional[str],
    client_base_url: str,
) -> str:
    """Return the provider whose short-lived credentials should be refreshed.

    Auto-routed auxiliary calls keep ``resolved_provider == "auto"`` even
    after _get_cached_client() selects a concrete backend. Infer the backend
    from the selected client's base URL so auth refresh works for auto →
    Copilot/Codex/Anthropic/Nous routes too. (#20832)
    """
    normalized = _normalize_aux_provider(resolved_provider)
    if normalized and normalized != "auto":
        return normalized
    if base_url_host_matches(client_base_url, "api.githubcopilot.com"):
        return "copilot"
    if base_url_host_matches(client_base_url, "chatgpt.com"):
        return "openai-codex"
    if base_url_host_matches(client_base_url, "api.anthropic.com"):
        return "anthropic"
    if base_url_host_matches(client_base_url, "inference-api.nousresearch.com"):
        return "nous"
    return normalized


def _fallback_chain_entry(task: Optional[str], fb_label: str) -> Optional[Dict[str, Any]]:
    """Resolve the fallback entry a stable indexed label points at.

    Labels minted by the configured and top-level selectors carry the entry
    index in a stable format. This is only a compatibility fallback for
    callers that did not attach the selected entry to the client; the normal
    execution path uses that attached identity so credentials/pools cannot be
    reconstructed from a display label.
    """
    if not fb_label:
        return None
    match = re.match(r"fallback_chain\[(\d+)\]", fb_label)
    source = "task"
    if not match:
        match = re.match(r"fallback_providers\[(\d+)\]", fb_label)
        source = "main"
    if not match:
        return None
    try:
        if source == "task":
            if not task:
                return None
            chain = _get_auxiliary_task_config(task).get("fallback_chain")
        else:
            from hermes_cli.config import load_config_readonly
            from hermes_cli.fallback_config import get_fallback_chain
            chain = get_fallback_chain(load_config_readonly())
        entry = chain[int(match.group(1))] if isinstance(chain, list) else None
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _fallback_entry_for_candidate(
    task: Optional[str], fb_client: Any, fb_label: str
) -> Dict[str, Any]:
    """Return the selected entry without losing top-level identity metadata."""
    attached = getattr(fb_client, "_hermes_fallback_entry", None)
    if isinstance(attached, dict):
        return attached
    return _fallback_chain_entry(task, fb_label) or {}


def _coerce_positive_timeout(raw: Any) -> Optional[float]:
    """Coerce a config ``timeout`` value to a positive float, or None.

    Rejects bools (``True``/``False`` are ``int`` subclasses in Python) and
    non-positive values. Shared by the aux client's fallback timeout resolver
    and the compression stall-fallback route resolver (#78981).
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def _fallback_entry_timeout(task: Optional[str], fb_label: str) -> Optional[float]:
    """Resolve a per-entry ``timeout`` for a configured fallback candidate.

    A fallback candidate previously inherited the exact timeout the primary
    provider was called with. When that deadline was tuned for the primary
    (or the primary simply consumed its whole budget before failing over),
    the fallback aborted on the same clock even when independently healthy —
    a 163k-token compression that needs ~90s on the fallback died at the
    primary's 30s deadline every turn (#62452).

    Entries in ``auxiliary.<task>.fallback_chain`` may declare their own
    ``timeout`` (seconds). Returns ``None`` when the label is not a
    configured-chain candidate, the entry has no ``timeout``, or the value
    is invalid — callers then keep the task-level timeout, preserving
    existing behavior.
    """
    entry = _fallback_chain_entry(task, fb_label)
    raw = entry.get("timeout") if entry else None
    return _coerce_positive_timeout(raw)


def _fallback_provider_from_label(label: str) -> str:
    """Recover the provider identifier from a fallback display label."""
    match = re.match(
        r"(?:fallback_chain\[\d+\]|fallback_providers\[\d+\]|main-agent)\(([^)]+)\)$",
        label or "",
    )
    return match.group(1).strip() if match else str(label or "").strip()


class _FallbackDestination(NamedTuple):
    provider: str
    base_url: str
    api_mode: Optional[str]
    model: Optional[str]
