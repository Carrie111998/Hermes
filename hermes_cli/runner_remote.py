"""Outbound WebSocket client for a device-local Hermes workspace runner."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import websockets

from hermes_cli.runner_process import RunnerProcessServer
from hermes_cli.runner_protocol import RunnerCommand, sign_envelope, verify_envelope
from hermes_cli.workspace_codex_worker import CodexWorker

_CONTROL_METHODS = frozenset(
    {
        "device.heartbeat",
        "events.ack",
        "events.pending",
        "lease.acquire",
        "lease.release",
    }
)
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9:+])/(?:Users|home|private|tmp|var|opt|srv)/[^\s\"']+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\r\n\"']+")
_log = logging.getLogger(__name__)


def _redact_local_paths(value: str) -> str:
    value = _POSIX_PATH_RE.sub("[REDACTED_LOCAL_PATH]", value)
    return _WINDOWS_PATH_RE.sub("[REDACTED_LOCAL_PATH]", value)


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_local_paths(value)
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_public_value(item) for key, item in value.items()}
    return value


class RunnerCredentialsFile:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def save(self, *, runner_id: str, device_token: str, command_key: bytes) -> None:
        if len(command_key) < 32 or not runner_id or not device_token:
            raise ValueError("runner credentials are invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "command_key": base64.urlsafe_b64encode(command_key).decode("ascii"),
            "device_token": device_token,
            "runner_id": runner_id,
        }
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> dict[str, str]:
        if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise ValueError("runner credentials file permissions are too broad")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("runner credentials file is malformed")
        required = {"command_key", "device_token", "runner_id"}
        if set(value) != required or not all(isinstance(value[key], str) and value[key] for key in required):
            raise ValueError("runner credentials file is malformed")
        try:
            key = base64.urlsafe_b64decode(value["command_key"].encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("runner command key is invalid") from exc
        if len(key) < 32:
            raise ValueError("runner command key is invalid")
        return {key: str(value[key]) for key in sorted(required)}


class RemoteRunner:
    def __init__(
        self,
        *,
        state_path: str | Path,
        command_key: bytes,
        codex_worker: CodexWorker | None = None,
    ):
        self.command_key = command_key
        self.server = RunnerProcessServer(
            state_path=state_path,
            device_key=command_key,
            codex_worker=codex_worker,
        )

    def close(self) -> None:
        self.server.close()

    def register_binding(
        self,
        *,
        binding_id: str,
        label: str,
        path: str | Path,
        project_id: str,
    ) -> dict[str, Any]:
        return self.server.dispatch(
            "binding.register",
            {
                "binding_id": binding_id,
                "label": label,
                "path": str(path),
                "project_id": project_id,
            },
        )

    def public_bindings(self) -> list[dict[str, Any]]:
        return self.server.dispatch("binding.list", {})

    def capabilities(self) -> list[str]:
        return sorted(self.server.runner.operation_handlers)

    def pending_event_batch(self) -> dict[str, Any] | None:
        events = [
            _sanitize_public_value(event.to_dict())
            for event in self.server.spool.pending_events_all(limit=256)
        ]
        if not events:
            return None
        payload = {"events": events}
        return {
            "envelope": sign_envelope(payload, self.command_key),
            "type": "event.batch",
        }

    def acknowledge_event_batch(self, frame: dict[str, Any]) -> None:
        payload = verify_envelope(frame.get("envelope") or {}, self.command_key)
        event_ids = payload.get("event_ids")
        if not isinstance(event_ids, list) or any(not isinstance(item, str) for item in event_ids):
            raise ValueError("runner event acknowledgement is malformed")
        self.server.spool.ack_event_ids(event_ids)

    def _signed_response(
        self,
        *,
        correlation_id: str,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": error is None,
            "request_id": correlation_id,
        }
        if error is None:
            payload["result"] = _sanitize_public_value(result)
        else:
            payload["error"] = _redact_local_paths(error)
        return {
            "envelope": sign_envelope(payload, self.command_key),
            "request_id": correlation_id,
            "type": "response",
        }

    def _command_result_frame(
        self,
        command_id: str,
        outcome: dict[str, Any] | None,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        value = _sanitize_public_value(outcome or {})
        if not isinstance(value, dict):
            value = {}
        replayed = value.get("replayed") is True
        uncertain = value.get("state") == "uncertain" or value.get("uncertain") is True
        if error is not None:
            ok = False
            state = "failed"
            result = None
            safe_error = _redact_local_paths(error)
        elif uncertain:
            ok = False
            state = "uncertain"
            result = value.get("result")
            safe_error = str(value.get("error") or "runner command outcome is uncertain")
        elif value.get("ok") is True:
            ok = True
            state = "completed"
            result = value.get("result")
            safe_error = None
        else:
            ok = False
            state = str(value.get("state") or "failed")
            if state not in {"failed", "uncertain", "canceled"}:
                state = "failed"
            result = value.get("result")
            safe_error = str(value.get("error") or "runner command failed")
        payload: dict[str, Any] = {
            "command_id": command_id,
            "ok": ok,
            "replayed": replayed,
            "result": result,
            "state": state,
        }
        if safe_error is not None:
            payload["error"] = _redact_local_paths(safe_error)
        return {
            "command_id": command_id,
            "envelope": sign_envelope(payload, self.command_key),
            "type": "command.result",
        }

    def accept_command_frame(
        self,
        frame: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None, RunnerCommand | None]:
        command_id = str(frame.get("command_id") or "")
        payload = verify_envelope(frame.get("envelope") or {}, self.command_key)
        command = RunnerCommand.from_dict(payload)
        if command.command_id != command_id:
            raise ValueError("runner command correlation is invalid")
        try:
            accepted, replay = self.server.runner.accept(command)
        except Exception as exc:
            stored = self.server.runner.spool.command_result(command_id)
            if stored is None:
                raise
            outcome = stored.get("result")
            if not isinstance(outcome, dict):
                outcome = {"error": str(exc) or type(exc).__name__, "ok": False}
            ack_state = "accepted"
            result_frame = self._command_result_frame(command_id, outcome)
            accepted = False
        else:
            ack_state = "accepted" if accepted else "replayed"
            replay_pending = (
                not accepted
                and isinstance(replay, dict)
                and replay.get("result") is None
                and replay.get("state") == "accepted"
            )
            result_frame = (
                None
                if accepted or replay_pending
                else self._command_result_frame(command_id, replay or {})
            )
        ack_payload = {
            "accepted_at": time.time(),
            "command_id": command_id,
            "state": ack_state,
        }
        ack_frame = {
            "command_id": command_id,
            "envelope": sign_envelope(ack_payload, self.command_key),
            "type": "command.ack",
        }
        return ack_frame, result_frame, command if accepted else None

    def execute_accepted_command(self, command: RunnerCommand) -> dict[str, Any] | None:
        try:
            outcome = self.server.runner.execute_accepted(command)
            return self._command_result_frame(command.command_id, outcome)
        except Exception as exc:
            stored = self.server.runner.spool.command_result(command.command_id)
            outcome = stored.get("result") if stored is not None else None
            if isinstance(outcome, dict):
                return self._command_result_frame(command.command_id, outcome)
            if stored is not None and stored.get("state") == "accepted":
                return None
            return self._command_result_frame(
                command.command_id,
                None,
                error=str(exc) or type(exc).__name__,
            )

    def process_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        frame_type = str(frame.get("type") or "")
        if frame_type == "control":
            request_id = str(frame.get("request_id") or "")
            payload = verify_envelope(frame.get("envelope") or {}, self.command_key)
            if payload.get("request_id") != request_id:
                raise ValueError("runner control correlation is invalid")
            method = str(payload.get("method") or "")
            params = payload.get("params") or {}
            if method not in _CONTROL_METHODS or not isinstance(params, dict):
                raise ValueError("runner control method is not allowed")
            try:
                result = self.server.dispatch(method, params)
            except Exception as exc:
                return self._signed_response(
                    correlation_id=request_id,
                    error=str(exc) or type(exc).__name__,
                )
            return self._signed_response(correlation_id=request_id, result=result)

        if frame_type == "command":
            _ack, result, command = self.accept_command_frame(frame)
            if result is not None:
                return result
            if command is None:
                raise ValueError("runner command was not accepted")
            executed = self.execute_accepted_command(command)
            if executed is None:
                raise RuntimeError("runner command completion remains pending")
            return executed
        raise ValueError("runner frame is not supported")


def _validate_websocket_url(value: str, runner_id: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("runner server URL must be ws:// or wss://")
    if parsed.scheme == "ws":
        try:
            import ipaddress

            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise ValueError("non-loopback runner transport requires wss://")
    query = dict(item.split("=", 1) for item in parsed.query.split("&") if "=" in item)
    query["runner_id"] = runner_id
    return urlunparse(parsed._replace(query=urlencode(query)))


class OutboundRunnerClient:
    def __init__(
        self,
        *,
        credentials_path: str | Path,
        enrollment_token: str | None,
        runner_id: str,
        server_url: str,
        state_path: str | Path,
        codex_worker: CodexWorker | None = None,
    ):
        self.credentials = RunnerCredentialsFile(credentials_path)
        self.enrollment_token = enrollment_token
        self.runner_id = runner_id
        self.server_url = _validate_websocket_url(server_url, runner_id)
        self.state_path = Path(state_path).expanduser()
        self.codex_worker = codex_worker
        self.remote: RemoteRunner | None = None
        self.device_token: str | None = None
        self.command_key: bytes | None = None
        self._command_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._active_websocket: Any | None = None
        if self.credentials.path.exists():
            saved = self.credentials.load()
            if saved["runner_id"] != runner_id:
                raise ValueError("runner credentials belong to another runner")
            self.device_token = saved["device_token"]
            self.command_key = base64.urlsafe_b64decode(saved["command_key"].encode("ascii"))

    def register_bindings(self, bindings: list[dict[str, str]]) -> None:
        if self.remote is None:
            if self.command_key is None:
                raise ValueError("runner must enroll before registering bindings")
            self.remote = RemoteRunner(
                state_path=self.state_path,
                command_key=self.command_key,
                codex_worker=self.codex_worker,
            )
        for binding in bindings:
            self.remote.register_binding(
                binding_id=binding["binding_id"],
                label=binding["label"],
                path=binding["path"],
                project_id=binding["project_id"],
            )

    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await websocket.send(json.dumps(payload, separators=(",", ":")))

    async def _flush_events(self, websocket: Any) -> None:
        if self.remote is None:
            return
        batch = await asyncio.to_thread(self.remote.pending_event_batch)
        if batch is not None:
            await self._send_json(websocket, batch)

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(20)
            await self._send_json(websocket, {"type": "heartbeat"})
            await self._flush_events(websocket)

    async def _execute_command(
        self,
        command: RunnerCommand,
    ) -> None:
        if self.remote is None:
            return
        async with self._command_lock:
            result = await asyncio.to_thread(self.remote.execute_accepted_command, command)
        if result is None:
            return
        websocket = self._active_websocket
        if websocket is None:
            return
        try:
            await self._send_json(websocket, result)
            await self._flush_events(websocket)
        except Exception:
            # The durable local spool and central replay recover a lost result.
            return

    async def _handle_message(self, websocket: Any, message: dict[str, Any]) -> None:
        if self.remote is None:
            raise ValueError("runner is not initialized")
        if message.get("type") == "command":
            ack, result, command = await asyncio.to_thread(
                self.remote.accept_command_frame,
                message,
            )
            await self._send_json(websocket, ack)
            if result is not None:
                await self._send_json(websocket, result)
                await self._flush_events(websocket)
                return
            if command is None:
                return
            task = asyncio.create_task(self._execute_command(command))
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)
            return
        if message.get("type") == "event.ack":
            await asyncio.to_thread(self.remote.acknowledge_event_batch, message)
            return
        response = await asyncio.to_thread(self.remote.process_frame, message)
        await self._send_json(websocket, response)

    async def connect_once(self, bindings: list[dict[str, str]]) -> None:
        if self.device_token:
            authorization = f"Runner {self.device_token}"
        elif self.enrollment_token:
            authorization = f"Enrollment {self.enrollment_token}"
        else:
            raise ValueError("runner enrollment token or saved credentials are required")
        async with websockets.connect(
            self.server_url,
            additional_headers={"Authorization": authorization},
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self._active_websocket = websocket
            first = json.loads(await websocket.recv())
            if first.get("type") == "enrolled":
                command_key = base64.urlsafe_b64decode(first["command_key"].encode("ascii"))
                self.credentials.save(
                    runner_id=self.runner_id,
                    device_token=str(first["device_token"]),
                    command_key=command_key,
                )
                self.device_token = str(first["device_token"])
                self.command_key = command_key
                self.enrollment_token = None
                _log.info("workspace runner enrolled")
            elif first.get("type") != "connected" or self.command_key is None:
                raise ValueError("runner server handshake is invalid")
            else:
                _log.info("workspace runner reconnected")
            if self.remote is None:
                self.remote = RemoteRunner(
                    state_path=self.state_path,
                    command_key=self.command_key,
                    codex_worker=self.codex_worker,
                )
            self.register_bindings(bindings)
            await self._send_json(
                websocket,
                {
                    "bindings": self.remote.public_bindings(),
                    "capabilities": self.remote.capabilities(),
                    "type": "hello",
                },
            )
            _log.info("workspace runner binding inventory synchronized")
            await self._flush_events(websocket)
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    if message.get("type") in {"connected", "heartbeat.ack", "hello.ack"}:
                        continue
                    _log.info("workspace runner received %s frame", message.get("type"))
                    await self._handle_message(websocket, message)
            finally:
                heartbeat.cancel()
                if self._active_websocket is websocket:
                    self._active_websocket = None
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self, bindings: list[dict[str, str]]) -> None:
        delay = 1.0
        while True:
            try:
                await self.connect_once(bindings)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "code", None) in {4401, 4409}:
                    _log.error("workspace runner credentials were revoked or superseded")
                    return
                _log.warning(
                    "workspace runner connection failed; retrying: %s",
                    _redact_local_paths(str(exc) or type(exc).__name__),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def close(self) -> None:
        for task in self._command_tasks:
            task.cancel()
        self._command_tasks.clear()
        if self.remote is not None:
            self.remote.close()
            self.remote = None


def _load_binding_config(path: str | Path) -> list[dict[str, str]]:
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("runner binding config must be a list")
    required = {"binding_id", "label", "path", "project_id"}
    bindings: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("runner binding config is malformed")
        bindings.append({key: str(item[key]) for key in sorted(required)})
    return bindings


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(prog="hermes-workspace-runner")
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--codex-policy")
    parser.add_argument("--codex-state")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    enrollment_token = os.environ.pop("HERMES_RUNNER_ENROLLMENT_TOKEN", None)
    codex_worker = None
    if args.codex_policy:
        codex_worker = CodexWorker.from_policy(
            args.codex_policy,
            state_dir=args.codex_state or str(Path(args.state).with_suffix(".codex")),
        )
    client = OutboundRunnerClient(
        codex_worker=codex_worker,
        credentials_path=args.credentials,
        enrollment_token=enrollment_token,
        runner_id=args.runner_id,
        server_url=args.server,
        state_path=args.state,
    )
    bindings = _load_binding_config(args.bindings)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            asyncio.run(client.run(bindings))
        except KeyboardInterrupt:
            return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
