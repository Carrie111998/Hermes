"""No-fallback terminal backend for the AF_UNIX isolated worker service."""

from __future__ import annotations

import base64
import os
import threading
from typing import Any, Sequence
from pathlib import Path

from gateway.isolated_worker import (
    IsolatedWorkerClient,
    ProtocolError,
    canonical_lease_id,
)
from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle


class IsolatedWorkerEnvironment(BaseEnvironment):
    """Route every shell operation through one session-derived worker lease."""

    def __init__(
        self,
        *,
        socket_path: Path,
        expected_server_uid: int,
        expected_server_gid: int,
        expected_socket_uid: int,
        expected_socket_gid: int,
        task_id: str,
        cwd: str = "/workspace",
        timeout: int = 180,
    ):
        if not isinstance(task_id, str) or not task_id or task_id == "default":
            raise ValueError("isolated_worker_requires_exact_session_id")
        socket_path = Path(socket_path)
        if not socket_path.is_absolute():
            raise ValueError("isolated_worker_socket_path_invalid")
        try:
            cwd_path = Path(cwd)
            cwd_path.relative_to(Path("/workspace"))
        except (TypeError, ValueError):
            raise ValueError("isolated_worker_cwd_invalid")
        if not cwd_path.is_absolute() or ".." in cwd_path.parts:
            raise ValueError("isolated_worker_cwd_invalid")
        self._lease_id = canonical_lease_id(task_id)
        self._client = IsolatedWorkerClient(
            socket_path,
            lease_id=self._lease_id,
            expected_server_uid=expected_server_uid,
            expected_server_gid=expected_server_gid,
            expected_socket_uid=expected_socket_uid,
            expected_socket_gid=expected_socket_gid,
        )
        self._client_close_lock = threading.Lock()
        self._proof_receipt_lock = threading.Lock()
        self._last_completed_proof_receipt: dict[str, Any] | None = None
        self._closed = False
        self._persistent = True
        super().__init__(cwd=cwd, timeout=timeout)
        bootstrap = self._execute_worker(
            "umask 077; mkdir -p /workspace/.hermes-runtime",
            cwd=Path("/workspace"),
            timeout=min(max(int(timeout), 1), 300),
        )
        if bootstrap[1] != 0:
            self.cleanup()
            raise RuntimeError("isolated_worker_bootstrap_failed")
        self.init_session()

    @property
    def lease_id(self) -> str:
        return self._lease_id

    def get_temp_dir(self) -> str:
        # /tmp is a fresh tmpfs for every bwrap invocation.  Session metadata
        # must stay in the lease-private workspace to persist across calls.
        return "/workspace/.hermes-runtime"

    def _execute_worker(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: int,
        stdin_data: str | None = None,
        job_ref: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        if self._closed:
            raise RuntimeError("isolated_worker_environment_closed")
        session_id = self._client.start(
            command,
            cwd=cwd,
            timeout_seconds=min(max(int(timeout), 1), 300),
            stdin=(stdin_data or "").encode("utf-8"),
        )
        if job_ref is not None:
            job_ref["session_id"] = session_id
        stdout = bytearray()
        stderr = bytearray()
        while True:
            result = self._client.poll(session_id, wait_milliseconds=250)
            stdout.extend(base64.b64decode(result.get("stdout_b64", ""), validate=True))
            stderr.extend(base64.b64decode(result.get("stderr_b64", ""), validate=True))
            if (
                result.get("state") != "running"
                and result.get("drained") is True
                and result.get("complete") is True
            ):
                returncode = result.get("returncode")
                if type(returncode) is not int:
                    returncode = 124 if result.get("state") == "timed_out" else 137
                receipt = result.get("proof_receipt")
                if not isinstance(receipt, dict):
                    raise ProtocolError("proof_receipt_missing")
                if job_ref is not None:
                    job_ref["proof_receipt"] = dict(receipt)
                combined = bytes(stdout) + bytes(stderr)
                return combined.decode("utf-8", errors="replace"), returncode

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        del login  # The worker intentionally uses a sealed, profile-free shell.
        job_ref: dict[str, Any] = {
            "session_id": None,
            "proof_receipt": None,
            "proof_error": None,
        }

        def execute() -> tuple[str, int]:
            try:
                return self._execute_worker(
                    cmd_string,
                    cwd=Path("/workspace"),
                    timeout=timeout,
                    stdin_data=stdin_data,
                    job_ref=job_ref,
                )
            except (OSError, ProtocolError) as exc:
                job_ref["proof_error"] = str(exc) or type(exc).__name__
                return f"isolated worker rejected execution: {exc}\n", 1

        def cancel() -> None:
            session_id = job_ref.get("session_id")
            if session_id:
                try:
                    self._client.cancel(session_id)
                except (OSError, ProtocolError):
                    pass

        handle = _ThreadedProcessHandle(execute, cancel_fn=cancel)
        handle._isolated_worker_job_ref = job_ref
        return handle

    def _wait_for_process(
        self,
        proc,
        timeout: int = 120,
        *,
        bounded_capture: bool = False,
    ) -> dict:
        result = super()._wait_for_process(
            proc,
            timeout=timeout,
            bounded_capture=bounded_capture,
        )
        job_ref = getattr(proc, "_isolated_worker_job_ref", None)
        if isinstance(job_ref, dict):
            receipt = job_ref.get("proof_receipt")
            if isinstance(receipt, dict):
                result["isolated_worker_proof_receipt"] = dict(receipt)
                with self._proof_receipt_lock:
                    self._last_completed_proof_receipt = dict(receipt)
            if job_ref.get("proof_error"):
                result["isolated_worker_proof_error"] = str(
                    job_ref["proof_error"]
                )
        return result

    def proof_status(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("isolated_worker_environment_closed")
        receipt = dict(self._client.proof_status())
        with self._proof_receipt_lock:
            self._last_completed_proof_receipt = dict(receipt)
        return receipt

    def mark_edited(self, paths: Sequence[str]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("isolated_worker_environment_closed")
        normalized: list[str] = []
        for raw in paths:
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = Path(self.cwd or "/workspace") / candidate
            candidate = Path(os.path.normpath(str(candidate)))
            try:
                candidate.relative_to(Path("/workspace"))
            except ValueError as exc:
                raise ProtocolError("proof_path_outside_lease") from exc
            normalized.append(str(candidate))
        if not normalized:
            raise ProtocolError("proof_paths_invalid")
        with self._proof_receipt_lock:
            prior = dict(self._last_completed_proof_receipt or {})
        observed_generation = prior.get("edit_generation")
        if type(observed_generation) is not int:
            observed_generation = None
        receipt = dict(
            self._client.mark_edited(
                sorted(set(normalized)),
                observed_generation=observed_generation,
            )
        )
        with self._proof_receipt_lock:
            self._last_completed_proof_receipt = dict(receipt)
        return receipt

    def cleanup(self) -> None:
        with self._client_close_lock:
            if self._closed:
                return
            self._closed = True
            self._client.close()


__all__ = ["IsolatedWorkerEnvironment"]
