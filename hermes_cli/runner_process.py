"""Standalone JSONL process boundary for a local Hermes workspace runner."""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol

from hermes_cli.runner import WorkspaceRunner
from hermes_cli.runner_protocol import RunnerCommand, verify_envelope
from hermes_cli.runner_spool import RunnerSpool


class CodexWorkerProtocol(Protocol):
    def run(
        self,
        *,
        prompt: str,
        workdir: str | Path,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class RunnerProcessServer:
    def __init__(
        self,
        *,
        state_path: str | Path,
        device_key: bytes,
        codex_worker: CodexWorkerProtocol | None = None,
    ):
        if len(device_key) < 32:
            raise ValueError("runner device key must be at least 32 bytes")
        self.device_key = device_key
        self.spool = RunnerSpool(state_path)
        self.reconciled_commands = self.spool.reconcile_incomplete_commands()
        handlers: dict[str, Callable[[RunnerCommand, Path], dict[str, Any]]] | None = None
        if codex_worker is not None:
            def run_codex(command: RunnerCommand, root: Path) -> dict[str, Any]:
                return codex_worker.run(
                    prompt=str(command.params.get("prompt") or ""),
                    workdir=root,
                    timeout_seconds=float(command.params.get("timeout_seconds", 1800)),
                )

            handlers = {"worker.codex": run_codex}
        self.runner = WorkspaceRunner(self.spool, operation_handlers=handlers)
        self._write_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hermes-runner")

    def close(self) -> None:
        self.runner.shutdown()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.spool.close()

    def _write(self, response: dict[str, Any]) -> None:
        line = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "binding.register":
            binding = self.runner.register_binding(
                binding_id=params.get("binding_id"),
                label=str(params.get("label") or "Workspace"),
                project_id=str(params.get("project_id") or ""),
                root_path=str(params.get("path") or ""),
            )
            return binding.public_dict()
        if method == "binding.list":
            return self.spool.public_bindings()
        if method == "binding.resolve-local":
            return {"path": str(self.spool.resolve_binding(str(params.get("binding_id") or "")))}
        if method == "binding.revoke":
            self.spool.revoke_binding(str(params.get("binding_id") or ""))
            return {"revoked": True}
        if method == "lease.acquire":
            binding_id = str(params.get("binding_id") or "")
            root = self.spool.resolve_binding(binding_id)
            expected_head = (
                params.get("expected_head")
                if "expected_head" in params
                else self.runner._git_head(root)
            )
            lease = self.runner.acquire_lease(
                binding_id=binding_id,
                expected_head=expected_head,
                now=params.get("now"),
                owner=str(params.get("owner") or ""),
                ttl_seconds=float(params.get("ttl_seconds", 60)),
            )
            return asdict(lease)
        if method == "lease.release":
            released = self.spool.release_lease(
                binding_id=str(params.get("binding_id") or ""),
                fencing_token=int(params.get("fencing_token", -1)),
                lease_id=str(params.get("lease_id") or ""),
            )
            return {"released": released}
        if method == "command.execute":
            envelope = params.get("envelope")
            if not isinstance(envelope, dict):
                raise ValueError("signed command envelope is required")
            command = RunnerCommand.from_dict(verify_envelope(envelope, self.device_key))
            return self.runner.execute(command)
        if method == "events.pending":
            attempt_id = str(params.get("attempt_id") or "")
            return [
                event.to_dict()
                for event in self.spool.pending_events(
                    attempt_id,
                    limit=int(params.get("limit", 1000)),
                )
            ]
        if method == "events.ack":
            self.spool.ack_events(
                str(params.get("attempt_id") or ""),
                through_sequence=int(params.get("through_sequence", 0)),
            )
            return {"acked": True}
        if method == "device.heartbeat":
            return {
                "bindings": self.spool.public_bindings(),
                "protocol_version": 1,
                "reconciled_commands": self.reconciled_commands,
                "sandbox_available": self.runner.terminal_sandbox_available,
            }
        raise ValueError("runner process method is not allowed")

    def _handle(self, request_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "request_id": request_id,
                "result": self.dispatch(method, params),
            }
        except Exception as exc:
            return {
                "error": str(exc) or type(exc).__name__,
                "ok": False,
                "request_id": request_id,
            }

    def _finish(self, future: Future[dict[str, Any]]) -> None:
        try:
            self._write(future.result())
        except Exception as exc:
            self._write({"error": str(exc) or type(exc).__name__, "ok": False, "request_id": "unknown"})

    def serve(self) -> int:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("runner process request must be an object")
                request_id = str(request.get("request_id") or "")
                method = str(request.get("method") or "")
                params = request.get("params") or {}
                if not request_id or not isinstance(params, dict):
                    raise ValueError("runner process request is malformed")
            except Exception as exc:
                self._write(
                    {
                        "error": str(exc) or "invalid runner process request",
                        "ok": False,
                        "request_id": "unknown",
                    }
                )
                continue

            future = self._executor.submit(self._handle, request_id, method, params)
            future.add_done_callback(self._finish)
        return 0


def _device_key_from_environment() -> bytes:
    encoded = os.environ.get("HERMES_RUNNER_DEVICE_KEY", "").strip()
    if not encoded:
        raise ValueError("HERMES_RUNNER_DEVICE_KEY is required")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("HERMES_RUNNER_DEVICE_KEY is invalid") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-runner-process")
    parser.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    server = RunnerProcessServer(
        device_key=_device_key_from_environment(),
        state_path=args.state,
    )
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            return server.serve()
        except KeyboardInterrupt:
            return 0
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
