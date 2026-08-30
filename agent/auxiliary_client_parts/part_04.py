

def _complete_fallback_destination(
    provider: str,
    base_url: str,
    api_mode: Optional[str],
    model: Optional[str],
) -> _FallbackDestination:
    if not api_mode:
        if _endpoint_speaks_anthropic_messages(base_url):
            api_mode = "anthropic_messages"
        else:
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider

                runtime = resolve_runtime_provider(
                    requested=provider,
                    explicit_base_url=base_url or None,
                    target_model=model or "",
                )
                api_mode = str(runtime.get("api_mode") or "").strip() or None
            except Exception:
                pass
    return _FallbackDestination(provider, base_url, api_mode, model)


def _fallback_destination_from_entry(
    entry: Dict[str, Any],
    fb_client: Any,
    fb_model: Optional[str],
) -> _FallbackDestination:
    provider = str(entry.get("provider") or "").strip()
    base_url = str(
        entry.get("base_url") or getattr(fb_client, "base_url", "") or ""
    ).strip()
    api_mode = str(
        entry.get("api_mode") or entry.get("transport") or ""
    ).strip() or None
    model = fb_model or str(entry.get("model") or "").strip() or None
    return _complete_fallback_destination(provider, base_url, api_mode, model)


def _fallback_destination(
    task: Optional[str],
    fb_client: Any,
    fb_model: Optional[str],
    fb_label: str,
) -> _FallbackDestination:
    """Return the resolved route identity used by a fallback request."""
    attached = getattr(fb_client, "_hermes_fallback_destination", None)
    if isinstance(attached, _FallbackDestination):
        return attached

    provider = _fallback_provider_from_label(fb_label)
    base_url = str(getattr(fb_client, "base_url", "") or "")
    api_mode = None
    model = fb_model

    entry = _fallback_chain_entry(task, fb_label)
    if entry is not None:
        return _fallback_destination_from_entry(entry, fb_client, fb_model)

    return _complete_fallback_destination(provider, base_url, api_mode, model)


def _replan_synchronous_cache_sections(
    messages: list,
    tools: Optional[list],
    *,
    destination: _FallbackDestination,
) -> tuple[list, list]:
    """Strip source decoration and plan one synchronous destination locally."""
    from agent.agent_runtime_helpers import (
        configured_cache_ttl,
        plan_cache_sections_for_destination,
    )

    return plan_cache_sections_for_destination(
        messages,
        tools,
        provider=destination.provider,
        base_url=destination.base_url,
        api_mode=destination.api_mode or "",
        model=destination.model or "",
        # Thread the operator's configured tier so auxiliary fallback
        # requests stop regressing a configured 1h to the 5m default
        # (#84733); the planner clamps per-destination (Qwen → 5m). There
        # is no live agent here, so read the same config key agent_init
        # snapshots into agent._cache_ttl.
        cache_ttl=configured_cache_ttl(),
    )


def _call_fallback_candidate_sync(
    fb_client: Any,
    fb_model: Optional[str],
    fb_label: str,
    *,
    task: Optional[str],
    messages: list,
    temperature: Optional[float],
    max_tokens: Optional[int],
    tools: Optional[list],
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: Optional[dict],
    fallback_error_sink: Optional[list] = None,
) -> Optional[Any]:
    """Call one fallback candidate with stale-credential recovery.

    A fallback candidate can itself carry a stale credential (e.g. an expired
    ``ANTHROPIC_TOKEN`` picked up by ``_try_anthropic``). Before this helper,
    such a 401 propagated out of the fallback site and aborted the auxiliary
    task (for compression: a 60s cooldown + context marker) even though other
    healthy candidates remained. Live case: a Codex-timeout → Anthropic
    fallback 401-looped five times in one session (mattalachia debug dump,
    Jul 2026).

    On an auth error: refresh the candidate's provider credentials and retry
    once with a rebuilt client; if the retry also auth-fails (non-refreshable
    expired token), mark the provider unhealthy and return ``None`` so the
    caller can continue to the next fallback layer. Non-auth errors are
    recorded in ``fallback_error_sink`` and also return ``None`` so sibling
    candidates are attempted.

    ``effective_timeout`` is the task-level deadline; a configured-chain
    candidate with its own ``timeout`` entry gets that instead, so a
    fallback tuned differently from the primary is allowed its own budget
    (#62452).
    """
    fb_timeout = _fallback_entry_timeout(task, fb_label)
    if fb_timeout is not None and fb_timeout != effective_timeout:
        logger.info(
            "Auxiliary %s: %s using its configured timeout %.0fs "
            "(task-level was %.0fs)",
            task or "call", fb_label, fb_timeout, effective_timeout,
        )
        effective_timeout = fb_timeout
    destination = _fallback_destination(task, fb_client, fb_model, fb_label)
    task_config = _get_auxiliary_task_config(task) if task == "compression" else {}
    fallback_entry = _fallback_entry_for_candidate(task, fb_client, fb_label)
    fallback_max_tokens, fallback_extra_body = _compression_fast_lane_controls(
        task,
        actual_provider=destination.provider,
        actual_model=destination.model,
        requested_provider=fallback_entry.get("provider"),
        requested_model=fallback_entry.get("model"),
        route_config=fallback_entry,
        leak_guard_config=task_config,
        max_tokens=max_tokens,
        extra_body=effective_extra_body,
    )
    fallback_messages, fallback_tools = _replan_synchronous_cache_sections(
        messages,
        tools,
        destination=destination,
    )
    fb_kwargs = _build_call_kwargs(
        destination.provider, destination.model, fallback_messages,
        temperature=temperature, max_tokens=fallback_max_tokens,
        tools=fallback_tools, timeout=effective_timeout,
        extra_body=fallback_extra_body, reasoning_config=reasoning_config,
        base_url=destination.base_url, task=task)
    if fallback_max_tokens is not None and max_tokens is None:
        fb_kwargs.update(
            auxiliary_max_tokens_param(fallback_max_tokens, model=destination.model)
        )
    try:
        return _validate_llm_response(
            _relay_sync_completion(
                fb_client,
                fb_kwargs,
                provider=destination.provider,
                api_mode=destination.api_mode,
                create=lambda request: _create_with_progress(
                    fb_client,
                    request,
                    task,
                    force_stream=_provider_requires_stream(
                        destination.provider, destination.base_url
                    ),
                ),
            ),
            task,
        )
    except Exception as fb_err:
        if not _is_auth_error(fb_err):
            if not (
                fallback_entry.get("api_key")
                or fallback_entry.get("key_env")
                or fallback_entry.get("api_key_env")
                or fallback_entry.get("credential_pool")
            ):
                _mark_provider_unhealthy(_fallback_provider_from_label(fb_label))
            logger.warning(
                "Auxiliary %s: fallback candidate %s failed (%s) — continuing chain",
                task or "call", fb_label, fb_err,
            )
            if fallback_error_sink is not None:
                fallback_error_sink.append(fb_err)
            return None
        if _fallback_entry_has_isolated_credentials(fallback_entry):
            if fallback_entry.get("credential_pool"):
                _recover_provider_pool(
                    destination.provider,
                    fb_err,
                    failed_api_key=str(
                        getattr(fb_client, "api_key", "")
                        or _fallback_entry_api_key(fallback_entry)
                        or ""
                    ),
                )
            if fallback_error_sink is not None:
                fallback_error_sink.append(fb_err)
            logger.warning(
                "Auxiliary %s: isolated fallback candidate %s has a stale "
                "credential (%s) — continuing chain",
                task or "call", fb_label, fb_err,
            )
            return None
        fb_provider = _auth_refresh_provider_for_route(
            destination.provider, destination.base_url
        )
        if fb_provider not in {"auto", "", None} and _refresh_provider_credentials(fb_provider):
            retry_client, retry_model = _get_cached_client(
                fb_provider,
                destination.model,
                base_url=destination.base_url or None,
                api_mode=destination.api_mode,
            )
            if retry_client is not None:
                retry_destination = _FallbackDestination(
                    fb_provider,
                    destination.base_url
                    or str(getattr(retry_client, "base_url", "") or ""),
                    destination.api_mode,
                    retry_model or destination.model,
                )
                retry_messages, retry_tools = _replan_synchronous_cache_sections(
                    messages,
                    tools,
                    destination=retry_destination,
                )
                retry_max_tokens, retry_extra_body = _compression_fast_lane_controls(
                    task,
                    actual_provider=retry_destination.provider,
                    actual_model=retry_destination.model,
                    requested_provider=fallback_entry.get("provider"),
                    requested_model=fallback_entry.get("model"),
                    route_config=fallback_entry,
                    leak_guard_config=task_config,
                    max_tokens=max_tokens,
                    extra_body=effective_extra_body,
                )
                retry_kwargs = _build_call_kwargs(
                    retry_destination.provider,
                    retry_destination.model,
                    retry_messages,
                    temperature=temperature, max_tokens=retry_max_tokens,
                    tools=retry_tools, timeout=effective_timeout,
                    extra_body=retry_extra_body,
                    reasoning_config=reasoning_config,
                    base_url=retry_destination.base_url, task=task)
                if retry_max_tokens is not None and max_tokens is None:
                    retry_kwargs.update(
                        auxiliary_max_tokens_param(
                            retry_max_tokens, model=retry_destination.model
                        )
                    )
                try:
                    return _validate_llm_response(
                        _relay_sync_completion(
                            retry_client,
                            retry_kwargs,
                            provider=retry_destination.provider,
                            api_mode=retry_destination.api_mode,
                            create=lambda request: _create_with_progress(
                                retry_client,
                                request,
                                task,
                                force_stream=_provider_requires_stream(
                                    retry_destination.provider,
                                    retry_destination.base_url,
                                ),
                            ),
                        ),
                        task,
                    )
                except Exception as retry_err:
                    if not _is_auth_error(retry_err):
                        logger.warning(
                            "Auxiliary %s: refreshed fallback candidate %s failed "
                            "(%s) — continuing chain",
                            task or "call", fb_label, retry_err,
                        )
                        if fallback_error_sink is not None:
                            fallback_error_sink.append(retry_err)
                        return None
        # Refresh unavailable or the refreshed credential still 401s —
        # the token is dead (expired setup token with no refresh token).
        # Quarantine the candidate so subsequent chain walks skip it, and
        # let the caller move on instead of aborting the whole task.
        # A per-entry credential is isolated from the provider-wide health
        # quarantine; another entry under the same label may still recover.
        if not (
            fallback_entry.get("api_key")
            or fallback_entry.get("key_env")
            or fallback_entry.get("api_key_env")
            or fallback_entry.get("credential_pool")
        ):
            _mark_provider_unhealthy(fb_provider or fb_label)
        logger.warning(
            "Auxiliary %s: fallback candidate %s has a stale/unrefreshable "
            "credential (%s) — skipping to next fallback",
            task or "call", fb_label, fb_err,
        )
        return None


async def _call_fallback_candidate_async(
    fb_client: Any,
    fb_model: Optional[str],
    fb_label: str,
    *,
    task: Optional[str],
    messages: list,
    temperature: Optional[float],
    max_tokens: Optional[int],
    tools: Optional[list],
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: Optional[dict],
    fallback_error_sink: Optional[list] = None,
) -> Optional[Any]:
    """Async mirror of :func:`_call_fallback_candidate_sync`."""
    fb_timeout = _fallback_entry_timeout(task, fb_label)
    if fb_timeout is not None and fb_timeout != effective_timeout:
        logger.info(
            "Auxiliary %s: %s using its configured timeout %.0fs "
            "(task-level was %.0fs)",
            task or "call", fb_label, fb_timeout, effective_timeout,
        )
        effective_timeout = fb_timeout
    destination = _fallback_destination(task, fb_client, fb_model, fb_label)
    fallback_entry = _fallback_entry_for_candidate(task, fb_client, fb_label)
    fallback_messages, fallback_tools = _replan_synchronous_cache_sections(
        messages,
        tools,
        destination=destination,
    )
    fb_kwargs = _build_call_kwargs(
        destination.provider, destination.model, fallback_messages,
        temperature=temperature, max_tokens=max_tokens,
        tools=fallback_tools, timeout=effective_timeout,
        extra_body=effective_extra_body, reasoning_config=reasoning_config,
        base_url=destination.base_url, task=task)
    try:
        return _validate_llm_response(
            await _relay_async_completion(
                fb_client,
                fb_kwargs,
                provider=destination.provider,
                api_mode=destination.api_mode,
            ),
            task,
        )
    except Exception as fb_err:
        if not _is_auth_error(fb_err):
            if not (
                fallback_entry.get("api_key")
                or fallback_entry.get("key_env")
                or fallback_entry.get("api_key_env")
                or fallback_entry.get("credential_pool")
            ):
                _mark_provider_unhealthy(_fallback_provider_from_label(fb_label))
            logger.warning(
                "Auxiliary %s: fallback candidate %s failed (%s) — continuing chain",
                task or "call", fb_label, fb_err,
            )
            if fallback_error_sink is not None:
                fallback_error_sink.append(fb_err)
            return None
        if _fallback_entry_has_isolated_credentials(fallback_entry):
            if fallback_entry.get("credential_pool"):
                _recover_provider_pool(
                    destination.provider,
                    fb_err,
                    failed_api_key=str(
                        getattr(fb_client, "api_key", "")
                        or _fallback_entry_api_key(fallback_entry)
                        or ""
                    ),
                )
            if fallback_error_sink is not None:
                fallback_error_sink.append(fb_err)
            logger.warning(
                "Auxiliary %s: isolated fallback candidate %s has a stale "
                "credential (%s) — continuing chain",
                task or "call", fb_label, fb_err,
            )
            return None
        fb_provider = _auth_refresh_provider_for_route(
            destination.provider, destination.base_url
        )
        if fb_provider not in {"auto", "", None} and _refresh_provider_credentials(fb_provider):
            retry_client, retry_model = _get_cached_client(
                fb_provider,
                destination.model,
                async_mode=True,
                base_url=destination.base_url or None,
                api_mode=destination.api_mode,
            )
            if retry_client is not None:
                retry_destination = _FallbackDestination(
                    fb_provider,
                    destination.base_url
                    or str(getattr(retry_client, "base_url", "") or ""),
                    destination.api_mode,
                    retry_model or destination.model,
                )
                retry_messages, retry_tools = _replan_synchronous_cache_sections(
                    messages,
                    tools,
                    destination=retry_destination,
                )
                retry_kwargs = _build_call_kwargs(
                    retry_destination.provider,
                    retry_destination.model,
                    retry_messages,
                    temperature=temperature, max_tokens=max_tokens,
                    tools=retry_tools, timeout=effective_timeout,
                    extra_body=effective_extra_body,
                    reasoning_config=reasoning_config,
                    base_url=retry_destination.base_url, task=task)
                try:
                    return _validate_llm_response(
                        await _relay_async_completion(
                            retry_client,
                            retry_kwargs,
                            provider=retry_destination.provider,
                            api_mode=retry_destination.api_mode,
                        ),
                        task,
                    )
                except Exception as retry_err:
                    if not _is_auth_error(retry_err):
                        logger.warning(
                            "Auxiliary %s: refreshed fallback candidate %s failed "
                            "(%s) — continuing chain",
                            task or "call", fb_label, retry_err,
                        )
                        if fallback_error_sink is not None:
                            fallback_error_sink.append(retry_err)
                        return None
        if not (
            fallback_entry.get("api_key")
            or fallback_entry.get("key_env")
            or fallback_entry.get("api_key_env")
            or fallback_entry.get("credential_pool")
        ):
            _mark_provider_unhealthy(fb_provider or fb_label)
        logger.warning(
            "Auxiliary %s (async): fallback candidate %s has a stale/unrefreshable "
            "credential (%s) — skipping to next fallback",
            task or "call", fb_label, fb_err,
        )
        return None


def _run_fallback_chain_sync(
    selector: Callable[..., Tuple[Optional[Any], Optional[str], str]],
    selector_kwargs: Dict[str, Any],
    call_kwargs: Dict[str, Any],
    fallback_error_sink: Optional[list] = None,
) -> Optional[Any]:
    """Try every selected sibling; candidate failures do not abort the chain."""
    excluded_labels: set[str] = set()
    while True:
        fb_client, fb_model, fb_label = selector(
            **selector_kwargs, excluded_labels=excluded_labels
        )
        if fb_client is None:
            return None
        excluded_labels.add(fb_label)
        _record_route_info(
            call_kwargs.get("route_info"),
            _fallback_provider_from_label(fb_label),
            fb_model,
        )
        candidate_call_kwargs = {
            key: value for key, value in call_kwargs.items() if key != "route_info"
        }
        response = _call_fallback_candidate_sync(
            fb_client, fb_model, fb_label, **candidate_call_kwargs,
            fallback_error_sink=fallback_error_sink,
        )
        if response is not None:
            return response


async def _run_fallback_chain_async(
    selector: Callable[..., Tuple[Optional[Any], Optional[str], str]],
    selector_kwargs: Dict[str, Any],
    call_kwargs: Dict[str, Any],
    fallback_error_sink: Optional[list] = None,
) -> Optional[Any]:
    """Async mirror of :func:`_run_fallback_chain_sync`."""
    excluded_labels: set[str] = set()
    while True:
        fb_client, fb_model, fb_label = selector(
            **selector_kwargs, excluded_labels=excluded_labels
        )
        if fb_client is None:
            return None
        excluded_labels.add(fb_label)
        _record_route_info(
            call_kwargs.get("route_info"),
            _fallback_provider_from_label(fb_label),
            fb_model,
        )
        async_call_kwargs = {
            key: value for key, value in call_kwargs.items() if key != "route_info"
        }
        async_fb, async_fb_model = _to_async_client(
            fb_client, fb_model or "", is_vision=(call_kwargs.get("task") == "vision")
        )
        response = await _call_fallback_candidate_async(
            async_fb, async_fb_model or fb_model, fb_label,
            **async_call_kwargs,
            fallback_error_sink=fallback_error_sink,
        )
        if response is not None:
            return response


def _try_payment_fallback(
    failed_provider: str,
    task: str = None,
    reason: str = "payment error",
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try alternative providers after a payment/credit or connection error.

    Iterates the standard auto-detection chain, skipping the provider that
    failed.

    Returns:
        (client, model, provider_label) or (None, None, "") if no fallback.
    """
    # Normalise the failed provider label for matching.
    skip = failed_provider.lower().strip()
    # Also skip Step-1 main-provider path if it maps to the same backend.
    # (e.g. main_provider="openrouter" → skip "openrouter" in chain)
    main_provider = _read_main_provider()
    skip_labels = {skip}
    if main_provider and main_provider.lower() in skip:
        skip_labels.add(main_provider.lower())
    # Map common resolved_provider values back to chain labels.
    _alias_to_label = {"openrouter": "openrouter", "nous": "nous",
                       "openai-codex": "openai-codex", "codex": "openai-codex",
                       "custom": "local/custom", "local/custom": "local/custom"}
    skip_chain_labels = {_alias_to_label.get(s, s) for s in skip_labels}

    tried = []
    for label, try_fn in _get_provider_chain():
        if label in skip_chain_labels:
            continue
        if _is_provider_unhealthy(label):
            _log_skip_unhealthy(label, task)
            tried.append(f"{label} (unhealthy)")
            continue
        client, model = try_fn()
        if client is not None:
            logger.info(
                "Auxiliary %s: %s on %s — falling back to %s (%s)",
                task or "call", reason, failed_provider, label, model or "default",
            )
            return client, model, label
        tried.append(label)

    logger.warning(
        "Auxiliary %s: %s on %s and no fallback available (tried: %s)",
        task or "call", reason, failed_provider, ", ".join(tried),
    )
    return None, None, ""


def _try_main_agent_model_fallback(
    failed_provider: str,
    task: str = None,
    reason: str = "error",
    failed_model: Optional[str] = None,
    failed_base_url: Optional[str] = None,
    failed_api_key: Optional[str] = None,
    failed_credential_id: Optional[str] = None,
    failure_scope: Any = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Last-resort fallback to the user's main agent provider + model.

    Used after the configured fallback_chain is exhausted (or empty) for
    users with an explicit auxiliary provider.  This is the "safety net"
    layer: if nothing the user asked for can serve the request, try the
    main chat model before giving up.

    ``failure_scope`` selects the identity axis invalidated by the failure,
    mirroring :func:`_try_configured_fallback_chain`.  This matters for
    self-hosted /
    custom endpoints serving several models behind one provider label: the
    aux compression model timing out says nothing about the health of the
    main agent model deployed on the same URL (real incident: aux
    ``glm-5.2`` hung and timed out while main ``macaron-v1-venti`` on the
    identical endpoint was serving 448K-token turns fine — the
    provider-label skip discarded the one fallback that would have worked).

    - Model-specific runtime failures (timeout, rate limit, model-incompatible,
      invalid response) pass ``failed_model``: skip the main model only when it
      IS the exact model that failed.
    - Endpoint failures pass ``failed_base_url``: skip every model behind the
      unreachable endpoint, even when its provider or model differs.
    - Provider-wide failures (auth 401, payment 402) and legacy callers
      leave ``failed_model`` as None, keeping the whole-provider skip —
      the shared credentials/account are broken, so the main model on the
      same provider cannot help either.

    Returns:
        (client, model, provider_label) or (None, None, "") if no fallback.
    """
    main_provider = (_read_main_provider() or "").strip()
    main_model = (_read_main_model() or "").strip()
    if main_provider.lower() == "moa":
        # MoA virtual provider: fall back to the preset's aggregator — the
        # acting model — instead of the unreachable "moa"/<preset-name> pair.
        _agg_provider, _agg_model = _resolve_moa_aggregator(main_model)
        if not _agg_provider or not _agg_model:
            return None, None, ""
        main_provider, main_model = _agg_provider, _agg_model
    if not main_provider or not main_model or main_provider.lower() in {"auto", ""}:
        return None, None, ""

    # Identity + scope semantics owned by agent.backend_identity (#72468):
    # model-scoped failures skip only the exact deployment that failed;
    # provider-wide failures (no failed_model) skip the credential surface.
    from agent.backend_identity import (
        BackendIdentity,
        FailureScope,
        should_skip_candidate,
    )

    skip_model = (failed_model or "").strip().lower() or None
    if failure_scope is None:
        failure_scope = FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL
    if should_skip_candidate(
        BackendIdentity.build(
            provider=main_provider,
            model=main_model,
            base_url=_read_main_base_url(),
            api_key=_read_main_api_key(),
        ),
        BackendIdentity.build(
            provider=failed_provider,
            model=skip_model,
            base_url=failed_base_url,
            api_key=failed_api_key,
            credential_id=failed_credential_id,
            ),
        failure_scope,
    ):
        # The thing that failed IS the main model (or the failure was
        # provider-wide) — nothing to fall back to.
        return None, None, ""
    if _is_provider_unhealthy(main_provider):
        _log_skip_unhealthy(main_provider, task)
        return None, None, ""

    try:
        client, resolved_model = resolve_provider_client(
            provider=main_provider, model=main_model,
        )
    except Exception:
        client, resolved_model = None, None

    if client is None:
        return None, None, ""

    label = f"main-agent({main_provider})"
    logger.info(
        "Auxiliary %s: %s on %s — falling back to main agent model %s (%s)",
        task or "call", reason, failed_provider, label, resolved_model or main_model,
    )
    return client, resolved_model or main_model, label


# ── Context-window screening for runtime fallback chains (issue #52392) ──
#
# When the runtime auxiliary fallback chain selects a candidate that is
# reachable but has a context window smaller than the compression task
# requires, the call errors out instead of continuing to the next, viable
# candidate. The startup feasibility check in
# ``agent.conversation_compression.check_compression_model_feasibility``
# already filters too-small auxiliary models at startup, but the runtime
# fallback chain (``_try_configured_fallback_chain`` and
# ``_try_main_fallback_chain``) does not apply the same filter, so
# compression can stop at the first alive door even if the room behind it
# is too small.
#
# The helpers below screen each candidate by its effective context window
# before it is returned. ``None`` results from ``get_model_context_length``
# are passed through (we cannot prove a model is too small, so we do not
# block it). This preserves the existing fallback surface for
# unrecognised/custom models while closing the gap on the well-known ones.

def _task_minimum_context_length(task: Optional[str]) -> Optional[int]:
    """Return the minimum context length required for an auxiliary task.

    Only ``compression`` carries an explicit minimum today (the same
    ``MINIMUM_CONTEXT_LENGTH`` (64K) floor that
    ``check_compression_model_feasibility`` already enforces at startup).
    Other tasks (``vision``, ``title_generation``,
    ``skills_hub``, ``mcp``, ``session_search``) return ``None`` — they
    have no per-task context floor and the runtime chain must remain
    permissive for them.

    Returns ``None`` for an empty/``None`` task name so the helper is a
    safe no-op when called from generic sites.
    """
    if not task:
        return None
    if task == "compression":
        return MINIMUM_CONTEXT_LENGTH
    return None


def _candidate_context_window(
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
) -> Optional[int]:
    """Resolve the effective context window for a fallback candidate.

    Thin wrapper around :func:`agent.model_metadata.get_model_context_length`
    that swallows probe failures (returns ``None``). Callers treat
    ``None`` as "unknown — pass through" so the existing fallback
    surface is preserved when the context-length resolver chain cannot
    determine a value (custom endpoints, models not in the registry,
    offline endpoints).

    Best-effort, never raises — the runtime fallback chain must keep
    moving even if the resolver hits a probe error.
    """
    if not model:
        return None
    try:
        ctx = get_model_context_length(
            model,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
        )
    except Exception as exc:
        logger.debug(
            "Auxiliary fallback: could not resolve context window for %s/%s: %s",
            provider, model, exc,
        )
        return None
    # ``get_model_context_length`` returns an int (with a 256K default
    # fallback when nothing else matches). We still propagate ``None`` if
    # a future change returns ``Optional[int]`` — being explicit is
    # cheap and the test suite covers both shapes.
    if isinstance(ctx, int) and ctx > 0:
        return ctx
    return None


def _try_configured_fallback_chain(
    task: str,
    failed_provider: str,
    reason: str = "error",
    failed_model: Optional[str] = None,
    failed_base_url: Optional[str] = None,
    failed_api_key: Optional[str] = None,
    failed_credential_id: Optional[str] = None,
    failure_scope: Any = None,
    excluded_labels: Optional[set[str]] = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try user-configured fallback_chain for a specific auxiliary task.

    Reads auxiliary.<task>.fallback_chain from config.yaml and tries each
    entry in order.  Each entry must have at least ``provider``; ``model``,
    ``base_url``, and ``api_key`` are optional.

    ``failure_scope`` selects the identity axis invalidated by the failure.
    ``failed_model`` and ``failed_base_url`` provide the corresponding failed
    identity fields. Without an explicit scope, legacy callers retain the
    original model-vs-provider behavior:

    - Model-specific runtime failures (timeout, rate limit, model-incompatible,
      invalid response) pass ``failed_model`` so a chain that intentionally lists several models under the same provider
      — e.g. two more NVIDIA NIM models after the primary NIM model times
      out — is not skipped wholesale. Only the exact model that failed is
      skipped; the siblings still run instead of jumping straight to the
      main-agent-model safety net.
    - Provider-wide failures (auth 401, payment 402) and "no client could
      be built" callers leave ``failed_model`` as None, keeping the whole
      provider skipped — the shared credentials/account behind every model
      on that provider are broken, so a sibling can't help and the
      main-agent-model safety net should be reached instead.

    Returns:
        (client, model, provider_label) or (None, None, "") if no fallback.
    """
    if not task:
        return None, None, ""

    task_config = _get_auxiliary_task_config(task)
    chain = task_config.get("fallback_chain")
    if not chain or not isinstance(chain, list):
        return None, None, ""

    skip_model = (failed_model or "").strip().lower() or None
    # Identity + scope semantics are owned by agent.backend_identity.
    from agent.backend_identity import (
        BackendIdentity,
        FailureScope,
        should_skip_candidate,
    )

    if failure_scope is None:
        failure_scope = FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL
    failed_ident = BackendIdentity.build(
        provider=failed_provider,
        model=skip_model,
        base_url=failed_base_url,
        api_key=failed_api_key,
        credential_id=failed_credential_id,
    )
    excluded_labels = excluded_labels or set()
    tried = []
    min_ctx = _task_minimum_context_length(task)

    for i, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        fb_provider = str(entry.get("provider", "")).strip()
        if not fb_provider:
            continue
        fb_model_raw = str(entry.get("model", "")).strip()
        label = f"fallback_chain[{i}]({fb_provider})"
        if label in excluded_labels:
            continue
        if should_skip_candidate(
            _backend_identity_for_entry(entry, resolved_model=fb_model_raw),
            failed_ident,
            failure_scope,
        ):
            continue
        fb_model = fb_model_raw or None

        try:
            fb_client, resolved_model = _resolve_fallback_entry(entry)
        except Exception:
            fb_client, resolved_model = None, None

        if fb_client is not None:
            if min_ctx is not None and resolved_model:
                fb_ctx = _candidate_context_window(
                    fb_provider,
                    resolved_model,
                    base_url=str(entry.get("base_url") or ""),
                    api_key=_fallback_entry_api_key(entry) or "",
                )
                if fb_ctx is not None and fb_ctx < min_ctx:
                    logger.info(
                        "Auxiliary %s: skipping %s (%s context=%d < min=%d), continuing chain",
                        task, label, resolved_model, fb_ctx, min_ctx,
                    )
                    tried.append(f"{label} (context too small: {fb_ctx}<{min_ctx})")
                    continue
            logger.info(
                "Auxiliary %s: %s on %s — configured fallback to %s (%s)",
                task, reason, failed_provider, label, resolved_model or fb_model or "default",
            )
            return fb_client, resolved_model or fb_model, label
        tried.append(label)

    if tried:
        logger.debug(
            "Auxiliary %s: configured fallback_chain exhausted (tried: %s)",
            task, ", ".join(tried),
        )
    return None, None, ""


def _try_configured_fallback_for_unavailable_client(
    task: Optional[str],
    failed_provider: str,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try task fallback_chain when an explicit aux provider cannot build.

    This covers the "no client" case before any request is sent: missing
    raw env key, unavailable OAuth/pool credentials, or provider resolver
    returning ``(None, None)``.  It deliberately stops at the configured
    per-task fallback chain; the main-agent model remains the last-resort
    runtime fallback for request-time capacity errors.
    """
    explicit = (failed_provider or "").strip().lower()
    if not task or not explicit or explicit in {"auto"}:
        return None, None, ""
    return _try_configured_fallback_chain(
        task,
        explicit,
        reason="provider unavailable",
    )


def _fallback_entry_api_key(entry: Dict[str, Any]) -> Optional[str]:
    """Resolve inline or env-backed API key from a fallback-chain entry.

    Delegates to the centralized, secret-scope-aware resolver so this path
    doesn't leak another profile's credential via a raw ``os.getenv`` under
    gateway multiplexing (see ``hermes_cli.fallback_config.resolve_entry_api_key``).
    """
    from hermes_cli.fallback_config import resolve_entry_api_key

    return resolve_entry_api_key(entry)


def _fallback_entry_has_isolated_credentials(entry: Dict[str, Any]) -> bool:
    """Whether an entry must not fall back to provider-wide credentials."""
    return any(
        str(entry.get(field) or "").strip()
        for field in ("api_key", "key_env", "api_key_env", "credential_pool")
    )


def _fallback_entry_credential_id(
    entry: Dict[str, Any],
    *,
    api_key: Optional[str],
) -> Optional[str]:
    """Return a non-secret identity for an entry's credential source.

    Resolved key material is fingerprinted by ``BackendIdentity``. When a
    ``key_env`` value is unavailable in the current secret scope, retain the
    env-var name as a source identity rather than falling back to the provider
    label (or collapsing the entry during selection). Environment variable
    names are not credential values and are safe to compare.
    """
    if api_key:
        return None
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        return f"key-env:{key_env.casefold()}"
    pool = str(entry.get("credential_pool") or "").strip()
    if pool:
        provider = str(entry.get("provider") or "").strip().lower()
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        # Pool labels are scoped by provider in auth.json, and custom pools
        # are additionally scoped by endpoint. Keep that scope in the
        # non-secret identity so equal labels never become one ambient route.
        return f"pool:{provider}:{base_url}:{pool}"
    return None


def _backend_identity_for_entry(
    entry: Dict[str, Any],
    *,
    resolved_model: Optional[str] = None,
):
    """Build a skip identity while keeping credential material out of logs."""
    from agent.backend_identity import BackendIdentity

    api_key = _fallback_entry_api_key(entry)
    return BackendIdentity.build(
        provider=str(entry.get("provider") or ""),
        model=resolved_model or str(entry.get("model") or ""),
        base_url=str(entry.get("base_url") or ""),
        api_key=api_key,
        credential_id=_fallback_entry_credential_id(entry, api_key=api_key),
    )


def _resolve_fallback_entry(entry: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve one fallback entry through the central provider router."""
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip() or None
    if not provider or not model:
        return None, None
    base_url = str(entry.get("base_url") or "").strip() or None
    api_key = _fallback_entry_api_key(entry)
    isolated_credentials = _fallback_entry_has_isolated_credentials(entry)
    if isolated_credentials and not api_key:
        # A declared key_env/api_key_env/pool is an entry-local credential
        # contract. If it is unavailable, reject this candidate before the
        # provider router can borrow ambient or provider-wide credentials.
        logger.debug(
            "Auxiliary fallback %s/%s has no usable entry-local credential",
            provider,
            model,
        )
        return None, None
    api_mode = str(entry.get("api_mode") or entry.get("transport") or "").strip() or None
    resolve_kwargs = {
        "model": model,
        "explicit_base_url": base_url,
        "explicit_api_key": api_key,
        "api_mode": api_mode,
    }
    if isolated_credentials:
        # Keep the ordinary provider-resolution call shape unchanged while
        # explicitly binding isolated entries to the no-ambient-fallback mode.
        resolve_kwargs["allow_provider_fallback"] = False
    client, resolved_model = resolve_provider_client(provider, **resolve_kwargs)
    if client is not None:
        try:
            client._hermes_fallback_entry = dict(entry)
            client._hermes_fallback_destination = _fallback_destination_from_entry(
                entry, client, resolved_model
            )
        except Exception:
            pass
    return client, resolved_model


def _try_main_fallback_chain(
    task: Optional[str],
    failed_provider: str = "",
    reason: str = "error",
    failed_model: Optional[str] = None,
    failed_base_url: Optional[str] = None,
    failed_api_key: Optional[str] = None,
    failed_credential_id: Optional[str] = None,
    failure_scope: Any = None,
    excluded_labels: Optional[set[str]] = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try the top-level main-agent fallback chain for an auxiliary call.

    ``provider: auto`` auxiliary tasks should respect the user's declared
    main fallback policy before dropping into Hermes' built-in discovery
    chain. The top-level chain is read through ``get_fallback_chain`` so
    both modern ``fallback_providers`` and legacy ``fallback_model`` entries
    participate in the same order as the main agent.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.fallback_config import get_fallback_chain

        chain = get_fallback_chain(load_config_readonly())
    except Exception as exc:
        logger.debug("Auxiliary %s: could not load main fallback chain: %s", task or "call", exc)
        return None, None, ""

    if not chain:
        return None, None, ""

    from agent.backend_identity import (
        BackendIdentity,
        FailureScope,
        same_route,
        should_skip_candidate,
    )

    main_norm = (_read_main_provider() or "").strip().lower()
    excluded_labels = excluded_labels or set()
    skip_model = (failed_model or "").strip().lower() or None
    if failure_scope is None:
        failure_scope = FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL
    failed_ident = BackendIdentity.build(
        provider=failed_provider,
        model=skip_model,
        base_url=failed_base_url,
        api_key=failed_api_key,
        credential_id=failed_credential_id,
    )
    main_ident = BackendIdentity.build(
        provider=main_norm,
        model=_read_main_model(),
        base_url=_read_main_base_url(),
        api_key=_read_main_api_key(),
    )
    tried: List[str] = []
    min_ctx = _task_minimum_context_length(task)

    for i, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        fb_provider = str(entry.get("provider") or "").strip()
        fb_model = str(entry.get("model") or "").strip()
        if not fb_provider or not fb_model:
            continue
        fb_norm = fb_provider.lower()
        label = f"fallback_providers[{i}]({fb_provider})"
        if label in excluded_labels:
            continue
        candidate_ident = _backend_identity_for_entry(entry, resolved_model=fb_model)
        if same_route(candidate_ident, main_ident) or should_skip_candidate(
            candidate_ident,
            failed_ident,
            failure_scope,
        ):
            tried.append(f"{label} (skipped)")
            continue
        if _is_provider_unhealthy(fb_norm) and not (
            _fallback_entry_api_key(entry) or entry.get("credential_pool")
        ):
            _log_skip_unhealthy(fb_norm, task)
            tried.append(f"{label} (unhealthy)")
            continue
        try:
            fb_client, resolved_model = _resolve_fallback_entry(entry)
        except Exception as exc:
            logger.debug("Auxiliary %s: main fallback %s failed to resolve: %s", task or "call", label, exc)
            fb_client, resolved_model = None, None
        if fb_client is not None:
            if min_ctx is not None:
                fb_ctx = _candidate_context_window(
                    fb_provider,
                    resolved_model or fb_model,
                    base_url=str(entry.get("base_url") or ""),
                    api_key=_fallback_entry_api_key(entry) or "",
                )
                if fb_ctx is not None and fb_ctx < min_ctx:
                    logger.info(
                        "Auxiliary %s: skipping %s (context=%d < min=%d), continuing chain",
                        task or "call", label, fb_ctx, min_ctx,
                    )
                    tried.append(f"{label} (context too small: {fb_ctx}<{min_ctx})")
                    continue
            logger.info(
                "Auxiliary %s: %s on %s — main fallback chain to %s (%s)",
                task or "call", reason, failed_provider or "auto", label,
                resolved_model or fb_model,
            )
            return fb_client, resolved_model or fb_model, label
        tried.append(label)

    if tried:
        logger.debug(
            "Auxiliary %s: main fallback chain exhausted (tried: %s)",
            task or "call", ", ".join(tried),
        )
    return None, None, ""


def _resolve_single_provider(
    provider: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[Any]:
    """Resolve a single provider entry from fallback_chain to an OpenAI client.

    Uses the existing provider resolution infrastructure where possible.
    """
    # Reuse resolve_provider_client which handles provider→client mapping.
    client, resolved_model = resolve_provider_client(
        provider=provider,
        model=model,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    return client

def _resolve_auto_route(
    main_runtime: Optional[Dict[str, Any]] = None,
    task: Optional[str] = None,
) -> Tuple[Optional[OpenAI], Optional[str], str]:
    """Full auto-detection chain, including the selected provider identity.

    Priority:
      1. User's main provider + main model, regardless of provider type.
         This means auxiliary tasks (compression, vision, web extraction,
         session search, etc.) use the same model the user configured for
         chat.  Users on OpenRouter/Nous get their chosen chat model; users
         on DeepSeek/ZAI/Alibaba get theirs; etc.  Running aux tasks on the
         user's picked model keeps behavior predictable — no surprise
         switches to a cheap fallback model for side tasks.
      2. OpenRouter → Nous → custom → Codex → API-key providers (fallback
         chain, only used when the main provider has no working client).
    """
    global auxiliary_is_nous, _stale_base_url_warned
    auxiliary_is_nous = False  # Reset — _try_nous() will set True if it wins
    runtime = _normalize_main_runtime(main_runtime)
    runtime_provider = runtime.get("provider", "")
    runtime_model = str(runtime.get("model") or "")
    runtime_base_url = str(runtime.get("base_url") or "")
    runtime_api_key = runtime.get("api_key", "")
    runtime_api_mode = str(runtime.get("api_mode") or "")

    # ── Warn once if OPENAI_BASE_URL is set but config.yaml uses a named
    #    provider (not 'custom').  This catches the common "env poisoning"
    #    scenario where a user switches providers via `hermes model` but the
    #    old OPENAI_BASE_URL lingers in ~/.hermes/.env. ──
    if not _stale_base_url_warned:
        _env_base = os.getenv("OPENAI_BASE_URL", "").strip()
        _cfg_provider = runtime_provider or _read_main_provider()
        if (_env_base and _cfg_provider
                and _cfg_provider != "custom"
                and not _cfg_provider.startswith("custom:")):
            logger.warning(
                "OPENAI_BASE_URL is set (%s) but model.provider is '%s'. "
                "Auxiliary clients may route to the wrong endpoint. "
                "Run: hermes model to reconfigure, or remove "
                "OPENAI_BASE_URL from ~/.hermes/.env",
                _env_base, _cfg_provider,
            )
            _stale_base_url_warned = True

    # ── Step 1: main provider + main model → use them directly ──
    #
    # This is the primary aux backend for every user.  "auto" means
    # "use my main chat model for side tasks as well" — including users
    # on aggregators (OpenRouter, Nous) who previously got routed to a
    # cheap provider-side default.  Explicit per-task overrides set via
    # config.yaml (auxiliary.<task>.provider) still win over this.
    main_provider = str(runtime_provider or _read_main_provider() or "")
    main_model = str(runtime_model or _read_main_model() or "")

    # Latency-critical tasks can explicitly prefer the provider's registered
    # fast model over the main chat model. Titling is the only eligible task:
    # it names a visible sidebar row, produces ~8 tokens, and running it on a
    # frontier reasoning model costs seconds per new session. This remains an
    # opt-in because every settings surface defines "auto" as using the main
    # model; silently overriding that choice makes the selected model cosmetic.
    if _task_prefers_fast_model(task) and main_provider and main_provider not in {"auto", ""}:
        fast_model = _get_aux_model_for_provider(main_provider, prefer_fast=True)
        if fast_model and fast_model != main_model:
            logger.debug(
                "Auxiliary task %s: preferring fast model %s over main model %s",
                task, fast_model, main_model,
            )
            main_model = fast_model

    # MoA virtual provider: the "model" is a preset name (e.g. "opus-gpt") and
    # there is no real "moa" HTTP endpoint, so resolving an aux client against
    # provider="moa"/model=<preset> sends the preset name as the model id and
    # the provider 400s ("opus-gpt is not a valid model ID"). Auxiliary tasks
    # (title generation, compression, vision, …) don't need the reference
    # fan-out — they should run on the aggregator, which is the preset's acting
    # model. Resolve the MoA preset to its aggregator slot and continue Step 1
    # with that real provider+model. Mirrors the MoA context-length resolution.
    if main_provider == "moa":
        _agg_provider, _agg_model = _resolve_moa_aggregator(main_model)
        if _agg_provider and _agg_model:
            main_provider = _agg_provider
            main_model = _agg_model
            # The MoA virtual runtime carries a non-HTTP base_url
            # ("moa://local") and a placeholder api_key; they belong to the
            # facade, not the aggregator's real provider. Drop them so the
            # aggregator resolves through its own provider credentials.
            runtime_base_url = ""
            runtime_api_key = ""
            runtime_api_mode = ""

    if (main_provider and main_model
            and main_provider not in {"auto", ""}):
        resolved_provider = main_provider
        explicit_base_url = runtime_base_url or None
        explicit_api_key = None
        if runtime_base_url and main_provider == "custom":
            # Anonymous custom endpoint (OPENAI_BASE_URL / config.model.base_url)
            # — pass through with explicit base_url + api_key.
            resolved_provider = "custom"
            explicit_base_url = runtime_base_url
            explicit_api_key = runtime_api_key or None
        elif main_provider.startswith("custom:"):
            # Named custom provider (custom_providers / providers dict entry).
            _has_named_entry = False
            try:
                from hermes_cli.runtime_provider import _get_named_custom_provider
                _has_named_entry = _get_named_custom_provider(main_provider) is not None
            except ImportError:
                pass
            if _has_named_entry:
                # KEEP the full ``custom:<name>`` so resolve_provider_client
                # lands in the named-custom-provider arm — that arm honours the
                # entry's api_mode (e.g. anthropic_messages →
                # AnthropicAuxiliaryClient, avoiding the /anthropic→/v1 rewrite
                # that 404s against proxies like Palantir Foundry's Anthropic
                # surface).  Do NOT collapse to plain "custom"; that path
                # strips /anthropic and routes through OpenAI chat.completions.
                # base_url and api_key come from the named entry itself, so
                # leave the explicit_* overrides unset.
                resolved_provider = main_provider
                explicit_base_url = None
            elif runtime_base_url:
                # Config-less named custom provider (#34777): the entry only
                # exists in the live runtime, so collapse to the anonymous
                # custom arm with the runtime endpoint + key.
                resolved_provider = "custom"
                explicit_base_url = runtime_base_url
                explicit_api_key = runtime_api_key or None
            elif runtime_api_key:
                explicit_api_key = runtime_api_key
        elif runtime_api_key:
            # Pin auxiliary to the same api_key as the active main chat session
            # so that a working key is reused instead of re-selecting from the pool
            # (which might pick a different, potentially exhausted key).
            explicit_api_key = runtime_api_key
        # Skip Step-1 if the main provider was recently 402'd. The unhealthy
        # cache TTL bounds how long we bypass it, so a topped-up account
        # recovers automatically. If we tried Step-1 anyway, every aux call
        # on a depleted main provider would pay one doomed 402 RTT before
        # falling to Step-2.
        main_chain_label = _normalize_chain_label(resolved_provider)
        if main_chain_label and _is_provider_unhealthy(main_chain_label):
            _log_skip_unhealthy(main_chain_label)
        else:
            client, resolved = resolve_provider_client(
                resolved_provider,
                main_model,
                explicit_base_url=explicit_base_url,
                explicit_api_key=explicit_api_key,
                api_mode=runtime_api_mode or None,
            )
            if client is not None:
                logger.info("Auxiliary auto-detect: using main provider %s (%s)",
                            main_provider, resolved or main_model)
                return client, resolved or main_model, resolved_provider

    # ── Step 2: user-configured fallback policy ─────────────────────────
    # In auto mode, respect the task-specific fallback chain first, then the
    # main agent's top-level fallback_providers/fallback_model chain. The
    # hardcoded provider discovery chain below is only the convenience default
    # for users who have not declared a fallback policy.
    if task:
        fb_client, fb_model, fb_label = _try_configured_fallback_chain(
            task, main_provider or "auto", reason="main provider unavailable")
        if fb_client is not None:
            return fb_client, fb_model, _fallback_provider_from_label(fb_label)
    fb_client, fb_model, fb_label = _try_main_fallback_chain(
        task, main_provider or "auto", reason="main provider unavailable")
    if fb_client is not None:
        return fb_client, fb_model, fb_label

    # ── Step 3: aggregator / fallback chain ──────────────────────────────
    tried = []
    for label, try_fn in _get_provider_chain():
        if _is_provider_unhealthy(label):
            _log_skip_unhealthy(label)
            tried.append(f"{label} (unhealthy)")
            continue
        client, model = try_fn()
        if client is not None:
            if tried:
                logger.info("Auxiliary auto-detect: using %s (%s) — skipped: %s",
                            label, model or "default", ", ".join(tried))
            else:
                logger.info("Auxiliary auto-detect: using %s (%s)", label, model or "default")
            return client, model, label
        tried.append(label)
    logger.warning("Auxiliary auto-detect: no provider available (tried: %s). "
                   "Compression, summarization, and memory flush will not work. "
                   "Set OPENROUTER_API_KEY or configure a local model in config.yaml.",
                   ", ".join(tried))
    return None, None, ""


def _resolve_auto(
    main_runtime: Optional[Dict[str, Any]] = None,
    task: Optional[str] = None,
) -> Tuple[Optional[OpenAI], Optional[str]]:
    """Backward-compatible auto resolver for callers that only need client/model."""
    client, model, _provider = _resolve_auto_route(main_runtime=main_runtime, task=task)
    return client, model


def _tag_effective_provider(client: Any, provider: str) -> None:
    """Retain auto-routing identity on the client that survives cache reuse."""
    if client is None or not provider:
        return
    try:
        setattr(client, "_hermes_aux_effective_provider", provider)
    except (AttributeError, TypeError):
        logger.debug(
            "Auxiliary client %s cannot retain effective provider %s",
            type(client).__name__, provider,
        )


def _effective_provider_for_client(client: Any, fallback: str) -> str:
    """Return the concrete provider selected for an auto-routed client."""
    effective_provider = getattr(client, "_hermes_aux_effective_provider", "")
    if isinstance(effective_provider, str) and effective_provider:
        return effective_provider
    return str(fallback or "")


# ── Centralized Provider Router ─────────────────────────────────────────────
#
# resolve_provider_client() is the single entry point for creating a properly
# configured client given a (provider, model) pair.  It handles auth lookup,
# base URL resolution, provider-specific headers, and API format differences
# (Chat Completions vs Responses API for Codex).
#
# All auxiliary consumer code should go through this or the public helpers
# below — never look up auth env vars ad-hoc.


def _to_async_client(sync_client, model: str, is_vision: bool = False):
    """Convert a sync client to its async counterpart, preserving Codex routing.

    When ``is_vision=True`` and the underlying base URL is Copilot, the
    resulting async client carries the ``Copilot-Vision-Request: true``
    header so the request is routed to Copilot's vision-capable
    infrastructure (otherwise vision payloads silently time out).
    """
    from openai import AsyncOpenAI

    if isinstance(sync_client, _AuxProbeClientStub):
        return sync_client, model
    if isinstance(sync_client, CodexAuxiliaryClient):
        return AsyncCodexAuxiliaryClient(sync_client), model
    if isinstance(sync_client, AnthropicAuxiliaryClient):
        return AsyncAnthropicAuxiliaryClient(sync_client), model
    if isinstance(sync_client, BedrockAuxiliaryClient):
        return AsyncBedrockAuxiliaryClient(sync_client), model
    try:
        from agent.gemini_native_adapter import GeminiNativeClient, AsyncGeminiNativeClient

        if isinstance(sync_client, GeminiNativeClient):
            return AsyncGeminiNativeClient(sync_client), model
    except ImportError:
        pass
    try:
        from agent.copilot_acp_client import CopilotACPClient
        if isinstance(sync_client, CopilotACPClient):
            return sync_client, model
    except ImportError:
        pass

    async_kwargs = {
        "api_key": sync_client.api_key,
        "base_url": str(sync_client.base_url),
    }
    sync_base_url = str(sync_client.base_url)
    if base_url_host_matches(sync_base_url, "openrouter.ai"):
        async_kwargs["default_headers"] = build_or_headers()
    elif base_url_host_matches(sync_base_url, "githubcopilot.com"):
        from hermes_cli.copilot_auth import copilot_request_headers

        async_kwargs["default_headers"] = copilot_request_headers(
            is_agent_turn=True, is_vision=is_vision
        )
    elif base_url_host_matches(sync_base_url, "api.kimi.com"):
        async_kwargs["default_headers"] = {"User-Agent": "claude-code/0.1.0"}
    elif base_url_host_matches(sync_base_url, "integrate.api.nvidia.com"):
        async_kwargs["default_headers"] = build_nvidia_nim_headers(sync_base_url)
    elif _is_official_codex_base_url(sync_base_url):
        async_kwargs["default_headers"] = _codex_cloudflare_headers(
            sync_client.api_key, base_url=sync_base_url,
        )
    elif base_url_host_matches(sync_base_url, "x.ai"):
        from tools.xai_http import hermes_xai_default_headers

        async_kwargs["default_headers"] = hermes_xai_default_headers()
    else:
        # Fall back to profile.default_headers for providers that declare
        # client-level headers on their ProviderProfile (e.g. attribution
        # User-Agent strings). Provider is inferred from the hostname.
        try:
            from agent.model_metadata import _infer_provider_from_url
            from providers import get_provider_profile as _gpf_async
            _inferred = _infer_provider_from_url(sync_base_url)
            if _inferred:
                _ph_async = _gpf_async(_inferred)
                if _ph_async and _ph_async.default_headers:
                    async_kwargs["default_headers"] = dict(_ph_async.default_headers)
        except Exception:
            pass
    _merged_async = _apply_user_default_headers(async_kwargs.get("default_headers"))
    if _merged_async:
        async_kwargs["default_headers"] = _merged_async
    _apply_required_codex_headers(
        async_kwargs, access_token=sync_client.api_key, base_url=sync_base_url,
    )
    async_kwargs = {
        **_openai_http_client_kwargs(sync_base_url, async_mode=True),
        **async_kwargs,
    }
    # See _create_openai_client: disable SDK-internal retries so Hermes owns
    # the auxiliary retry/timeout budget (issue #54465).
    async_kwargs.setdefault("max_retries", 0)
    return AsyncOpenAI(**async_kwargs), model


def _normalize_resolved_model(model_name: Optional[str], provider: str) -> Optional[str]:
    """Normalize a resolved model for the provider that will receive it."""
    if not model_name:
        return model_name
    try:
        from hermes_cli.model_normalize import normalize_model_for_provider

        return normalize_model_for_provider(model_name, provider)
    except Exception:
        return model_name
