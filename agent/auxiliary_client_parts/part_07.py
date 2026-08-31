

def _call_llm_impl(
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
) -> Any:
    """Centralized synchronous LLM call.

    Resolves provider + model (from task config, explicit args, or auto-detect),
    handles auth, request formatting, and model-specific arg adjustments.

    Args:
        task: Auxiliary task name ("compression", "vision",
              "session_search", "skills_hub", "mcp", "title_generation").
              Reads provider:model from config/env. Ignored if provider is set.
        provider: Explicit provider override.
        model: Explicit model override.
        api_mode: Explicit API mode override (e.g. "codex_responses",
              "anthropic_messages"). Takes precedence over task config.
        messages: Chat messages list.
        temperature: Sampling temperature (None = provider default).
        max_tokens: Max output tokens (handles max_tokens vs max_completion_tokens).
        tools: Tool definitions (for function calling).
        timeout: Request timeout in seconds (None = read from auxiliary.{task}.timeout config).
        extra_body: Additional request body fields.
        reasoning_config: Optional Hermes reasoning config for direct model calls
              such as MoA reference/aggregator slots.
        extra_headers: Additional per-request HTTP headers. These override
            client-level defaults for providers that gate capabilities on
            request attribution (for example Copilot's ``x-initiator``).
        stream: When True, return the raw SDK streaming iterator instead of a
            validated complete response. The caller is responsible for consuming
            chunks (and for any fallback). Used by the MoA aggregator so its
            output can stream to the user.
        stream_options: Passed through to the request when stream is True
            (e.g. {"include_usage": True}).

    Returns:
        Response object with .choices[0].message.content, OR — when stream=True —
        the raw streaming iterator from client.chat.completions.create().

    Raises:
        RuntimeError: If no provider is configured.
    """
    # Capture one immutable runtime snapshot for keying, resolution, retries,
    # and fallbacks. Reading ambient state independently in each phase lets a
    # concurrent /model switch produce a key for one runtime and a client for
    # another.
    main_runtime = _normalize_main_runtime(main_runtime)
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, resolved_api_mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key)
    if api_mode:
        resolved_api_mode = api_mode
    effective_extra_body = _get_task_extra_body(task)
    effective_extra_body.update(extra_body or {})
    effective_provider = resolved_provider

    if task == "vision":
        effective_provider, client, final_model = resolve_vision_provider_client(
            provider=resolved_provider if resolved_provider != "auto" else provider,
            model=resolved_model or model,
            base_url=resolved_base_url or base_url,
            api_key=resolved_api_key or api_key,
            async_mode=False,
            main_runtime=main_runtime,
        )
        if client is None and resolved_provider != "auto" and not resolved_base_url:
            logger.warning(
                "Vision provider %s unavailable, falling back to auto vision backends",
                resolved_provider,
            )
            effective_provider, client, final_model = resolve_vision_provider_client(
                provider="auto",
                model=resolved_model,
                async_mode=False,
                main_runtime=main_runtime,
            )
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup"
            )
        resolved_provider = effective_provider or resolved_provider
    else:
        client, final_model = _get_cached_client(
            resolved_provider,
            resolved_model,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            api_mode=resolved_api_mode,
            main_runtime=main_runtime,
            task=task,
        )
        effective_provider = _effective_provider_for_client(
            client, resolved_provider,
        )
        if client is None:
            # When the user explicitly chose a non-OpenRouter provider but no
            # credentials were found, honor the task fallback_chain before
            # raising.  Missing raw env keys are recoverable for auxiliary
            # tasks because fallback entries may use OAuth / credential-pool
            # auth (for example openai-codex).
            _explicit = (resolved_provider or "").strip().lower()
            if _explicit and _explicit not in {"auto", "openrouter", "custom"}:
                fb_client, fb_model, fb_label = _try_configured_fallback_for_unavailable_client(
                    task, _explicit,
                )
                if fb_client is not None:
                    client, final_model = fb_client, fb_model
                    resolved_provider = fb_label or resolved_provider
                    effective_provider = resolved_provider
                else:
                    raise RuntimeError(
                        f"Provider '{_explicit}' is set in config.yaml but no API key "
                        f"was found. Set the {_explicit.upper()}_API_KEY environment "
                        f"variable, or switch to a different provider with `hermes model`."
                    )
            # For auto/custom with no credentials, try the full auto chain
            # rather than hardcoding OpenRouter (which may be depleted).
            # Pass model=None so each provider uses its own default —
            # resolved_model may be an OpenRouter-format slug that doesn't
            # work on other providers.
            if client is None and not resolved_base_url:
                logger.info("Auxiliary %s: provider %s unavailable, trying auto-detection chain",
                            task or "call", resolved_provider)
                client, final_model = _get_cached_client(
                    "auto", main_runtime=main_runtime, task=task,
                )
                effective_provider = _effective_provider_for_client(
                    client, "auto",
                )
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup")

    effective_timeout = _effective_aux_timeout(task, timeout)
    request_provider = effective_provider or resolved_provider
    compression_config = (
        _get_auxiliary_task_config("compression") if task == "compression" else {}
    )
    fast_compression_cap, effective_extra_body = _compression_fast_lane_controls(
        task,
        actual_provider=request_provider,
        actual_model=final_model,
        requested_provider=provider,
        requested_model=model,
        route_config=compression_config,
        leak_guard_config=compression_config,
        max_tokens=max_tokens,
        extra_body=effective_extra_body,
    )
    _set_relay_auxiliary_route(
        request_provider,
        final_model,
        resolved_api_mode,
    )
    _record_route_info(
        route_info, _fallback_provider_from_label(request_provider), final_model
    )

    # Log what we're about to do — makes auxiliary operations visible
    _base_info = str(getattr(client, "base_url", resolved_base_url) or "")
    if task:
        logger.info("Auxiliary %s: using %s (%s)%s",
                     task, request_provider or "auto", final_model or "default",
                     f" at {_base_info}" if _base_info and "openrouter" not in _base_info else "")

    # Pass the client's actual base_url (not just resolved_base_url) so
    # endpoint-specific temperature overrides can distinguish
    # api.moonshot.ai vs api.kimi.com/coding even on auto-detected routes.
    kwargs = _build_call_kwargs(
        request_provider, final_model, messages,
        temperature=temperature, max_tokens=max_tokens,
        tools=tools, timeout=effective_timeout, extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=_base_info or resolved_base_url, task=task)
    if fast_compression_cap is not None and max_tokens is None:
        # Normal auxiliary calls intentionally omit a cap on most
        # OpenAI-compatible/local providers.  This is the narrow exception:
        # the configured compression route is concrete and certified
        # non-reasoning, so a bounded summary request is intentional.
        # ``max_tokens is None`` restricts the forced param to caps the
        # certified lane itself produced — an explicit caller max_tokens is
        # passed through untouched and keeps _build_call_kwargs's
        # provider-quirk handling (same guard as the fallback path).
        kwargs.update(auxiliary_max_tokens_param(fast_compression_cap, model=final_model))
    if extra_headers:
        kwargs["extra_headers"] = dict(extra_headers)

    # Convert image blocks for Anthropic-compatible endpoints (e.g. MiniMax)
    _client_base = str(getattr(client, "base_url", "") or "")
    if _is_anthropic_compat_endpoint(request_provider, _client_base):
        kwargs["messages"] = _convert_openai_images_to_anthropic(kwargs["messages"])

    # Streaming path: return the raw SDK Stream iterator directly. This is used by
    # the MoA aggregator so its tokens stream to the user. It deliberately skips
    # _validate_llm_response and the temperature/max_tokens/payment fallback chain
    # below — those all assume a complete response object, whereas a stream is
    # consumed chunk-by-chunk by the caller. The caller (the agent's streaming
    # consumer) owns chunk reassembly, stale-stream detection, and falling back to
    # a non-streaming call on error. stream_options is best-effort: providers that
    # reject it surface an error the caller's fallback already handles.
    if stream:
        kwargs["stream"] = True
        if stream_options:
            kwargs["stream_options"] = stream_options
        if task == "moa_aggregator" and isinstance(client, CodexAuxiliaryClient):
            # CodexAuxiliaryClient (openai-codex, xai-oauth, and any other
            # Responses-shim provider) consumes the provider stream internally
            # and returns a completed response object. Routing that nested
            # MoA stream through Relay's generic managed stream makes the
            # manager iterate the completed SimpleNamespace itself (#55933).
            # Return the provider call directly; the MoA facade converts a
            # completed response into a one-chunk delta iterator at its
            # boundary.
            return client.chat.completions.create(**kwargs)
        return _relay_sync_stream(
            client,
            kwargs,
            provider=request_provider,
            api_mode=resolved_api_mode,
        )

    # Handle unsupported temperature, max_tokens vs max_completion_tokens retry,
    # then payment fallback.
    try:
        # Retry on the same provider for a transient transport blip
        # (connection reset / streaming-close / incomplete chunked read / 5xx /
        # 408) before the except-chain below escalates to provider/model
        # fallback. A dropped connection shouldn't abandon an otherwise-healthy
        # provider — this especially matters for pinned auxiliary calls like MoA
        # reference advisors, where "fallback to another provider" is not a
        # meaningful recovery (the advisor is a specific model), so a transient
        # blip that isn't retried simply loses that advisor for the turn (root
        # of the run2 double-advisor "Connection error" collapse — a genuine
        # upstream blip hitting both parallel advisors at once).
        #
        # Attempts are bounded and use exponential backoff. Count is configurable
        # via auxiliary.transient_retries (default 2 retries → 3 total attempts);
        # a second/third failure or any non-transient error falls through to
        # ``first_err`` and the existing fallback handling unchanged. Unified home
        # for the transient retry every auxiliary task shares. (PR #16587)
        try:
            return _validate_llm_response(
                _relay_sync_completion(
                    client,
                    kwargs,
                    provider=request_provider,
                    api_mode=resolved_api_mode,
                    create=lambda request: _create_with_progress(
                        client,
                        request,
                        task,
                        force_stream=_provider_requires_stream(
                            request_provider, _base_info or resolved_base_url,
                        ),
                    ),
                ),
                task,
                provider=request_provider, base_url=_base_info)
        except Exception as transient_err:
            if not _is_transient_transport_error(transient_err):
                raise
            # Compression is on the critical preflight path: a user cannot
            # continue or resume an oversized session until it compacts. A
            # same-provider retry on a timeout means another full ``timeout``-
            # long wall-clock block before the except-chain below can fall
            # back — doubling the user-visible stall (issue #54465). Skip the
            # same-provider retry for compression on a full-budget timeout and
            # fall straight through to provider/model fallback; fast blips (a
            # streaming-close or a 5xx) still retry, since those are cheap.
            if task == "compression" and _is_timeout_error(transient_err):
                logger.info(
                    "Auxiliary compression: timeout on the critical path; "
                    "skipping same-provider retry and falling back: %s",
                    transient_err,
                )
                raise
            _max_transient_retries = _transient_retry_count()
            _last_transient = transient_err
            for _attempt in range(1, _max_transient_retries + 1):
                _backoff = min(_TRANSIENT_RETRY_BACKOFF_BASE * (2.0 ** (_attempt - 1)), 8.0)
                logger.info(
                    "Auxiliary %s: transient transport error (attempt %d/%d); "
                    "retrying same provider after %.1fs before fallback: %s",
                    task or "call", _attempt, _max_transient_retries, _backoff,
                    _last_transient,
                )
                time.sleep(_backoff)
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            client,
                            kwargs,
                            provider=request_provider,
                            api_mode=resolved_api_mode,
                            create=lambda request: _create_with_progress(
                                client,
                                request,
                                task,
                                force_stream=_provider_requires_stream(
                                    request_provider,
                                    _base_info or resolved_base_url,
                                ),
                            ),
                        ),
                        task)
                except Exception as retry_transient:
                    if not _is_transient_transport_error(retry_transient):
                        raise
                    _last_transient = retry_transient
            # Retries exhausted — fall through to first_err fallback handling.
            raise _last_transient
    except Exception as first_err:
        if "temperature" in kwargs and _is_unsupported_temperature_error(first_err):
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("temperature", None)
            logger.info(
                "Auxiliary %s: provider rejected temperature; retrying once without it",
                task or "call",
            )
            try:
                return _validate_llm_response(
                    _relay_sync_completion(
                        client,
                        retry_kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)
            except Exception as retry_err:
                retry_err_str = str(retry_err)
                # If retry still fails, fall through to the max_tokens /
                # payment / auth chains below using the temperature-stripped
                # kwargs.  Re-raise only if the retry hit something those
                # chains won't handle.
                if not (
                    _is_payment_error(retry_err)
                    or _is_connection_error(retry_err)
                    or _is_auth_error(retry_err)
                    or "max_tokens" in retry_err_str
                    or "unsupported_parameter" in retry_err_str
                ):
                    raise
                first_err = retry_err
                kwargs = retry_kwargs

        if _is_structured_output_rejection(first_err):
            retry_kwargs = _without_structured_output_format(kwargs)
            if retry_kwargs is not None:
                logger.info(
                    "Auxiliary %s: provider rejected the structured-output "
                    "format field; retrying once without it (schema "
                    "enforcement degrades to prompt compliance): %s",
                    task or "call", _safe_provider_exception_text(first_err),
                )
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            client,
                            retry_kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    # Same contract as the temperature rung: fall through to
                    # the max_tokens / payment / auth chains below with the
                    # stripped kwargs; re-raise anything those chains do not
                    # handle.
                    if not (
                        _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_auth_error(retry_err)
                        or "max_tokens" in str(retry_err)
                        or "unsupported_parameter" in str(retry_err)
                    ):
                        raise
                    first_err = retry_err
                    kwargs = retry_kwargs

        err_str = str(first_err)
        # ZAI vision models (glm-4v-flash etc.) return error code 1210
        # ("API 调用参数有误") when max_tokens is passed on multimodal
        # calls.  The error message does NOT contain "max_tokens" so the
        # generic retry below never fires.  Detect the ZAI-specific error
        # and strip max_tokens before retrying.
        _is_zai_param_error = (
            "1210" in err_str
            and "bigmodel" in str(getattr(client, "base_url", ""))
        )
        if max_tokens is not None and (
            "max_tokens" in err_str
            or "unsupported_parameter" in err_str
            or _is_unsupported_parameter_error(first_err, "max_tokens")
            or _is_zai_param_error
        ):
            kwargs.pop("max_tokens", None)
            kwargs.pop("max_completion_tokens", None)
            try:
                return _validate_llm_response(
                    _relay_sync_completion(
                        client,
                        kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)
            except Exception as retry_err:
                # If the max_tokens retry also hits a payment or connection
                # error, fall through to the fallback chain below.
                if not (_is_payment_error(retry_err) or _is_connection_error(retry_err) or _is_rate_limit_error(retry_err)):
                    raise
                first_err = retry_err

        # ── Stale-model self-heal (Nous Portal recommendation drift) ───
        # A long-lived process can pin a Portal-recommended model that has
        # since been dropped from the Nous → OpenRouter catalog, so every
        # auxiliary call 404s with "model does not exist". Force a fresh
        # Portal fetch and retry once with the current recommendation (or the
        # known-good default). Only applies to Nous-routed calls.
        _heal_is_nous = (
            resolved_provider == "nous"
            or base_url_host_matches(_base_info, "inference-api.nousresearch.com")
        )
        if _is_model_not_found_error(first_err) and _heal_is_nous:
            healed_model = _refresh_nous_recommended_model(
                vision=(task == "vision"), stale_model=kwargs.get("model"))
            if healed_model and healed_model != kwargs.get("model"):
                logger.warning(
                    "Auxiliary %s: model %r no longer in Nous catalog; "
                    "retrying with refreshed recommendation %r",
                    task or "call", kwargs.get("model"), healed_model,
                )
                kwargs["model"] = healed_model
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    first_err = retry_err

        # ── Nous auth refresh parity with main agent ──────────────────
        client_is_nous = (
            resolved_provider == "nous"
            or base_url_host_matches(_base_info, "inference-api.nousresearch.com")
        )
        if (
            _is_payment_error(first_err)
            and client_is_nous
            and _nous_portal_account_has_fresh_paid_access()
        ):
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=False,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                main_runtime=main_runtime,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info(
                    "Auxiliary %s: refreshed Nous runtime credentials after paid account check, retrying",
                    task or "call",
                )
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            refreshed_client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    if not (
                        _is_auth_error(retry_err)
                        or _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_rate_limit_error(retry_err)
                    ):
                        raise
                    first_err = retry_err

        if _is_auth_error(first_err) and client_is_nous:
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=False,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                main_runtime=main_runtime,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info("Auxiliary %s: refreshed Nous runtime credentials after 401, retrying",
                            task or "call")
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                return _validate_llm_response(
                    _relay_sync_completion(
                        refreshed_client,
                        kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)

        # ── Auth refresh retry ───────────────────────────────────────
        auth_refresh_provider = _auth_refresh_provider_for_route(
            resolved_provider, _base_info)
        if (_is_auth_error(first_err)
                and auth_refresh_provider not in {"auto", "", None}
                and not client_is_nous):
            if _refresh_provider_credentials(auth_refresh_provider):
                if auth_refresh_provider != _normalize_aux_provider(resolved_provider):
                    # The stale client is cached under the route label
                    # (e.g. "auto"), not the concrete backend we refreshed.
                    _evict_cached_clients(resolved_provider)
                logger.info(
                    "Auxiliary %s: refreshed %s credentials after auth error, retrying",
                    task or "call", auth_refresh_provider,
                )
                return _retry_same_provider_sync(
                    task=task,
                    resolved_provider=auth_refresh_provider,
                    resolved_model=resolved_model or final_model,
                    resolved_base_url=resolved_base_url,
                    resolved_api_key=resolved_api_key,
                    resolved_api_mode=resolved_api_mode,
                    main_runtime=main_runtime,
                    final_model=final_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    effective_timeout=effective_timeout,
                    effective_extra_body=effective_extra_body,
                    reasoning_config=reasoning_config,
                    extra_headers=extra_headers,
                )

        # ── Same-provider credential-pool recovery ─────────────────────
        pool_provider = _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime)
        # Capture the exact API key used so mark_exhausted_and_rotate can find
        # the correct pool entry even when another process rotated the pool
        # between this call and recovery (which leaves current()=None and makes
        # _select_unlocked() return the NEXT key by mistake).
        _client_api_key = str(getattr(client, "api_key", "") or "")
        if pool_provider and (_is_auth_error(first_err) or _is_payment_error(first_err) or _is_rate_limit_error(first_err)):
            recovery_err = first_err
            # Skip the extra retry for clear payment/quota errors — the endpoint
            # won't accept another request with the same exhausted key.
            if _is_rate_limit_error(first_err) and not _is_payment_error(first_err):
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    if not (_is_auth_error(retry_err) or _is_payment_error(retry_err) or _is_rate_limit_error(retry_err)):
                        raise
                    recovery_err = retry_err
            if _recover_provider_pool(pool_provider, recovery_err, failed_api_key=_client_api_key):
                logger.info(
                    "Auxiliary %s: recovered %s via credential-pool rotation after %s",
                    task or "call", pool_provider, type(recovery_err).__name__,
                )
                try:
                    return _retry_same_provider_sync(
                        task=task,
                        resolved_provider=resolved_provider,
                        resolved_model=resolved_model,
                        resolved_base_url=resolved_base_url,
                        resolved_api_key=resolved_api_key,
                        resolved_api_mode=resolved_api_mode,
                        main_runtime=main_runtime,
                        final_model=final_model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        effective_timeout=effective_timeout,
                        effective_extra_body=effective_extra_body,
                        reasoning_config=reasoning_config,
                        extra_headers=extra_headers,
                    )
                except Exception as retry2_err:
                    # The rotated key also hit a quota/auth wall.  Mark it
                    # immediately so concurrent processes don't make a
                    # redundant API call to discover it's exhausted too.
                    # Then fall through to the payment fallback below so
                    # alternative providers can still serve the request.
                    if (_is_payment_error(retry2_err) or _is_auth_error(retry2_err)
                            or _is_rate_limit_error(retry2_err)):
                        _recover_provider_pool(pool_provider, retry2_err)
                        first_err = retry2_err
                    else:
                        raise

        # ── Payment / credit exhaustion fallback ──────────────────────
        # When the resolved provider returns 402 or a credit-related error,
        # try alternative providers instead of giving up.  This handles the
        # common case where a user runs out of OpenRouter credits but has
        # Codex OAuth or another provider available.
        #
        # ── Connection error fallback ────────────────────────────────
        # When a provider endpoint is unreachable (DNS failure, connection
        # refused, timeout), try alternative providers.  This handles stale
        # Codex/OAuth tokens that authenticate but whose endpoint is down,
        # and providers the user never configured that got picked up by
        # the auto-detection chain.
        #
        # ── Rate-limit fallback (#13579) ─────────────────────────────
        # When the provider returns a 429 rate-limit (not billing), fall
        # back to an alternative provider instead of exhausting retries
        # against the same rate-limited endpoint.
        #
        # ── Auth error fallback (#21165) ─────────────────────────────
        # When the resolved provider returns 401 and neither the Nous
        # refresh path nor explicit provider credential refresh applies,
        # fall back to an alternative provider instead of dropping the
        # auxiliary task on the floor (silent compression failure /
        # message loss). Auth is NOT a capacity error: it only bypasses
        # the explicit-provider gate when the user is in auto mode.
        should_fallback = (
            _is_auth_error(first_err)
            or _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
            or _is_model_incompatible_error(first_err)
            or _is_invalid_aux_response_error(first_err)
            or _is_transient_transport_error(first_err)
        )
        # Respect explicit provider choice for transient errors (auth, request
        # validation, etc.) but allow fallback when the provider clearly cannot
        # serve the request due to capacity: payment/quota exhaustion and
        # connection failures are capacity problems, not request constraints.
        # See #26803: daily token quota (429 + "too many tokens per day") must
        # fall back just like a 402 credit error.
        is_auto = resolved_provider in {"auto", "", None}
        # Capacity errors bypass the explicit-provider gate: the provider
        # literally cannot serve this request regardless of user intent.
        # Rate limits are included: after retries are exhausted, a 429 means
        # the provider cannot serve this request — fall back. See #52228.
        # Model-incompatibility 400s are also a hard capability mismatch (the
        # route cannot run this model at all — e.g. a codex/ChatGPT-account
        # fallback asked to compress a glm-5.2 conversation), so they bypass
        # the explicit-provider gate and continue to the next candidate
        # instead of aborting the auxiliary task and churning the session.
        is_capacity_error = (
            _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
            or _is_model_incompatible_error(first_err)
            or _is_invalid_aux_response_error(first_err)
            or _is_transient_transport_error(first_err)
        )
        if should_fallback and (is_auto or is_capacity_error):
            if _is_auth_error(first_err):
                reason = "auth error"
            elif _is_payment_error(first_err):
                reason = "payment error"
                # Resolve the actual provider label (resolved_provider may be
                # "auto"; the client's base_url tells us which backend got the
                # 402). Mark THAT label unhealthy so subsequent aux calls
                # skip it instead of paying another doomed RTT.
                _mark_provider_unhealthy(
                    _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime) or resolved_provider
                )
            elif _is_rate_limit_error(first_err):
                reason = "rate limit"
            elif _is_model_incompatible_error(first_err):
                reason = "model incompatible with route"
            elif _is_invalid_aux_response_error(first_err):
                reason = "invalid provider response"
            elif _is_endpoint_unreachable_error(first_err):
                reason = "endpoint unreachable"
            elif _is_timeout_error(first_err):
                reason = "timeout"
            elif _is_connection_error(first_err):
                reason = "connection blip"
            else:
                reason = "connection error"
            logger.info("Auxiliary %s: %s on %s (%s), trying fallback",
                        task or "call", reason, resolved_provider, _safe_provider_exception_text(first_err))

            # Keep the failure scope attached to the failed route. Endpoint
            # failures invalidate every model behind that URL; timeouts,
            # rate limits, and model errors invalidate only one deployment.
            from agent.backend_identity import FailureScope, classify_failure_scope
            failure_scope = classify_failure_scope(reason)
            _chain_failed_model = (
                final_model if failure_scope is FailureScope.MODEL else None
            )
            _failed_base_url = _base_info or resolved_base_url
            _failed_api_key = next(
                (
                    value for value in (
                        getattr(client, "api_key", None),
                        resolved_api_key,
                        api_key,
                    )
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            call_kwargs = {
                "task": task,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                "effective_timeout": effective_timeout,
                "effective_extra_body": effective_extra_body,
                "reasoning_config": reasoning_config,
                "route_info": route_info,
            }
            selector_kwargs = {
                "task": task,
                "failed_provider": resolved_provider or "auto",
                "reason": reason,
                "failed_model": _chain_failed_model,
                "failed_base_url": _failed_base_url,
                "failed_api_key": _failed_api_key,
                "failure_scope": failure_scope,
            }
            fallback_error_sink: list[Exception] = []
            fb_resp = _run_fallback_chain_sync(
                _try_configured_fallback_chain, selector_kwargs, call_kwargs,
                fallback_error_sink=fallback_error_sink,
            )
            if fb_resp is None and is_auto:
                fb_resp = _run_fallback_chain_sync(
                    _try_main_fallback_chain, selector_kwargs, call_kwargs,
                    fallback_error_sink=fallback_error_sink,
                )
            if fb_resp is not None:
                return fb_resp
            if is_auto:
                payment_seen: set[str] = set()
                while True:
                    fb_client, fb_model, fb_label = _try_payment_fallback(
                        resolved_provider,
                        task,
                        reason="stale fallback credential" if payment_seen else reason,
                    )
                    if fb_client is None or fb_label in payment_seen:
                        break
                    payment_seen.add(fb_label)
                    fb_resp = _call_fallback_candidate_sync(
                        fb_client,
                        fb_model,
                        fb_label,
                        **{key: value for key, value in call_kwargs.items() if key != "route_info"},
                        fallback_error_sink=fallback_error_sink,
                    )
                    if fb_resp is not None:
                        return fb_resp
            else:
                fb_client, fb_model, fb_label = _try_main_agent_model_fallback(
                    resolved_provider,
                    task,
                    reason=reason,
                    failed_model=_chain_failed_model,
                    failed_base_url=_failed_base_url,
                    failed_api_key=_failed_api_key,
                    failure_scope=failure_scope,
                )
                if fb_client is not None:
                    fb_resp = _call_fallback_candidate_sync(
                        fb_client,
                        fb_model,
                        fb_label,
                        **{key: value for key, value in call_kwargs.items() if key != "route_info"},
                        fallback_error_sink=fallback_error_sink,
                    )
                    if fb_resp is not None:
                        return fb_resp
            if fallback_error_sink:
                raise fallback_error_sink[-1]
            # All fallback layers exhausted — emit a single user-visible
            # warning so the operator knows aux task is about to fail.
            # (#26882) The error itself is re-raised below.
            logger.warning(
                "Auxiliary %s: %s on %s and all fallbacks exhausted "
                "(fallback_chain + main agent model). Raising original error.",
                task or "call", reason, resolved_provider,
            )
        # Connection/timeout errors leave the cached client poisoned (closed
        # httpx transport, half-read stream, dead async loop).  Drop it from
        # the cache regardless of whether we found a fallback above so the
        # next auxiliary call rebuilds a fresh client instead of reusing the
        # dead one.  See issue #23432.
        if _is_connection_error(first_err):
            try:
                _evict_cached_client_instance(client)
            except Exception:
                logger.debug("Auxiliary: cache eviction after connection error failed",
                             exc_info=True)
        raise


def extract_content_or_reasoning(response) -> str:
    """Extract content from an LLM response, falling back to reasoning fields.

    Mirrors the main agent loop's behavior when a reasoning model (DeepSeek-R1,
    Qwen-QwQ, etc.) returns ``content=None`` with reasoning in structured fields.

    Resolution order:
      1. ``message.content`` — strip inline think/reasoning blocks, check for
         remaining non-whitespace text.
      2. ``message.reasoning`` / ``message.reasoning_content`` — direct
         structured reasoning fields (DeepSeek, Moonshot, NovitaAI, etc.).
      3. ``message.reasoning_details`` — OpenRouter unified array format.

    Returns the best available text, or ``""`` if nothing found.
    """
    import re

    msg = response.choices[0].message
    content = (msg.content or "").strip()

    if content:
        # Strip inline think/reasoning blocks (mirrors _strip_think_blocks)
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>"
            r".*?"
            r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
            "", content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned

    # Content is empty or reasoning-only — try structured reasoning fields
    reasoning_parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        val = getattr(msg, field, None)
        if val and isinstance(val, str) and val.strip() and val not in reasoning_parts:
            reasoning_parts.append(val.strip())

    details = getattr(msg, "reasoning_details", None)
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = (
                    detail.get("summary")
                    or detail.get("content")
                    or detail.get("text")
                )
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(summary.strip() if isinstance(summary, str) else str(summary))

    if reasoning_parts:
        return "\n\n".join(reasoning_parts)

    return ""


@_relay_auxiliary_call_async
async def async_call_llm(
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
    route_info: Optional[Dict[str, str]] = None,
) -> Any:
    """Run an asynchronous auxiliary LLM request under the configured limit."""
    semaphore = _acquire_async_aux_semaphore(task)
    if semaphore is not None:
        await semaphore.acquire()
    try:
        return await _async_call_llm_impl(
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
            route_info=route_info,
        )
    finally:
        if semaphore is not None:
            semaphore.release()


async def _async_call_llm_impl(
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
    route_info: Optional[Dict[str, str]] = None,
) -> Any:
    """Centralized asynchronous LLM call.

    Same as call_llm() but async. See call_llm() for full documentation.
    """
    # Keep every async phase on the same runtime identity, even if another
    # session switches models while this task is awaiting network I/O.
    main_runtime = _normalize_main_runtime(main_runtime)
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, resolved_api_mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key)
    effective_extra_body = _get_task_extra_body(task)
    effective_extra_body.update(extra_body or {})
    effective_provider = resolved_provider

    if task == "vision":
        effective_provider, client, final_model = resolve_vision_provider_client(
            provider=resolved_provider if resolved_provider != "auto" else provider,
            model=resolved_model or model,
            base_url=resolved_base_url or base_url,
            api_key=resolved_api_key or api_key,
            async_mode=True,
            main_runtime=main_runtime,
        )
        if client is None and resolved_provider != "auto" and not resolved_base_url:
            logger.warning(
                "Vision provider %s unavailable, falling back to auto vision backends",
                resolved_provider,
            )
            effective_provider, client, final_model = resolve_vision_provider_client(
                provider="auto",
                model=resolved_model,
                async_mode=True,
                main_runtime=main_runtime,
            )
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup"
            )
        resolved_provider = effective_provider or resolved_provider
    else:
        client, final_model = _get_cached_client(
            resolved_provider,
            resolved_model,
            async_mode=True,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            api_mode=resolved_api_mode,
            main_runtime=main_runtime,
            task=task,
        )
        effective_provider = _effective_provider_for_client(
            client, resolved_provider,
        )
        if client is None:
            _explicit = (resolved_provider or "").strip().lower()
            if _explicit and _explicit not in {"auto", "openrouter", "custom"}:
                fb_client, fb_model, fb_label = _try_configured_fallback_for_unavailable_client(
                    task, _explicit,
                )
                if fb_client is not None:
                    client, final_model = _to_async_client(
                        fb_client, fb_model or "", is_vision=(task == "vision")
                    )
                    resolved_provider = fb_label or resolved_provider
                    effective_provider = resolved_provider
                else:
                    raise RuntimeError(
                        f"Provider '{_explicit}' is set in config.yaml but no API key "
                        f"was found. Set the {_explicit.upper()}_API_KEY environment "
                        f"variable, or switch to a different provider with `hermes model`."
                    )
            if client is None and not resolved_base_url:
                logger.info("Auxiliary %s: provider %s unavailable, trying auto-detection chain",
                            task or "call", resolved_provider)
                client, final_model = _get_cached_client(
                    "auto",
                    async_mode=True,
                    main_runtime=main_runtime,
                    task=task,
                )
                effective_provider = _effective_provider_for_client(
                    client, "auto",
                )
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup")

    effective_timeout = _effective_aux_timeout(task, timeout)
    request_provider = effective_provider or resolved_provider
    _set_relay_auxiliary_route(
        request_provider,
        final_model,
        resolved_api_mode,
    )
    _record_route_info(
        route_info, _fallback_provider_from_label(request_provider), final_model
    )

    # Pass the client's actual base_url (not just resolved_base_url) so
    # endpoint-specific temperature overrides can distinguish
    # api.moonshot.ai vs api.kimi.com/coding even on auto-detected routes.
    _client_base = str(getattr(client, "base_url", "") or "")
    kwargs = _build_call_kwargs(
        request_provider, final_model, messages,
        temperature=temperature, max_tokens=max_tokens,
        tools=tools, timeout=effective_timeout, extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=_client_base or resolved_base_url, task=task)

    # Convert image blocks for Anthropic-compatible endpoints (e.g. MiniMax)
    if _is_anthropic_compat_endpoint(request_provider, _client_base):
        kwargs["messages"] = _convert_openai_images_to_anthropic(kwargs["messages"])

    try:
        # Retry ONCE on the same provider for a transient transport blip
        # before the except-chain escalates to fallback — see call_llm()
        # for the rationale. (PR #16587)
        _force_stream_async = (
            _provider_requires_stream(
                request_provider, _client_base or resolved_base_url,
            )
            and not isinstance(client, (
                AsyncCodexAuxiliaryClient,
                AsyncAnthropicAuxiliaryClient,
                AsyncBedrockAuxiliaryClient,
            ))
        )

        async def _acreate(_kwargs: Dict[str, Any]) -> Any:
            if _force_stream_async:
                return await _acreate_with_stream(client, _kwargs, task)
            return await client.chat.completions.create(**_kwargs)

        try:
            return _validate_llm_response(
                await _relay_async_completion(
                    client,
                    kwargs,
                    provider=request_provider,
                    api_mode=resolved_api_mode,
                    create=_acreate,
                ),
                task,
                provider=request_provider, base_url=_client_base)
        except Exception as transient_err:
            if not _is_transient_transport_error(transient_err):
                raise
            # See call_llm(): compression is on the critical preflight path,
            # so skip the same-provider retry on a full-budget timeout and
            # fall straight through to fallback (issue #54465).
            if task == "compression" and _is_timeout_error(transient_err):
                logger.info(
                    "Auxiliary compression (async): timeout on the critical "
                    "path; skipping same-provider retry and falling back: %s",
                    transient_err,
                )
                raise
            _max_transient_retries = _transient_retry_count()
            _last_transient = transient_err
            import asyncio
            for _attempt in range(1, _max_transient_retries + 1):
                _backoff = min(_TRANSIENT_RETRY_BACKOFF_BASE * (2.0 ** (_attempt - 1)), 8.0)
                logger.info(
                    "Auxiliary %s (async): transient transport error (attempt %d/%d); "
                    "retrying same provider after %.1fs before fallback: %s",
                    task or "call", _attempt, _max_transient_retries, _backoff,
                    _last_transient,
                )
                await asyncio.sleep(_backoff)
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            client,
                            kwargs,
                            provider=request_provider,
                            api_mode=resolved_api_mode,
                            create=_acreate,
                        ),
                        task,
                    )
                except Exception as retry_transient:
                    if not _is_transient_transport_error(retry_transient):
                        raise
                    _last_transient = retry_transient
            raise _last_transient
    except Exception as first_err:
        if "temperature" in kwargs and _is_unsupported_temperature_error(first_err):
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("temperature", None)
            logger.info(
                "Auxiliary %s (async): provider rejected temperature; retrying once without it",
                task or "call",
            )
            try:
                return _validate_llm_response(
                    await _relay_async_completion(
                        client,
                        retry_kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)
            except Exception as retry_err:
                retry_err_str = str(retry_err)
                if not (
                    _is_payment_error(retry_err)
                    or _is_connection_error(retry_err)
                    or _is_auth_error(retry_err)
                    or "max_tokens" in retry_err_str
                    or "unsupported_parameter" in retry_err_str
                ):
                    raise
                first_err = retry_err
                kwargs = retry_kwargs

        if _is_structured_output_rejection(first_err):
            retry_kwargs = _without_structured_output_format(kwargs)
            if retry_kwargs is not None:
                logger.info(
                    "Auxiliary %s (async): provider rejected the "
                    "structured-output format field; retrying once without "
                    "it (schema enforcement degrades to prompt "
                    "compliance): %s",
                    task or "call", _safe_provider_exception_text(first_err),
                )
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            client,
                            retry_kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    # Same contract as the temperature rung: fall through to
                    # the max_tokens / payment / auth chains below with the
                    # stripped kwargs; re-raise anything those chains do not
                    # handle.
                    if not (
                        _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_auth_error(retry_err)
                        or "max_tokens" in str(retry_err)
                        or "unsupported_parameter" in str(retry_err)
                    ):
                        raise
                    first_err = retry_err
                    kwargs = retry_kwargs

        err_str = str(first_err)
        # ZAI vision models (glm-4v-flash etc.) return error code 1210
        # ("API 调用参数有误") when max_tokens is passed on multimodal
        # calls.  The error message does NOT contain "max_tokens" so the
        # generic retry below never fires.  Detect the ZAI-specific error
        # and strip max_tokens before retrying.
        _is_zai_param_error = (
            "1210" in err_str
            and "bigmodel" in str(getattr(client, "base_url", ""))
        )
        if max_tokens is not None and (
            "max_tokens" in err_str
            or "unsupported_parameter" in err_str
            or _is_unsupported_parameter_error(first_err, "max_tokens")
            or _is_zai_param_error
        ):
            kwargs.pop("max_tokens", None)
            kwargs.pop("max_completion_tokens", None)
            try:
                return _validate_llm_response(
                    await _relay_async_completion(
                        client,
                        kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)
            except Exception as retry_err:
                # If the max_tokens retry also hits a payment or connection
                # error, fall through to the fallback chain below.
                if not (_is_payment_error(retry_err) or _is_connection_error(retry_err) or _is_rate_limit_error(retry_err)):
                    raise
                first_err = retry_err

        # ── Stale-model self-heal (Nous Portal recommendation drift) ───
        # See the sync call_llm() path for the rationale: a long-lived process
        # can pin a Portal-recommended model that has since been dropped from
        # the Nous → OpenRouter catalog, 404'ing every auxiliary call. Force a
        # fresh Portal fetch and retry once with the current recommendation.
        _heal_is_nous = (
            resolved_provider == "nous"
            or base_url_host_matches(_client_base, "inference-api.nousresearch.com")
        )
        if _is_model_not_found_error(first_err) and _heal_is_nous:
            healed_model = _refresh_nous_recommended_model(
                vision=(task == "vision"), stale_model=kwargs.get("model"))
            if healed_model and healed_model != kwargs.get("model"):
                logger.warning(
                    "Auxiliary %s (async): model %r no longer in Nous catalog; "
                    "retrying with refreshed recommendation %r",
                    task or "call", kwargs.get("model"), healed_model,
                )
                kwargs["model"] = healed_model
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    first_err = retry_err

        # ── Nous auth refresh parity with main agent ──────────────────
        client_is_nous = (
            resolved_provider == "nous"
            or base_url_host_matches(_client_base, "inference-api.nousresearch.com")
        )
        if (
            _is_payment_error(first_err)
            and client_is_nous
            and _nous_portal_account_has_fresh_paid_access()
        ):
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=True,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info(
                    "Auxiliary %s (async): refreshed Nous runtime credentials after paid account check, retrying",
                    task or "call",
                )
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            refreshed_client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    if not (
                        _is_auth_error(retry_err)
                        or _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_rate_limit_error(retry_err)
                    ):
                        raise
                    first_err = retry_err

        if _is_auth_error(first_err) and client_is_nous:
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=True,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info("Auxiliary %s (async): refreshed Nous runtime credentials after 401, retrying",
                            task or "call")
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                return _validate_llm_response(
                    await _relay_async_completion(
                        refreshed_client,
                        kwargs,
                        provider=resolved_provider,
                        api_mode=resolved_api_mode,
                    ), task)

        # ── Auth refresh retry (mirrors sync call_llm) ───────────────
        auth_refresh_provider = _auth_refresh_provider_for_route(
            resolved_provider, _client_base)
        if (_is_auth_error(first_err)
                and auth_refresh_provider not in {"auto", "", None}
                and not client_is_nous):
            if _refresh_provider_credentials(auth_refresh_provider):
                if auth_refresh_provider != _normalize_aux_provider(resolved_provider):
                    # The stale client is cached under the route label
                    # (e.g. "auto"), not the concrete backend we refreshed.
                    _evict_cached_clients(resolved_provider)
                logger.info(
                    "Auxiliary %s (async): refreshed %s credentials after auth error, retrying",
                    task or "call", auth_refresh_provider,
                )
                return await _retry_same_provider_async(
                    task=task,
                    resolved_provider=auth_refresh_provider,
                    resolved_model=resolved_model or final_model,
                    resolved_base_url=resolved_base_url,
                    resolved_api_key=resolved_api_key,
                    resolved_api_mode=resolved_api_mode,
                    final_model=final_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    effective_timeout=effective_timeout,
                    effective_extra_body=effective_extra_body,
                    reasoning_config=reasoning_config,
                )

        # ── Same-provider credential-pool recovery (mirrors sync) ─────
        pool_provider = _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime)
        _client_api_key = str(getattr(client, "api_key", "") or "")
        if pool_provider and (_is_auth_error(first_err) or _is_payment_error(first_err) or _is_rate_limit_error(first_err)):
            recovery_err = first_err
            # Skip the extra retry for clear payment/quota errors — the endpoint
            # won't accept another request with the same exhausted key.
            if _is_rate_limit_error(first_err) and not _is_payment_error(first_err):
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            client,
                            kwargs,
                            provider=resolved_provider,
                            api_mode=resolved_api_mode,
                        ), task)
                except Exception as retry_err:
                    if not (_is_auth_error(retry_err) or _is_payment_error(retry_err) or _is_rate_limit_error(retry_err)):
                        raise
                    recovery_err = retry_err
            if _recover_provider_pool(pool_provider, recovery_err, failed_api_key=_client_api_key):
                logger.info(
                    "Auxiliary %s (async): recovered %s via credential-pool rotation after %s",
                    task or "call", pool_provider, type(recovery_err).__name__,
                )
                try:
                    return await _retry_same_provider_async(
                        task=task,
                        resolved_provider=resolved_provider,
                        resolved_model=resolved_model,
                        resolved_base_url=resolved_base_url,
                        resolved_api_key=resolved_api_key,
                        resolved_api_mode=resolved_api_mode,
                        final_model=final_model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        effective_timeout=effective_timeout,
                        effective_extra_body=effective_extra_body,
                        reasoning_config=reasoning_config,
                    )
                except Exception as retry2_err:
                    if (_is_payment_error(retry2_err) or _is_auth_error(retry2_err)
                            or _is_rate_limit_error(retry2_err)):
                        _recover_provider_pool(pool_provider, retry2_err)
                        first_err = retry2_err
                    else:
                        raise

        # ── Payment / connection / rate-limit fallback (mirrors sync call_llm) ──
        # Auth error fallback (#21165): a 401 that survived the refresh path
        # falls back in auto mode just like the sync call_llm() path. Auth is
        # NOT a capacity error, so on an explicit provider it still respects
        # the user's choice (handled by the is_auto/is_capacity_error gate).
        should_fallback = (
            _is_auth_error(first_err)
            or _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
            or _is_model_incompatible_error(first_err)
            or _is_invalid_aux_response_error(first_err)
            or _is_transient_transport_error(first_err)
        )
        # Capacity errors (payment/quota/connection/rate-limit) bypass the
        # explicit-provider gate — the provider cannot serve the request
        # regardless of user intent. Rate limits are included: after retries
        # are exhausted, a 429 means the provider is at capacity. See #52228.
        # See #26803: daily token quota must fall back like a 402 credit error.
        # Model-incompatibility 400s (route cannot run this model at all)
        # bypass the gate too — see the sync call_llm() path for rationale.
        is_auto = resolved_provider in {"auto", "", None}
        is_capacity_error = (
            _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
            or _is_model_incompatible_error(first_err)
            or _is_invalid_aux_response_error(first_err)
            or _is_transient_transport_error(first_err)
        )
        if should_fallback and (is_auto or is_capacity_error):
            if _is_auth_error(first_err):
                reason = "auth error"
            elif _is_payment_error(first_err):
                reason = "payment error"
                _mark_provider_unhealthy(
                    _recoverable_pool_provider(resolved_provider, client) or resolved_provider
                )
            elif _is_rate_limit_error(first_err):
                reason = "rate limit"
            elif _is_model_incompatible_error(first_err):
                reason = "model incompatible with route"
            elif _is_invalid_aux_response_error(first_err):
                reason = "invalid provider response"
            elif _is_endpoint_unreachable_error(first_err):
                reason = "endpoint unreachable"
            elif _is_timeout_error(first_err):
                reason = "timeout"
            elif _is_connection_error(first_err):
                reason = "connection blip"
            else:
                reason = "connection error"
            logger.info("Auxiliary %s (async): %s on %s (%s), trying fallback",
                        task or "call", reason, resolved_provider, _safe_provider_exception_text(first_err))

            # Keep the failure scope attached to the failed route. Endpoint
            # failures invalidate every model behind that URL; timeouts,
            # rate limits, and model errors invalidate only one deployment.
            from agent.backend_identity import FailureScope, classify_failure_scope
            failure_scope = classify_failure_scope(reason)
            _chain_failed_model = (
                final_model if failure_scope is FailureScope.MODEL else None
            )
            _failed_base_url = _client_base or resolved_base_url
            _failed_api_key = next(
                (
                    value for value in (
                        getattr(client, "api_key", None),
                        resolved_api_key,
                        api_key,
                    )
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            call_kwargs = {
                "task": task,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
                "effective_timeout": effective_timeout,
                "effective_extra_body": effective_extra_body,
                "reasoning_config": reasoning_config,
                "route_info": route_info,
            }
            selector_kwargs = {
                "task": task,
                "failed_provider": resolved_provider or "auto",
                "reason": reason,
                "failed_model": _chain_failed_model,
                "failed_base_url": _failed_base_url,
                "failed_api_key": _failed_api_key,
                "failure_scope": failure_scope,
            }
            fallback_error_sink: list[Exception] = []
            fb_resp = await _run_fallback_chain_async(
                _try_configured_fallback_chain, selector_kwargs, call_kwargs,
                fallback_error_sink=fallback_error_sink,
            )
            if fb_resp is None and is_auto:
                fb_resp = await _run_fallback_chain_async(
                    _try_main_fallback_chain, selector_kwargs, call_kwargs,
                    fallback_error_sink=fallback_error_sink,
                )
            if fb_resp is not None:
                return fb_resp
            if is_auto:
                payment_seen: set[str] = set()
                while True:
                    fb_client, fb_model, fb_label = _try_payment_fallback(
                        resolved_provider,
                        task,
                        reason="stale fallback credential" if payment_seen else reason,
                    )
                    if fb_client is None or fb_label in payment_seen:
                        break
                    payment_seen.add(fb_label)
                    async_fb, async_fb_model = _to_async_client(
                        fb_client, fb_model or "", is_vision=(task == "vision")
                    )
                    fb_resp = await _call_fallback_candidate_async(
                        async_fb, async_fb_model or fb_model, fb_label,
                        **{key: value for key, value in call_kwargs.items() if key != "route_info"},
                        fallback_error_sink=fallback_error_sink,
                    )
                    if fb_resp is not None:
                        return fb_resp
            else:
                fb_client, fb_model, fb_label = _try_main_agent_model_fallback(
                    resolved_provider,
                    task,
                    reason=reason,
                    failed_model=_chain_failed_model,
                    failed_base_url=_failed_base_url,
                    failed_api_key=_failed_api_key,
                    failure_scope=failure_scope,
                )
                if fb_client is not None:
                    async_fb, async_fb_model = _to_async_client(
                        fb_client, fb_model or "", is_vision=(task == "vision")
                    )
                    fb_resp = await _call_fallback_candidate_async(
                        async_fb, async_fb_model or fb_model, fb_label,
                        **{key: value for key, value in call_kwargs.items() if key != "route_info"},
                        fallback_error_sink=fallback_error_sink,
                    )
                    if fb_resp is not None:
                        return fb_resp
            if fallback_error_sink:
                raise fallback_error_sink[-1]
            # All fallback layers exhausted — warn before re-raising. (#26882)
            logger.warning(
                "Auxiliary %s (async): %s on %s and all fallbacks exhausted "
                "(fallback_chain + main agent model). Raising original error.",
                task or "call", reason, resolved_provider,
            )
        # Mirror the sync path: drop poisoned clients on connection/timeout
        # so the next aux call rebuilds.  See issue #23432.
        if _is_connection_error(first_err):
            try:
                _evict_cached_client_instance(client)
            except Exception:
                logger.debug("Auxiliary (async): cache eviction after connection error failed",
                             exc_info=True)
        raise
