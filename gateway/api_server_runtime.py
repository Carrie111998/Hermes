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
        self.deadline_seconds = max(1.0, min((deadline_ms or 600_000) / 1000, 3600.0))
        self.loaded_skills: dict[str, str] = {}
        self.pending: dict[str, _PendingTool] = {}
        self.agent_ref: list[Any] = [None]
        self.lock = threading.RLock()
        self.interrupted = threading.Event()

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
        if not name:
            return
        serialized = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if '"error"' in serialized[:200].lower():
            return
        digest = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.lock:
            self.loaded_skills[name] = digest

    def invoke_platform_tool(self, name: str, args: dict[str, Any], call_id: str) -> str:
        if not call_id:
            return json.dumps({"error": {"code": "invalid_tool_request", "message": "tool call id is required"}})
        pending = _PendingTool()
        with self.lock:
            if call_id in self.pending:
                return json.dumps({"error": {"code": "idempotency_conflict", "message": "duplicate active tool call id"}})
            self.pending[call_id] = pending
            required_skill = str(self.definitions[name].get("required_skill") or "")
            digest = self.loaded_skills.get(required_skill, "")
        payload: dict[str, Any] = {"call_id": call_id, "name": name, "arguments": args}
        if required_skill and digest:
            payload["skill"] = {"name": required_skill, "digest": digest}
        self.emit("tool_request", payload)
        if not pending.ready.wait(self.deadline_seconds) or self.interrupted.is_set():
            with self.lock:
                self.pending.pop(call_id, None)
            return json.dumps({"error": {"code": "runtime_deadline_exceeded", "message": "tool result was not delivered"}})
        result = pending.result or {}
        if result.get("ok"):
            return json.dumps(result.get("result"), ensure_ascii=False, separators=(",", ":"))
        return json.dumps({"error": result.get("error") or {"code": "invalid_tool_result", "message": "tool failed"}}, ensure_ascii=False)

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
            if not run_id or not isinstance(messages, list) or not messages:
                raise ValueError("run_id and messages are required")
            instructions = _replacement_system_prompt(system_context)
            schemas = _tool_schemas(definitions)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"error": {"code": "invalid_param", "message": str(exc)}}, status=422)

        history = [{"role": str(item.get("role") or ""), "content": _message_text(item.get("content"))} for item in messages[:-1]]
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return web.json_response({"error": {"code": "invalid_param", "message": "last message must be user"}}, status=422)
        user_message = _message_text(last.get("content"))
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
            native = [tool for tool in (agent.tools or []) if tool.get("function", {}).get("name") in {"skill_view", "skills_list"}]
            agent.tools = native + schemas
            agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
            agent.model = str(body.get("model") or agent.model)
            agent.ephemeral_system_prompt = None
            agent._cached_system_prompt = instructions
            agent._build_system_prompt = lambda _system_message=None: instructions
            session.agent_ref[0] = agent

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
        return web.Response(status=204)
