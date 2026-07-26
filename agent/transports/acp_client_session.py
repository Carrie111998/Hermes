"""Session adapter for ACP client runtime.

Owns one ACP session per Hermes session. Drives ``session/new`` + ``session/prompt``,
consumes streaming ``session/update`` notifications (AgentMessageChunk), handles
server-initiated requests (fs/*, terminal/*, permission — allowed-once by default),
and returns a TurnResult that acp_runtime.run_acp_client_turn() can splice into
the ``messages`` list.

Lifecycle:
    session = ACPClientSession(command="acp-agent", model="some-model")
    session.ensure_started(cwd="/home/x/proj")      # spawns + initialize + session/new
    result = session.run_turn("hello")               # blocks until session/prompt returns
    # result.final_text          → assistant text returned to caller
    # result.projected_messages  → list of {role, content} for messages list
    # result.tool_iterations     → count of tool-shaped update events (skill nudge)
    # result.should_retire       → True if session wedged (timeout, crash)
    session.close()                                  # session/close + subprocess teardown

Threading model: single-threaded from the caller's perspective.
The underlying ACPClient owns its own reader threads but exposes
blocking-with-timeout queues that this adapter polls in a loop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.transports.acp_client import ACPClient, ACPClientError
from agent.transports.acp_session_mapping import ACPSessionBinding, ACPSessionMapper

logger = logging.getLogger(__name__)

# ACP wire method names (from acp.meta)
_METHOD_INITIALIZE = "initialize"
_METHOD_SESSION_NEW = "session/new"
_METHOD_SESSION_PROMPT = "session/prompt"
_METHOD_SESSION_CLOSE = "session/close"
_METHOD_SESSION_CANCEL = "session/cancel"
_METHOD_SESSION_UPDATE = "session/update"
_METHOD_SESSION_SET_CONFIG = "session/set_config_option"

# Server-initiated (client-side) methods we receive
_METHOD_FS_READ = "fs/read_text_file"
_METHOD_FS_WRITE = "fs/write_text_file"
_METHOD_PERMISSION = "session/request_permission"
_METHOD_TERMINAL_CREATE = "terminal/create"
_METHOD_TERMINAL_OUTPUT = "terminal/output"
_METHOD_TERMINAL_RELEASE = "terminal/release"
_METHOD_TERMINAL_WAIT = "terminal/wait_for_exit"
_METHOD_TERMINAL_KILL = "terminal/kill"

# ACP session/update discriminator values for streaming chunks.
# Only agent_message_chunk carries user-facing text — agent_thought_chunk is
# the model's internal reasoning and MUST NOT be merged into the reply (Fix 2).
_UPDATE_AGENT_MESSAGE = "agent_message_chunk"
_UPDATE_AGENT_THOUGHT = "agent_thought_chunk"
_UPDATE_TOOL_CALL = "tool_call_update"
_UPDATE_TOOL_CALL_START = "tool_call"

# Terminal ToolCallStatus values (the ACP Literal is
# "pending" | "in_progress" | "completed" | "failed"). A "tool_call" start
# notification opens a call; a "tool_call_update" carrying one of these
# statuses closes it and emits the assistant+tool message pair.
_TOOL_CALL_TERMINAL_STATUSES = {"completed", "failed"}

# How many trailing stderr lines to show in error messages
_STDERR_TAIL_LINES = 12


@dataclass
class TurnResult:
    """Result of one user->assistant turn through an ACP-compliant agent."""

    final_text: str = ""
    projected_messages: list[dict] = field(default_factory=list)
    tool_iterations: int = 0
    interrupted: bool = False
    error: Optional[str] = None
    # True when the session is wedged (timeout, crash, bad response).
    # The caller should retire and re-create the session on the next turn.
    should_retire: bool = False


def _extract_text_from_update(params: dict) -> str:
    """Extract plain text from an ACP session/update notification params.

    ``session/update`` params carry:
      { "sessionId": "...", "update": { "sessionUpdate": "agent_message_chunk",
                                        "content": { "type": "text", "text": "..." } } }

    Fix 2 -- ONLY extract text from ``agent_message_chunk`` updates. The server
    also emits ``agent_thought_chunk`` for the model's internal reasoning; those
    must NOT be included in the user-facing reply. Keying on the discriminator
    instead of content.type avoids silently leaking future reasoning variants.
    """
    update = params.get("update") or {}
    # Support both camelCase (sessionUpdate) and snake_case (session_update) keys
    # to match whatever the server emits -- mirrors _is_tool_iteration's approach.
    kind = update.get("sessionUpdate") or update.get("session_update") or ""
    if kind != _UPDATE_AGENT_MESSAGE:
        # Intentionally skip agent_thought_chunk and anything else that is not
        # a confirmed user-facing text chunk (YAGNI -- do not whitelist speculatively).
        return ""
    content = update.get("content") or {}
    if isinstance(content, dict) and content.get("type") == "text":
        return content.get("text") or ""
    return ""


def _is_tool_iteration(params: dict) -> bool:
    """Return True if the update represents a tool call completion."""
    update = params.get("update") or {}
    kind = update.get("sessionUpdate") or update.get("session_update") or ""
    return kind in {_UPDATE_TOOL_CALL, _UPDATE_TOOL_CALL_START}


def _stringify_tool_payload(value: Any) -> str:
    """Coerce a tool rawInput/rawOutput value into an OpenAI-shaped string.

    ACP carries rawInput/rawOutput as ``Optional[Any]`` — usually a dict of
    arguments for input, a string for output, but legally any JSON value.
    OpenAI tool_calls.function.arguments and the tool-role message content
    must be strings, so dicts/lists are JSON-encoded (``ensure_ascii=False``
    to keep multilingual tool arguments readable) and any other non-string
    value is stringified. ``None`` -> ``""``. Mirrors the arguments coercion
    in codex_responses_adapter so ACP-projected turns are byte-compatible
    with native/codex turns for DB persistence and runtime switching.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class ACPClientSession:
    """One ACP session per Hermes session, lifetime owned by AIAgent.

    Not thread-safe -- one caller drives it at a time, matching how AIAgent's
    run_conversation() loop is structured today.
    """

    def __init__(
        self,
        *,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        mcp_servers: Optional[list[dict]] = None,
        session_meta: Optional[dict] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        approval_callback: Optional[Callable[..., str]] = None,
        auto_approve_permissions: bool = False,
        client_factory: Optional[Callable[..., ACPClient]] = None,
        session_start_timeout: float = 30.0,
        mapper: Optional[ACPSessionMapper] = None,
        hermes_session_id: str = "",
        provider: str = "",
    ) -> None:
        """
        Args:
            command: ACP agent binary to spawn (e.g. "claude-agent-acp").
            args: Additional arguments to pass to the command.
            env: Extra environment variables for the subprocess.
            model: Model identifier to pin on the ACP session after session/new
                (Fix 1). Sent via ``session/set_config_option`` so the ACP server
                does not default to its own server default model. Only sent
                when set; servers that do not support set_config_option are
                tolerated -- the call is wrapped in a try/except and a warning
                is logged rather than hard-failing the session.
            permission_mode: Permission / edit-approval mode to pin on the
                ACP session after session/new. Sent via
                ``session/set_config_option`` with ``configId="mode"``. Only
                sent when set; servers that do not support set_config_option
                are tolerated (warning logged). Accepted values are
                server-specific (for claude-agent-acp: ``"default"``,
                ``"acceptEdits"``, ``"plan"``, ``"auto"``, ``"dontAsk"``,
                ``"bypassPermissions"``). Like ``model``, a server that
                accepts the call but silently rejects the value raises an
                ``ACPClientError`` so the caller knows the pin did not take.
            mcp_servers: Pre-translated ACP McpServer dicts to forward in
                session/new.  Build with ``_translate_mcp_servers()`` from
                Hermes' mcp_servers config.  Hermes does NOT open these
                connections in-process -- the external ACP agent owns them.
                None or [] → send an empty list (current default behaviour).
            session_meta: Opaque dict forwarded verbatim as ``params["_meta"]``
                in session/new.  The core does NOT inspect or rewrap it --
                callers (a coding-tool plugin, or a future trusted config seam)
                construct whatever vendor-specific ``_meta`` the target ACP
                server expects.  A deep copy is stored so later mutations by
                the caller do not alter the value sent on the wire.  Default
                ``None`` → no ``_meta`` key is included (the server's own
                defaults apply).
            session_start_timeout: Timeout in seconds for the initialize +
                session/new handshake.  Default 30s (was 15s hardcoded).
                Covers npx startup, npm cache checks, and network latency
                to the ACP agent's API endpoint.
            on_delta: Optional callback invoked with each text delta during streaming.
                      Bridges to Hermes' ``_fire_stream_delta`` for live output.
            approval_callback: Optional callback ``(command_label: str,
                description: str, *, allow_permanent: bool) -> str`` invoked when
                the ACP agent sends a ``session/request_permission``.  The return
                value selects the permission outcome:

                ``"once"``    → select allow_once (or cancelled if absent)
                ``"session"`` → prefer allow_always, fall back to allow_once
                ``"always"``  → same as session
                ``"deny"``    → prefer reject_once, fall back to reject_always
                anything else → fail-closed (same as deny)

                Exceptions from the callback are caught and logged; the request
                fails closed (reject path) rather than propagating or wedging.
                Non-string return values (e.g. ``True``, ``42``, objects from
                buggy callbacks) are also treated as fail-closed deny.
            auto_approve_permissions: When True, select the first ``allow_once``
                option without calling the callback (bypass mode).  Falls back to
                ``_pick_allow_option`` which prefers ``allow_once``, then
                ``allow_always``, then returns ``None`` (→ cancelled) when no
                allow-kind option is present.  **Never** selects a reject-kind
                option — bypass mode must not deny.
            client_factory: Inject a custom ACPClient constructor for testing.
            mapper: Optional :class:`ACPSessionMapper` for persisting
                Hermes↔ACP session bindings so a resumed Hermes session can
                reattach to the same ACP session. When ``None`` (default) the
                resume and persistence paths are skipped and behaviour is
                identical to before — existing callers are unaffected.
            hermes_session_id: Hermes session id this ACP session is bound to.
                Required together with ``mapper`` for resume/persistence.
            provider: ACP provider name (e.g. ``"claude"``) identifying which
                binding to resume when a Hermes session drives several ACP
                providers.
        """
        self._command = command
        self._args = list(args or [])
        self._env = env
        self._model = model
        self._permission_mode = permission_mode
        self._mcp_servers: list[dict] = list(mcp_servers or [])
        # Deep copy: insulates the session/new payload from caller mutations
        # after __init__ — including nested dicts (a plugin may reuse a shared
        # config dict across sessions and mutate nested fields between them).
        # deepcopy is safe here because session_meta is expected to be a small
        # JSON-serializable dict, never a large or recursive structure.
        self._session_meta: Optional[dict] = (
            deepcopy(session_meta) if session_meta else None
        )
        self._on_delta = on_delta
        self._approval_callback = approval_callback
        self._auto_approve_permissions = auto_approve_permissions
        self._client_factory = client_factory or ACPClient
        self._session_start_timeout = session_start_timeout
        self._mapper = mapper
        self._hermes_session_id = hermes_session_id
        self._provider = provider

        self._client: Optional[ACPClient] = None
        self._session_id: Optional[str] = None
        self._closed = False
        # In-flight tool calls awaiting a terminal "tool_call_update", keyed
        # by toolCallId. Captured on the "tool_call" start notification and
        # consumed when the matching update arrives with a terminal status,
        # so the full assistant+tool message pair can be projected for DB
        # persistence and runtime switching (ACP -> Native). Cleared at the
        # start of each run_turn() to avoid stale state across turns.
        self._pending_tool_calls: dict[str, dict] = {}

    # ---------- lifecycle ----------

    def ensure_started(self, cwd: Optional[str] = None) -> str:
        """Spawn the subprocess, do the initialize handshake, and start a
        session. Returns the ACP session_id. Idempotent -- repeated calls
        return the same session_id."""
        if self._session_id is not None:
            return self._session_id
        if self._client is None:
            self._client = self._client_factory(
                command=self._command,
                args=self._args,
                env=self._env,
            )
        self._client.initialize(
            client_name="hermes",
            client_version=_get_hermes_version(),
            timeout=self._session_start_timeout,
        )

        # --- Resume attempt (if mapper is configured) ---
        # Before paying for a fresh session/new, try to reattach to a persisted
        # ACP session for this Hermes session. Falls through to session/new when
        # there is no binding, the binding is stale, or the server reports the
        # session gone (-32002 resourceNotFound — marked stale so the next call
        # goes straight to session/new).
        if self._mapper and self._hermes_session_id:
            binding = self._mapper.lookup(self._hermes_session_id, self._provider)
            if binding and binding.status == "active":
                try:
                    self._client.request("session/resume", {
                        "sessionId": binding.acp_session_id,
                        "cwd": cwd or binding.cwd,
                    })
                    self._session_id = binding.acp_session_id
                    # Defensive: confirm subprocess survived the resume
                    if not self._client.is_alive():
                        raise RuntimeError(
                            f"ACP subprocess died after session/resume "
                            f"(session={binding.acp_session_id})"
                        )
                    self._pin_model_and_permission()
                    self._mapper.update_activity(self._hermes_session_id)
                    return self._session_id
                except Exception as exc:
                    if self._is_resource_not_found(exc):
                        self._mapper.mark_stale(self._hermes_session_id)
                    else:
                        raise

        # Build session/new params.  ``_meta`` is an opaque vendor passthrough
        # — the core does not construct any vendor-specific structures.  When
        # the caller supplied a non-empty ``session_meta`` dict, it is forwarded
        # verbatim as ``params["_meta"]``; otherwise the key is omitted and the
        # server uses its own defaults.
        session_new_params: dict = {
            "cwd": cwd or os.getcwd(),
            "mcpServers": self._mcp_servers,
        }
        if self._session_meta:
            session_new_params["_meta"] = self._session_meta
        result = self._client.request(
            _METHOD_SESSION_NEW,
            session_new_params,
            timeout=self._session_start_timeout,
        )
        session_id = result.get("sessionId") or result.get("session_id") or ""
        if not session_id:
            raise ACPClientError(
                code=-32603,
                message=(
                    "ACP session/new returned no sessionId "
                    f"(payload keys: {sorted(result.keys())})"
                ),
            )
        self._session_id = session_id
        logger.info(
            "ACP client session started: id=%s command=%r cwd=%s",
            self._session_id[:8],
            self._command,
            cwd or os.getcwd(),
        )

        # Pin model and permission mode on the fresh session. Raises loud on a
        # value rejection (clearing the session id so the next ensure_started()
        # call retries instead of short-circuiting on the idempotency guard).
        self._pin_model_and_permission()

        # --- Persist binding (if mapper is configured) ---
        # Recorded only after session/new + config pins succeed, so a binding is
        # never persisted for a session that failed to pin its model/mode.
        if self._mapper and self._hermes_session_id:
            self._mapper.bind(ACPSessionBinding(
                hermes_session_id=self._hermes_session_id,
                acp_session_id=self._session_id,
                provider=self._provider or "unknown",
                cwd=cwd or "",
                model=self._model,
                permission_mode=self._permission_mode,
                created_at=time.time(),
                last_active_at=time.time(),
            ))

        return self._session_id

    def _pin_model_and_permission(self) -> None:
        """Send set_config_option to pin model and permission_mode on the
        current session.

        Shared by the resume path and the session/new path so a resumed
        session honors the same configured model/mode as a freshly created
        one. On an ACPClientError value rejection ``self._session_id`` is
        cleared (so the next ensure_started() retries instead of
        short-circuiting on the idempotency guard) and the error re-raises so
        run_turn can surface it without retiring.
        """
        if self._model:
            try:
                self._send_config_option(self._session_id, "model", self._model)
            except ACPClientError:
                self._session_id = None
                raise

        if self._permission_mode:
            try:
                self._send_config_option(
                    self._session_id, "mode", self._permission_mode,
                )
            except ACPClientError:
                self._session_id = None
                raise

    @staticmethod
    def _is_resource_not_found(exc: Exception) -> bool:
        """Check if an ACP error is a -32002 resourceNotFound (session expired/deleted)."""
        # ACP SDK raises exceptions with a 'code' attribute or message containing the code
        code = getattr(exc, "code", None)
        if code == -32002:
            return True
        msg = str(exc).lower()
        return "resource not found" in msg or "-32002" in msg

    def _send_config_option(
        self, session_id: str, config_id: str, value: str,
    ) -> None:
        """Send ``session/set_config_option`` to pin a config value on the ACP
        session, then verify the server honoured the value.

        Currently used for ``config_id`` of:

        * ``"model"`` — the model identifier the ACP agent should use.
        * ``"mode"`` — the permission / edit-approval mode (e.g.
          ``"default"``, ``"acceptEdits"``, ``"plan"``).

        The value is sent as-is.  The server decides which values it accepts;
        consult the server's config options response for the accepted list.

        Verification is REQUIRED for ``"model"`` because a wrong value may
        silently fall back to the server's default model, which may differ in
        cost or capability.  For other config ids, the server's response shape
        varies; this method still verifies when a matching configOption is
        present, but tolerates a generic server that returns no configOptions.

        Two-layer exception strategy:
          • transport/protocol failure (request() raises) → TOLERATE: server may
            not implement set_config_option at all; warn and continue.
          • server supported but value rejected or silently ignored (request()
            succeeds but currentValue != requested) → FAIL LOUD: raise
            ACPClientError so the caller knows the pin didn't take.
        """
        assert self._client is not None

        # Tolerance layer: wraps only the wire call.  Only a -32601
        # method-not-found error indicates the server does not implement
        # set_config_option at all; any other JSON-RPC error code (including
        # -32603 internal error, which Claude ACP raises when the value is
        # rejected) is treated as "server supports the method but refused
        # the value" and fails loud below.
        try:
            result = self._client.request(
                _METHOD_SESSION_SET_CONFIG,
                {
                    "sessionId": session_id,
                    "configId": config_id,
                    "value": value,
                },
                timeout=5,
            )
        except TimeoutError as exc:
            # Transport-level timeout -- server may be slow or stuck.
            # Tolerate: not a value rejection.
            logger.warning(
                "ACP client: session/set_config_option timed out "
                "(configId=%s, value=%r, session=%s): %s -- session "
                "continues with server default",
                config_id,
                value,
                session_id[:8],
                exc,
            )
            return
        except ACPClientError as exc:
            if exc.code == -32601:
                # Method not implemented by this server.  Tolerate.
                logger.warning(
                    "ACP client: session/set_config_option not implemented "
                    "by server (configId=%s, session=%s): %s -- session "
                    "continues with server default",
                    config_id,
                    session_id[:8],
                    exc,
                )
                return
            # Any other error code: the server understood the call but
            # refused the value.  Fail loud so the caller knows the pin
            # did not take.
            raise ACPClientError(
                code=1,  # positive = config rejection (see run_turn)
                message=(
                    f"ACP {config_id} config option rejected by server "
                    f"(code={exc.code}): {exc.message}"
                ),
            )
        except RuntimeError as exc:
            # Other runtime errors from the transport -- ambiguous, but
            # safer to tolerate than to fail every turn on a flaky server.
            logger.warning(
                "ACP client: session/set_config_option transport error "
                "(configId=%s, value=%r, session=%s): %s -- session "
                "continues with server default",
                config_id,
                value,
                session_id[:8],
                exc,
            )
            return

        # Verification layer: the server responded successfully.  Extract the
        # matching configOption's currentValue from the response.  The response
        # shape is:
        #   {"configOptions": [{"id": "model"|"mode"|..., "currentValue": "..."}]}
        config_opts = result.get("configOptions") or []
        opt = next((o for o in config_opts if o.get("id") == config_id), None)

        if opt is None:
            # Server returned a successful response but no matching
            # configOption.  Generic ACP server -- cannot verify; proceed
            # without confirmation.
            logger.warning(
                "ACP client: set_config_option succeeded but response carried "
                "no %r configOption -- cannot verify pin (session=%s)",
                config_id,
                session_id[:8],
            )
            return

        current_value = opt.get("currentValue")
        if current_value == value:
            # Pin confirmed.
            logger.info(
                "ACP client: config %r pinned and verified: %r on session %s",
                config_id,
                value,
                session_id[:8],
            )
            return

        # Server accepted the call but currentValue does not match the requested
        # value.  Continuing would silently run every turn on the server's
        # default.  Report the mismatch so the caller can fix the configured
        # value.
        accepted = [o.get("value") for o in opt.get("options", [])]
        raise ACPClientError(
            code=1,  # positive = config rejection, not a transport crash (see run_turn)
            message=(
                f"ACP config pin rejected: configId={config_id!r} requested "
                f"{value!r} but server currentValue={current_value!r}. "
                f"Continuing would silently run on {current_value!r} (server "
                f"default) instead of the configured value. Set the value to "
                f"one of the server's accepted values: {accepted}."
            ),
        )

    # Backwards-compatible alias. Old callers/tests referenced
    # ``_send_model_config``; keep it as a thin shim so external test suites
    # (e.g. the plugin's pinned deps) do not break before they migrate.
    def _send_model_config(self, session_id: str, model: str) -> None:
        """Deprecated alias for ``_send_config_option(sid, "model", model)``."""
        self._send_config_option(session_id, "model", model)

    def set_config_option(self, config_id: str, value: str) -> None:
        """Live-switch a config option on the existing ACP session.

        Sends ``session/set_config_option`` against the session created by
        :meth:`ensure_started` so the model or permission mode can be changed
        at runtime **without** rebuilding the session. If the session has not
        been started yet, this is a no-op (and a warning is logged) — callers
        that want to set a startup pin should pass ``model`` /
        ``permission_mode`` to :meth:`__init__` instead.

        Parameters
        ----------
        config_id
            One of ``"model"`` or ``"mode"`` (other server-defined ids are
            forwarded verbatim). ``"mode"`` is the permission / edit-approval
            mode.
        value
            The new value (e.g. ``"sonnet"`` or ``"acceptEdits"``).

        Fault policy mirrors the startup pin (see :meth:`_send_config_option`):

        * transport/protocol failure (server does not implement the method,
          timeout) → tolerated, a warning is logged, no exception raised.
        * server accepts the call but rejects the value (currentValue does
          not match) → raises :class:`ACPClientError` so the caller knows the
          switch failed.

        Raises
        ------
        ACPClientError
            If the server accepts the call but refuses the value. Positive
            ``code`` (1) distinguishes this from a transport crash.
        """
        if self._session_id is None or self._client is None:
            logger.warning(
                "ACP client: set_config_option(%r, %r) called before "
                "ensure_started() -- ignoring (no live session)",
                config_id, value,
            )
            return
        self._send_config_option(self._session_id, config_id, value)

    def set_model(self, model: str) -> None:
        """Live-switch the model on the existing session. See
        :meth:`set_config_option`."""
        self.set_config_option("model", model)

    def set_permission_mode(self, mode: str) -> None:
        """Live-switch the permission / edit-approval mode on the existing
        session. See :meth:`set_config_option`."""
        self.set_config_option("mode", mode)

    def close(self) -> None:
        """Send session/close and tear down the subprocess."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None and self._session_id is not None:
            try:
                self._client.request(
                    _METHOD_SESSION_CLOSE,
                    {"sessionId": self._session_id},
                    timeout=5,
                )
            except Exception:
                pass  # best-effort
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._session_id = None

        # Mark any persisted binding stale so the next session for this Hermes
        # session does not try to resume a closed ACP session. Best-effort.
        if self._mapper and self._hermes_session_id:
            try:
                self._mapper.mark_stale(self._hermes_session_id)
            except Exception:
                pass

    def __enter__(self) -> "ACPClientSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- turn ----------

    def run_turn(
        self,
        user_input: Any,
        *,
        cwd: Optional[str] = None,
        turn_timeout: float = 600.0,
        notification_poll_timeout: float = 0.25,
    ) -> TurnResult:
        """Send a user message and block until session/prompt returns.

        Streams session/update notifications to on_delta as they arrive.
        Projects streamed content into projected_messages so memory/skill
        review keep working.

        Returns a TurnResult. Sets should_retire=True on crash/timeout.
        """
        result = TurnResult()
        # Reset in-flight tool-call tracking so a tool call that started but
        # never completed in a previous (interrupted) turn does not leak into
        # this turn's projected history.
        self._pending_tool_calls.clear()
        # Ensure session is open (lazy start on first turn)
        try:
            self.ensure_started(cwd=cwd)
        except ACPClientError as exc:
            result.error = f"ACP client session startup failed: {exc}"
            # Positive error code = config rejection (model pin mismatch, not a
            # session crash).  Do NOT retire: retiring would respawn the session and
            # hit the same mismatch every turn, creating an infinite retry loop.
            # The caller must fix the configured model alias and redeploy.
            result.should_retire = exc.code <= 0
            return result
        except (TimeoutError, RuntimeError) as exc:
            result.error = f"ACP client session startup failed: {exc}"
            result.should_retire = True
            return result

        assert self._client is not None and self._session_id is not None

        user_text = _coerce_user_input(user_input)

        # Build ACP prompt request
        prompt_params = {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": user_text}],
        }

        # --- Inactivity tracking ---
        # ``turn_timeout`` is a *continuous inactivity* limit, NOT a total
        # wall-clock ceiling.  We track ``last_activity`` with time.monotonic()
        # guarded by a lock.  The dynamic wait callback (``_wait_cb``) is
        # invoked by ``ACPClient.request`` on each ``queue.Empty``; it checks
        # the idle age and either returns a positive finite next-timeout or
        # raises RuntimeError to terminate the request.
        _activity_lock = threading.Lock()
        _last_activity = time.monotonic()

        def _touch_activity() -> None:
            nonlocal _last_activity
            with _activity_lock:
                _last_activity = time.monotonic()

        def _idle_age() -> float:
            with _activity_lock:
                return time.monotonic() - _last_activity

        def _wait_cb() -> float:
            idle = _idle_age()
            if idle >= turn_timeout:
                raise RuntimeError(
                    f"ACP session inactive for {idle:.1f}s "
                    f"(limit {turn_timeout}s) -- terminating session/prompt"
                )
            return max(60.0, turn_timeout - idle)

        # The initial queue.get timeout for session/prompt.  Using turn_timeout
        # (instead of a hardcoded 60.0) ensures the first inactivity check
        # happens at the configured interval -- critical when turn_timeout < 60.
        # Guard against non-positive or non-finite values for core-direct-call
        # compatibility (config is validated 1..3600, but be defensive).
        _initial_wait = turn_timeout if (turn_timeout and turn_timeout > 0 and turn_timeout != float("inf")) else 60.0

        # session/prompt is a request that blocks until the agent returns
        # PromptResponse. While waiting, the agent sends session/update
        # notifications which arrive in the _notifications queue.
        # We poll both in a deadline loop.
        text_chunks: list[str] = []

        # Send session/prompt in a background thread so we can drain
        # notifications concurrently. The result arrives via a shared dict.
        _response: dict = {}
        _error: list = []  # [exc] if the request raised

        def _do_request() -> None:
            try:
                r = self._client.request(
                    _METHOD_SESSION_PROMPT,
                    prompt_params,
                    timeout=_initial_wait,
                    wait_cb=_wait_cb,
                )
                _response["result"] = r
                # Final response counts as activity.
                _touch_activity()
            except (ACPClientError, TimeoutError, RuntimeError) as exc:
                _error.append(exc)

        req_thread = threading.Thread(target=_do_request, daemon=True)
        req_thread.start()

        def _process_notification(note: dict) -> bool:
            """Apply a single session/update notification to result + text_chunks.

            Returns True if the notification was a legitimate ``session/update``
            (and thus counts as activity for inactivity tracking), False
            otherwise.  Non-session/update notifications are silently dropped
            and must NOT renew the idle clock -- otherwise a junk notification
            would keep the session alive indefinitely.

            Factored out so the same logic runs during the live drain loop and
            the post-join tail-drain (notifications that arrived between the
            last loop poll and req_thread completion would otherwise be lost).
            """
            if note.get("method") != _METHOD_SESSION_UPDATE:
                return False
            params = note.get("params") or {}
            delta = _extract_text_from_update(params)
            if delta:
                text_chunks.append(delta)
                if self._on_delta is not None:
                    try:
                        self._on_delta(delta)
                    except Exception:
                        logger.debug("on_delta callback raised", exc_info=True)
            if _is_tool_iteration(params):
                self._capture_tool_call_event(params, result)
            return True

        # Drain notifications while waiting for the prompt response.
        # session/prompt blocks for the entire turn; req_thread sends it while
        # this loop concurrently drains session/update chunks.
        # _send_lock on ACPClient ensures the two threads don't interleave
        # writes to the same BufferedWriter (see ACPClient._send).
        #
        # No total wall-clock deadline: inactivity is tracked by _wait_cb
        # inside the request thread.  This loop runs as long as req_thread
        # is alive and the subprocess hasn't crashed.
        while req_thread.is_alive():
            if not (self._client and self._client.is_alive()):
                result.error = self._format_error("ACP agent subprocess exited unexpectedly")
                result.should_retire = True
                break

            # Handle server-initiated requests (fs/*, permission, terminal/*)
            sreq = self._client.take_server_request(timeout=0)
            if sreq is not None:
                # Touch BEFORE handling: permission approval may block while
                # waiting for user input, and the request thread's _wait_cb
                # would otherwise see stale idle time and fire inactivity.
                # The server request itself proves the agent is active.
                _touch_activity()
                self._handle_server_request(sreq)
                continue

            # Drain streaming notifications (session/update)
            note = self._client.take_notification(timeout=notification_poll_timeout)
            if note is None:
                continue
            # Only legitimate session/update notifications renew the idle
            # clock.  Junk/unknown notifications are dropped by
            # _process_notification (returns False) and must NOT touch --
            # otherwise they would keep the session alive indefinitely.
            if _process_notification(note):
                _touch_activity()

        req_thread.join(timeout=2.0)

        # Tail-drain: consume notifications that were parsed by the reader
        # thread between the last loop poll and req_thread completing. These
        # would be silently dropped without this drain -- short responses that
        # fit in the first chunks are the most likely to be affected.
        if self._client is not None:
            while True:
                note = self._client.take_notification(timeout=0)
                if note is None:
                    break
                _process_notification(note)

        if _error:
            exc = _error[0]
            result.error = f"ACP session/prompt failed: {exc}"
            # RuntimeError = inactivity timeout from _wait_cb -> retire.
            # TimeoutError or negative-code ACPClientError -> also retire.
            if isinstance(exc, (TimeoutError, RuntimeError)) or (
                isinstance(exc, ACPClientError) and exc.code < 0
            ):
                result.should_retire = True
            return result

        if "result" not in _response and not result.should_retire:
            # req_thread ended without response and without error -- only
            # reachable if the subprocess died (caught by the break above)
            # or an unexpected join timeout.
            result.error = f"ACP session/prompt ended without response"
            result.should_retire = True
            result.interrupted = True
            return result

        if result.should_retire:
            return result

        # Assemble final text from streamed chunks. If chunks are empty,
        # look for text in the PromptResponse itself (some implementations
        # may put content there instead of streaming).
        prompt_result = _response.get("result") or {}
        assembled = "".join(text_chunks)
        if not assembled:
            # Fallback: look for content in the PromptResponse
            for block in (prompt_result.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    assembled += block.get("text") or ""

        result.final_text = assembled

        # Project into messages so curator/memory/skill review can see the turn.
        if assembled:
            result.projected_messages.append(
                {"role": "assistant", "content": assembled}
            )

        return result

    # ---------- internals ----------

    def _capture_tool_call_event(self, params: dict, result: TurnResult) -> None:
        """Capture one tool-call lifecycle event from a session/update.

        ACP streams a tool call as two notifications on the same id:

        * ``tool_call`` (start) -- opens the call, carries ``title`` (the
          tool name) and ``rawInput``.
        * ``tool_call_update`` (progress) -- closes the call when it reaches
          a terminal ``status`` ("completed"/"failed"), carrying
          ``rawOutput``.

        On start we tick ``tool_iterations`` (once per call, matching the
        codex transport's per-item semantics) and stash the title/rawInput
        keyed by ``toolCallId``. On a terminal update we pop the pending
        entry and append an OpenAI-shaped assistant+tool message pair to
        ``projected_messages``. The pair lands before the final assistant
        text message because notifications arrive in order and the final
        text is projected at the very end of ``run_turn``.

        ACP delivers notifications in order on a single JSON-RPC stream, so
        a terminal update is always preceded by its start. If a terminal
        update has no pending entry (e.g. an unexpected id), the pair is
        skipped rather than fabricated. Entries left dangling by an
        interrupted turn are cleared at the start of the next run_turn.
        """
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or update.get("session_update") or ""
        tool_call_id = update.get("toolCallId") or ""

        if kind == _UPDATE_TOOL_CALL_START:
            # A new tool call is beginning. Count it once here -- not again
            # on the terminal update -- so tool_iterations reflects actual
            # tool calls (acp_runtime uses it for skill-nudge counting).
            result.tool_iterations += 1
            if tool_call_id:
                self._pending_tool_calls[tool_call_id] = {
                    "tool_name": update.get("title") or "",
                    "raw_input": update.get("rawInput"),
                }
            return

        if kind != _UPDATE_TOOL_CALL:
            return

        # tool_call_update: only the terminal statuses close a call. Non-
        # terminal updates (in_progress) refresh transient fields we do not
        # persist, so they are ignored.
        if update.get("status") not in _TOOL_CALL_TERMINAL_STATUSES:
            return

        if not tool_call_id:
            return

        pending = self._pending_tool_calls.pop(tool_call_id, None)
        if not pending:
            # No start seen for this id (start notification dropped or an
            # id we never opened). Do not fabricate a pair -- skip it.
            return

        arguments = _stringify_tool_payload(pending.get("raw_input")) or "{}"
        output = _stringify_tool_payload(update.get("rawOutput"))

        result.projected_messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tool_call_id,
                "function": {
                    "name": pending["tool_name"],
                    "arguments": arguments,
                },
            }],
        })
        result.projected_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": output,
            "tool_name": pending["tool_name"],
        })

    def _handle_server_request(self, req: dict) -> None:
        """Handle server-initiated requests from the ACP agent.

        Permission requests are granted (allow_once) because this transport
        talks to a trusted local ACP agent process that Hermes itself spawned
        -- the user already consented to the agent's capabilities by configuring
        it.  A future policy knob could toggle this per-agent.

        fs/* and terminal/* are declined: Hermes controls those surfaces
        through its own tool executor.
        """
        if self._client is None:
            return
        method = req.get("method", "")
        rid = req.get("id")

        if method == _METHOD_PERMISSION:
            outcome = self._resolve_permission_outcome(req)
            self._client.respond(rid, {"outcome": outcome})
            logger.debug(
                "ACP client: permission request -> outcome=%r optionId=%r",
                outcome.get("outcome"),
                outcome.get("optionId"),
            )
        elif method in {
            _METHOD_FS_READ, _METHOD_FS_WRITE,
            _METHOD_TERMINAL_CREATE, _METHOD_TERMINAL_OUTPUT,
            _METHOD_TERMINAL_RELEASE, _METHOD_TERMINAL_WAIT,
            _METHOD_TERMINAL_KILL,
        }:
            # Decline fs/terminal proxying -- Hermes drives its own tool
            # executor. ACP agents that need fs/terminal ops should spawn
            # their own processes.
            logger.debug("ACP client: declining server request %r (not proxied in v1)", method)
            self._client.respond_error(
                rid,
                code=-32601,
                message=f"Method not supported by Hermes ACP client v1: {method}",
            )
        else:
            logger.warning("ACP client: unknown server request %r", method)
            self._client.respond_error(
                rid,
                code=-32601,
                message=f"Unknown method: {method}",
            )

    # ---------- permission resolution ----------

    def _resolve_permission_outcome(self, req: dict) -> dict:
        """Resolve a ``session/request_permission`` into an ACP outcome dict.

        Returns one of:
          ``{"outcome": "selected", "optionId": "<id>"}``
          ``{"outcome": "cancelled"}``

        Decision priority (see __init__ docstring for the full matrix):
          1. ``auto_approve_permissions=True`` → allow_once, then allow_always,
             or cancelled if no allow-kind option exists (never reject).
          2. ``approval_callback`` present → map its return value to a kind.
          3. Neither → fail-closed (deny path).

        Callback exceptions are caught and logged; the request fails closed
        rather than propagating or wedging the turn.
        """
        params = req.get("params") or {}
        options = params.get("options") or []

        # No options at all — malformed request, cancel to avoid wedging.
        if not options:
            return {"outcome": "cancelled"}

        tool_call = params.get("toolCall") or {}

        # ---- 1. Bypass mode ------------------------------------------------
        if self._auto_approve_permissions:
            chosen = _pick_allow_option(options)
            if chosen is not None:
                return {"outcome": "selected", "optionId": chosen}
            return {"outcome": "cancelled"}

        # ---- 2. Callback mode ---------------------------------------------
        if self._approval_callback is not None:
            return self._resolve_via_callback(tool_call, options)

        # ---- 3. Default: fail-closed --------------------------------------
        return self._deny_outcome(options)

    def _resolve_via_callback(self, tool_call: dict, options: list) -> dict:
        """Invoke the approval callback and map its decision to an ACP outcome.

        The callback signature matches Hermes' standard approval flow:
            ``(command_label: str, description: str, *, allow_permanent: bool) -> str``

        Non-string return values (True, 42, objects) are treated as deny
        (fail-closed) — they never raise or crash the turn.  See the
        ``__init__`` docstring for the full decision matrix.
        """
        title = tool_call.get("title") or ""
        kind = tool_call.get("kind") or ""
        tool_call_id = tool_call.get("toolCallId") or ""

        command_label = title or kind or "tool"

        # Build a safe description that includes context but NOT rawInput
        # (which may contain secrets like API keys).
        desc_parts = []
        if title:
            desc_parts.append(f"Tool: {title}")
        if kind:
            desc_parts.append(f"Kind: {kind}")
        if tool_call_id:
            desc_parts.append(f"Call: {tool_call_id}")
        description = " | ".join(desc_parts) if desc_parts else "ACP permission request"

        try:
            decision = self._approval_callback(
                command_label, description, allow_permanent=False,
                kind=kind,
                tool_call=tool_call,
            )
        except Exception:
            logger.warning(
                "ACP client: approval callback raised -- failing closed",
                exc_info=True,
            )
            return self._deny_outcome(options)

        # Normalize the decision safely.  The callback contract says the
        # return value must be a string, but buggy callbacks may return
        # truthy non-string values (True, 42, list, object).  Calling
        # .strip() on those would raise AttributeError *outside* the try
        # guard above, crashing the turn / wedging the permission.  Fail
        # closed instead: non-str values go straight to the deny path.
        if not isinstance(decision, str):
            # Log only the type name, never the value or repr — a buggy
            # callback object's __repr__ may embed secrets (e.g. tokens,
            # API keys) that must not reach the log stream.
            logger.warning(
                "ACP client: approval callback returned non-string "
                "(type %s) -- failing closed",
                type(decision).__name__,
            )
            return self._deny_outcome(options)

        decision = decision.strip().lower()

        if decision in ("once",):
            return self._select_option(options, ("allow_once",))
        if decision in ("session", "always"):
            return self._select_option(options, ("allow_always", "allow_once"))
        # deny / unknown / None → fail-closed
        return self._deny_outcome(options)

    @staticmethod
    def _select_option(options: list, preferred_kinds: tuple) -> dict:
        """Select the first option whose kind is in ``preferred_kinds``.

        Returns ``{"outcome": "cancelled"}`` if no matching kind is found.
        """
        for kind in preferred_kinds:
            for opt in options:
                if isinstance(opt, dict) and opt.get("kind") == kind:
                    oid = opt.get("optionId")
                    if oid is not None:
                        return {"outcome": "selected", "optionId": oid}
        return {"outcome": "cancelled"}

    @staticmethod
    def _deny_outcome(options: list) -> dict:
        """Build a deny outcome, preferring reject_once over reject_always."""
        return ACPClientSession._select_option(
            options, ("reject_once", "reject_always"),
        )

    def _format_error(self, prefix: str) -> str:
        """Build a user-facing error string, appending stderr tail when available.

        All stderr content is force-redacted via redact_sensitive_text(force=True)
        so secrets that leaked into the agent's stderr never reach the user.
        """
        if self._client is None:
            return prefix
        try:
            tail = self._client.stderr_tail(_STDERR_TAIL_LINES)
        except Exception:
            return prefix
        if not tail:
            return prefix
        joined = "\n".join(line.rstrip() for line in tail if line)
        if not joined.strip():
            return prefix
        # Force redaction: secrets in stderr must never reach user-visible
        # error strings regardless of the global security.redact_secrets pref.
        from agent.redact import redact_sensitive_text
        joined = redact_sensitive_text(joined, force=True)
        return f"{prefix}\nACP agent stderr (last {len(tail)} lines):\n{joined}"


def _translate_mcp_servers(servers: dict) -> list[dict]:
    """Translate Hermes mcp_servers config dict to ACP McpServer wire shapes.

    Hermes config (from config.yaml ``mcp_servers:`` key) is a dict of
    ``{name: server_cfg}`` where server_cfg contains either stdio or HTTP/SSE
    transport fields.  The ACP server expects a typed list with exact shapes
    (probed empirically against claude-agent-acp v0.39).

    Accepted ACP shapes:
      stdio (NO type field):
        {"name": str, "command": str, "args": [str], "env": [{"name": str, "value": str}]}
        env is REQUIRED and must be an array -- use [] when the config has none.
      http:
        {"type": "http", "name": str, "url": str, "headers": [{"name": str, "value": str}]}
        headers is REQUIRED array.
      sse:
        {"type": "sse", "name": str, "url": str, "headers": [{"name": str, "value": str}]}
        headers is REQUIRED array.

    Both env/headers must be [{name, value}] arrays -- dict/object shapes are
    rejected (-32602) by the native server.  They are always emitted ([] when
    empty) so the server never sees a missing required field.

    Hermes-only keys (timeout, connect_timeout, auth, sampling) are dropped;
    ACP does not accept unknown fields.  Values are already ${VAR}-interpolated
    by _load_mcp_config() -- no re-interpolation here.

    Entries with neither ``command`` nor ``url`` are skipped with a warning
    rather than crashing the session setup.
    """
    out = []
    for name, cfg in (servers or {}).items():
        if not isinstance(cfg, dict):
            logger.warning(
                "ACP MCP translate: skipping %r -- config is not a dict (%r)",
                name, type(cfg).__name__,
            )
            continue

        has_command = bool(cfg.get("command"))
        has_url = bool(cfg.get("url"))

        if has_command and has_url:
            # Prefer stdio when both are set, matching the codex translator.
            logger.debug(
                "ACP MCP translate: %r has both command and url -- using stdio", name
            )
            has_url = False

        if has_command:
            # Stdio transport.  env dict -> [{name, value}] array (always present).
            raw_env = cfg.get("env") or {}
            env_array = [{"name": str(k), "value": str(v)} for k, v in raw_env.items()]
            args = [str(a) for a in (cfg.get("args") or [])]
            out.append({
                "name": name,
                "command": str(cfg["command"]),
                "args": args,
                "env": env_array,
                # No "type" field for stdio -- ACP spec requires its absence.
            })
        elif has_url:
            # HTTP or SSE transport.  Hermes uses "transport" key ("sse" hint).
            # headers dict -> [{name, value}] array (always present).
            raw_headers = cfg.get("headers") or {}
            headers_array = [{"name": str(k), "value": str(v)} for k, v in raw_headers.items()]
            # Hermes writes "transport" (not "type") for the sse hint.
            # Honor explicit "type" too for forward-compat.
            transport_hint = cfg.get("transport") or cfg.get("type") or ""
            acp_type = "sse" if transport_hint.lower() == "sse" else "http"
            out.append({
                "type": acp_type,
                "name": name,
                "url": str(cfg["url"]),
                "headers": headers_array,
            })
        else:
            logger.warning(
                "ACP MCP translate: skipping %r -- no 'command' or 'url' field", name
            )
            continue

    return out


def _pick_allow_option(options: list) -> Optional[str]:
    """Return the optionId to grant from a permission request's options list.

    Selection priority (auto-approve / bypass semantics):

    1. First option whose kind is ``allow_once``.
    2. First option whose kind is ``allow_always``.
    3. ``None`` — no allow-kind option found.

    Returns ``None`` when the list is empty or contains no allow-kind option.
    **Never** returns a reject-kind option: bypass mode must not deny
    (the old 'first-any' fallback could silently select ``reject_once`` /
    ``reject_always``, which violates auto-approve semantics).
    """
    # Pass 1: allow_once
    for opt in options:
        if isinstance(opt, dict) and opt.get("kind") == "allow_once":
            oid = opt.get("optionId")
            if oid is not None:
                return oid
    # Pass 2: allow_always
    for opt in options:
        if isinstance(opt, dict) and opt.get("kind") == "allow_always":
            oid = opt.get("optionId")
            if oid is not None:
                return oid
    # No allow-kind option — do NOT fall back to reject
    return None


def _coerce_user_input(user_input: Any) -> str:
    """Collapse Hermes/OpenAI rich content into plain text for ACP session/prompt."""
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        parts: list[str] = []
        for item in user_input:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item_type in {"image", "image_url", "input_image"}:
                parts.append("[image attached]")
        text = "\n\n".join(p for p in parts if p).strip()
        return text or "What do you see in this image?"
    return "" if user_input is None else str(user_input)


def _get_hermes_version() -> str:
    """Best-effort Hermes version string for ACP initialize."""
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:  # pragma: no cover
        return "0.0.0"
