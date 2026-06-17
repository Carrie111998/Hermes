"""Session chat control endpoints for the API server."""

from __future__ import annotations

from gateway.api_server_audit import log_api_decision, request_id_headers
from gateway.api_server_shared import *


class APIServerSessionControlMixin:
    async def _handle_session_chat_stop(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat/stop — stop an active session stream."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err

        active = self._active_session_streams.get(session_id)
        if not active:
            return web.json_response(
                {"object": "hermes.session.chat.stop", "session_id": session_id, "status": "idle"},
                headers=request_id_headers(request),
            )

        prompt_session_key = active.get("prompt_session_key")
        if prompt_session_key:
            try:
                from tools.clarify_gateway import clear_session

                clear_session(str(prompt_session_key))
            except Exception as exc:
                logger.debug("[api_server] session chat stop prompt cleanup failed: %s", exc)

        agent_ref = active.get("agent_ref")
        agent = agent_ref[0] if isinstance(agent_ref, list) and agent_ref else None
        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception as exc:
                logger.debug("[api_server] session chat stop interrupt failed: %s", exc)

        task = active.get("task")
        if task is not None and hasattr(task, "done") and not task.done():
            task.cancel()

        log_api_decision(
            request,
            action="session.chat_stop",
            result="allowed",
            status=200,
            principal_scope=principal_scope,
            session_id=session_id,
            reason="stop_requested",
        )
        return web.json_response(
            {
                "object": "hermes.session.chat.stop",
                "session_id": session_id,
                "run_id": active.get("run_id"),
                "status": "stopping",
            },
            headers=request_id_headers(request),
        )

    async def _handle_session_chat_approval(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat/approval — resolve session approval."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err

        active = self._active_session_streams.get(session_id)
        if not active or not active.get("approval_session_key"):
            return web.json_response(
                _openai_error("Session has no pending approval", code="approval_not_pending"),
                status=409,
                headers=request_id_headers(request),
            )

        body, body_err = await self._read_json_body(request)
        if body_err:
            return body_err
        raw_choice = str(body.get("choice", "")).strip().lower()
        aliases = {"approve": "once", "approved": "once", "allow": "once"}
        choice = aliases.get(raw_choice, raw_choice)
        allowed = {"once", "session", "always", "deny"}
        if choice not in allowed:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
                headers=request_id_headers(request),
            )

        resolve_all = (
            _coerce_request_bool(body.get("all"), default=False)
            or _coerce_request_bool(body.get("resolve_all"), default=False)
        )
        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(
            str(active["approval_session_key"]),
            choice,
            resolve_all=resolve_all,
        )
        if resolved <= 0:
            return web.json_response(
                _openai_error("Session has no pending approval", code="approval_not_pending"),
                status=409,
                headers=request_id_headers(request),
            )

        log_api_decision(
            request,
            action="session.chat_approval",
            result="allowed",
            status=200,
            principal_scope=principal_scope,
            session_id=session_id,
            reason=f"resolved:{choice}",
        )
        return web.json_response(
            {
                "object": "hermes.session.chat.approval_response",
                "session_id": session_id,
                "run_id": active.get("run_id"),
                "choice": choice,
                "resolved": resolved,
            },
            headers=request_id_headers(request),
        )

    async def _handle_session_chat_prompt(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat/prompt — resolve clarify/sudo prompts."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        principal_scope, principal_err = self._parse_principal_scope_headers(request)
        if principal_err is not None:
            return principal_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id, principal_scope, request=request)
        if err:
            return err

        active = self._active_session_streams.get(session_id)
        if not active:
            return web.json_response(
                _openai_error("Session has no pending prompt", code="prompt_not_pending"),
                status=409,
                headers=request_id_headers(request),
            )

        body, body_err = await self._read_json_body(request)
        if body_err:
            return body_err
        request_id = str(body.get("request_id") or "").strip()
        prompt_request_ids = active.get("prompt_request_ids") or set()
        if not request_id or request_id not in prompt_request_ids:
            return web.json_response(
                _openai_error("Prompt request is not pending for this session", code="prompt_not_pending"),
                status=409,
                headers=request_id_headers(request),
            )
        answer = body.get("answer")
        if answer is None:
            answer = body.get("value")
        if answer is None:
            answer = body.get("password")

        from tools.clarify_gateway import resolve_gateway_clarify

        resolved = resolve_gateway_clarify(request_id, "" if answer is None else str(answer))
        if not resolved:
            return web.json_response(
                _openai_error("Prompt request is no longer pending", code="prompt_not_pending"),
                status=409,
                headers=request_id_headers(request),
            )
        if hasattr(prompt_request_ids, "discard"):
            prompt_request_ids.discard(request_id)

        log_api_decision(
            request,
            action="session.chat_prompt",
            result="allowed",
            status=200,
            principal_scope=principal_scope,
            session_id=session_id,
            reason="resolved",
        )
        return web.json_response(
            {
                "object": "hermes.session.chat.prompt_response",
                "session_id": session_id,
                "run_id": active.get("run_id"),
                "request_id": request_id,
                "resolved": True,
            },
            headers=request_id_headers(request),
        )
