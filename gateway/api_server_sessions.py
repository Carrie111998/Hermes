"""Extracted API-server adapter methods.

This module is mechanically split from gateway.platforms.api_server.
"""

from __future__ import annotations

import json

from gateway.session_acl import has_principal_scope, scope_fields
from gateway.api_server_audit import log_api_decision, request_id_headers
from gateway.session_scope_store import (
    bind_session_scope,
    can_access_session,
    ensure_scope_schema,
    filter_sessions_for_scope,
    inherit_or_bind_session_scope,
)
from gateway.api_server_shared import *


class APIServerSessionsMixin:
    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """Return a stable, client-safe session representation."""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # Avoid exposing full system prompts/model_config through the client API;
        # callers only need to know whether those snapshots exist.
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    def _get_existing_session_or_404(
        self,
        session_id: str,
        principal_scope: Optional[Dict[str, Any]] = None,
        request: Optional["web.Request"] = None,
    ) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        db = self._ensure_session_db()
        if db is None:
            headers = request_id_headers(request) if request is not None else None
            return None, web.json_response(
                _openai_error("Session database unavailable", code="session_db_unavailable"),
                status=503,
                headers=headers,
            )
        session = db.get_session(session_id)
        if not session or not can_access_session(db, session_id, principal_scope):
            if request is not None:
                log_api_decision(
                    request,
                    action="session.access",
                    result="denied",
                    status=404,
                    principal_scope=principal_scope,
                    session_id=session_id,
                    reason="not_found_or_scope_denied",
                )
            headers = request_id_headers(request) if request is not None else None
            return None, web.json_response(
                _openai_error(f"Session not found: {session_id}", code="session_not_found"),
                status=404,
                headers=headers,
            )
        return session, None

    def _conversation_history_for_session(
        self,
        session_id: str,
        principal_scope: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        db = self._ensure_session_db()
        if db is None:
            return []
        try:
            resolved_id = db.resolve_resume_session_id(session_id)
            if resolved_id != session_id:
                inherit_or_bind_session_scope(
                    db,
                    resolved_id,
                    scope=principal_scope,
                    parent_session_id=session_id,
                )
                if not can_access_session(db, resolved_id, principal_scope):
                    return []
            history = db.get_messages_as_conversation(resolved_id)
            return [
                {key: value for key, value in message.items() if key != "timestamp"}
                for message in history
            ]
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions — list persisted Hermes sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        has_more = False
        if has_principal_scope(principal_scope):
            # 用 api_session_scopes 的 scope 索引直接查该 principal 的 session_ids
            # （按 updated_at DESC 分页），再精确取富数据——替代全表循环扫描 +
            # 逐条 scope 过滤（N+1，大 session 表会超时）。
            ensure_scope_schema(db)
            fields = scope_fields(principal_scope)
            with db._lock:
                scope_rows = db._conn.execute(
                    "SELECT session_id FROM api_session_scopes "
                    "WHERE tenant_id = ? AND workspace_id = ? AND project_id = ? AND user_id = ? "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (
                        fields["tenant_id"],
                        fields["workspace_id"],
                        fields["project_id"],
                        fields["user_id"],
                        limit + 1,
                        offset,
                    ),
                ).fetchall()
            scoped_ids = [row["session_id"] for row in scope_rows]
            has_more = len(scoped_ids) > limit
            page_ids = scoped_ids[:limit]
            sessions = (
                db.list_sessions_rich(
                    source=source,
                    session_ids=page_ids,
                    include_children=include_children,
                    order_by_last_active=True,
                )
                if page_ids
                else []
            )
        else:
            sessions = db.list_sessions_rich(
                source=source,
                limit=limit,
                offset=offset,
                include_children=include_children,
                order_by_last_active=True,
            )
            has_more = len(sessions) == limit
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions — create an empty Hermes session row."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        raw_id = body.get("id") or body.get("session_id")
        session_id = str(raw_id).strip() if raw_id else f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        if not session_id or re.search(r'[\r\n\x00]', session_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if len(session_id) > self._MAX_SESSION_HEADER_LEN:
            return web.json_response(_openai_error("Session ID too long", code="invalid_session_id"), status=400)
        existing = db.get_session(session_id)
        if existing and not can_access_session(db, session_id, principal_scope):
            return web.json_response(_openai_error("Session ID unavailable", code="session_exists"), status=409)
        if existing:
            return web.json_response(_openai_error(f"Session already exists: {session_id}", code="session_exists"), status=409)

        model = body.get("model") or self._model_name
        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        db.create_session(session_id, "api_server", model=str(model) if model else None, system_prompt=system_prompt)
        bind_session_scope(db, session_id, principal_scope)
        log_api_decision(
            request,
            action="session.create",
            result="allowed",
            status=201,
            principal_scope=principal_scope,
            session_id=session_id,
        )
        title = body.get("title")
        if title is not None:
            try:
                db.set_session_title(session_id, str(title))
            except ValueError as exc:
                db.delete_session(session_id)
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        session = db.get_session(session_id) or {"id": session_id, "source": "api_server", "model": model, "title": title}
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session, err = self._get_existing_session_or_404(
            request.match_info["session_id"],
            principal_scope,
            request=request,
        )
        if err:
            return err
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} — update client-safe session metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = self._ensure_session_db()
        if "title" in body:
            try:
                db.set_session_title(session_id, "" if body["title"] is None else str(body["title"]))
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            db.end_session(session_id, str(body["end_reason"]))
        session = db.get_session(session_id) or session
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err
        db = self._ensure_session_db()
        deleted = db.delete_session(session_id)
        return web.json_response({"object": "hermes.session.deleted", "id": session_id, "deleted": bool(deleted)})

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err
        db = self._ensure_session_db()
        resolved_id = db.resolve_resume_session_id(session_id)
        if resolved_id != session_id:
            inherit_or_bind_session_scope(
                db,
                resolved_id,
                scope=principal_scope,
                parent_session_id=session_id,
            )
            if not can_access_session(db, resolved_id, principal_scope):
                return web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        messages = db.get_messages(resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — branch via current SessionDB primitives."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        source, err = self._get_existing_session_or_404(source_id, principal_scope, request=request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = self._ensure_session_db()
        fork_id = str(body.get("id") or body.get("session_id") or f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}").strip()
        if not fork_id or re.search(r'[\r\n\x00]', fork_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        existing = db.get_session(fork_id)
        if existing and not can_access_session(db, fork_id, principal_scope):
            return web.json_response(_openai_error("Session ID unavailable", code="session_exists"), status=409)
        if existing:
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        # Match the CLI /branch semantics: mark the original as branched, then
        # create a child session that carries the transcript forward. This uses
        # SessionDB's native parent_session_id/end_reason visibility model rather
        # than inventing a parallel fork store.
        db.end_session(source_id, "branched")
        db.create_session(
            fork_id,
            "api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=source_id,
        )
        inherit_or_bind_session_scope(
            db,
            fork_id,
            scope=principal_scope,
            parent_session_id=source_id,
        )
        messages = db.get_messages(source_id)
        db.replace_messages(fork_id, messages)
        title = body.get("title")
        if title is None:
            base = source.get("title") or "fork"
            try:
                title = db.get_next_title_in_lineage(base)
            except Exception:
                title = f"{base} fork"
        try:
            db.set_session_title(fork_id, str(title))
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        fork = db.get_session(fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat — one synchronous agent turn."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        log_api_decision(
            request,
            action="session.chat",
            result="started",
            principal_scope=principal_scope,
            session_id=session_id,
        )
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = self._conversation_history_for_session(session_id, principal_scope)
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            principal_scope=principal_scope,
        )
        effective_session_id = result.get("session_id") if isinstance(result, dict) else session_id
        final_response = result.get("final_response", "") if isinstance(result, dict) else ""
        headers = {"X-Hermes-Session-Id": effective_session_id or session_id}
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        log_api_decision(
            request,
            action="session.chat",
            result="completed",
            status=200,
            principal_scope=principal_scope,
            session_id=effective_session_id or session_id,
        )
        return web.json_response(
            {
                "object": "hermes.session.chat.completion",
                "session_id": effective_session_id or session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
            },
            headers=headers,
        )

    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream — SSE wrapper over _run_agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        log_api_decision(
            request,
            action="session.chat_stream",
            result="started",
            principal_scope=principal_scope,
            session_id=session_id,
        )
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        approval_session_key = gateway_session_key or f"api-session:{session_id}:{run_id}"
        prompt_session_key = f"api-session-prompts:{session_id}:{run_id}"
        seq = 0
        tool_completion_meta: Dict[str, List[Dict[str, Any]]] = {}

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_preview(tool_name: str, args: Any) -> str:
            try:
                from agent.display import build_tool_preview

                return build_tool_preview(tool_name, args) or tool_name
            except Exception:
                return tool_name

        def _remember_tool_completion(tool_name: str, payload: Dict[str, Any]) -> None:
            tool_completion_meta.setdefault(tool_name or "tool", []).append(payload)

        def _pop_tool_completion(tool_name: str) -> Dict[str, Any]:
            queue = tool_completion_meta.get(tool_name or "tool") or []
            if not queue:
                return {}
            item = queue.pop(0)
            if not queue:
                tool_completion_meta.pop(tool_name or "tool", None)
            return item

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                is_error = bool(kwargs.get("is_error")) or event_type == "tool.failed"
                result = kwargs.get("result")
                error_text = ""
                if is_error and isinstance(result, str):
                    error_text = result[:500]
                    try:
                        parsed = json.loads(result)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict):
                        reason = str(parsed.get("reason") or parsed.get("error_type") or "")
                        message = str(parsed.get("error") or "")
                        error_text = ": ".join(part for part in (message, reason) if part) or error_text
                if event_type == "tool.started":
                    return
                _remember_tool_completion(tool_name or "tool", {
                    "duration": kwargs.get("duration"),
                    "event_name": "tool.failed" if is_error else "tool.completed",
                    "is_error": is_error,
                    "preview": error_text or preview,
                    "result": result,
                })

        def _tool_start(tool_call_id: str, function_name: str, function_args: Any) -> None:
            if not tool_call_id or str(function_name or "").startswith("_"):
                return
            _enqueue("tool.started", {
                "message_id": message_id,
                "tool_call_id": tool_call_id,
                "tool_name": function_name,
                "preview": _tool_preview(function_name, function_args),
                "args": function_args,
            })

        def _tool_complete(tool_call_id: str, function_name: str, function_args: Any, function_result: Any) -> None:
            if not tool_call_id or str(function_name or "").startswith("_"):
                return
            meta = _pop_tool_completion(function_name or "tool")
            duration = meta.get("duration")
            payload = {
                "message_id": message_id,
                "tool_call_id": tool_call_id,
                "tool_name": function_name,
                "preview": meta.get("preview") or "",
                "args": function_args,
                "is_error": bool(meta.get("is_error")),
                "result": meta.get("result", function_result),
            }
            if isinstance(duration, (int, float)):
                payload["duration"] = duration
                payload["duration_ms"] = int(float(duration) * 1000)
            _enqueue(str(meta.get("event_name") or "tool.completed"), payload)


        def _approval_notify(approval_data: Dict[str, Any]) -> None:
            payload = dict(approval_data or {})
            payload.update({
                "message_id": message_id,
                "run_id": run_id,
                "choices": ["once", "session", "always", "deny"],
            })
            _enqueue("approval.request", payload)

        def _prompt_notify(prompt_data: Dict[str, Any]) -> None:
            payload = dict(prompt_data or {})
            kind = str(payload.pop("kind", "clarify") or "clarify")
            request_id = str(payload.get("request_id") or "")
            active = self._active_session_streams.get(session_id)
            if active is not None and request_id:
                active.setdefault("prompt_request_ids", set()).add(request_id)
            payload.update({"message_id": message_id, "run_id": run_id})
            _enqueue(f"{kind}.request", payload)

        agent_ref: List[Any] = [None]

        async def _run_and_signal() -> None:
            try:
                await queue.put(_event_payload("run.started", {"user_message": {"role": "user", "content": user_message}}))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = self._conversation_history_for_session(session_id, principal_scope)
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    tool_start_callback=_tool_start,
                    tool_complete_callback=_tool_complete,
                    agent_ref=agent_ref,
                    gateway_session_key=gateway_session_key,
                    approval_session_key=approval_session_key,
                    approval_notify_callback=_approval_notify,
                    prompt_session_key=prompt_session_key,
                    prompt_notify_callback=_prompt_notify,
                    principal_scope=principal_scope,
                )
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                effective_session_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
                turn_messages = self._turn_transcript_messages(history, user_message, result) if isinstance(result, dict) else []
                await queue.put(_event_payload("assistant.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "completed": True,
                    "partial": False,
                    "interrupted": False,
                }))
                await queue.put(_event_payload("run.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "completed": True,
                    "messages": turn_messages,
                    "usage": usage,
                }))
                log_api_decision(
                    request,
                    action="session.chat_stream",
                    result="completed",
                    status=200,
                    principal_scope=principal_scope,
                    session_id=effective_session_id,
                )
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                log_api_decision(
                    request,
                    action="session.chat_stream",
                    result="failed",
                    status=500,
                    principal_scope=principal_scope,
                    session_id=session_id,
                    reason=str(exc),
                )
                await queue.put(_event_payload("error", {"message": str(exc)}))
            finally:
                await queue.put(_event_payload("done", {}))
                await queue.put(None)
                current = self._active_session_streams.get(session_id)
                if current and current.get("task") is asyncio.current_task():
                    self._active_session_streams.pop(session_id, None)

        task = asyncio.create_task(_run_and_signal())
        self._active_session_streams[session_id] = {
            "task": task,
            "agent_ref": agent_ref,
            "run_id": run_id,
            "approval_session_key": approval_session_key,
            "prompt_session_key": prompt_session_key,
            "prompt_request_ids": set(),
            "principal_scope": principal_scope,
            "started_at": time.time(),
        }
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
            **request_id_headers(request),
        }
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        last_write = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                last_write = time.monotonic()
        except (asyncio.CancelledError, ConnectionResetError):
            task.cancel()
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
        return response
