"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''



def _format_connect_error(exc: BaseException) -> str:
    """Render nested MCP connection errors into an actionable short message."""

    def _find_missing(current: BaseException) -> Optional[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                missing = _find_missing(child)
                if missing:
                    return missing
            return None
        if isinstance(current, FileNotFoundError):
            if getattr(current, "filename", None):
                return str(current.filename)
            match = re.search(r"No such file or directory: '([^']+)'", str(current))
            if match:
                return match.group(1)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                missing = _find_missing(nested_exc)
                if missing:
                    return missing
        return None

    def _flatten_messages(current: BaseException) -> List[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            flattened: List[str] = []
            for child in nested:
                flattened.extend(_flatten_messages(child))
            return flattened
        messages = []
        text = str(current).strip()
        if text:
            messages.append(text)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                messages.extend(_flatten_messages(nested_exc))
        return messages or [current.__class__.__name__]

    missing = _find_missing(exc)
    if missing:
        message = f"missing executable '{missing}'"
        if os.path.basename(missing) in {"npx", "npm", "node"}:
            message += (
                " (ensure Node.js is installed and PATH includes its bin directory, "
                "or set mcp_servers.<name>.command to an absolute path and include "
                "that directory in mcp_servers.<name>.env.PATH)"
            )
        return _sanitize_error(message)

    deduped: List[str] = []
    for item in _flatten_messages(exc):
        if item not in deduped:
            deduped.append(item)
    return _sanitize_error("; ".join(deduped[:3]))


# ---------------------------------------------------------------------------
# Sampling -- server-initiated LLM requests (MCP sampling/createMessage)
# ---------------------------------------------------------------------------

def _safe_numeric(value, default, coerce=int, minimum=1):
    """Coerce a config value to a numeric type, returning *default* on failure.

    Handles string values from YAML (e.g. ``"10"`` instead of ``10``),
    non-finite floats, and values below *minimum*.
    """
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


class SamplingHandler:
    """Handles sampling/createMessage requests for a single MCP server.

    .. deprecated-upstream:: MCP 2026-07-28 deprecates the Sampling feature
       (SEP-2577, 12-month window; suggested migration is direct LLM-provider
       integration server-side). This handler stays fully functional for the
       deprecation window because handshake-era servers in the wild still
       issue sampling/createMessage — but do NOT grow new capability here;
       modern servers use MRTR (``resultType: "input_required"``) instead of
       server-initiated requests, which the SDK's session layer handles.

    Each MCPServerTask that has sampling enabled creates one SamplingHandler.
    The handler is callable and passed directly to ``ClientSession`` as
    the ``sampling_callback``.  All state (rate-limit timestamps, metrics,
    tool-loop counters) lives on the instance -- no module-level globals.

    The callback is async and runs on the MCP background event loop.  The
    sync LLM call is offloaded to a thread via ``asyncio.to_thread()`` so
    it doesn't block the event loop.
    """

    _STOP_REASON_MAP = {"stop": "endTurn", "length": "maxTokens", "tool_calls": "toolUse"}

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.max_rpm = _safe_numeric(config.get("max_rpm", 10), 10, int)
        self.timeout = _safe_numeric(config.get("timeout", 30), 30, float)
        self.max_tokens_cap = _safe_numeric(config.get("max_tokens_cap", 4096), 4096, int)
        self.max_tool_rounds = _safe_numeric(
            config.get("max_tool_rounds", 5), 5, int, minimum=0,
        )
        self.model_override = config.get("model")
        self.allowed_models = config.get("allowed_models", [])

        _log_levels = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}
        self.audit_level = _log_levels.get(
            str(config.get("log_level", "info")).lower(), logging.INFO,
        )

        # Per-instance state
        self._rate_timestamps: List[float] = []
        self._tool_loop_count = 0
        self.metrics = {"requests": 0, "errors": 0, "tokens_used": 0, "tool_use_count": 0}

    # -- Rate limiting -------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Sliding-window rate limiter.  Returns True if request is allowed."""
        now = time.time()
        window = now - 60
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if t > window]
        if len(self._rate_timestamps) >= self.max_rpm:
            return False
        self._rate_timestamps.append(now)
        return True

    # -- Model resolution ----------------------------------------------------

    def _resolve_model(self, preferences) -> Optional[str]:
        """Config override > server hint > None (use default)."""
        if self.model_override:
            return self.model_override
        if preferences and hasattr(preferences, "hints") and preferences.hints:
            for hint in preferences.hints:
                if hasattr(hint, "name") and hint.name:
                    return hint.name
        return None

    # -- Message conversion --------------------------------------------------

    @staticmethod
    def _extract_tool_result_text(block) -> str:
        """Extract text from a ToolResultContent block."""
        if not hasattr(block, "content") or block.content is None:
            return ""
        items = block.content if isinstance(block.content, list) else [block.content]
        return "\n".join(item.text for item in items if hasattr(item, "text"))

    def _convert_messages(self, params) -> List[dict]:
        """Convert MCP SamplingMessages to OpenAI format.

        Uses ``msg.content_as_list`` (SDK helper) so single-block and
        list-of-blocks are handled uniformly.  Dispatches per block type
        with ``isinstance`` on real SDK types when available, falling back
        to duck-typing via ``hasattr`` for compatibility.
        """
        # The presence of a tool-use id is the discriminator for a tool
        # *result* block, so it has to be read under both spellings (see
        # mcp_field) — on mcp 2.x a bare ``hasattr(b, "toolUseId")`` is False
        # for every block, which silently drops tool results out of the
        # conversation and pushes them down the "unsupported block type" path
        # below.
        def _tool_use_id(block):
            return mcp_field(block, "tool_use_id", "toolUseId", _MISSING)

        def _is_tool_use(block):
            return hasattr(block, "name") and hasattr(block, "input")

        messages: List[dict] = []
        for msg in params.messages:
            blocks = msg.content_as_list if hasattr(msg, "content_as_list") else (
                msg.content if isinstance(msg.content, list) else [msg.content]
            )

            # Separate blocks by kind.
            tool_results = [b for b in blocks if _tool_use_id(b) is not _MISSING]
            tool_uses = [
                b for b in blocks
                if _is_tool_use(b) and _tool_use_id(b) is _MISSING
            ]
            content_blocks = [
                b for b in blocks
                if _tool_use_id(b) is _MISSING and not _is_tool_use(b)
            ]

            # Emit tool result messages (role: tool)
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": _tool_use_id(tr),
                    "content": self._extract_tool_result_text(tr),
                })

            # Emit assistant tool_calls message
            if tool_uses:
                tc_list = []
                for tu in tool_uses:
                    tc_list.append({
                        "id": getattr(tu, "id", f"call_{len(tc_list)}"),
                        "type": "function",
                        "function": {
                            "name": tu.name,
                            "arguments": json.dumps(tu.input, ensure_ascii=False) if isinstance(tu.input, dict) else str(tu.input),
                        },
                    })
                msg_dict: dict = {"role": msg.role, "tool_calls": tc_list}
                # Include any accompanying text
                text_parts = [b.text for b in content_blocks if hasattr(b, "text")]
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                messages.append(msg_dict)
            elif content_blocks:
                # Pure text/image content
                if len(content_blocks) == 1 and hasattr(content_blocks[0], "text"):
                    messages.append({"role": msg.role, "content": content_blocks[0].text})
                else:
                    parts = []
                    for block in content_blocks:
                        block_mime = mcp_field(
                            block, "mime_type", "mimeType", _MISSING
                        )
                        if hasattr(block, "text"):
                            parts.append({"type": "text", "text": block.text})
                        elif hasattr(block, "data") and block_mime is not _MISSING:
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{block_mime};base64,{block.data}"},
                            })
                        else:
                            logger.warning(
                                "Unsupported sampling content block type: %s (skipped)",
                                type(block).__name__,
                            )
                    if parts:
                        messages.append({"role": msg.role, "content": parts})

        return messages

    # -- Error helper --------------------------------------------------------

    @staticmethod
    def _error(message: str, code: int = -1):
        """Return ErrorData (MCP spec) or raise as fallback."""
        if _MCP_SAMPLING_TYPES:
            return ErrorData(code=code, message=message)
        raise Exception(message)

    # -- Response building ---------------------------------------------------

    def _build_tool_use_result(self, choice, response):
        """Build a CreateMessageResultWithTools from an LLM tool_calls response."""
        self.metrics["tool_use_count"] += 1

        # Tool loop governance
        if self.max_tool_rounds == 0:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loops disabled for server '{self.server_name}' (max_tool_rounds=0)"
            )

        self._tool_loop_count += 1
        if self._tool_loop_count > self.max_tool_rounds:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loop limit exceeded for server '{self.server_name}' "
                f"(max {self.max_tool_rounds} rounds)"
            )

        content_blocks = []
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "MCP server '%s': malformed tool_calls arguments "
                        "from LLM (wrapping as raw): %.100s",
                        self.server_name, args,
                    )
                    parsed = {"_raw": args}
            else:
                parsed = args if isinstance(args, dict) else {"_raw": str(args)}

            content_blocks.append(ToolUseContent(
                type="tool_use",
                id=tc.id,
                name=tc.function.name,
                input=parsed,
            ))

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s, tool_calls=%d",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
            len(content_blocks),
        )

        return CreateMessageResultWithTools(
            role="assistant",
            content=content_blocks,
            model=response.model,
            stopReason="toolUse",
        )

    def _build_text_result(self, choice, response):
        """Build a CreateMessageResult from a normal text response."""
        self._tool_loop_count = 0  # reset on text response
        response_text = choice.message.content or ""

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
        )

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=_sanitize_error(response_text)),
            model=response.model,
            stopReason=self._STOP_REASON_MAP.get(choice.finish_reason, "endTurn"),
        )

    # -- Session kwargs helper -----------------------------------------------

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for sampling support."""
        return {
            "sampling_callback": self,
            "sampling_capabilities": SamplingCapability(
                tools=SamplingToolsCapability(),
            ),
        }

    # -- Main callback -------------------------------------------------------

    async def __call__(self, context, params):
        """Sampling callback invoked by the MCP SDK.

        Conforms to ``SamplingFnT`` protocol.  Returns
        ``CreateMessageResult``, ``CreateMessageResultWithTools``, or
        ``ErrorData``.
        """
        # Rate limit
        if not self._check_rate_limit():
            logger.warning(
                "MCP server '%s' sampling rate limit exceeded (%d/min)",
                self.server_name, self.max_rpm,
            )
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling rate limit exceeded for server '{self.server_name}' "
                f"({self.max_rpm} requests/minute)"
            )

        # Resolve model
        model = self._resolve_model(
            mcp_field(params, "model_preferences", "modelPreferences")
        )

        # Get auxiliary LLM client via centralized router
        from agent.auxiliary_client import call_llm

        # Model whitelist check (we need to resolve model before calling)
        resolved_model = model or self.model_override or ""

        if self.allowed_models and resolved_model and resolved_model not in self.allowed_models:
            logger.warning(
                "MCP server '%s' requested model '%s' not in allowed_models",
                self.server_name, resolved_model,
            )
            self.metrics["errors"] += 1
            return self._error(
                f"Model '{resolved_model}' not allowed for server "
                f"'{self.server_name}'. Allowed: {', '.join(self.allowed_models)}"
            )

        # Convert messages
        messages = self._convert_messages(params)
        system_prompt = mcp_field(params, "system_prompt", "systemPrompt")
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Build LLM call kwargs
        max_tokens = min(
            mcp_field(params, "max_tokens", "maxTokens", self.max_tokens_cap),
            self.max_tokens_cap,
        )
        call_temperature = None
        if hasattr(params, "temperature") and params.temperature is not None:
            call_temperature = params.temperature

        # Forward server-provided tools
        call_tools = None
        server_tools = getattr(params, "tools", None)
        if server_tools:
            call_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", "") or "",
                        "parameters": _normalize_mcp_input_schema(
                            mcp_field(t, "input_schema", "inputSchema")
                        ),
                    },
                }
                for t in server_tools
            ]

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling request: model=%s, max_tokens=%d, messages=%d",
            self.server_name, resolved_model, max_tokens, len(messages),
        )

        # Offload sync LLM call to thread (non-blocking)
        def _sync_call():
            return call_llm(
                task="mcp",
                model=resolved_model or None,
                messages=messages,
                temperature=call_temperature,
                max_tokens=max_tokens,
                tools=call_tools,
                timeout=self.timeout,
            )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(_sync_call), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call timed out after {self.timeout}s "
                f"for server '{self.server_name}'"
            )
        except Exception as exc:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call failed: {_sanitize_error(_exc_str(exc))}"
            )

        # Guard against empty choices (content filtering, provider errors)
        if not getattr(response, "choices", None):
            self.metrics["errors"] += 1
            return self._error(
                f"LLM returned empty response (no choices) for server "
                f"'{self.server_name}'"
            )

        # Track metrics
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if isinstance(total_tokens, int):
            self.metrics["tokens_used"] += total_tokens

        # Dispatch based on response type
        if (
            choice.finish_reason == "tool_calls"
            and hasattr(choice.message, "tool_calls")
            and choice.message.tool_calls
        ):
            return self._build_tool_use_result(choice, response)

        return self._build_text_result(choice, response)


# ---------------------------------------------------------------------------
# Elicitation handler
# ---------------------------------------------------------------------------

def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """Render a JSON-schema-ish requested_schema to a human-readable field list.

    Elicitation schemas are restricted to a flat object with named top-level
    properties. We surface field names, types, and descriptions so the user
    can tell what the server is asking for before approving.
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."

    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        field_type = ""
        field_desc = ""
        if isinstance(field_spec, dict):
            field_type = str(field_spec.get("type", "") or "")
            field_desc = str(field_spec.get("description", "") or "")
        suffix = f" ({field_type})" if field_type else ""
        if field_desc:
            lines.append(f"  - {field_name}{suffix}: {field_desc}")
        else:
            lines.append(f"  - {field_name}{suffix}")
    return "\n".join(lines)


class ElicitationHandler:
    """Handles ``elicitation/create`` requests for a single MCP server.

    Each ``MCPServerTask`` that has elicitation enabled creates one handler.
    The handler is callable and passed directly to ``ClientSession`` as the
    ``elicitation_callback`` (added in mcp Python SDK 1.11.0).

    Elicitation lets a server ask the client to collect structured input from
    the user mid-tool-call (e.g. payment authorization, OAuth confirmation).
    Form-mode elicitations are routed through Hermes' existing approval
    system (``tools.approval.prompt_dangerous_approval``), which surfaces
    the prompt on whichever surface the active session uses -- CLI, TUI,
    Telegram, Slack, etc. URL-mode elicitations are declined as unsupported.

    Failure modes are fail-closed: any timeout, exception, or unexpected
    state returns ``decline``/``cancel`` rather than silently accepting.
    The server treats this as the user not approving.
    """

    # Outer cap for the approval await. ``prompt_dangerous_approval`` runs
    # its own input() timeout via the approval-config value; this is an
    # asyncio-side safety net so the MCP event loop never blocks
    # indefinitely if the inner timeout machinery is bypassed.
    _OUTER_TIMEOUT_GRACE_SECONDS = 5

    def __init__(self, server_name: str, config: dict, owner: Optional["MCPServerTask"] = None):
        self.server_name = server_name
        # Per-elicitation timeout. Default 5 min mirrors the gateway approval
        # default so users on async surfaces (Telegram, Slack) have time to
        # respond before the server gives up.
        self.timeout = _safe_numeric(config.get("timeout", 300), 300, float)
        # Back-reference to the MCPServerTask so we can read the agent's
        # captured contextvars snapshot at elicitation time. Optional so
        # the handler stays unit-testable in isolation.
        self.owner = owner
        self.metrics = {
            "requests": 0,
            "accepted": 0,
            "declined": 0,
            "errors": 0,
        }

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for elicitation support."""
        return {"elicitation_callback": self}

    async def __call__(self, context, params):
        """Elicitation callback invoked by the MCP SDK.

        Conforms to ``ElicitationFnT`` protocol. Returns ``ElicitResult``
        or ``ErrorData``.
        """
        self.metrics["requests"] += 1

        # URL-mode elicitations point the user to an external URL for
        # sensitive out-of-band flows (OAuth, payment processing). Honouring
        # them requires opening a browser to that URL and waiting for the
        # server's notifications/elicitation/complete -- out of scope for
        # the initial implementation. Decline cleanly so the server does
        # not hang.
        mode = getattr(params, "mode", "form")
        if mode == "url":
            logger.info(
                "MCP server '%s' requested URL-mode elicitation; "
                "declining (URL-mode elicitation not implemented)",
                self.server_name,
            )
            self.metrics["declined"] += 1
            return ElicitResult(action="decline")

        message = getattr(params, "message", "") or (
            f"MCP server '{self.server_name}' is requesting your approval"
        )
        # The SDK model spells this field ``requestedSchema`` on mcp 1.x (the
        # pinned version) and ``requested_schema`` on 2.0, which renamed model
        # fields to snake_case and kept camelCase only as a serialization
        # alias -- and pydantic aliases do not apply to attribute access. A
        # single-spelling read therefore returns the ``{}`` default on the
        # other generation, and _format_elicitation_schema_summary degrades to
        # its generic "Approval requested by ..." line, so the user is asked to
        # approve without being told which fields the server wants.
        schema = (
            getattr(params, "requestedSchema", None)
            or getattr(params, "requested_schema", None)
            or {}
        )
        description = _format_elicitation_schema_summary(schema, self.server_name)

        logger.info(
            "MCP server '%s' elicitation request: %s",
            self.server_name, _sanitize_error(message)[:200],
        )

        # Lazy import: tools.approval is imported very early during process
        # bootstrap; matching the lazy pattern used by _fire_approval_hook
        # avoids any chance of import-order coupling.
        try:
            from tools.approval import request_elicitation_consent
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error(
                "MCP server '%s' elicitation: approval system unavailable: %s",
                self.server_name, exc,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        # Offload the sync consent flow to a worker thread. Running it
        # inline would freeze the MCP background event loop, blocking every
        # other RPC on this session. request_elicitation_consent() routes
        # itself to the right surface (gateway notify_cb for Telegram /
        # Slack / etc., prompt_dangerous_approval for CLI / TUI) and
        # normalizes the answer to one of accept / decline / cancel.
        #
        # The recv-loop task that fires this callback does NOT inherit
        # the agent's contextvars (HERMES_SESSION_PLATFORM etc.). When
        # the MCP tool wrapper captured the agent's context onto
        # owner._pending_call_context we replay it here via
        # contextvars.Context.run so the gateway-platform detection in
        # request_elicitation_consent picks up the right session.
        captured = getattr(self.owner, "_pending_call_context", None) if self.owner else None

        def _invoke_consent() -> str:
            if captured is None:
                return request_elicitation_consent(
                    message,
                    description,
                    timeout_seconds=int(self.timeout),
                    surface=f"mcp-elicitation/{self.server_name}",
                )
            # Context.run can only execute a context once — copy to allow
            # multiple elicitations within a single tool call.
            return captured.copy().run(
                request_elicitation_consent,
                message,
                description,
                timeout_seconds=int(self.timeout),
                surface=f"mcp-elicitation/{self.server_name}",
            )

        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(_invoke_consent),
                timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "MCP server '%s' elicitation timed out after %ds",
                self.server_name, int(self.timeout),
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        except Exception as exc:
            logger.error(
                "MCP server '%s' elicitation failed: %s",
                self.server_name, exc, exc_info=True,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        if answer == "accept":
            self.metrics["accepted"] += 1
            return ElicitResult(action="accept", content={})
        if answer == "cancel":
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        self.metrics["declined"] += 1
        return ElicitResult(action="decline")


# ---------------------------------------------------------------------------
# Server task -- each MCP server lives in one long-lived asyncio Task
# ---------------------------------------------------------------------------

class _MCPServerTaskLifecycleMixin:
    __slots__ = ()

    def __init__(self, name: str):
            self.name = name
            self.session: Optional[Any] = None
            self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
            self._task: Optional[asyncio.Task] = None
            self._ready = asyncio.Event()
            self._shutdown_event = asyncio.Event()
            # Set by tool handlers on auth failure after manager.handle_401()
            # confirms recovery is viable. When set, _run_http / _run_stdio
            # exit their async-with blocks cleanly (no exception), and the
            # outer run() loop re-enters the transport so the MCP session is
            # rebuilt with fresh credentials.
            self._reconnect_event = asyncio.Event()
            self._tools: list = []
            self._error: Optional[Exception] = None
            self._config: dict = {}
            self._sampling: Optional[SamplingHandler] = None
            self._elicitation: Optional[ElicitationHandler] = None
            self._registered_tool_names: list[str] = []
            self._reconnect_retries: int = 0
            # Rapid-drop budget (#62212): a freshly (re)established session is
            # UNPROVEN until it demonstrates real health — it survived at least
            # one full keepalive interval (keepalive success path) or served at
            # least one successful tool call. Only a proven session clears the
            # reconnect budget; a transport that flaps right after the handshake
            # keeps getting charged and still reaches the park instead of
            # hot-cycling respawns forever.
            self._session_proven: bool = False
            # Set once tools have ever been registered and never cleared again,
            # unlike ``_ready`` (which is cleared on every reconnect cycle). Used
            # to tell a genuine first-connection failure from a later reconnect
            # failure that merely happens to occur while ``_ready`` is
            # momentarily clear — see the ``initial_retries`` ladder in run().
            self._ever_connected: bool = False
            # True while parked (reconnect budget exhausted) or after a park,
            # until the session proves healthy again — used to log the
            # parked→revived transition exactly once.
            self._was_parked: bool = False
            # In-flight RPC bookkeeping (#48069 salvage): user-visible requests
            # registered while running so a reconnect/shutdown teardown can fail
            # them fast instead of orphaning them on a dying transport.
            self._inflight_tasks: set = set()
            # True while a deliberate teardown is failing in-flight calls — lets
            # _track_inflight_rpc convert the cancel into a retryable error.
            self._reconnecting: bool = False
            # SuspectableBackend state (#81051/#77765/#84132): latched by races
            # (teardown-vs-keepalive, auth-lock corruption); verified lazily by
            # ensure_healthy() before the next call reuses the connection.
            self._suspect_reason: Optional[str] = None
            # Set when a teardown failed >=1 in-flight call: the following
            # reconnect is a RACE RECOVERY, not a transport failure, and must not
            # charge the rapid-drop budget (a single race must never reach park).
            self._teardown_race: bool = False
            # One-time grace: an auth/permanent-classified failure on a previously
            # PROVEN session gets one suspect+reconnect cycle before the park
            # ladder applies (single auth-lock corruption must not park).
            self._permanent_grace_used: bool = False
            # PIDs of the stdio subprocess spawned for the current transport
            # (captured in _run_stdio). Used to fail in-flight calls FAST when
            # the child dies instead of waiting out the full tool timeout
            # (#81995).
            self._stdio_child_pids: Set[int] = set()
            self._auth_type: str = ""
            self._refresh_lock = asyncio.Lock()
            # MCP stdio sessions are a single JSON-RPC stream. Some servers emit
            # list_changed notifications during startup; if the notification
            # handler calls list_tools while a normal tool call is in flight, the
            # stream can wedge and the user-visible tool call times out. Serialize
            # client-initiated RPCs per server. The lock is also applied to HTTP
            # transports for conservative per-server ordering.
            self._rpc_lock = asyncio.Lock()
            self._pending_refresh_tasks: set[asyncio.Task] = set()
            # contextvars snapshot of the agent task that's currently in
            # session.call_tool(). The MCP recv loop dispatches incoming
            # elicitation/create requests on a SEPARATE asyncio task whose
            # context doesn't inherit HERMES_SESSION_PLATFORM, so the
            # elicitation handler has no way to detect the gateway session
            # that triggered the call. Capturing the agent's context here
            # and replaying it inside the elicitation callback restores
            # gateway-platform attribution and routes the approval prompt
            # to the right surface (Telegram, Slack, etc.).
            self._pending_call_context: Optional[contextvars.Context] = None
            now = time.monotonic()
            self._lifecycle_started_at: float = now
            self._last_tool_call_at: float = now
            self._idle_timeout_seconds: Optional[float] = None
            self._max_lifetime_seconds: Optional[float] = None
            self._recycled_reason: Optional[str] = None
            # Captures the ``InitializeResult`` returned by
            # ``await session.initialize()`` so downstream code can inspect the
            # server's real advertised capabilities (``.capabilities.resources``,
            # ``.capabilities.prompts``) instead of assuming every ``ClientSession``
            # method attribute corresponds to a supported server method. See #18051.
            self.initialize_result: Optional[Any] = None
            # SEP-2549 cache hints from the last tools/list (ttl_ms, cache_scope).
            self._list_cache_meta: dict = {}
            # Set True the first time a keepalive ``ping`` returns JSON-RPC
            # -32601 (method not found): the server is tool-capable but doesn't
            # implement the optional ``ping`` utility. Subsequent keepalives fall
            # back to ``list_tools`` (the pre-ping probe) so we neither spam pings
            # nor reconnect-loop. Reset on each fresh transport connection.
            self._ping_unsupported: bool = False

    def _is_http(self) -> bool:
            """Check if this server uses HTTP transport."""
            return "url" in self._config

    def _advertises_tools(self) -> bool:
            """Whether the server advertises the ``tools`` capability.

            Per the MCP spec, ``InitializeResult.capabilities.tools`` is non-None
            iff the server implements the ``tools/*`` request family. Prompt-only
            or resource-only servers omit it, and calling ``tools/list`` against
            them raises ``MCPError(-32601 Method not found)`` — which previously
            killed the connection during discovery and made every keepalive fail.
            (Ported from anomalyco/opencode#31271.)

            Returns True when no capability info was captured (legacy fallback:
            preserve the old always-call-list_tools behavior rather than regress
            any server that was working before this gate).
            """
            init_result = self.initialize_result
            caps = getattr(init_result, "capabilities", None) if init_result is not None else None
            if caps is None:
                return True
            return getattr(caps, "tools", None) is not None

    async def _negotiate_session(self, session, connect_timeout: float):
            """Negotiate the protocol era with the server and return its result.

            MCP 2026-07-28 replaced the ``initialize``/``initialized`` handshake
            with a stateless core: every request is self-describing and clients
            MAY probe ``server/discover`` up front (SEP-2575). The SDK exposes
            both paths on ``ClientSession`` (``initialize()`` / ``discover()``)
            and ``adopt()``s whichever result installs the outbound stamp, so
            the rest of this file is era-agnostic.

            Per-server ``protocol`` config key:

            - ``auto`` (default): try the legacy handshake FIRST, and fall back
              to ``server/discover`` when the server signals it is modern-only
              (``UnsupportedProtocolVersion`` -32022, or ``initialize`` missing
              -32601). This is the reverse of the SDK's own discover-first auto
              mode, on purpose: nearly every configured/catalog server today
              speaks the handshake era, and initialize-first means ZERO extra
              round-trips and zero behavior change for all of them, while
              stateless-only servers still connect via the fallback.
            - ``stateless``: probe ``server/discover`` first (one legacy retry
              on MCPError, so a handshake-only server still connects).
            - ``legacy``: handshake only, no fallback (escape hatch for servers
              that misbehave on unknown methods).

            Both result types expose ``.capabilities``, so downstream gates
            (``_advertises_tools``, ``_select_utility_schemas``, the config
            probe) work unchanged on either.
            """
            mode = str((self._config or {}).get("protocol", "auto")).lower().strip()
            if mode in ("stateless", "modern", "2026-07-28"):
                try:
                    return await asyncio.wait_for(
                        session.discover(), timeout=connect_timeout
                    )
                except asyncio.TimeoutError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.info(
                        "MCP server '%s': server/discover rejected (%s) despite "
                        "protocol=%s — falling back to the legacy handshake",
                        self.name, exc, mode,
                    )
                    return await asyncio.wait_for(
                        session.initialize(), timeout=connect_timeout
                    )
            if mode in ("legacy", "handshake"):
                return await asyncio.wait_for(
                    session.initialize(), timeout=connect_timeout
                )
            if mode != "auto":
                logger.warning(
                    "MCP server '%s': unknown protocol=%r — treating as 'auto' "
                    "(valid: auto, stateless, legacy)", self.name, mode,
                )
            try:
                return await asyncio.wait_for(
                    session.initialize(), timeout=connect_timeout
                )
            except asyncio.TimeoutError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _handshake_rejected_as_modern(exc):
                    raise
                if not hasattr(session, "discover"):
                    # Legacy SDK generation (mcp 1.x) has no server/discover
                    # client — nothing to fall back to.
                    raise
                logger.info(
                    "MCP server '%s': legacy handshake rejected (%s) — "
                    "retrying via server/discover (2026-07-28 stateless server)",
                    self.name, exc,
                )
                return await asyncio.wait_for(
                    session.discover(), timeout=connect_timeout
                )

    def _is_recycled_stdio(self) -> bool:
            """Return True when a stdio server was intentionally recycled."""
            return not self._is_http() and self._recycled_reason is not None

    def mark_tool_call(self) -> None:
            """Record that a user-visible MCP operation is starting."""
            self._last_tool_call_at = time.monotonic()

    def _mark_lifecycle_started(self) -> None:
            now = time.monotonic()
            self._lifecycle_started_at = now
            self._last_tool_call_at = now
            self._recycled_reason = None

    def _stdio_recycle_reason(self, now: Optional[float] = None) -> Optional[str]:
            """Return the stdio recycle reason if idle/age limits have elapsed."""
            if self._is_http() or self._rpc_lock.locked():
                return None
            now = time.monotonic() if now is None else now
            if (
                self._max_lifetime_seconds is not None
                and now - self._lifecycle_started_at >= self._max_lifetime_seconds
            ):
                return "max_lifetime_seconds"
            if (
                self._idle_timeout_seconds is not None
                and now - self._last_tool_call_at >= self._idle_timeout_seconds
            ):
                return "idle_timeout_seconds"
            return None

    def _next_stdio_recycle_deadline(self) -> Optional[float]:
            """Return the next monotonic recycle deadline for stdio, if any."""
            if self._is_http() or self._rpc_lock.locked():
                return None
            deadlines = []
            if self._max_lifetime_seconds is not None:
                deadlines.append(self._lifecycle_started_at + self._max_lifetime_seconds)
            if self._idle_timeout_seconds is not None:
                deadlines.append(self._last_tool_call_at + self._idle_timeout_seconds)
            return min(deadlines) if deadlines else None

    def _mark_stdio_recycled(self, reason: str) -> None:
            """Mark a stdio session dormant before its transport finishes closing."""
            self._recycled_reason = reason
            self.session = None

    async def _refresh_tools_task(self):
            """Run a dynamic tool refresh and log failures from background tasks."""
            try:
                await self._refresh_tools()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MCP server '%s': dynamic tool refresh failed", self.name)

    def _schedule_tools_refresh(self) -> asyncio.Task:
            """Schedule a background tool refresh and keep it strongly referenced."""
            task = asyncio.create_task(self._refresh_tools_task())
            self._pending_refresh_tasks.add(task)
            task.add_done_callback(self._pending_refresh_tasks.discard)
            return task

    def _make_logging_callback(self):
            """Build a ``logging_callback`` for ``ClientSession``.

            Routes MCP ``notifications/message`` log notifications from the
            server into Hermes' logging (agent.log via hermes_logging), tagged
            with the server name.  Without this, the SDK's default callback
            silently discards them, so server-side warnings/errors during a
            tool call were invisible.  Port of anomalyco/opencode#34529.
            """
            async def _on_log(params):
                try:
                    level = _MCP_LOG_LEVEL_MAP.get(
                        str(getattr(params, "level", "info")).lower(), logging.INFO,
                    )
                    data = getattr(params, "data", None)
                    if not isinstance(data, str):
                        try:
                            data = json.dumps(data, ensure_ascii=False, default=str)
                        except (TypeError, ValueError):
                            data = str(data)
                    # Cap pathological payloads so a chatty/broken server can't
                    # flood agent.log with megabyte lines.
                    if len(data) > 2000:
                        data = data[:2000] + "... [truncated]"
                    logger_name = getattr(params, "logger", None)
                    origin = f"{self.name}/{logger_name}" if logger_name else self.name
                    logger.log(level, "MCP server log [%s]: %s", origin, data)
                except Exception:
                    logger.debug(
                        "Failed to handle MCP log notification from '%s'",
                        self.name, exc_info=True,
                    )
            return _on_log

    def _make_message_handler(self):
            """Build a ``message_handler`` callback for ``ClientSession``.

            Dispatches on notification type.  Only ``ToolListChangedNotification``
            triggers a refresh; prompt and resource change notifications are
            logged as stubs for future work.
            """
            async def _handler(message):
                try:
                    if isinstance(message, Exception):
                        logger.debug("MCP message handler (%s): exception: %s", self.name, message)
                        return
                    if _MCP_NOTIFICATION_TYPES and isinstance(message, ServerNotification):
                        # mcp 2.0 turned ServerNotification from a RootModel into
                        # a plain union of the concrete notification types, so the
                        # payload IS the message instead of living under ``.root``.
                        # ``isinstance`` accepts a union, so the guard above still
                        # holds on both generations; only the unwrap changes.
                        # Without this, ``message.root`` raises AttributeError into
                        # the catch-all below and tools/list_changed refreshes stop
                        # firing silently.
                        match getattr(message, "root", message):
                            case ToolListChangedNotification():
                                logger.info(
                                    "MCP server '%s': received tools/list_changed notification",
                                    self.name,
                                )
                                # Some servers (notably mongodb-mcp-server) emit
                                # tools/list_changed immediately after initialize,
                                # while the client may already be executing another
                                # request. Refreshing synchronously inside the SDK
                                # notification handler can race with that request
                                # and wedge the stdio JSON-RPC stream, making all
                                # subsequent tool calls time out. Do the refresh in
                                # a separate task and let the handler return
                                # promptly.
                                self._schedule_tools_refresh()
                                # Yield one loop tick so tests and short-lived
                                # notification contexts can observe the scheduled
                                # refresh without awaiting the full server RPC.
                                await asyncio.sleep(0)
                            case PromptListChangedNotification():
                                logger.debug("MCP server '%s': prompts/list_changed (ignored)", self.name)
                            case ResourceListChangedNotification():
                                logger.debug("MCP server '%s': resources/list_changed (ignored)", self.name)
                            case _:
                                pass
                except Exception:
                    logger.exception("Error in MCP message handler for '%s'", self.name)
            return _handler

    async def _refresh_tools(self):
            """Re-fetch tools from the server and update the registry.

            Called when the server sends ``notifications/tools/list_changed``.
            The lock prevents overlapping refreshes from rapid-fire notifications.
            After the initial ``await`` (list_tools), all mutations are synchronous
            — atomic from the event loop's perspective.
            """
            from tools.registry import registry

            if not self._advertises_tools():
                # A server that doesn't implement tools/* should never send
                # tools/list_changed, but guard anyway — calling tools/list
                # would raise MCPError(-32601).
                return

            async with self._refresh_lock:
                # Capture old tool names for change diff
                old_tool_names = set(self._registered_tool_names)

                # 1. Fetch current tool list from server (follow nextCursor)
                async with self._rpc_lock:
                    new_mcp_tools = await _paginate_full_list(
                        self.session.list_tools, "tools", self.name
                    )

                # 2. Re-register with fresh tool list. Avoid nuke-and-repave for
                # all names: live agent turns may already have tool-call IDs
                # pointing at existing handler functions. Replacing entries
                # in-place is enough for unchanged names and avoids transient
                # "tool not connected" / stale-handler races during startup
                # notifications. Tools absent from the fresh list are no longer
                # callable, so remove only those stale registry entries first.
                toolset_name = f"mcp-{self.name}"
                stale_tool_names = old_tool_names - {
                    mcp_prefixed_tool_name(self.name, tool.name)
                    for tool in new_mcp_tools
                }
                for tool_name in stale_tool_names:
                    # Never let one server's refresh remove a colliding name that
                    # is currently owned by another server.
                    if registry.get_toolset_for_tool(tool_name) != toolset_name:
                        continue
                    registry.deregister(tool_name)
                    _forget_mcp_tool_server(tool_name)

                # 3. Re-register with the fresh list. The helper may skip names that
                # are ambiguous after normalization.
                self._tools = new_mcp_tools
                registered_names = _register_server_tools(
                    self.name, self, self._config
                )

                # A previously unique raw name can become ambiguous without changing
                # its normalized registry name. In that case the pre-pass above does
                # not consider it stale, so remove any old entry that the final,
                # collision-checked registration set no longer owns.
                registered_name_set = set(registered_names)
                for tool_name in old_tool_names - registered_name_set:
                    if registry.get_toolset_for_tool(tool_name) != toolset_name:
                        continue
                    registry.deregister(tool_name)
                    _forget_mcp_tool_server(tool_name)
                self._registered_tool_names = registered_names

                # 4. Log what changed (user-visible notification)
                new_tool_names = set(self._registered_tool_names)
                added = new_tool_names - old_tool_names
                removed = old_tool_names - new_tool_names
                changes = []
                if added:
                    changes.append(f"added: {', '.join(sorted(added))}")
                if removed:
                    changes.append(f"removed: {', '.join(sorted(removed))}")
                if changes:
                    logger.warning(
                        "MCP server '%s': tools changed dynamically — %s. "
                        "Verify these changes are expected.",
                        self.name, "; ".join(changes),
                    )
                else:
                    logger.info(
                        "MCP server '%s': dynamically refreshed %d tool(s) (no changes)",
                        self.name, len(self._registered_tool_names),
                    )

    async def _keepalive_probe(self) -> None:
            """Exercise the session to detect a stale/expired connection.

            Uses ``ping`` (cheap, transport-agnostic liveness) by default. ``ping``
            is an OPTIONAL MCP utility: a server that doesn't implement it answers
            JSON-RPC -32601. The first time that happens we latch
            ``_ping_unsupported`` and fall back to the pre-ping probe — capability
            permitting, ``list_tools``; otherwise ``ping`` is the only option and
            the -32601 propagates (a server advertising neither a working ping nor
            tools has no liveness primitive left). The latch resets on each fresh
            transport connection so a server that gains ping support after a
            reconnect is re-probed with the cheap path.

            Raises on a genuine connection failure so the caller triggers a
            reconnect; returns normally when the session is alive.
            """
            if not self._ping_unsupported:
                try:
                    await asyncio.wait_for(self.session.send_ping(), timeout=30.0)
                    return
                except Exception as exc:
                    # Only a "method not found" means ping is unsupported. Any
                    # other error (timeout, closed transport, session expired) is
                    # a real liveness failure — propagate so we reconnect.
                    if not _is_method_not_found_error(exc):
                        raise
                    if not self._advertises_tools():
                        # No ping, no tools → no cheaper probe to fall back to.
                        raise
                    self._ping_unsupported = True
                    logger.info(
                        "MCP server '%s': does not implement the optional 'ping' "
                        "utility (-32601); using 'list_tools' for keepalive on "
                        "this connection.",
                        self.name,
                    )

            # Fallback probe for servers without ping support.
            await asyncio.wait_for(self.session.list_tools(), timeout=30.0)

    def _mark_session_proven(self) -> None:
            """Record that the current session demonstrated real health.

            Called from the keepalive success path (session survived at least one
            full keepalive interval) and the tool-call success path. Only then is
            the reconnect budget cleared: a handshake that completes but drops
            moments later must keep consuming ``_reconnect_retries`` so a flapping
            transport still reaches the park instead of respawning forever
            (#62212 — 6212 spawns in 63h).
            """
            if not self._session_proven:
                self._session_proven = True
                self._reconnect_retries = 0
                if self._was_parked:
                    self._was_parked = False
                    logger.warning(
                        "MCP server '%s': revived — session healthy again after "
                        "parking (state: parked → connected)",
                        self.name,
                    )
                # A session that just proved healthy on a fresh transport clears
                # the one-time permanent-failure grace and any race bookkeeping.
                self._permanent_grace_used = False
                self._teardown_race = False

    def mark_suspect(self, reason: str) -> None:
            """Latch a suspicion about this connection. Cheap — no I/O.

            The NEXT call verifies via :meth:`ensure_healthy` and recycles the
            transport if the probe fails, instead of the connection silently
            staying poisoned until process restart (#81051/#77765/#84132).
            """
            if self._suspect_reason is None and reason:
                logger.warning(
                    "MCP server '%s': connection marked suspect (%s); next call "
                    "will health-check it",
                    self.name, reason,
                )
            self._suspect_reason = reason or None

    async def ensure_healthy(self, timeout: float = 5.0) -> bool:
            """Verify a suspect connection before reuse; recycle if dead.

            Returns True when healthy (suspicion cleared). On failure, requests a
            reconnect, drops the stale session reference so the caller's normal
            no-session path takes over, and returns False. Never raises.
            """
            reason = self._suspect_reason
            if not reason:
                return True
            if self.session is None:
                # Nothing to verify — the reconnect path owns recovery now.
                self._suspect_reason = None
                self._reconnect_event.set()
                return False
            try:
                await asyncio.wait_for(self._keepalive_probe(), timeout=timeout)
            except Exception as exc:
                root = _unwrap_exception_group(exc)
                logger.warning(
                    "MCP server '%s': suspect connection (%s) failed health "
                    "check (%s: %s) — requesting reconnect (state: suspect → "
                    "degraded)",
                    self.name, reason, type(root).__name__, root,
                )
                self._suspect_reason = None
                self.mark_suspect(f"health check failed after {reason}")
                self.session = None
                self._ready.clear()
                self._reconnect_event.set()
                return False
            logger.info(
                "MCP server '%s': suspect connection passed health check "
                "(%s) — clearing suspicion",
                self.name, reason,
            )
            self._suspect_reason = None
            self._mark_session_proven()
            return True

    def _fail_inflight_calls(self, reason: str) -> None:
            """Cancel every in-flight RPC attached to this connection.

            Called from the lifecycle exits (reconnect/shutdown/recycle) BEFORE
            the transport unwinds: the MCP SDK does not always fail pending
            requests when its streams close, so without this an in-flight call
            would wait out the full tool timeout on a dying transport. Cancelling
            at least one task flags the cycle as a teardown race
            (``_teardown_race``) so run() treats the following reconnect as
            recovery rather than charging the rapid-drop budget.
            """
            victims = [t for t in self._inflight_tasks if not t.done()]
            if not victims:
                return
            self._reconnecting = True
            self._teardown_race = True
            self.mark_suspect(f"{reason} tore down {len(victims)} in-flight call(s)")
            for task in victims:
                task.cancel()

    def _stdio_children_dead(self) -> bool:
            """True when every stdio child we spawned has exited.

            Best-effort: only meaningful for stdio transports with captured PIDs;
            returns False (unknown → don't fail fast) otherwise.
            """
            pids = getattr(self, "_stdio_child_pids", None)
            if not pids or self._is_http():
                return False
            try:
                import psutil
            except ImportError:
                return False  # unknown → don't fail fast
            for pid in pids:
                # pid_exists handles Windows without signal-permission noise; a
                # probe failure is unknown, not proof that every child exited.
                try:
                    alive = psutil.pid_exists(pid)
                except Exception:
                    return False  # unknown → don't fail fast
                if alive:
                    return False  # at least one child alive → not all dead
            return True

    async def _watch_stdio_children(self) -> None:
            """Poll child liveness while a stdio RPC is in flight (#81995).

            Resolves when a tracked child dies; the caller then cancels the RPC
            immediately instead of letting it hang for the full tool timeout.
            """
            while True:
                if self._stdio_children_dead():
                    return
                await asyncio.sleep(0.25)

    async def _wait_for_lifecycle_event(self) -> str:
            """Block until either _shutdown_event or _reconnect_event fires.

            Returns:
                "shutdown"  if the server should exit the run loop entirely.
                "reconnect" if the server should tear down the current MCP
                            session and re-enter the transport (fresh OAuth
                            tokens, new session ID, etc.). The reconnect event
                            is cleared before return so the next cycle starts
                            with a fresh signal.
                "recycle"   if a stdio idle/max-lifetime limit elapsed. The
                            current transport is torn down and restarted lazily
                            on the next tool call.

            Shutdown takes precedence if both events are set simultaneously.

            Periodically sends a lightweight keepalive (``ping``, with a
            ``list_tools`` fallback for servers that don't implement the optional
            ping utility — see :meth:`_keepalive_probe`) to prevent TCP/session
            state from going stale during idle periods (#17003). If the keepalive
            fails, triggers a reconnect.

            The cadence is ``keepalive_interval`` from server config (default
            :data:`_DEFAULT_KEEPALIVE_INTERVAL`, floored at
            :data:`_MIN_KEEPALIVE_INTERVAL`). Servers that GC idle sessions on a
            short TTL (e.g. Unreal Engine's editor MCP, ~15s) need an interval
            below that TTL, otherwise every idle tool call lands on an
            already-expired session and pays the full reconnect path.
            """
            # Refresh faster than the server's session TTL. ``ping`` (MCP base
            # protocol liveness) is used rather than ``list_tools`` so the probe
            # stays a few bytes regardless of how many tools the server exposes —
            # a ``list_tools`` keepalive against an 830-tool server would pull
            # ~1 MB every cycle. Tool-list changes still arrive out-of-band via
            # ``notifications/tools/list_changed`` → ``_refresh_tools``.
            keepalive_interval = max(
                _MIN_KEEPALIVE_INTERVAL,
                float(self._config.get("keepalive_interval", _DEFAULT_KEEPALIVE_INTERVAL)),
            )

            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            reconnect_task = asyncio.create_task(self._reconnect_event.wait())
            try:
                while True:
                    recycle_reason = self._stdio_recycle_reason()
                    if recycle_reason is not None:
                        self._mark_stdio_recycled(recycle_reason)
                        return "recycle"

                    timeout = keepalive_interval
                    recycle_deadline = self._next_stdio_recycle_deadline()
                    if recycle_deadline is not None:
                        timeout = max(0.0, min(timeout, recycle_deadline - time.monotonic()))

                    done, _pending = await asyncio.wait(
                        {shutdown_task, reconnect_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if done:
                        break

                    recycle_reason = self._stdio_recycle_reason()
                    if recycle_reason is not None:
                        self._mark_stdio_recycled(recycle_reason)
                        return "recycle"

                    # Timeout — no lifecycle event fired.  Probe the connection
                    # to detect stale/expired sessions — but NEVER while an RPC
                    # is in flight (#48069): the stdio session is a single
                    # JSON-RPC stream and a concurrent ping/list_tools can wedge
                    # the in-flight request. A busy server is provably alive.
                    if self.session:
                        if self._rpc_lock.locked() or any(
                            not t.done() for t in self._inflight_tasks
                        ):
                            continue
                        try:
                            async def _probe_under_lock():
                                async with self._rpc_lock:
                                    await self._keepalive_probe()

                            await _probe_under_lock()
                        except Exception as exc:
                            root = _unwrap_exception_group(exc)
                            logger.warning(
                                "MCP server '%s' keepalive failed, triggering "
                                "reconnect (state: connected → degraded): %s: %s",
                                self.name, type(root).__name__, root,
                            )
                            self.mark_suspect(
                                f"keepalive failed: {type(root).__name__}: {root}"
                            )
                            self._reconnect_event.set()
                            break
                        # Keepalive succeeded — the session survived a full
                        # keepalive interval, which is real proof of health.
                        # Clear the rapid-drop budget (#62212).
                        self._mark_session_proven()
            finally:
                for t in (shutdown_task, reconnect_task):
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

            if self._shutdown_event.is_set():
                self._fail_inflight_calls("shutdown")
                return "shutdown"
            # Deliberate teardown: fail any in-flight RPC NOW so it doesn't ride
            # the dying transport to the full tool timeout (#48069/#81995).
            self._fail_inflight_calls("reconnect")
            self._reconnect_event.clear()
            return "reconnect"
'''

EXPORTED_NAMES = ('_format_connect_error', '_safe_numeric', 'SamplingHandler', '_format_elicitation_schema_summary', 'ElicitationHandler', '_MCPServerTaskLifecycleMixin')
SOURCE_PATH = Path(__file__)

def __getattr__(name: str):
    """Resolve exports when this shard is imported before the facade."""
    if name not in EXPORTED_NAMES:
        raise AttributeError(name)
    # The facade owns executable globals; importing it here avoids duplicating
    # the shard loader and keeps direct shard imports cycle-safe.
    from tools import mcp_tool
    try:
        return getattr(mcp_tool, name)
    except AttributeError:
        raise AttributeError(name) from None


def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
