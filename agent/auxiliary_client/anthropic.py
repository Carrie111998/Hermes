"""Anthropic Messages API auxiliary client adapters.

Extracted from the former ``agent/auxiliary_client.py`` monolith into a
subpackage module. These classes translate ``chat.completions.create(**kwargs)``
calls into the Anthropic Messages API.
"""

from types import SimpleNamespace
from typing import Any

from . import _aux_progress_active, _notify_aux_progress

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
        # survive. Two exclusions:
        #   - ``reasoning``: the OpenAI-shaped config dict is TRANSLATED into
        #     the native ``thinking`` field above (build_anthropic_kwargs);
        #     forwarding the raw field alongside would double-specify
        #     reasoning and 400 on strict gateways.
        #   - ``_``-prefixed keys: private Hermes plumbing (_reasoning_config
        #     et al.), never wire fields.
        caller_extra_body = kwargs.get("extra_body")
        if caller_extra_body and isinstance(caller_extra_body, dict):
            passthrough = {
                k: v for k, v in caller_extra_body.items()
                if k != "reasoning" and not str(k).startswith("_")
            }
            if passthrough:
                existing = anthropic_kwargs.get("extra_body") or {}
                if not isinstance(existing, dict):
                    existing = {}
                anthropic_kwargs["extra_body"] = {**existing, **passthrough}

        response = create_anthropic_message(
            self._client,
            anthropic_kwargs,
            # Tick the aux forward-progress hook per streamed event so hosts
            # watching liveness (gateway session hygiene) don't kill a
            # slow-but-generating summary model. No-op when no hook is
            # installed (None keeps the fast get_final_message path).
            on_stream_event=(
                (lambda _event: _notify_aux_progress())
                if _aux_progress_active() else None
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
