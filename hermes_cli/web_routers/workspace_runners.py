"""Outbound WebSocket enrollment, command dispatch, and presence for workspace runners."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hermes_cli.runner_protocol import RunnerCommand, sign_envelope, verify_envelope
from hermes_cli.workspace_runner_registry import WorkspaceRunnerRegistry
from hermes_constants import get_hermes_home

router = APIRouter()
_registries: dict[str, WorkspaceRunnerRegistry] = {}


@dataclass(slots=True)
class _RunnerConnection:
    command_key: bytes
    send_lock: asyncio.Lock
    websocket: WebSocket


_connections: dict[str, _RunnerConnection] = {}
_waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
_background_tasks: set[asyncio.Task[None]] = set()


class RunnerEnrollmentBody(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class RemoteRunnerCommandBody(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    command_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_head: str | None = Field(default=None, max_length=128)
    method: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    run_id: str = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(default=60, ge=1, le=300)


class ReconcileRunnerCommandBody(BaseModel):
    decision: Literal["abandon", "retry"]


def _registry() -> WorkspaceRunnerRegistry:
    home = get_hermes_home().expanduser().resolve()
    key = str(home)
    registry = _registries.get(key)
    if registry is None:
        registry = WorkspaceRunnerRegistry(
            home / "workspace-control.db",
            master_key_path=home / ".workspace-control-master-key",
        )
        _registries[key] = registry
    return registry


def get_workspace_runner_registry() -> WorkspaceRunnerRegistry:
    """Return the current profile's durable runner registry."""
    return _registry()


def reset_workspace_runner_state_for_tests() -> None:
    for task in _background_tasks:
        task.cancel()
    _background_tasks.clear()
    for registry in _registries.values():
        registry.close()
    _registries.clear()
    _connections.clear()
    for future in _waiters.values():
        if not future.done():
            future.cancel()
    _waiters.clear()


def _transport_allowed(websocket: WebSocket) -> bool:
    if websocket.url.scheme == "wss":
        return True
    peer = websocket.client.host if websocket.client else ""
    if peer == "testclient":
        return True
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _authorization(websocket: WebSocket) -> tuple[str, str]:
    value = websocket.headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    return scheme.strip().lower(), token.strip()


def _new_waiter(runner_id: str, correlation_id: str) -> asyncio.Future[dict[str, Any]]:
    key = (runner_id, correlation_id)
    existing = _waiters.get(key)
    if existing is not None and not existing.done():
        raise ValueError("runner request is already pending")
    future = asyncio.get_running_loop().create_future()
    _waiters[key] = future
    return future


def _resolve_waiter(runner_id: str, correlation_id: str, value: dict[str, Any]) -> None:
    future = _waiters.get((runner_id, correlation_id))
    if future is not None and not future.done():
        future.set_result(value)


def _fail_waiters(runner_id: str, reason: str) -> None:
    for (candidate_runner, _), future in list(_waiters.items()):
        if candidate_runner == runner_id and not future.done():
            future.set_exception(ConnectionError(reason))


async def _send(runner_id: str, frame: dict[str, Any]) -> None:
    connection = _connections.get(runner_id)
    if connection is None:
        raise ConnectionError("runner is offline")
    async with connection.send_lock:
        await connection.websocket.send_json(frame)


async def _control(
    runner_id: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    connection = _connections.get(runner_id)
    if connection is None:
        raise ConnectionError("runner is offline")
    request_id = f"ctl_{uuid.uuid4().hex}"
    payload = {"method": method, "params": params, "request_id": request_id}
    future = _new_waiter(runner_id, request_id)
    try:
        await _send(
            runner_id,
            {
                "envelope": sign_envelope(payload, connection.command_key),
                "request_id": request_id,
                "type": "control",
            },
        )
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    finally:
        if _waiters.get((runner_id, request_id)) is future:
            _waiters.pop((runner_id, request_id), None)


async def _dispatch_command(
    runner_id: str,
    frame: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    command_id = str(frame["command_id"])
    future = _new_waiter(runner_id, command_id)
    try:
        await _send(runner_id, frame)
        _registry().mark_command_sent(command_id)
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    finally:
        if _waiters.get((runner_id, command_id)) is future:
            _waiters.pop((runner_id, command_id), None)


async def _replay_pending(runner_id: str) -> None:
    for frame in _registry().pending_commands(runner_id):
        try:
            await _send(runner_id, frame)
            _registry().mark_command_sent(str(frame["command_id"]))
        except (ConnectionError, WebSocketDisconnect):
            return


async def _release_terminal_command_lease(runner_id: str, command_id: str) -> None:
    try:
        connection = _connections.get(runner_id)
        if connection is None:
            return
        frame = _registry().command_frame(runner_id, command_id)
        payload = verify_envelope(frame.get("envelope") or {}, connection.command_key)
        command = RunnerCommand.from_dict(payload)
        await _control(
            runner_id,
            "lease.release",
            {
                "binding_id": command.binding_id,
                "fencing_token": command.fencing_token,
                "lease_id": command.lease_id,
            },
            timeout=5,
        )
    except (ConnectionError, TimeoutError, ValueError):
        return


def _schedule_terminal_lease_release(runner_id: str, command_id: str) -> None:
    task = asyncio.create_task(_release_terminal_command_lease(runner_id, command_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("/api/workspace/runners/enroll")
def enroll_runner(body: RunnerEnrollmentBody):
    enrollment = _registry().create_enrollment(body.label)
    return {
        "enrollment_token": enrollment.enrollment_token,
        "expires_at": enrollment.expires_at,
        "runner_id": enrollment.runner_id,
        "websocket_path": "/api/workspace/runners/connect",
    }


@router.get("/api/workspace/runners")
def list_workspace_runners():
    return {"runners": _registry().list_runners()}


@router.get("/api/workspace/runners/{runner_id}/commands/{command_id}")
def workspace_runner_command_status(runner_id: str, command_id: str):
    try:
        return _registry().command_status(runner_id, command_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/workspace/runners/{runner_id}/commands/{command_id}/reconcile")
async def reconcile_workspace_runner_command(
    runner_id: str,
    command_id: str,
    body: ReconcileRunnerCommandBody,
):
    registry = _registry()
    if body.decision == "abandon":
        try:
            registry.abandon_command(runner_id, command_id)
            return registry.command_status(runner_id, command_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        frame = registry.command_frame(runner_id, command_id)
        original = RunnerCommand.from_dict(
            verify_envelope(frame.get("envelope") or {}, registry.command_key(runner_id))
        )
        registry.begin_reconciliation(runner_id, command_id, decision="retry")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        response = await dispatch_workspace_runner_command(
            runner_id,
            RemoteRunnerCommandBody(
                attempt_id=f"{original.attempt_id[:80]}.retry.{uuid.uuid4().hex}",
                binding_id=original.binding_id,
                expected_head=None,
                method=original.method,
                params=original.params,
                run_id=original.run_id,
                timeout_seconds=60,
            ),
        )
        replacement = (
            json.loads(bytes(response.body))
            if isinstance(response, JSONResponse)
            else response
        )
        replacement_id = str(replacement["command_id"])
        outcome = "failed" if replacement.get("state") == "failed" else "resumed"
        registry.finish_reconciliation(
            runner_id,
            command_id,
            outcome=outcome,
            replacement_command_id=replacement_id if outcome == "resumed" else None,
        )
        return {
            "command": registry.command_status(runner_id, command_id),
            "replacement": replacement,
        }
    except Exception:
        try:
            registry.finish_reconciliation(
                runner_id,
                command_id,
                outcome="failed",
                replacement_command_id=None,
            )
        except ValueError:
            pass
        raise


@router.post("/api/workspace/runners/{runner_id}/commands")
async def dispatch_workspace_runner_command(runner_id: str, body: RemoteRunnerCommandBody):
    registry = _registry()
    try:
        registry.require_binding(runner_id, body.binding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if runner_id not in _connections:
        raise HTTPException(status_code=409, detail="runner is offline")

    if body.command_id is not None:
        try:
            existing = registry.command_status(runner_id, body.command_id)
        except ValueError:
            existing = None
        if existing is not None:
            status_code = 200 if existing["state"] in {"completed", "failed"} else 202
            return JSONResponse(status_code=status_code, content=existing)

    try:
        lease_response = await _control(
            runner_id,
            "lease.acquire",
            {
                "binding_id": body.binding_id,
                "expected_head": body.expected_head,
                "owner": body.run_id,
                "ttl_seconds": body.timeout_seconds + 15,
            },
            timeout=min(body.timeout_seconds, 30),
        )
    except (ConnectionError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "runner lease failed") from exc
    if lease_response.get("ok") is not True or not isinstance(lease_response.get("result"), dict):
        raise HTTPException(
            status_code=409,
            detail=str(lease_response.get("error") or "runner lease was rejected"),
        )
    lease = lease_response["result"]
    try:
        command = RunnerCommand.create(
            attempt_id=body.attempt_id,
            binding_id=body.binding_id,
            fencing_token=int(lease["fencing_token"]),
            lease_id=str(lease["lease_id"]),
            method=body.method,
            params=body.params,
            run_id=body.run_id,
            command_id=body.command_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    connection = _connections.get(runner_id)
    if connection is None:
        raise HTTPException(status_code=503, detail="runner disconnected before command dispatch")
    frame = {
        "command_id": command.command_id,
        "envelope": sign_envelope(command.to_dict(), connection.command_key),
        "type": "command",
    }
    try:
        try:
            registry.queue_command(runner_id, command.command_id, frame)
        except ValueError as exc:
            status_code = 413 if "size limit" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        try:
            await _dispatch_command(runner_id, frame, timeout=body.timeout_seconds)
        except (ConnectionError, TimeoutError):
            status = registry.command_status(runner_id, command.command_id)
            return JSONResponse(status_code=202, content=status)
        return registry.command_status(runner_id, command.command_id)
    finally:
        try:
            command_state = registry.command_status(runner_id, command.command_id)["state"]
        except ValueError:
            command_state = "unknown"
        if command_state not in {"sent", "acknowledged"} and runner_id in _connections:
            try:
                await _control(
                    runner_id,
                    "lease.release",
                    {
                        "binding_id": body.binding_id,
                        "fencing_token": int(lease["fencing_token"]),
                        "lease_id": str(lease["lease_id"]),
                    },
                    timeout=5,
                )
            except (ConnectionError, TimeoutError):
                pass


@router.post("/api/workspace/runners/{runner_id}/revoke")
async def revoke_workspace_runner(runner_id: str):
    try:
        _registry().revoke_runner(runner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _fail_waiters(runner_id, "runner revoked")
    connection = _connections.pop(runner_id, None)
    if connection is not None:
        await connection.websocket.close(code=4401, reason="runner revoked")
    return {"ok": True, "runner_id": runner_id}


@router.websocket("/api/workspace/runners/connect")
async def workspace_runner_connect(
    websocket: WebSocket,
    runner_id: str = Query(min_length=3, max_length=80),
):
    if not _transport_allowed(websocket):
        await websocket.close(code=4403, reason="runner transport requires WSS")
        return
    scheme, token = _authorization(websocket)
    registry = _registry()
    response: dict[str, Any]
    if scheme == "enrollment":
        try:
            credentials = registry.consume_enrollment(runner_id, token)
        except ValueError:
            await websocket.close(code=4401, reason="invalid runner enrollment")
            return
        command_key = credentials.command_key
        response = {
            "command_key": base64.urlsafe_b64encode(credentials.command_key).decode("ascii"),
            "device_token": credentials.device_token,
            "runner_id": runner_id,
            "type": "enrolled",
        }
    elif scheme == "runner" and registry.authenticate(runner_id, token):
        command_key = registry.command_key(runner_id)
        response = {"runner_id": runner_id, "type": "connected"}
    else:
        await websocket.close(code=4401, reason="invalid runner credential")
        return

    await websocket.accept()
    previous = _connections.get(runner_id)
    if previous is not None:
        _fail_waiters(runner_id, "runner connection replaced")
        await previous.websocket.close(code=4409, reason="runner connection replaced")
    connection = _RunnerConnection(command_key, asyncio.Lock(), websocket)
    _connections[runner_id] = connection
    registry.heartbeat(runner_id)
    await websocket.send_json(response)

    try:
        while True:
            message = await websocket.receive_json()
            message_type = str(message.get("type") or "")
            if message_type == "hello":
                bindings = message.get("bindings")
                capabilities = message.get("capabilities", [])
                if not isinstance(bindings, list) or not isinstance(capabilities, list):
                    await websocket.close(code=1008, reason="runner bindings are invalid")
                    return
                registry.sync_bindings(runner_id, bindings)
                registry.sync_capabilities(runner_id, capabilities)
                await websocket.send_json({"type": "hello.ack"})
                await _replay_pending(runner_id)
            elif message_type == "heartbeat":
                registry.heartbeat(runner_id)
                await websocket.send_json({"type": "heartbeat.ack"})
            elif message_type == "response":
                request_id = str(message.get("request_id") or "")
                payload = verify_envelope(message.get("envelope") or {}, command_key)
                if payload.get("request_id") != request_id:
                    raise ValueError("runner response correlation is invalid")
                _resolve_waiter(runner_id, request_id, payload)
            elif message_type == "event.batch":
                payload = verify_envelope(message.get("envelope") or {}, command_key)
                events = payload.get("events")
                if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
                    raise ValueError("runner event batch is malformed")
                event_ids = registry.ingest_events(runner_id, events)
                await _send(
                    runner_id,
                    {
                        "envelope": sign_envelope({"event_ids": event_ids}, command_key),
                        "type": "event.ack",
                    },
                )
            elif message_type == "command.ack":
                command_id = str(message.get("command_id") or "")
                payload = verify_envelope(message.get("envelope") or {}, command_key)
                if payload.get("command_id") != command_id:
                    raise ValueError("runner command acknowledgement correlation is invalid")
                _registry().acknowledge_command(
                    runner_id,
                    command_id,
                    ack_state=str(payload.get("state") or ""),
                )
                continue
            elif message_type == "command.result":
                command_id = str(message.get("command_id") or "")
                had_waiter = (runner_id, command_id) in _waiters
                payload = verify_envelope(message.get("envelope") or {}, command_key)
                if payload.get("command_id") != command_id:
                    raise ValueError("runner command result correlation is invalid")
                try:
                    registry.complete_command(runner_id, command_id, result=payload)
                except ValueError as exc:
                    if "size limit" not in str(exc):
                        raise
                    rejected = {
                        "command_id": command_id,
                        "error": "runner result rejected by control-plane size policy",
                        "ok": False,
                    }
                    registry.complete_command(runner_id, command_id, result=rejected)
                    _resolve_waiter(runner_id, command_id, rejected)
                    if not had_waiter:
                        _schedule_terminal_lease_release(runner_id, command_id)
                    await websocket.close(code=1009, reason="runner result exceeds size policy")
                    return
                _resolve_waiter(runner_id, command_id, payload)
                if not had_waiter:
                    _schedule_terminal_lease_release(runner_id, command_id)
            else:
                await websocket.close(code=1008, reason="runner frame is not supported")
                return
    except (ValueError, WebSocketDisconnect):
        pass
    finally:
        if _connections.get(runner_id) is connection:
            _connections.pop(runner_id, None)
            _fail_waiters(runner_id, "runner disconnected")
        command_key = b""
