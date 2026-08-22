"""Authenticated HTTP surface for the opt-in Codex-first gateway bridge."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from aiohttp import web

from gateway.codex_bridge import request_fingerprint
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


_IDEMPOTENCY_RE = re.compile(r"^[^\r\n\x00]{1,256}$")


def _error(message: str, *, code: str, status: int) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "codex_bridge_error", "code": code}},
        status=status,
    )


def _runner(adapter: Any, request: web.Request) -> Any | None:
    return getattr(adapter, "gateway_runner", None) or request.app.get(
        "gateway_runner"
    )


def _idempotency_key(request: web.Request, body: dict[str, Any]) -> str | None:
    value = str(
        request.headers.get("Idempotency-Key") or body.get("idempotency_key") or ""
    ).strip()
    return value if _IDEMPOTENCY_RE.fullmatch(value) else None


def _bridge_context(adapter: Any, request: web.Request):
    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return None, None, None, auth_error
    runner = _runner(adapter, request)
    if runner is None:
        return None, None, None, _error(
            "Gateway runner is unavailable", code="gateway_unavailable", status=503
        )
    settings = runner._codex_bridge_settings()
    if not settings.enabled or Platform.API_SERVER.value not in settings.allowed_origins:
        return None, None, None, _error(
            "Codex bridge HTTP origin is disabled",
            code="codex_bridge_disabled",
            status=403,
        )
    conversation_id, conversation_error = adapter._parse_session_key_header(request)
    if conversation_error is not None:
        return None, None, None, conversation_error
    if not conversation_id:
        return None, None, None, _error(
            "X-Hermes-Session-Key is required",
            code="conversation_id_required",
            status=400,
        )
    service = runner._ensure_codex_bridge_service(settings)
    return runner, service, conversation_id, None


def _source(conversation_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.API_SERVER,
        chat_id=conversation_id,
        user_id="authenticated-api-key",
        user_name="Authenticated API client",
        chat_type="dm",
    )


def _task_payload(service: Any, task_id: str, response: str | None = None) -> dict:
    mapping = service.store.get_by_job_id(task_id)
    if mapping is None:
        raise LookupError(task_id)
    pending = service.store.get_latest_pending_question(task_id)
    return {
        "object": "hermes.codex_task",
        "task_id": mapping.hermes_job_id,
        "phase": mapping.phase,
        "result": mapping.final_result or response,
        "artifacts": list(mapping.artifacts),
        "prompt_id": pending.prompt_id if pending else None,
        "question": pending.question if pending else None,
        "events": service.store.list_events(task_id),
    }


def _origin_matches(mapping: Any, conversation_id: str) -> bool:
    return (
        mapping.origin.get("type") == Platform.API_SERVER.value
        and mapping.origin.get("conversation_id") == conversation_id
        and mapping.origin.get("user_id") == "authenticated-api-key"
    )


async def start_codex_task(adapter: Any, request: web.Request) -> web.Response:
    runner, service, conversation_id, error = _bridge_context(adapter, request)
    if error is not None:
        return error
    body, body_error = await adapter._read_json_body(request)
    if body_error is not None:
        return body_error
    prompt = str(body.get("input") or "").strip()
    workspace = str(body.get("workspace") or "").strip()
    idempotency_key = _idempotency_key(request, body)
    if not prompt:
        return _error("input is required", code="input_required", status=400)
    if not workspace:
        return _error("workspace is required", code="workspace_required", status=400)
    if not idempotency_key:
        return _error(
            "A valid Idempotency-Key is required",
            code="idempotency_key_required",
            status=400,
        )

    event = MessageEvent(
        text=prompt,
        message_id=idempotency_key,
        source=_source(conversation_id),
        metadata={
            "codex_bridge_request": True,
            "workspace": workspace,
            "idempotency_key": idempotency_key,
        },
    )
    try:
        bridge_request = runner._build_bridge_request(
            event, runner._codex_bridge_settings()
        )
    except ValueError as exc:
        return _error(str(exc), code="codex_bridge_rejected", status=400)
    if bridge_request is None:
        return _error(
            "Request was not accepted by the Codex bridge",
            code="codex_bridge_not_handled",
            status=409,
        )
    mapping = service.store.get_by_idempotency(idempotency_key)
    if mapping is not None and not _origin_matches(mapping, conversation_id):
        return _error(
            "Codex task origin does not match this conversation",
            code="origin_mismatch",
            status=409,
        )
    if (
        mapping is not None
        and mapping.request_fingerprint
        and mapping.request_fingerprint != request_fingerprint(bridge_request.prompt)
    ):
        return _error(
            "Idempotency-Key was already used for a different request",
            code="idempotency_conflict",
            status=409,
        )
    if mapping is not None and mapping.phase in {"needs_user", "done", "failed"}:
        return web.json_response(
            _task_payload(service, mapping.hermes_job_id),
            status=200 if mapping.phase in {"done", "failed"} else 202,
        )

    emitted = []
    background = asyncio.create_task(
        runner._maybe_handle_codex_bridge(event, emitted.append)
    )
    active = getattr(adapter, "_codex_bridge_http_tasks", None)
    if active is None:
        active = set()
        adapter._codex_bridge_http_tasks = active
    active.add(background)

    def _forget(done: asyncio.Task) -> None:
        active.discard(done)
        if not done.cancelled():
            # Retrieve an unexpected exception so aiohttp does not emit an
            # unhandled-task warning. The bridge normally records failures.
            done.exception()

    background.add_done_callback(_forget)
    # Bridge capture/working persistence happens before its first executor await.
    await asyncio.sleep(0)
    mapping = service.store.get_by_idempotency(idempotency_key)
    if mapping is None:
        return _error(
            "Codex task could not be captured",
            code="codex_bridge_capture_failed",
            status=500,
        )
    return web.json_response(_task_payload(service, mapping.hermes_job_id), status=202)


async def get_codex_task(adapter: Any, request: web.Request) -> web.Response:
    _runner_obj, service, _conversation_id, error = _bridge_context(adapter, request)
    if error is not None:
        return error
    task_id = str(request.match_info.get("task_id") or "")
    mapping = service.store.get_by_job_id(task_id)
    if mapping is None:
        return _error("Codex task not found", code="task_not_found", status=404)
    if not _origin_matches(mapping, _conversation_id):
        return _error("Codex task not found", code="task_not_found", status=404)
    try:
        return web.json_response(_task_payload(service, task_id))
    except LookupError:
        return _error("Codex task not found", code="task_not_found", status=404)


async def reply_to_codex_task(adapter: Any, request: web.Request) -> web.Response:
    runner, service, conversation_id, error = _bridge_context(adapter, request)
    if error is not None:
        return error
    body, body_error = await adapter._read_json_body(request)
    if body_error is not None:
        return body_error
    task_id = str(request.match_info.get("task_id") or "")
    mapping = service.store.get_by_job_id(task_id)
    if mapping is None:
        return _error("Codex task not found", code="task_not_found", status=404)
    if not _origin_matches(mapping, conversation_id):
        return _error(
            "Codex task origin does not match this conversation",
            code="origin_mismatch",
            status=409,
        )
    prompt_id = str(body.get("prompt_id") or "").strip()
    answer = str(body.get("answer") or "").strip()
    idempotency_key = _idempotency_key(request, body)
    pending = service.store.get_pending_question(prompt_id) if prompt_id else None
    if pending is None or pending.hermes_job_id != task_id:
        return _error(
            "prompt_id does not belong to this task",
            code="prompt_task_mismatch",
            status=409,
        )
    if not answer:
        return _error("answer is required", code="answer_required", status=400)
    if not idempotency_key:
        return _error(
            "A valid Idempotency-Key is required",
            code="idempotency_key_required",
            status=400,
        )

    event = MessageEvent(
        text=answer,
        message_id=idempotency_key,
        source=_source(conversation_id),
        metadata={
            "codex_bridge_prompt_id": prompt_id,
            "idempotency_key": idempotency_key,
        },
    )
    emitted = []
    result = await runner._maybe_handle_codex_bridge(event, emitted.append)
    if result.response and result.response.startswith("Codex bridge rejected"):
        return _error(
            result.response,
            code="codex_bridge_rejected",
            status=409,
        )
    payload = _task_payload(service, task_id, result.response)
    return web.json_response(
        payload, status=202 if payload["phase"] == "needs_user" else 200
    )
