"""Run Orchestrator Runtime Driver surface for the API server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from gateway.api_server_shared import AIOHTTP_AVAILABLE, web

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, "RuntimeBridgeSession"] = {}
_SESSIONS_LOCK = threading.RLock()
_REGISTERED_MANAGERS: set[int] = set()
_LOCAL_ACTIVITY_TOOLS = {"skill_view"}
_TERMINAL_PLATFORM_ERROR_CODES = {
    "auth_rejected",
    "configuration_error",
    "cost_budget_exceeded",
    "idempotency_conflict",
    "insufficient_credits",
    "internal_error",
    "invalid_tool_result",
    "model_incompatible",
    "model_not_allowed",
    "provider_unavailable",
    "scope_denied",
    "tool_not_allowed",
    "tool_not_implemented",
    "unsupported_capability",
}


def _pin_run_model(agent: Any, requested_model: Any) -> str:
    """Lock one Runtime run to its requested model with no fallback chain."""
    pinned_model = str(requested_model or getattr(agent, "model", "")).strip()
    if not pinned_model:
        raise ValueError("model is required")

    current_model = str(getattr(agent, "model", "") or "").strip()
    switch_model = getattr(agent, "switch_model", None)
    if pinned_model != current_model and callable(switch_model):
        switch_model(
            pinned_model,
            str(getattr(agent, "provider", "") or ""),
            getattr(agent, "api_key", ""),
            str(getattr(agent, "base_url", "") or ""),
            str(getattr(agent, "api_mode", "") or ""),
        )
    else:
        agent.model = pinned_model

    primary_runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(primary_runtime, dict):
        primary_runtime["model"] = pinned_model
        if "compressor_model" in primary_runtime:
            primary_runtime["compressor_model"] = pinned_model

    agent._run_model_pin = pinned_model
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._fallback_index = 0
    agent._fallback_activated = False
    return pinned_model


def _activity_arguments(tool_name: str, args: Any) -> dict[str, str]:
    if not isinstance(args, dict):
        return {}
    allowed = {"skill_view": ("name", "file_path")}
    return {
        key: str(args[key])
        for key in allowed.get(tool_name, ())
        if isinstance(args.get(key), str) and str(args[key]).strip()
    }


def _activity_failure_message(result: Any) -> str:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return ""
    if not isinstance(parsed, dict) or parsed.get("success") is not False:
        return ""
    message = parsed.get("error")
    if not isinstance(message, str) or not message.strip():
        return "runtime activity failed"
    return message.strip()[:240]


def _skill_scope_error(name: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"Skill '{name}' is not available for this run.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _allowed_skill_names(definitions: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        legacy = str(definition.get("required_skill") or "").strip()
        if legacy:
            names.add(legacy)
        for field_name in ("required_skills", "allowed_skills"):
            values = definition.get(field_name)
            if isinstance(values, list):
                names.update(str(value).strip() for value in values if str(value).strip())
    return names


def _discover_skill_metadata() -> list[dict[str, Any]]:
    from tools.skills_tool import _find_all_skills

    return _find_all_skills()


def _allowed_skills_prompt(allowed_names: set[str]) -> str:
    if not allowed_names:
        return ""
    available = {
        str(item.get("name") or "").strip(): item
        for item in _discover_skill_metadata()
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    missing = sorted(allowed_names - available.keys())
    if missing:
        raise ValueError("allowed skills unavailable: " + ", ".join(missing))
    lines = []
    for name in sorted(allowed_names):
        description = " ".join(str(available[name].get("description") or "").split())
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    return "\n\n<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _tool_schemas(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise ValueError(f"tools[{index}] must be an object")
        name = str(definition.get("name") or "").strip()
        parameters = definition.get("input_schema")
        if not name or name in seen or not isinstance(parameters, dict):
            raise ValueError(f"tools[{index}] has an invalid or duplicate definition")
        seen.add(name)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(definition.get("description") or ""),
                "parameters": parameters,
            },
        })
    return schemas


def _replacement_system_prompt(system_context: Any) -> str:
    if not isinstance(system_context, dict):
        raise ValueError("trusted system_context is required")
    version = str(system_context.get("version") or "").strip()
    mode = str(system_context.get("mode") or "").strip()
    digest = str(system_context.get("digest") or "").strip()
    stable = str(system_context.get("stable") or "").strip()
    turn = str(system_context.get("turn") or "").strip()
    if not version or mode != "replace" or not digest or not stable:
        raise ValueError("trusted replacement system_context is required")
    value = f"{version}\n{mode}\n{stable}\n{turn}"
    expected = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    if digest != expected:
        raise ValueError("system_context digest mismatch")
    return stable + ("\n\n" + turn if turn else "")


def _resume_runtime_history(
    messages: list[dict[str, Any]],
    checkpoint: Any,
    tool_results: Any,
) -> list[dict[str, Any]]:
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("message"), dict):
        raise ValueError("runtime_checkpoint.message is required for tool-result resume")
    assistant = checkpoint["message"]
    calls = assistant.get("tool_calls")
    if assistant.get("role") != "assistant" or not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("runtime checkpoint must contain exactly one platform tool call")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
    tool_name = str(function.get("name") or "") if isinstance(function, dict) else ""
    if not call_id or not tool_name:
        raise ValueError("runtime checkpoint tool call id and name are required")
    if not isinstance(tool_results, list) or len(tool_results) != 1 or not isinstance(tool_results[0], dict):
        raise ValueError("exactly one tool_result is required for runtime resume")
    result = tool_results[0]
    if str(result.get("tool_call_id") or "") != call_id:
        raise ValueError("tool_result does not match runtime checkpoint")
    status = str(result.get("status") or "")
    if status == "succeeded":
        content = result.get("output")
    elif status == "failed" and isinstance(result.get("error"), dict):
        content = {"error": result["error"]}
    else:
        raise ValueError("tool_result status must be succeeded or failed")
    return [
        *messages,
        json.loads(json.dumps(assistant, ensure_ascii=False)),
        {
            "role": "tool",
            "name": tool_name,
            "tool_call_id": call_id,
            "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _runtime_tool_middleware(**kwargs: Any) -> Any:
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
    next_call = kwargs.get("next_call")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        return next_call(args) if callable(next_call) else args
    if tool_name in session.tool_names:
        return session.invoke_platform_tool(
            tool_name,
            args,
            str(kwargs.get("tool_call_id") or ""),
        )
    if tool_name == "skill_view":
        requested = str(args.get("name") or args.get("skill") or "").strip()
        if not session.is_skill_allowed(requested):
            return _skill_scope_error(requested)
    result = next_call(args) if callable(next_call) else args
    if tool_name == "skill_view":
        session.record_loaded_skill(args, result)
    return result


def _ensure_runtime_middleware() -> None:
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager_id = id(manager)
    with _SESSIONS_LOCK:
        if manager_id in _REGISTERED_MANAGERS:
            return
        callbacks = manager._middleware.setdefault("tool_execution", [])
        if _runtime_tool_middleware not in callbacks:
            callbacks.insert(0, _runtime_tool_middleware)
        _REGISTERED_MANAGERS.add(manager_id)


@dataclass
class _PendingTool:
    name: str = ""
    signature_key: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class RuntimeBridgeSession:
    def __init__(
        self,
        run_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict[str, Any] | None]",
        definitions: list[dict[str, Any]],
        deadline_ms: int,
        agent_session_id: str,
    ) -> None:
        self.run_id = run_id
        self.agent_session_id = agent_session_id
        self.loop = loop
        self.queue = queue
        self.definitions = {str(item["name"]): dict(item) for item in definitions}
        self.tool_names = set(self.definitions)
        self.allowed_skill_names = _allowed_skill_names(definitions)
        self.deadline_seconds = max(0.001, deadline_ms / 1000) if deadline_ms > 0 else None
        self.loaded_skills: dict[str, str] = {}
        self.local_activities: dict[str, str] = {}
        self.pending: dict[str, _PendingTool] = {}
        self.non_retryable_failures: dict[str, str] = {}
        self.agent_ref: list[Any] = [None]
        self.lock = threading.RLock()
        self.interrupted = threading.Event()
        self.finished = threading.Event()

    def is_skill_allowed(self, name: str) -> bool:
        return bool(name) and name in self.allowed_skill_names

    @staticmethod
    def _tool_signature_key(name: str, args: dict[str, Any]) -> str:
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return name + ":" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _halt_tool_loop(self, name: str, args: dict[str, Any], code: str, message: str, count: int) -> None:
        agent = self.agent_ref[0]
        setter = getattr(agent, "_set_tool_guardrail_halt", None)
        if not callable(setter):
            return
        from agent.tool_guardrails import ToolCallSignature, ToolGuardrailDecision

        setter(ToolGuardrailDecision(
            action="halt",
            code=code,
            message=message,
            tool_name=name,
            count=count,
            signature=ToolCallSignature.from_call(name, args),
        ))

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"run_id": self.run_id, "type": event_type, "payload": payload}
        try:
            if asyncio.get_running_loop() is self.loop:
                self.queue.put_nowait(event)
                return
        except RuntimeError:
            pass
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def record_loaded_skill(self, args: dict[str, Any], result: Any) -> None:
        name = str(args.get("name") or args.get("skill") or "").strip()
        if not self.is_skill_allowed(name):
            return
        serialized = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if '"error"' in serialized[:200].lower():
            return
        digest = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.lock:
            self.loaded_skills[name] = digest

    def start_local_activity(self, call_id: str, name: str, args: Any) -> None:
        if not call_id or name not in _LOCAL_ACTIVITY_TOOLS:
            return
        with self.lock:
            if call_id in self.local_activities:
                return
            self.local_activities[call_id] = name
        self.emit("activity_started", {
            "call_id": call_id,
            "name": name,
            "arguments": _activity_arguments(name, args),
        })

    def complete_local_activity(self, call_id: str, name: str, result: Any) -> None:
        if not call_id:
            return
        with self.lock:
            started_name = self.local_activities.pop(call_id, "")
        if not started_name or started_name != name:
            return
        message = _activity_failure_message(result)
        payload: dict[str, Any] = {
            "call_id": call_id,
            "name": name,
            "status": "failed" if message else "completed",
        }
        if message:
            payload["error"] = {
                "code": "runtime_activity_failed",
                "message": message,
                "retryable": False,
            }
        self.emit("activity_completed", payload)

    def _checkpoint_message(self, call_id: str, name: str) -> dict[str, Any]:
        agent = self.agent_ref[0]
        candidate = getattr(agent, "_runtime_checkpoint_message", None)
        if not isinstance(candidate, dict):
            db = getattr(agent, "_session_db", None)
            loader = getattr(db, "get_messages_as_conversation", None)
            history = loader(self.agent_session_id) if callable(loader) else []
            candidate = next(
                (
                    message
                    for message in reversed(history)
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(message.get("tool_calls"), list)
                    and any(
                        isinstance(call, dict) and str(call.get("id") or "") == call_id
                        for call in message["tool_calls"]
                    )
                ),
                None,
            )
        calls = candidate.get("tool_calls") if isinstance(candidate, dict) else None
        active_calls = [
            call
            for call in calls or []
            if isinstance(call, dict) and str(call.get("id") or "") == call_id
        ]
        if len(active_calls) != 1:
            raise ValueError("runtime checkpoint must contain the active platform tool call")
        # One model turn may contain a local skill_view call beside a delegated
        # platform call. Only the active
        # platform call belongs in the restart checkpoint: local activity is
        # already complete and is not a resumable side effect.
        candidate = {**candidate, "tool_calls": active_calls}
        checkpoint = _resume_runtime_history(
            [],
            {"message": candidate},
            [{"tool_call_id": call_id, "status": "succeeded", "output": {}}],
        )[0]
        function = checkpoint["tool_calls"][0].get("function") or {}
        if str(function.get("name") or "") != name:
            raise ValueError("runtime checkpoint tool name does not match active call")
        return checkpoint

    def invoke_platform_tool(self, name: str, args: dict[str, Any], call_id: str) -> str:
        if not call_id:
            return json.dumps({"error": {"code": "invalid_tool_request", "message": "tool call id is required"}})
        signature_key = self._tool_signature_key(name, args)
        with self.lock:
            prior_code = self.non_retryable_failures.get(signature_key, "")
        if prior_code:
            message = (
                f"Blocked unchanged retry of {name}: the previous call failed with "
                f"non-retryable error {prior_code}."
            )
            self._halt_tool_loop(name, args, "repeated_non_retryable_tool_call", message, 2)
            return json.dumps({
                "error": {
                    "code": "repeated_non_retryable_tool_call",
                    "message": message,
                    "retryable": False,
                },
            }, ensure_ascii=False, separators=(",", ":"))
        try:
            checkpoint = self._checkpoint_message(call_id, name)
        except ValueError as exc:
            message = str(exc)
            self.emit("error", {"code": "runtime_checkpoint_invalid", "message": message})
            self.interrupt(message)
            return json.dumps({
                "error": {
                    "code": "runtime_checkpoint_invalid",
                    "message": message,
                    "retryable": False,
                },
            }, ensure_ascii=False, separators=(",", ":"))
        pending = _PendingTool(name=name, signature_key=signature_key)
        with self.lock:
            if call_id in self.pending:
                return json.dumps({"error": {"code": "idempotency_conflict", "message": "duplicate active tool call id"}})
            self.pending[call_id] = pending
            definition = self.definitions[name]
            required_skills = definition.get("required_skills")
            if not isinstance(required_skills, list):
                legacy = str(definition.get("required_skill") or "").strip()
                required_skills = [legacy] if legacy else []
            proofs = [
                {"name": skill_name, "digest": self.loaded_skills[skill_name]}
                for value in required_skills
                if (skill_name := str(value).strip()) and skill_name in self.loaded_skills
            ]
        payload: dict[str, Any] = {"call_id": call_id, "name": name, "arguments": args}
        if proofs:
            payload["skills"] = proofs
            payload["skill"] = proofs[0]
        self.emit("checkpoint", {"message": checkpoint})
        self.emit("tool_request", payload)
        if not pending.ready.wait(self.deadline_seconds) or self.interrupted.is_set():
            with self.lock:
                self.pending.pop(call_id, None)
            code = "runtime_deadline_exceeded" if self.deadline_seconds is not None else "run_interrupted"
            message = "explicit tool-result deadline exceeded" if self.deadline_seconds is not None else "run was interrupted"
            return json.dumps({"error": {"code": code, "message": message, "retryable": False}})
        result = pending.result or {}
        if result.get("ok"):
            return json.dumps(result.get("result"), ensure_ascii=False, separators=(",", ":"))
        error = result.get("error") if isinstance(result.get("error"), dict) else {
            "code": "invalid_tool_result",
            "message": "tool failed",
            "retryable": False,
        }
        code = str(error.get("code") or "invalid_tool_result")
        if error.get("retryable") is False and code != "domain_gate_required":
            with self.lock:
                self.non_retryable_failures[signature_key] = code
            if code in _TERMINAL_PLATFORM_ERROR_CODES:
                message = str(error.get("message") or f"{name} failed with {code}")
                self._halt_tool_loop(name, args, "terminal_platform_error", message, 1)
        return json.dumps({"error": error}, ensure_ascii=False, separators=(",", ":"))

    def submit_result(self, result: dict[str, Any]) -> bool:
        call_id = str(result.get("call_id") or "")
        with self.lock:
            pending = self.pending.pop(call_id, None)
            if pending is None:
                return False
            pending.result = result
            pending.ready.set()
        return True

    def interrupt(self, reason: str) -> None:
        self.interrupted.set()
        agent = self.agent_ref[0]
        if agent is not None:
            agent.interrupt(reason)
        with self.lock:
            for pending in self.pending.values():
                pending.ready.set()


class APIServerRuntimeMixin:
    async def _handle_runtime_run(self, request: "web.Request") -> "web.StreamResponse":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        try:
            body = await request.json()
            run_id = str(body.get("run_id") or "").strip()
            messages = body.get("messages")
            system_context = body.get("system_context")
            definitions = body.get("tools") or []
            tool_results = body.get("tool_results") or []
            runtime_checkpoint = body.get("runtime_checkpoint")
            if not run_id or not isinstance(messages, list) or not messages:
                raise ValueError("run_id and messages are required")
            resuming = bool(tool_results or runtime_checkpoint)
            if resuming and (not tool_results or runtime_checkpoint is None):
                raise ValueError("runtime_checkpoint and tool_results are both required for resume")
            allowed_skill_names = _allowed_skill_names(definitions)
            instructions = _replacement_system_prompt(system_context) + _allowed_skills_prompt(allowed_skill_names)
            schemas = _tool_schemas(definitions)
            normalized_messages = [
                {"role": str(item.get("role") or ""), "content": _message_text(item.get("content"))}
                for item in messages
                if isinstance(item, dict)
            ]
            if len(normalized_messages) != len(messages):
                raise ValueError("messages must contain only objects")
            if resuming:
                history = _resume_runtime_history(normalized_messages, runtime_checkpoint, tool_results)
                user_message = ""
            else:
                history = normalized_messages[:-1]
                last = normalized_messages[-1]
                if last.get("role") != "user":
                    raise ValueError("last message must be user")
                user_message = _message_text(last.get("content"))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"error": {"code": "invalid_param", "message": str(exc)}}, status=422)
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        queue: "asyncio.Queue[dict[str, Any] | None]" = asyncio.Queue()
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        agent_session_id = str(context.get("session_id") or run_id).strip()
        session = RuntimeBridgeSession(
            run_id,
            asyncio.get_running_loop(),
            queue,
            definitions,
            int(body.get("deadline_ms") or 0),
            agent_session_id,
        )
        _ensure_runtime_middleware()
        with _SESSIONS_LOCK:
            if run_id in _SESSIONS or agent_session_id in _SESSIONS:
                await response.write(json.dumps({"run_id": run_id, "type": "error", "payload": {"code": "run_state_conflict", "message": "run already active"}}).encode() + b"\n")
                return response
            _SESSIONS[run_id] = session
            _SESSIONS[agent_session_id] = session

        def configure_agent(agent: Any) -> None:
            native = [
                tool
                for tool in (agent.tools or [])
                if tool.get("function", {}).get("name") == "skill_view"
            ]
            agent.tools = native + schemas
            agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
            _pin_run_model(agent, body.get("model"))
            agent.ephemeral_system_prompt = None
            agent._cached_system_prompt = instructions
            agent._build_system_prompt = lambda _system_message=None: instructions
            agent._resume_from_tool_results = resuming
            session.agent_ref[0] = agent

        def on_tool_start(tool_call_id: str, function_name: str, function_args: Any) -> None:
            session.start_local_activity(tool_call_id, function_name, function_args)

        def on_tool_complete(
            tool_call_id: str,
            function_name: str,
            _function_args: Any,
            function_result: Any,
        ) -> None:
            session.complete_local_activity(tool_call_id, function_name, function_result)

        async def pump() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    return
                await response.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

        pump_task = asyncio.create_task(pump())
        session.emit("run_started", {
            "runtime": "hermes",
            "system_context_version": system_context["version"],
            "system_context_mode": system_context["mode"],
            "system_context_digest": system_context["digest"],
        })
        try:
            result, usage = await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=None,
                session_id=agent_session_id,
                stream_delta_callback=lambda delta: session.emit("text_delta", {"delta": delta}) if delta else None,
                tool_start_callback=on_tool_start,
                tool_complete_callback=on_tool_complete,
                agent_ref=session.agent_ref,
                agent_configurator=configure_agent,
            )
            text = str((result or {}).get("final_response") or "")
            session.emit("usage", usage or {})
            session.emit("completed", {"finish_reason": "stop", "text": text})
        except Exception as exc:
            logger.exception("Run Orchestrator runtime run failed: %s", run_id)
            session.emit("error", {"code": "runtime_unavailable", "message": str(exc)})
        finally:
            with _SESSIONS_LOCK:
                _SESSIONS.pop(run_id, None)
                _SESSIONS.pop(agent_session_id, None)
            session.finished.set()
            queue.put_nowait(None)
            await pump_task
        return response

    async def _handle_runtime_tool_result(self, request: "web.Request") -> "web.Response":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        run_id = request.match_info["run_id"]
        with _SESSIONS_LOCK:
            session = _SESSIONS.get(run_id)
        if session is None:
            return web.json_response({"error": {"code": "run_not_found", "message": "run is not active"}}, status=404)
        try:
            result = await request.json()
        except Exception:
            return web.json_response({"error": {"code": "invalid_param", "message": "invalid JSON"}}, status=400)
        if not isinstance(result, dict) or not session.submit_result(result):
            return web.json_response({"error": {"code": "invalid_tool_result", "message": "unknown call_id"}}, status=409)
        return web.Response(status=204)

    async def _handle_runtime_interrupt(self, request: "web.Request") -> "web.Response":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        run_id = request.match_info["run_id"]
        with _SESSIONS_LOCK:
            session = _SESSIONS.get(run_id)
        if session is None:
            return web.json_response({"error": {"code": "run_not_found", "message": "run is not active"}}, status=404)
        body = await request.json()
        session.interrupt(str(body.get("reason") or "interrupted by orchestrator"))
        finished = await asyncio.to_thread(session.finished.wait, 10)
        if not finished:
            return web.json_response(
                {"error": {"code": "interrupt_timeout", "message": "runtime session did not stop"}},
                status=503,
            )
        return web.Response(status=204)
