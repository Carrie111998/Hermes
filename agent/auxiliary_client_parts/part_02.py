

# ── Codex Responses → chat.completions adapter ─────────────────────────────
# All auxiliary consumers call client.chat.completions.create(**kwargs) and
# read response.choices[0].message.content. This adapter translates those
# calls to the Codex Responses API so callers don't need any changes.


class _CodexCompletionsAdapter:
    """Drop-in shim that accepts chat.completions.create() kwargs and
    routes them through the Codex Responses streaming API."""

    def __init__(self, real_client: OpenAI, model: str):
        self._client = real_client
        self._model = model

    def create(self, **kwargs) -> Any:
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)

        # Separate system/instructions from replayable conversation messages,
        # then route the rest through the SINGLE shared chat->Responses
        # converter used by the main agent transport
        # (agent/transports/codex.py). Maintaining a private conversion loop
        # here let chat-style messages with role="tool" leak straight into
        # Responses input[] — which the Responses API rejects with
        # "Invalid value: 'tool'. Supported values are: 'assistant', 'system',
        # 'developer', and 'user'." (issue #5709, hit hard by flush_memories()
        # / compression replaying real session history that includes assistant
        # tool_calls + role="tool" results). The shared converter encodes
        # assistant tool calls as `function_call` items and tool results as
        # `function_call_output` items with a valid call_id, so every
        # Responses path normalizes tool history identically and cannot drift.
        from agent.codex_responses_adapter import _chat_messages_to_responses_input
        from utils import base_url_host_matches

        instructions = "You are a helpful assistant."
        replay_messages: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "system":
                instructions = content if isinstance(content, str) else str(content)
            else:
                replay_messages.append(msg)

        # Copilot (githubcopilot.com) binds replayed codex_message_items ids
        # to a backend "connection" that doesn't survive credential
        # rotation/gateway restarts — replaying one gets HTTP 401 "input
        # item ID does not belong to this connection" (#32716). Auxiliary
        # calls (context compression, flush_memories, MoA aggregation) go
        # through this adapter instead of agent/transports/codex.py's
        # build_kwargs, so they need the same guard applied independently.
        _host_for_input = str(getattr(self._client, "base_url", "") or "")
        _is_github_for_input = base_url_host_matches(_host_for_input, "githubcopilot.com")
        # Auxiliary calls never send ``context_management`` (native
        # compaction is a main-turn feature), so they must never replay a
        # compaction checkpoint from the replayed history nor let one
        # restructure this request — the summarizer/aggregator model is
        # usually not even the one that minted the blob.
        input_items = _chat_messages_to_responses_input(
            replay_messages,
            is_github_responses=_is_github_for_input,
            native_compaction_eligible=False,
        )

        resp_kwargs: Dict[str, Any] = {
            # Strip the Hermes-side ``-900k`` large-context picker suffix —
            # the Codex backend only knows the base slug (mirrors the main
            # transport in agent/transports/codex.py::build_kwargs).
            "model": _strip_codex_ctx_variant(model),
            "instructions": instructions,
            "input": input_items or [{"role": "user", "content": ""}],
            "store": False,
        }

        # Preserve the chat.completions timeout contract. This adapter is used
        # by auxiliary calls such as context compression; if the timeout is not
        # forwarded and enforced, a Codex Responses stream can sit behind a
        # dead-looking CLI until the user force-interrupts the whole session.
        timeout = kwargs.get("timeout")
        if timeout is not None:
            resp_kwargs["timeout"] = timeout

        # Note: the Codex endpoint (chatgpt.com/backend-api/codex) does NOT
        # support max_output_tokens or temperature — omit to avoid 400 errors.

        # Translate extra_body.reasoning (chat.completions shape) into the
        # Responses API's top-level reasoning + include fields.  Mirrors
        # agent/transports/codex.py::build_kwargs() so auxiliary callers
        # that configure reasoning via auxiliary.<task>.extra_body get the
        # same behavior as the main agent's Codex transport.
        extra_body = kwargs.get("extra_body") or {}
        if isinstance(extra_body, dict):
            reasoning_cfg = extra_body.get("reasoning")
            if isinstance(reasoning_cfg, dict):
                if reasoning_cfg.get("enabled") is False:
                    # Reasoning explicitly disabled — do not set reasoning
                    # or include.  The Codex backend still thinks by
                    # default, but we honor the caller's intent where the
                    # API allows it.
                    pass
                else:
                    # Truthy-only check mirrors agent/transports/codex.py
                    # build_kwargs(): falsy values (None, "", 0) fall back
                    # to the default rather than being forwarded to the
                    # Codex backend, which rejects e.g. {"effort": null}
                    # with a 400.
                    effort = reasoning_cfg.get("effort") or "medium"
                    # Same declared vocabulary + shared clamp as the main
                    # Codex transport (agent.reasoning_effort): per-model —
                    # "max" is gpt-5.6-only, "minimal"/"ultra" always
                    # rejected (live-verified, #68365).
                    from agent.reasoning_effort import (
                        clamp_effort,
                        codex_supported_efforts,
                    )

                    effort = clamp_effort(effort, codex_supported_efforts(model))
                    resp_kwargs["reasoning"] = {
                        "effort": effort,
                        "summary": "auto",
                    }
                    resp_kwargs["include"] = ["reasoning.encrypted_content"]

        # Tools support for auxiliary callers (e.g. skills_hub) that pass function schemas
        tools = kwargs.get("tools")
        if tools:
            # xAI's Responses endpoint rejects ``pattern`` and ``format`` JSON Schema
            # keywords (HTTP 400). Strip them here to match the parity guarantee that
            # chat_completion_helpers.py provides for the main-agent xAI path.
            #
            # Deep-copy before sanitizing — ``list(tools)`` is only a shallow
            # copy of the outer list, but the sanitizers mutate the inner
            # parameter dicts in place.  Without a deep copy the caller's
            # tool registry permanently loses its slash-containing enum
            # constraints after the first auxiliary xAI call.  See #27907.
            try:
                import copy as _copy
                from tools.schema_sanitizer import (
                    strip_pattern_and_format,
                    strip_slash_enum,
                )
                tools = _copy.deepcopy(list(tools))
                tools, _ = strip_pattern_and_format(tools)
                tools, _ = strip_slash_enum(tools)
            except Exception as exc:
                logger.warning(
                    "Auxiliary client: failed to sanitize tool schemas for "
                    "Codex/xAI Responses path: %s", exc,
                )
            converted = []
            for t in tools:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                converted.append({
                    "type": "function",
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            if converted:
                resp_kwargs["tools"] = converted

        # Stable prompt-cache routing for the Codex/Responses aux path, mirroring
        # the main transport (agent/transports/codex.py::build_kwargs, which sets
        # prompt_cache_key = _content_cache_key(instructions, tools)). Without
        # this, MoA acting-aggregator and other auxiliary Responses calls stay
        # cache-cold while the main Responses transport is warm (issue #53735).
        # The key is content-addressed from the static prefix (instructions +
        # tool schemas) so it stays warm across turns/fires. Guard the top-level
        # field the same way the main transport does: xAI Responses takes the
        # key in extra_body (not top-level) and GitHub/Copilot Responses opts
        # out of cache-key routing entirely — for those hosts, skip it here.
        try:
            from agent.transports.codex import (
                _cache_scope_from_session_id,
                _content_cache_key,
                _default_prompt_cache_retention_for_request,
            )
            from utils import base_url_host_matches

            _host_src = str(getattr(self._client, "base_url", "") or "")
            _is_xai = base_url_host_matches(_host_src, "x.ai") or base_url_host_matches(_host_src, "api.x.ai")
            _is_github = (
                base_url_host_matches(_host_src, "githubcopilot.com")
                or base_url_host_matches(_host_src, "models.github.ai")
            )
            if not _is_xai and not _is_github and "prompt_cache_key" not in resp_kwargs:
                # Scope by the owning turn's conversation so two unrelated
                # sessions with the same instructions/tools (e.g. compression,
                # MoA, flush_memories firing back-to-back on different
                # sessions) don't bucket-share a prompt cache slot (#78941).
                # Prefer the rotation-stable logical scope threaded through
                # set_runtime_main() (compression-lineage root, #79017) and
                # fall back to the physical session id, mirroring the main
                # transport (agent/transports/codex.py::build_kwargs).
                _scope = _cache_scope_from_session_id(
                    _runtime_main_value("cache_scope")
                    or _runtime_main_value("session_id")
                )
                _cache_key = _content_cache_key(instructions, resp_kwargs.get("tools"), _scope)
                if _cache_key:
                    resp_kwargs["prompt_cache_key"] = _cache_key
            if "prompt_cache_retention" not in resp_kwargs:
                _cache_retention = _default_prompt_cache_retention_for_request(
                    model,
                    _host_src,
                )
                if _cache_retention:
                    resp_kwargs["prompt_cache_retention"] = _cache_retention
        except Exception:
            logger.debug(
                "Codex auxiliary: prompt_cache_key derivation skipped", exc_info=True
            )

        # Stream and collect the response
        text_parts: List[str] = []
        tool_calls_raw: List[Any] = []
        usage = None
        total_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else None
        deadline = time.monotonic() + float(total_timeout) if total_timeout else None
        timed_out = threading.Event()
        timeout_timer: Optional[threading.Timer] = None
        # A protected provider call may outlive its owning compression attempt:
        # the owner returns promptly on hard cancellation while this adapter is
        # still blocked in the SDK stream on its isolated worker. Timer threads
        # do not inherit this worker's thread-local protection state, so freeze
        # the hard-cancel source here, before creating the timer.
        protected_cancel_check = (
            _capture_aux_cancel_check() if _aux_interrupt_protected() else None
        )
        attempt_stream_lock = threading.Lock()
        attempt_stream: List[Any] = []

        def _timeout_message() -> str:
            return f"Codex auxiliary Responses stream exceeded {float(total_timeout):.1f}s total timeout"

        def _close_client_on_timeout() -> None:
            begin_timeout_cleanup = getattr(
                protected_cancel_check, "begin_timeout_cleanup", None
            )
            if callable(begin_timeout_cleanup):
                timeout_won = bool(begin_timeout_cleanup())
            else:
                timeout_won = not (
                    callable(protected_cancel_check)
                    and _captured_aux_cancel_requested(protected_cancel_check)
                )
            # Publish transport timeout only after the attempt-local decision is
            # fixed, so owner polling cannot observe completion in between.
            timed_out.set()
            if not timeout_won:
                # The request owner already hard-cancelled this attempt. The
                # OpenAI client is process-shared, so closing/evicting it here
                # would disrupt unrelated sessions. Wake only this attempt's
                # event stream when responses.create() returned one in time;
                # otherwise rely on the bounded SDK/provider timeout.
                with attempt_stream_lock:
                    stream = attempt_stream[0] if attempt_stream else None
                close_stream = getattr(stream, "close", None)
                if callable(close_stream):
                    try:
                        close_stream()
                    except Exception:
                        logger.debug(
                            "Codex auxiliary: cancelled attempt stream close "
                            "during timeout failed",
                            exc_info=True,
                        )
                return
            close = getattr(self._client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Codex auxiliary: client close during timeout failed", exc_info=True)
            # The cached auxiliary client wraps this same ``self._client``
            # (or *is* a ``CodexAuxiliaryClient`` whose ``_real_client`` is
            # this instance).  After we close the httpx transport above, the
            # cache must drop that entry — otherwise the next auxiliary call
            # (compression retry, memory flush, etc.) reuses the dead client
            # and fails fast with a connection error.  See issue #23432.
            try:
                _evict_cached_client_instance(self._client)
            except Exception:
                logger.debug("Codex auxiliary: cache eviction on timeout failed", exc_info=True)

        def _check_cancelled() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                if not timed_out.is_set():
                    _close_client_on_timeout()
                raise TimeoutError(_timeout_message())
            try:
                from tools.interrupt import is_interrupted
                # Honor interrupt protection for atomic aux tasks (compression):
                # a mid-flight gateway interrupt must NOT abort the summary call
                # and trigger a degraded fallback marker (#23975). Explicit host
                # cancellation has its own frozen exception; timeouts above still
                # fire and other aux tasks remain interruptible.
                if _aux_interrupt_cancel_requested():
                    raise AuxiliaryExplicitCancellation()
                if is_interrupted() and not _aux_interrupt_protected():
                    raise InterruptedError("Codex auxiliary Responses stream interrupted")
            except (InterruptedError, AuxiliaryExplicitCancellation):
                raise
            except Exception:
                # Interrupt state is a best-effort UX hook; never make it a
                # new failure mode for auxiliary calls.
                pass

        try:
            if total_timeout:
                timeout_timer = threading.Timer(float(total_timeout), _close_client_on_timeout)
                timeout_timer.daemon = True
                timeout_timer.start()
            _check_cancelled()

            # Event-driven Responses streaming via the low-level
            # ``responses.create(stream=True)`` path.  The high-level
            # ``responses.stream(...)`` helper does post-hoc typed
            # reconstruction from ``response.completed.response.output``,
            # which the chatgpt.com Codex backend has been observed to
            # return as ``null`` (gpt-5.5, May 2026) — that crashes the SDK
            # with ``TypeError: 'NoneType' object is not iterable``.
            # Consuming raw events and assembling the final response
            # ourselves from ``response.output_item.done`` makes us
            # structurally immune to that drift.
            from agent.codex_runtime import (
                _bypass_sdk_request_transform,
                _consume_codex_event_stream,
            )

            stream_kwargs = dict(resp_kwargs)
            stream_kwargs["stream"] = True
            # #93650: keep bulk wire-format payload out of the SDK's
            # GIL-holding request transform on auxiliary calls too.
            stream_kwargs = _bypass_sdk_request_transform(stream_kwargs)

            def _on_each_event(_event: Any) -> None:
                # Re-check timeout/cancellation per event, matching the
                # cadence the old in-line ``_check_cancelled()`` used.
                # Provider response timing (TTFP telemetry) records every
                # frame; forward progress for hosts watching liveness (the
                # compression commit fence) counts only substantive
                # payloads — lifecycle and keepalive events must not reset
                # the compression idle clock.
                if _codex_event_has_content(_event):
                    _notify_aux_provider_response()
                else:
                    _notify_aux_timing_response()
                _check_cancelled()

            event_stream = self._client.responses.create(**stream_kwargs)
            with attempt_stream_lock:
                attempt_stream.append(event_stream)
            # The timer can fire while responses.create() is blocked. If the
            # cancelled attempt had no stream to close at that instant, close it
            # now that it is safely attempt-owned; never touch the shared client.
            if (
                timed_out.is_set()
                and callable(protected_cancel_check)
                and _captured_aux_cancel_requested(protected_cancel_check)
            ):
                close_fn = getattr(event_stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        logger.debug(
                            "Codex auxiliary: late cancelled attempt stream close failed",
                            exc_info=True,
                        )
            try:
                # Some Codex-compatible hosts accept ``stream=True`` but return
                # a completed Responses object instead of an SSE iterator. Do
                # not hand that object to the event consumer: typed Responses
                # (and compatibility shims such as SimpleNamespace) are not
                # event streams and may not be iterable at all.
                if hasattr(event_stream, "output"):
                    final = event_stream
                else:
                    final = _consume_codex_event_stream(
                        event_stream,
                        model=str(resp_kwargs.get("model") or model),
                        on_event=_on_each_event,
                    )
            finally:
                close_fn = getattr(event_stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
                with attempt_stream_lock:
                    attempt_stream.clear()

            if final is None:
                raise RuntimeError("Codex auxiliary Responses stream did not return a final response")

            # Extract text and tool calls from the Responses output.
            # Items may be SimpleNamespace (raw-event path) or dicts
            # (some legacy fallback paths), so handle both shapes.
            def _item_get(obj: Any, key: str, default: Any = None) -> Any:
                val = getattr(obj, key, None)
                if val is None and isinstance(obj, dict):
                    val = obj.get(key, default)
                return val if val is not None else default

            for item in (getattr(final, "output", None) or []):
                item_type = _item_get(item, "type")
                if item_type == "message":
                    for part in (_item_get(item, "content") or []):
                        ptype = _item_get(part, "type")
                        if ptype in {"output_text", "text"}:
                            text_parts.append(_item_get(part, "text", ""))
                elif item_type == "function_call":
                    tool_calls_raw.append(SimpleNamespace(
                        id=_item_get(item, "call_id", ""),
                        type="function",
                        function=SimpleNamespace(
                            name=_item_get(item, "name", ""),
                            arguments=_item_get(item, "arguments", "{}"),
                        ),
                    ))

            resp_usage = getattr(final, "usage", None)
            if resp_usage:
                usage = SimpleNamespace(
                    prompt_tokens=getattr(resp_usage, "input_tokens", 0)
                        or (resp_usage.get("input_tokens", 0) if isinstance(resp_usage, dict) else 0),
                    completion_tokens=getattr(resp_usage, "output_tokens", 0)
                        or (resp_usage.get("output_tokens", 0) if isinstance(resp_usage, dict) else 0),
                    total_tokens=getattr(resp_usage, "total_tokens", 0)
                        or (resp_usage.get("total_tokens", 0) if isinstance(resp_usage, dict) else 0),
                )
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(_timeout_message()) from exc
            logger.debug("Codex auxiliary Responses API call failed: %s", exc)
            raise
        finally:
            if timeout_timer is not None:
                timeout_timer.cancel()

        content = "".join(text_parts).strip() or None

        # Build a response that looks like chat.completions
        message = SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls_raw or None,
        )
        choice = SimpleNamespace(
            index=0,
            message=message,
            finish_reason="stop" if not tool_calls_raw else "tool_calls",
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _CodexChatShim:
    """Wraps the adapter to provide client.chat.completions.create()."""

    def __init__(self, adapter: _CodexCompletionsAdapter):
        self.completions = adapter


class CodexAuxiliaryClient:
    """OpenAI-client-compatible wrapper that routes through Codex Responses API.

    Consumers can call client.chat.completions.create(**kwargs) as normal.
    Also exposes .api_key and .base_url for introspection by async wrappers.
    """

    def __init__(self, real_client: OpenAI, model: str):
        self._real_client = real_client
        adapter = _CodexCompletionsAdapter(real_client, model)
        self.chat = _CodexChatShim(adapter)
        self.api_key = real_client.api_key
        self.base_url = real_client.base_url

    def close(self):
        self._real_client.close()


class _AsyncCodexCompletionsAdapter:
    """Async version of the Codex Responses adapter.

    Wraps the sync adapter via asyncio.to_thread() so async consumers
    (web_tools, session_search) can await it as normal.
    """

    def __init__(self, sync_adapter: _CodexCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio
        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncCodexChatShim:
    def __init__(self, adapter: _AsyncCodexCompletionsAdapter):
        self.completions = adapter


class AsyncCodexAuxiliaryClient:
    """Async-compatible wrapper matching AsyncOpenAI.chat.completions.create()."""

    def __init__(self, sync_wrapper: "CodexAuxiliaryClient"):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncCodexCompletionsAdapter(sync_adapter)
        self.chat = _AsyncCodexChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        # Mirror the sync wrapper's _real_client so cache eviction by leaf
        # OpenAI client (e.g. _close_client_on_timeout in #23482) drops
        # this async entry too. Without this, sync and async cache entries
        # diverge on poisoning: the sync entry is evicted but the async
        # entry keeps reusing the closed transport, failing every
        # subsequent async aux call with 'Connection error' until the
        # gateway restarts.
        self._real_client = sync_wrapper._real_client


def _translate_anthropic_response_format(
    anthropic_kwargs: Dict[str, Any], response_format: Any,
) -> None:
    """Merge an OpenAI response format into Anthropic ``output_config``."""
    if not isinstance(response_format, dict):
        return

    format_type = response_format.get("type")
    if format_type == "json_schema":
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict) or "schema" not in json_schema:
            return
        native_format = {
            "type": "json_schema",
            "schema": json_schema["schema"],
        }
    elif format_type == "json_object":
        # Anthropic SDK 0.87.0 exposes only JSONOutputFormatParam, whose
        # required type is ``json_schema``; it has no schema-less JSON mode.
        native_format = {
            "type": "json_schema",
            "schema": {"type": "object"},
        }
    else:
        return

    output_config = anthropic_kwargs.get("output_config")
    if not isinstance(output_config, dict):
        output_config = {}
        anthropic_kwargs["output_config"] = output_config
    output_config["format"] = native_format


class _AnthropicCompletionsAdapter:
    """OpenAI-client-compatible adapter for Anthropic Messages API."""

    def __init__(
        self,
        real_client: Any,
        model: str,
        is_oauth: bool = False,
        base_url: str | None = None,
    ):
        self._client = real_client
        self._model = model
        self._is_oauth = is_oauth
        # Prefer the caller-supplied URL (AnthropicAuxiliaryClient keeps the
        # pre-strip Portal ``.../v1`` form). Only fall back to the SDK
        # client's host for Nous Portal — a blanket fallback would flip
        # MiniMax/Zhipu/etc. aux adapters from "unknown host = native
        # Anthropic" to third-party (stripping thinking signatures).
        self._base_url = base_url or None
        if not self._base_url:
            candidate = str(getattr(real_client, "base_url", "") or "") or None
            if candidate:
                try:
                    from agent.anthropic_adapter import _is_nous_portal_endpoint

                    if _is_nous_portal_endpoint(candidate):
                        self._base_url = candidate
                except Exception:
                    pass

    def create(self, **kwargs) -> Any:
        from agent.anthropic_adapter import build_anthropic_kwargs, create_anthropic_message
        from agent.transports import get_transport

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        reasoning_config = kwargs.get("_reasoning_config")
        # ZAI's Anthropic-compatible endpoint rejects max_tokens on vision
        # models (glm-4v-flash etc.) with error code 1210.  When the caller
        # signals this by setting _skip_zai_max_tokens in kwargs, omit it.
        _skip_mt = kwargs.pop("_skip_zai_max_tokens", False)
        if _skip_mt:
            max_tokens = None
        else:
            max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        temperature = kwargs.get("temperature")

        normalized_tool_choice = None
        if isinstance(tool_choice, str):
            normalized_tool_choice = tool_choice
        elif isinstance(tool_choice, dict):
            choice_type = str(tool_choice.get("type", "")).lower()
            if choice_type == "function":
                normalized_tool_choice = tool_choice.get("function", {}).get("name")
            elif choice_type in {"auto", "required", "none"}:
                normalized_tool_choice = choice_type

        # Reasoning priority: explicit per-call reasoning_config (MoA per-slot,
        # passed as _reasoning_config by _build_call_kwargs) wins over an
        # extra_body.reasoning dict (auxiliary.<task>.extra_body config).
        # build_anthropic_kwargs translates the config dict into the native
        # ``thinking`` field and handles models where thinking is mandatory.
        _reasoning_cfg = reasoning_config
        if _reasoning_cfg is None:
            _eb = kwargs.get("extra_body")
            if isinstance(_eb, dict):
                _rc = _eb.get("reasoning")
                if isinstance(_rc, dict):
                    _reasoning_cfg = _rc

        anthropic_kwargs = build_anthropic_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_config=_reasoning_cfg,
            tool_choice=normalized_tool_choice,
            is_oauth=self._is_oauth,
            # Portal routes on ``anthropic/<slug>`` catalog ids and replays
            # signed thinking like native Anthropic; both carve-outs key off
            # base_url. Omitting it normalizes the id to a bare Anthropic
            # slug and the Portal Messages route cannot resolve it.
            base_url=self._base_url,
        )
        # Opus 4.7+ rejects any non-default temperature/top_p/top_k; only set
        # temperature for models that still accept it. build_anthropic_kwargs
        # additionally strips these keys as a safety net — keep both layers.
        if temperature is not None:
            from agent.anthropic_adapter import _forbids_sampling_params
            if not _forbids_sampling_params(model):
                anthropic_kwargs["temperature"] = temperature

        # Pass through caller-supplied extra_body so providers behind
        # Anthropic-compatible gateways receive their per-vendor request
        # fields (thinking control, metadata, portal tags, ...). The dict
        # form is the documented Anthropic SDK passthrough for non-standard
        # request body keys; merge on top of whatever build_anthropic_kwargs
        # already produced (e.g. fast-mode ``speed``) so call-time settings
        # survive. Three exclusions:
        #   - ``reasoning``: the OpenAI-shaped config dict is TRANSLATED into
        #     the native ``thinking`` field above (build_anthropic_kwargs);
        #     forwarding the raw field alongside would double-specify
        #     reasoning and 400 on strict gateways.
        #   - ``response_format``: the OpenAI structured-output shape is
        #     TRANSLATED into top-level ``output_config.format`` below;
        #     forwarding the raw field 400s on strict Anthropic gateways.
        #   - ``_``-prefixed keys: private Hermes plumbing (_reasoning_config
        #     et al.), never wire fields.
        caller_extra_body = kwargs.get("extra_body")
        # A top-level ``response_format`` kwarg (the OpenAI SDK's documented
        # call shape) must get the same translation as the extra_body form.
        # The adapter builds the Messages body from a fixed allow-list of
        # kwargs, so before this an unrecognized top-level kwarg was dropped
        # on the floor: the request succeeded but the schema contract
        # silently became prompt compliance (#85626 review, point 2). When
        # both shapes are present, the extra_body form wins — it is the shape
        # every in-tree caller uses.
        top_level_response_format = kwargs.get("response_format")
        if top_level_response_format is not None:
            _translate_anthropic_response_format(
                anthropic_kwargs, top_level_response_format,
            )
        if caller_extra_body and isinstance(caller_extra_body, dict):
            _translate_anthropic_response_format(
                anthropic_kwargs, caller_extra_body.get("response_format"),
            )
            passthrough = {
                k: v for k, v in caller_extra_body.items()
                if k not in {"reasoning", "response_format"}
                and not str(k).startswith("_")
            }
            if passthrough:
                existing = anthropic_kwargs.get("extra_body") or {}
                if not isinstance(existing, dict):
                    existing = {}
                anthropic_kwargs["extra_body"] = {**existing, **passthrough}

        response = create_anthropic_message(
            self._client,
            anthropic_kwargs,
            # Per streamed event: record provider-response timing always, but
            # tick the forward-progress hook (hosts watching liveness —
            # gateway session hygiene / the compression commit fence) only
            # for substantive payloads, so keepalive pings cannot hold a
            # stalled summary open. No-op when no hook is installed (None
            # keeps the fast get_final_message path).
            on_stream_event=(
                (
                    lambda event: (
                        _notify_aux_provider_response()
                        if _anthropic_event_has_content(event)
                        else _notify_aux_timing_response()
                    )
                )
                if _aux_progress_active()
                else None
            ),
        )
        _transport = get_transport("anthropic_messages")
        _nr = _transport.normalize_response(
            response, strip_tool_prefix=self._is_oauth
        )

        # ToolCall already duck-types as OpenAI shape (.type, .function.name,
        # .function.arguments) via properties, so no wrapping needed.
        assistant_message = SimpleNamespace(
            content=_nr.content,
            tool_calls=_nr.tool_calls,
            reasoning=_nr.reasoning,
        )
        finish_reason = _nr.finish_reason

        usage = None
        if hasattr(response, "usage") and response.usage:
            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "output_tokens", 0) or 0
            total_tokens = getattr(response.usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
            usage = SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choice = SimpleNamespace(
            index=0,
            message=assistant_message,
            finish_reason=finish_reason,
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _AnthropicChatShim:
    def __init__(self, adapter: _AnthropicCompletionsAdapter):
        self.completions = adapter


class AnthropicAuxiliaryClient:
    """OpenAI-client-compatible wrapper over a native Anthropic client."""

    def __init__(self, real_client: Any, model: str, api_key: str, base_url: str, is_oauth: bool = False):
        self._real_client = real_client
        adapter = _AnthropicCompletionsAdapter(
            real_client, model, is_oauth=is_oauth, base_url=base_url,
        )
        self.chat = _AnthropicChatShim(adapter)
        self.api_key = api_key
        self.base_url = base_url

    def close(self):
        close_fn = getattr(self._real_client, "close", None)
        if callable(close_fn):
            close_fn()


class _AsyncAnthropicCompletionsAdapter:
    def __init__(self, sync_adapter: _AnthropicCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio
        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncAnthropicChatShim:
    def __init__(self, adapter: _AsyncAnthropicCompletionsAdapter):
        self.completions = adapter


class AsyncAnthropicAuxiliaryClient:
    def __init__(self, sync_wrapper: "AnthropicAuxiliaryClient"):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncAnthropicCompletionsAdapter(sync_adapter)
        self.chat = _AsyncAnthropicChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        # See AsyncCodexAuxiliaryClient: mirror _real_client so cache
        # eviction on a poisoned underlying client also drops this entry.
        self._real_client = sync_wrapper._real_client


class _BedrockCompletionsAdapter:
    """Translates ``chat.completions.create(**kwargs)`` into Bedrock Converse."""

    def __init__(self, region: str, model: str):
        self._region = region
        self._model = model

    def create(self, **kwargs) -> Any:
        from agent.bedrock_adapter import call_converse

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        # OpenAI accepts ``stop`` as str or list; Converse requires a list.
        stop = kwargs.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        if kwargs.get("tool_choice") is not None:
            # Converse's toolChoice isn't wired through call_converse();
            # no in-tree auxiliary caller passes tool_choice today. Surface
            # the drop instead of silently ignoring it.
            logger.debug(
                "BedrockAuxiliaryClient: tool_choice=%r not supported by the "
                "Converse shim — ignored.", kwargs.get("tool_choice"),
            )
        if kwargs.get("stream"):
            # Converse streaming isn't wired through this shim. Return a
            # complete response instead — call_llm's streaming consumer
            # detects a final object and downgrades to non-live output.
            logger.debug(
                "BedrockAuxiliaryClient: stream=True requested for %s — "
                "returning a complete response (Converse shim does not "
                "stream); caller downgrades to non-streaming.",
                model,
            )
        response = call_converse(
            region=self._region,
            model=model,
            messages=messages,
            tools=kwargs.get("tools"),
            # Omitted/None caller cap → None: build_converse_kwargs then omits
            # inferenceConfig.maxTokens so Bedrock uses the model's maximum
            # allowed output, matching the no-cap-by-default policy every
            # other aux wire already follows (#10809: vision descriptions
            # stayed capped at the shim's old hardcoded 4096 on Bedrock).
            # Truthiness (not `is None`) is deliberate — it matches the
            # sibling Anthropic shim's reading of max_tokens above, so a
            # nonsense explicit 0 is treated as "no cap" on both wires.
            max_tokens=int(max_tokens) if max_tokens else None,
            temperature=kwargs.get("temperature"),
            top_p=kwargs.get("top_p"),
            stop_sequences=stop,
        )
        # Converse is a complete-response API in this shim. Mark provider
        # progress only after the response returns so TTFP reflects real
        # Bedrock latency rather than dispatch/setup activity.
        _notify_aux_provider_response()
        return response


class _BedrockChatShim:
    def __init__(self, adapter: "_BedrockCompletionsAdapter"):
        self.completions = adapter


class BedrockAuxiliaryClient:
    """OpenAI-client-compatible wrapper over AWS Bedrock Converse API."""

    def __init__(self, region: str, model: str):
        self._region = region
        self._model = model
        adapter = _BedrockCompletionsAdapter(region, model)
        self.chat = _BedrockChatShim(adapter)
        self.api_key = "aws-sdk"
        self.base_url = f"https://bedrock-runtime.{region}.amazonaws.com"

    def close(self):
        pass


class _AsyncBedrockCompletionsAdapter:
    def __init__(self, sync_adapter: _BedrockCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio
        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncBedrockChatShim:
    def __init__(self, adapter: _AsyncBedrockCompletionsAdapter):
        self.completions = adapter


class AsyncBedrockAuxiliaryClient:
    def __init__(self, sync_wrapper: "BedrockAuxiliaryClient"):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncBedrockCompletionsAdapter(sync_adapter)
        self.chat = _AsyncBedrockChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url


def _endpoint_speaks_anthropic_messages(base_url: str) -> bool:
    """True if the endpoint at ``base_url`` speaks the Anthropic Messages
    protocol instead of OpenAI chat.completions.

    Mirrors ``hermes_cli.runtime_provider._detect_api_mode_for_url`` so the
    auxiliary client and the main agent stay in sync on transport selection.
    Covers:

    - Any URL ending in ``/anthropic`` (MiniMax, Zhipu GLM, LiteLLM proxies,
      Anthropic-compatible gateways).
    - ``api.kimi.com/coding`` (Kimi Coding Plan — the /coding route only
      speaks Claude-Code's native Anthropic shape; ``chat.completions``
      returns 404 on Anthropic-only model aliases like ``kimi-for-coding``).
    - ``api.anthropic.com`` (native Anthropic).
    """
    normalized = (base_url or "").strip().lower().rstrip("/")
    if not normalized:
        return False
    path = urlparse(normalized).path.rstrip("/")
    if path.endswith("/anthropic") or path.endswith("/anthropic/v1"):
        return True
    hostname = base_url_hostname(normalized)
    if hostname == "api.anthropic.com":
        return True
    if hostname == "api.kimi.com" and "/coding" in normalized:
        return True
    return False


def _maybe_wrap_anthropic(
    client_obj: Any,
    model: str,
    api_key: str,
    base_url: str,
    api_mode: Optional[str] = None,
) -> Any:
    """Rewrap a plain OpenAI client in ``AnthropicAuxiliaryClient`` when
    the endpoint actually speaks Anthropic Messages.

    This is the single chokepoint for aux-client transport correction.
    Runs at the end of every ``resolve_provider_client`` branch so that
    api_key providers (Kimi Coding Plan), the ``custom`` endpoint, and
    future /anthropic gateways all land on the right wire format
    regardless of which branch built the client.

    Returns ``client_obj`` unchanged when:

    - It's already an Anthropic/Codex/Gemini/CopilotACP wrapper.
    - The endpoint is an OpenAI-wire endpoint.
    - ``api_mode`` is explicitly set to a non-Anthropic transport.
    - The ``anthropic`` SDK is not installed (falls back to OpenAI wire).
    """
    # Already wrapped — don't double-wrap.
    if isinstance(client_obj, _AuxProbeClientStub):
        # Availability probe: transport correction is irrelevant — the stub
        # only signals resolvability. Skipping also avoids importing adapter
        # modules (copilot_acp_client pulls in openai.types) on the probe path.
        return client_obj
    if _safe_isinstance(client_obj, AnthropicAuxiliaryClient):
        return client_obj
    if _safe_isinstance(client_obj, BedrockAuxiliaryClient):
        return client_obj
    # Other specialized adapters we should never re-dispatch.
    if _safe_isinstance(client_obj, CodexAuxiliaryClient):
        return client_obj
    try:
        from agent.gemini_native_adapter import GeminiNativeClient
        if _safe_isinstance(client_obj, GeminiNativeClient):
            return client_obj
    except ImportError:
        pass
    try:
        from agent.copilot_acp_client import CopilotACPClient
        if _safe_isinstance(client_obj, CopilotACPClient):
            return client_obj
    except ImportError:
        pass

    # Explicit non-anthropic api_mode wins over URL heuristics.
    if api_mode and api_mode != "anthropic_messages":
        return client_obj

    should_wrap = (
        api_mode == "anthropic_messages"
        or _endpoint_speaks_anthropic_messages(base_url)
    )
    if not should_wrap:
        return client_obj

    try:
        from agent.anthropic_adapter import build_anthropic_client
    except ImportError:
        logger.warning(
            "Endpoint %s speaks Anthropic Messages but the anthropic SDK is "
            "not installed — falling back to OpenAI-wire (will likely 404).",
            base_url,
        )
        return client_obj

    try:
        real_client = build_anthropic_client(api_key, base_url)
    except Exception as exc:
        logger.warning(
            "Failed to build Anthropic client for %s (%s) — falling back to "
            "OpenAI-wire client.", base_url, exc,
        )
        return client_obj

    logger.debug(
        "Auxiliary transport: wrapping client in AnthropicAuxiliaryClient "
        "(model=%s, base_url=%s, api_mode=%s)",
        model, base_url[:60] if base_url else "", api_mode or "auto-detected",
    )
    return AnthropicAuxiliaryClient(
        real_client, model, api_key, base_url, is_oauth=False,
    )


def _read_nous_auth() -> Optional[dict]:
    """Read and validate ~/.hermes/auth.json for an active Nous provider.

    Returns the provider state dict if Nous is active with tokens,
    otherwise None.
    """
    pool_present, entry = _select_pool_entry("nous")
    if pool_present:
        if entry is None:
            return None
        return {
            "access_token": getattr(entry, "access_token", ""),
            "refresh_token": getattr(entry, "refresh_token", None),
            "agent_key": getattr(entry, "agent_key", None),
            "inference_base_url": _pool_runtime_base_url(entry, _NOUS_DEFAULT_BASE_URL),
            "portal_base_url": getattr(entry, "portal_base_url", None),
            "client_id": getattr(entry, "client_id", None),
            "scope": getattr(entry, "scope", None),
            "token_type": getattr(entry, "token_type", "Bearer"),
            "source": "pool",
        }

    try:
        if not _AUTH_JSON_PATH.is_file():
            return None
        data = json.loads(_AUTH_JSON_PATH.read_text(encoding="utf-8-sig"))
        if data.get("active_provider") != "nous":
            return None
        provider = data.get("providers", {}).get("nous", {})
        # Must have at least an access_token or agent_key
        if not provider.get("agent_key") and not provider.get("access_token"):
            return None
        return provider
    except Exception as exc:
        logger.debug("Could not read Nous auth: %s", exc)
        return None


def _nous_api_key(provider: dict) -> str:
    """Extract a usable Nous inference JWT from stored auth state."""
    from hermes_cli.auth import _nous_invoke_jwt_is_usable

    for token_key, expiry_key in (
        ("agent_key", "agent_key_expires_at"),
        ("access_token", "expires_at"),
    ):
        token = provider.get(token_key)
        if not isinstance(token, str) or not token.strip():
            continue
        if _nous_invoke_jwt_is_usable(
            token,
            scope=provider.get("scope"),
            expires_at=provider.get(expiry_key),
        ):
            return token
    return ""


def _nous_base_url() -> str:
    """Resolve the Nous inference base URL from env or default."""
    return os.getenv("NOUS_INFERENCE_BASE_URL", _NOUS_DEFAULT_BASE_URL)


def _resolve_nous_pool_runtime_api(*, force_refresh: bool = False) -> Optional[tuple[str, str]]:
    """Resolve Nous auxiliary credentials from the selected pool entry."""
    try:
        from hermes_cli.auth import _agent_key_is_usable

        pool = load_pool("nous")
    except Exception as exc:
        logger.debug("Auxiliary Nous pool credential resolution failed: %s", exc)
        return None

    if not pool or not pool.has_credentials():
        return None

    try:
        entry = pool.select()
    except Exception as exc:
        logger.debug("Auxiliary Nous pool selection failed: %s", exc)
        return None

    if entry is None:
        return None

    state = {
        "agent_key": getattr(entry, "agent_key", None),
        "agent_key_expires_at": getattr(entry, "agent_key_expires_at", None),
        "scope": getattr(entry, "scope", None),
    }
    if force_refresh or not _agent_key_is_usable(state, _nous_min_key_ttl_seconds()):
        try:
            refreshed = pool.try_refresh_current()
        except Exception as exc:
            logger.debug("Auxiliary Nous pool refresh failed: %s", exc)
            refreshed = None
        if refreshed is None:
            return None
        entry = refreshed

    provider = {
        "agent_key": getattr(entry, "agent_key", None),
        "agent_key_expires_at": getattr(entry, "agent_key_expires_at", None),
        "access_token": getattr(entry, "access_token", None),
        "expires_at": getattr(entry, "expires_at", None),
        "scope": getattr(entry, "scope", None),
    }
    api_key = _nous_api_key(provider)
    base_url = _pool_runtime_base_url(entry, _NOUS_DEFAULT_BASE_URL)
    if not api_key or not base_url:
        return None
    return api_key, base_url


def _resolve_nous_runtime_api(*, force_refresh: bool = False) -> Optional[tuple[str, str]]:
    """Return fresh Nous runtime credentials when available.

    This mirrors the main agent's 401 recovery path and keeps auxiliary
    clients aligned with the singleton auth store + JWT refresh flow instead of
    relying only on whatever raw tokens happen to be sitting in auth.json
    or the credential pool.
    """
    pooled = _resolve_nous_pool_runtime_api(force_refresh=force_refresh)
    if pooled is not None:
        return pooled

    try:
        from hermes_cli.auth import resolve_nous_runtime_credentials

        creds = resolve_nous_runtime_credentials(
            timeout_seconds=env_float("HERMES_NOUS_TIMEOUT_SECONDS", 15),
            force_refresh=force_refresh,
        )
    except Exception as exc:
        logger.debug("Auxiliary Nous runtime credential resolution failed: %s", exc)
        return None

    api_key = str(creds.get("api_key") or "").strip()
    base_url = str(creds.get("base_url") or "").strip().rstrip("/")
    if not api_key or not base_url:
        return None
    return api_key, base_url


def _resolve_xai_oauth_for_aux() -> Optional[Tuple[str, str]]:
    """Resolve a fresh xAI OAuth (api_key, base_url) for auxiliary clients.

    Prefer the credential pool, matching the main runtime/provider status
    path.  Some xAI OAuth logins live only as pool entries; falling straight
    to the singleton auth-store resolver would make auxiliary tasks such as
    compression report "no provider configured" even though ``hermes auth
    status`` shows xAI OAuth as logged in.

    Falls back to ``hermes_cli.auth``'s singleton runtime resolver for older
    auth-store-only logins. Returns ``None`` if the user is not authenticated
    with xAI Grok OAuth.
    """
    try:
        from hermes_cli.auth import (
            DEFAULT_XAI_OAUTH_BASE_URL,
            _xai_validate_inference_base_url,
        )

        pool = load_pool("xai-oauth")
        if pool and pool.has_credentials():
            entry = pool.select()
            if entry is not None:
                api_key = str(
                    getattr(entry, "runtime_api_key", None)
                    or getattr(entry, "access_token", "")
                    or ""
                ).strip()
                base_url = _xai_validate_inference_base_url(
                    os.getenv("HERMES_XAI_BASE_URL", "").strip().rstrip("/")
                    or os.getenv("XAI_BASE_URL", "").strip().rstrip("/")
                    or str(getattr(entry, "runtime_base_url", None) or "").strip().rstrip("/")
                    or str(getattr(entry, "base_url", None) or "").strip().rstrip("/"),
                    fallback=DEFAULT_XAI_OAUTH_BASE_URL,
                )
                if api_key and base_url:
                    return api_key, base_url
    except Exception as exc:
        logger.debug("Auxiliary xAI OAuth pool credential resolution failed: %s", exc)

    try:
        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

        creds = resolve_xai_oauth_runtime_credentials()
    except Exception as exc:
        logger.debug("Auxiliary xAI OAuth runtime credential resolution failed: %s", exc)
        return None

    api_key = str(creds.get("api_key") or "").strip()
    base_url = str(creds.get("base_url") or "").strip().rstrip("/")
    if not api_key or not base_url:
        return None
    return api_key, base_url


def _read_codex_access_token() -> Optional[str]:
    """Read a valid, non-expired Codex OAuth access token from Hermes auth store.

    If a credential pool exists but currently has no selectable runtime entry
    (for example all pool slots are marked exhausted), fall back to the
    profile's auth.json token instead of hard-failing. This keeps explicit
    fallback-to-Codex working when the pool state is stale but the stored OAuth
    token is still valid.
    """
    pool_present, entry = _select_pool_entry("openai-codex")
    if pool_present:
        token = _pool_runtime_api_key(entry)
        if token:
            return token

    try:
        from hermes_cli.auth import _read_codex_tokens
        data = _read_codex_tokens()
        tokens = data.get("tokens", {})
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return None

        # Check JWT expiry — expired tokens block the auto chain and
        # prevent fallback to working providers (e.g. Anthropic).
        try:
            import base64
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            exp = claims.get("exp", 0)
            if exp and time.time() > exp:
                logger.debug("Codex access token expired (exp=%s), skipping", exp)
                return None
        except Exception:
            pass  # Non-JWT token or decode error — use as-is

        return access_token.strip()
    except Exception as exc:
        logger.debug("Could not read Codex auth for auxiliary client: %s", exc)
        return None


def _resolve_api_key_provider() -> Tuple[Optional[OpenAI], Optional[str]]:
    """Try each API-key provider in PROVIDER_REGISTRY order.

    Returns (client, model) for the first provider with usable runtime
    credentials, or (None, None) if none are configured.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, resolve_api_key_provider_credentials
    except ImportError:
        logger.debug("Could not import PROVIDER_REGISTRY for API-key fallback")
        return None, None

    for provider_id, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != "api_key":
            continue
        if _is_provider_unhealthy(provider_id):
            logger.debug("Auxiliary api-key chain: %s is unhealthy, skipping", provider_id)
            continue
        if provider_id == "anthropic":
            # Only try anthropic when the user has explicitly configured it.
            # Without this gate, Claude Code credentials get silently used
            # as auxiliary fallback when the user's primary provider fails.
            try:
                from hermes_cli.auth import is_provider_explicitly_configured
                if not is_provider_explicitly_configured("anthropic"):
                    continue
            except ImportError:
                pass
            return _try_anthropic()

        pool_present, entry = _select_pool_entry(provider_id)
        if pool_present:
            api_key = _pool_runtime_api_key(entry)
            if not api_key:
                continue

            raw_base_url = _pool_runtime_base_url(entry, pconfig.inference_base_url) or pconfig.inference_base_url
            base_url = _to_openai_base_url(raw_base_url)
            model = _get_aux_model_for_provider(provider_id) or None
            if model is None:
                continue  # skip provider if we don't know a valid aux model
            logger.debug("Auxiliary text client: %s (%s) via pool", pconfig.name, model)
            if provider_id == "gemini":
                from agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url

                if is_native_gemini_base_url(base_url):
                    return GeminiNativeClient(api_key=api_key, base_url=base_url), model
            extra = {}
            if base_url_host_matches(base_url, "api.kimi.com"):
                extra["default_headers"] = {"User-Agent": "claude-code/0.1.0"}
            elif base_url_host_matches(base_url, "githubcopilot.com"):
                from hermes_cli.models import copilot_default_headers

                extra["default_headers"] = copilot_default_headers()
            elif base_url_host_matches(base_url, "integrate.api.nvidia.com"):
                extra["default_headers"] = build_nvidia_nim_headers(base_url)
            else:
                try:
                    from providers import get_provider_profile as _gpf_aux
                    _ph_aux = _gpf_aux(provider_id)
                    if _ph_aux and _ph_aux.default_headers:
                        extra["default_headers"] = dict(_ph_aux.default_headers)
                except Exception:
                    pass
            _merged_aux = _apply_user_default_headers(extra.get("default_headers"))
            if _merged_aux:
                extra["default_headers"] = _merged_aux
            _client = _create_openai_client(api_key=api_key, base_url=base_url, **extra)
            _client = _maybe_wrap_anthropic(_client, model, api_key, raw_base_url)
            return _client, model

        creds = resolve_api_key_provider_credentials(provider_id)
        api_key = str(creds.get("api_key", "")).strip()
        if not api_key:
            continue

        raw_base_url = str(creds.get("base_url", "")).strip().rstrip("/") or pconfig.inference_base_url
        base_url = _to_openai_base_url(raw_base_url)
        model = _get_aux_model_for_provider(provider_id) or None
        if model is None:
            continue  # skip provider if we don't know a valid aux model
        logger.debug("Auxiliary text client: %s (%s)", pconfig.name, model)
        if provider_id == "gemini":
            from agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url

            if is_native_gemini_base_url(base_url):
                return GeminiNativeClient(api_key=api_key, base_url=base_url), model
        extra = {}
        if base_url_host_matches(base_url, "api.kimi.com"):
            extra["default_headers"] = {"User-Agent": "claude-code/0.1.0"}
        elif base_url_host_matches(base_url, "githubcopilot.com"):
            from hermes_cli.models import copilot_default_headers

            extra["default_headers"] = copilot_default_headers()
        elif base_url_host_matches(base_url, "integrate.api.nvidia.com"):
            extra["default_headers"] = build_nvidia_nim_headers(base_url)
        else:
            try:
                from providers import get_provider_profile as _gpf_aux2
                _ph_aux2 = _gpf_aux2(provider_id)
                if _ph_aux2 and _ph_aux2.default_headers:
                    extra["default_headers"] = dict(_ph_aux2.default_headers)
            except Exception:
                pass
        _merged_aux2 = _apply_user_default_headers(extra.get("default_headers"))
        if _merged_aux2:
            extra["default_headers"] = _merged_aux2
        _client = _create_openai_client(api_key=api_key, base_url=base_url, **extra)
        _client = _maybe_wrap_anthropic(_client, model, api_key, raw_base_url)
        return _client, model

    return None, None


# ── Provider resolution helpers ─────────────────────────────────────────────


_paid_lane_warned: set = set()


def _is_free_model(model: Optional[str]) -> bool:
    """True when ``model`` is a free SKU (``:free`` suffix or ``stealth/`` prefix).

    Naming-convention trust: a paid model shipped under ``stealth/`` would
    silently bypass both the free_only gate and the paid-lane warning.
    """
    if not model:
        return False
    normalized = str(model).strip()
    return normalized.endswith(":free") or normalized.startswith("stealth/")


def _aux_openrouter_settings() -> Tuple[bool, str]:
    """Read free_only and openrouter_model from config in one pass.

    Returns (free_only, model) — defaults (False, _OPENROUTER_MODEL) on any
    config-read failure.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        cfg = load_config_readonly()
        free_only = bool(cfg_get(cfg, "auxiliary", "free_only", default=False))
        val = cfg_get(cfg, "auxiliary", "openrouter_model")
        model = val.strip() if isinstance(val, str) and val.strip() else _OPENROUTER_MODEL
        return free_only, model
    except Exception:
        return False, _OPENROUTER_MODEL


def _warn_paid_lane_once(model: str) -> None:
    """Log a WARNING the first time a non-free (neither ``:free`` nor
    ``stealth/``) OpenRouter model is engaged."""
    if model in _paid_lane_warned:
        return
    _paid_lane_warned.add(model)
    logger.warning(
        "Auxiliary client: PAID lane engaged for auxiliary task — OpenRouter "
        "fallback model %r is not a :free SKU and may incur real spend. Set "
        "auxiliary.free_only: true to restrict auxiliary fallbacks to free "
        "models, or auxiliary.openrouter_model to a :free model.",
        model,
    )


def _try_openrouter(explicit_api_key: str = None, model: str = None) -> Tuple[Optional[OpenAI], Optional[str]]:
    free_only, cfg_model = _aux_openrouter_settings()
    or_model = model or cfg_model
    if free_only and not _is_free_model(or_model):
        logger.warning(
            "Auxiliary client: auxiliary.free_only is enabled but the "
            "OpenRouter fallback model %r is not a :free SKU — skipping the "
            "OpenRouter fallback. Set auxiliary.openrouter_model to a :free "
            "model (e.g. nvidia/nemotron-3-ultra-550b-a55b:free) or disable "
            "auxiliary.free_only.",
            or_model,
        )
        return None, None
    if not _is_free_model(or_model):
        _warn_paid_lane_once(or_model)

    pool_present, entry = _select_pool_entry("openrouter")
    if pool_present:
        or_key = explicit_api_key or _pool_runtime_api_key(entry)
        if or_key:
            base_url = _pool_runtime_base_url(entry, OPENROUTER_BASE_URL) or OPENROUTER_BASE_URL
            logger.debug("Auxiliary client: OpenRouter via pool")
            return _create_openai_client(api_key=or_key, base_url=base_url,
                           default_headers=build_or_headers()), or_model
        # Pool exists but is exhausted (no usable runtime key) — fall through to
        # the OPENROUTER_API_KEY env-var path rather than failing outright.
        logger.debug("Auxiliary client: OpenRouter pool exhausted, trying OPENROUTER_API_KEY")

    or_key = explicit_api_key or _scoped_key_env("OPENROUTER_API_KEY")
    if not or_key:
        _mark_provider_unhealthy("openrouter", ttl=60)
        return None, None
    logger.debug("Auxiliary client: OpenRouter")
    return _create_openai_client(api_key=or_key, base_url=OPENROUTER_BASE_URL,
                   default_headers=build_or_headers()), or_model


def _describe_openrouter_unavailable(model: str = None) -> str:
    """Return the policy or credential reason OpenRouter was unavailable."""
    free_only, cfg_model = _aux_openrouter_settings()
    or_model = model or cfg_model
    if free_only and not _is_free_model(or_model):
        return (
            f"auxiliary.free_only rejected non-free model {or_model!r}; "
            "the request was skipped before provider availability checks"
        )
    pool_present, entry = _select_pool_entry("openrouter")
    if pool_present:
        if entry is None:
            return "OpenRouter credential pool has no usable entries (credentials may be exhausted)"
        if not _pool_runtime_api_key(entry):
            return "OpenRouter credential pool entry is missing a runtime API key"
    if not _scoped_key_env("OPENROUTER_API_KEY"):
        return "OPENROUTER_API_KEY not set"
    return "no usable OpenRouter credentials found"


def _try_nous(vision: bool = False) -> Tuple[Optional[OpenAI], Optional[str]]:
    # Check cross-session rate limit guard before attempting Nous —
    # if another session already recorded a 429, skip Nous entirely
    # to avoid piling more requests onto the tapped RPH bucket.
    try:
        from agent.nous_rate_guard import nous_rate_limit_remaining
        _remaining = nous_rate_limit_remaining()
        if _remaining is not None and _remaining > 0:
            logger.debug(
                "Auxiliary: skipping Nous Portal (rate-limited, resets in %.0fs)",
                _remaining,
            )
            _mark_provider_unhealthy("nous", ttl=_remaining)
            return None, None
    except Exception:
        pass

    nous = _read_nous_auth()
    runtime = _resolve_nous_runtime_api(force_refresh=False)
    if runtime is None and not nous:
        logger.warning(
            "Auxiliary Nous client unavailable: no Nous authentication found "
            "(run: hermes auth)."
        )
        _mark_provider_unhealthy("nous", ttl=60)
        return None, None
    if runtime is None and nous:
        logger.debug(
            "Auxiliary Nous: runtime JWT refresh failed; checking stored "
            "auth.json token."
        )
    global auxiliary_is_nous
    auxiliary_is_nous = True
    logger.debug("Auxiliary client: Nous Portal")

    # Ask the Portal which model it currently recommends for this task type.
    # The /api/nous/recommended-models endpoint is the authoritative source:
    # it distinguishes paid vs free tier recommendations, and get_nous_recommended_aux_model
    # auto-detects the caller's tier via check_nous_free_tier().  Fall back to
    # _NOUS_MODEL (google/gemini-3-flash-preview) when the Portal is unreachable
    # or returns a null recommendation for this task type.
    model = _NOUS_MODEL
    if not _aux_probe_active():
        # Availability probes skip the recommended-model lookup: the exact
        # model is irrelevant to "is Nous resolvable?", and the Portal
        # recommended-models fetch below can hit the network.
        try:
            from hermes_cli.models import get_nous_recommended_aux_model
            recommended = get_nous_recommended_aux_model(vision=vision)
            if recommended:
                model = recommended
                logger.debug(
                    "Auxiliary/%s: using Portal-recommended model %s",
                    "vision" if vision else "text", model,
                )
            else:
                logger.debug(
                    "Auxiliary/%s: no Portal recommendation, falling back to %s",
                    "vision" if vision else "text", model,
                )
        except Exception as exc:
            logger.debug(
                "Auxiliary/%s: recommended-models lookup failed (%s); "
                "falling back to %s",
                "vision" if vision else "text", exc, model,
            )

    if runtime is not None:
        api_key, base_url = runtime
    else:
        api_key = _nous_api_key(nous or {})
        if not api_key:
            logger.warning(
                "Auxiliary Nous client unavailable: no usable inference JWT found "
                "(run: hermes auth add nous)."
            )
            _mark_provider_unhealthy("nous", ttl=60)
            return None, None
        base_url = str((nous or {}).get("inference_base_url") or _nous_base_url()).rstrip("/")
    return (
        _create_openai_client(
            api_key=api_key,
            base_url=base_url,
        ),
        model,
    )


def _refresh_nous_recommended_model(
    *, vision: bool, stale_model: Optional[str]
) -> Optional[str]:
    """Re-fetch the Nous Portal's recommended model after a stale-model 404.

    Long-lived processes (gateway, watchers) cache the Portal's
    ``recommended-models`` payload for 10 minutes and, in practice, can pin a
    model for the whole process lifetime. When that model is later dropped from
    the Nous → OpenRouter catalog, every auxiliary call 404s with
    "model does not exist". This forces a fresh Portal fetch and returns a
    model name to retry with:

      * the Portal's current recommendation for the task, if it differs from
        the model that just failed; otherwise
      * ``_NOUS_MODEL`` (google/gemini-3-flash-preview), the known-good default,
        if it too differs from the failed model.

    Returns ``None`` when no usable alternative is available (e.g. the Portal
    still recommends the exact model that just 404'd and the default also
    matches it) — callers should then let the original error propagate.
    """
    stale = (stale_model or "").strip().lower()
    fresh: Optional[str] = None
    try:
        from hermes_cli.models import get_nous_recommended_aux_model

        fresh = get_nous_recommended_aux_model(vision=vision, force_refresh=True)
    except Exception as exc:
        logger.debug(
            "Nous recommended-model refresh failed (%s); using default %s",
            exc, _NOUS_MODEL,
        )
    if fresh and fresh.strip().lower() != stale:
        return fresh
    # Portal recommendation unchanged or unavailable — fall back to the
    # hardcoded known-good default, but only if it's actually different.
    if _NOUS_MODEL.strip().lower() != stale:
        return _NOUS_MODEL
    return None


def _read_main_model() -> str:
    """Read the user's configured main model from config.yaml.

    config.yaml model.default is the single source of truth for the active
    model. Environment variables are no longer consulted.

    Runtime override: when an AIAgent is active with a CLI/gateway-provided
    model that differs from config.yaml, ``set_runtime_main()`` records the
    override in a process-local global. This is consulted FIRST so tools
    that gate on "the active main model" (e.g. ``vision_analyze``'s native
    fast path) see the live runtime, not the persisted config default.
    """
    override = _runtime_main_value("model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str) and model_cfg.strip():
            return model_cfg.strip()
        if isinstance(model_cfg, dict):
            default = model_cfg.get("default", "")
            if isinstance(default, str) and default.strip():
                return default.strip()
    except Exception:
        pass
    return ""


def _read_main_provider() -> str:
    """Read the user's configured main provider from config.yaml.

    Returns the lowercase provider id (e.g. "alibaba", "openrouter") or ""
    if not configured.

    Runtime override: see ``_read_main_model`` — same mechanism for the
    provider half of the runtime tuple.
    """
    override = _runtime_main_value("provider")
    if isinstance(override, str) and override.strip():
        return override.strip().lower()
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            provider = model_cfg.get("provider", "")
            if isinstance(provider, str) and provider.strip():
                return provider.strip().lower()
    except Exception:
        pass
    return ""


def _read_main_api_key() -> str:
    """Read the user's main model API key from the runtime override or config.

    Mirrors ``_read_main_model`` / ``_read_main_provider``: checks the
    process-local ``_RUNTIME_MAIN_API_KEY`` override first (set by
    ``set_runtime_main`` when an AIAgent is active), then falls back to
    ``model.api_key`` in config.yaml.

    Used by the ``custom`` provider fallback chain so that auxiliary tasks
    configured with an explicit ``base_url`` but empty ``api_key`` inherit
    the main model's credentials instead of falling to ``no-key-required``
    (issue #9318).
    """
    override = _runtime_main_value("api_key")
    if isinstance(override, str) and override.strip():
        return override.strip()
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            key = model_cfg.get("api_key", "")
            if isinstance(key, str) and key.strip():
                return key.strip()
    except Exception:
        pass
    return ""


def _read_main_base_url() -> str:
    """Read the main model's base_url from the runtime override or config.

    Same override-then-config pattern as ``_read_main_api_key``.
    """
    override = _runtime_main_value("base_url")
    if isinstance(override, str) and override.strip():
        return override.strip()
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            base = model_cfg.get("base_url", "")
            if isinstance(base, str) and base.strip():
                return base.strip()
    except Exception:
        pass
    return ""


def _resolve_moa_aggregator(preset_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a MoA preset to its aggregator (provider, model) pair.

    "moa" is a virtual provider — the acting model of a preset is its
    aggregator slot, and there is no real "moa" HTTP endpoint. Auxiliary
    tasks (title generation, compression, vision, commit messages, …) don't
    need the reference fan-out, so every aux resolution layer maps
    provider="moa"/model=<preset> to the aggregator's real provider+model
    through this single helper (shared by ``_resolve_auto``,
    ``_resolve_task_provider_model``, and ``resolve_provider_client`` so the
    preset lookup and validation cannot drift between paths).

    Args:
        preset_name: The MoA preset name (usually carried in the "model"
            field), or None/"" to resolve the user's default preset.

    Returns:
        (aggregator_provider, aggregator_model), or (None, None) when the
        preset cannot be resolved (missing config, renamed/deleted preset,
        or a malformed aggregator slot).
    """
    try:
        from hermes_cli.config import load_config
        from hermes_cli.moa_config import resolve_moa_preset

        preset = resolve_moa_preset(load_config().get("moa") or {}, preset_name or None)
        agg = preset.get("aggregator") or {}
        agg_provider = str(agg.get("provider") or "").strip()
        agg_model = str(agg.get("model") or "").strip()
        if agg_provider and agg_model and agg_provider.lower() != "moa":
            return agg_provider, agg_model
    except Exception:
        logger.debug(
            "MoA aggregator resolution failed for preset %r", preset_name, exc_info=True
        )
    return None, None


def _read_main_model_for_aux() -> str:
    """Main model with MoA presets unwrapped to the aggregator's model.

    When the main provider is ``moa``, ``_read_main_model()`` returns a MoA
    *preset name* (e.g. "opus-gpt") — never a valid wire model id on any
    provider. Auxiliary fallback chains that pre-fill a missing model from
    the main model must use this reader instead, so unset aux models default
    to the preset's acting (aggregator) model. Returns "" when the main
    provider is moa but the preset cannot be resolved — sending nothing is
    strictly better than sending a preset name that 400s.
    """
    model = _read_main_model()
    if (_read_main_provider() or "").strip().lower() == "moa":
        _, agg_model = _resolve_moa_aggregator(model)
        return agg_model or ""
    return model


def _read_main_api_key_if_same_host(aux_base_url: str) -> str:
    """Return the main api_key only when *aux_base_url* points at the same
    host as the main model's base_url.

    The #9318 use case is an auxiliary task sharing the main model's
    self-hosted gateway (same host, different model) with an empty per-task
    api_key. Inheriting unconditionally would send the main credential to
    ANY host a misconfigured aux base_url names — a cross-host credential
    leak. A host mismatch keeps the previous fail-safe behavior
    (``no-key-required`` → 401).
    """
    aux_host = base_url_hostname(aux_base_url)
    if not aux_host:
        return ""
    main_host = base_url_hostname(_read_main_base_url())
    if not main_host or aux_host != main_host:
        return ""
    return _read_main_api_key()


# Compatibility mirrors for older readers/tests. The authoritative value is
# the ContextVar below: gateway sessions can overlap in one process, so a
# process-global tuple is not safe as routing or cache-key input.
_RUNTIME_MAIN_PROVIDER: str = ""
_RUNTIME_MAIN_MODEL: str = ""
_RUNTIME_MAIN_BASE_URL: str = ""
_RUNTIME_MAIN_API_KEY: Any = ""
_RUNTIME_MAIN_API_MODE: str = ""
_RUNTIME_MAIN_AUTH_MODE: str = ""
