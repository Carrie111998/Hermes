"""Opt-in full Gateway-process E2E for the Codex-first Phase 1 contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


API_KEY = "phase1-process-test-key-that-is-long-enough"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _hermes_executable() -> str:
    name = "hermes.exe" if os.name == "nt" else "hermes"
    sibling = Path(sys.executable).with_name(name)
    found = shutil.which("hermes")
    if sibling.exists():
        return str(sibling)
    if found:
        return found
    pytest.skip("hermes gateway executable is unavailable")


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    conversation: str,
    idempotency_key: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "X-Hermes-Session-Key": conversation,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_ready(base_url: str, process: subprocess.Popen, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"gateway exited early with {process.returncode}")
        try:
            status, _payload = _request(
                base_url,
                "GET",
                "/v1/capabilities",
                conversation="phase1-process-client",
            )
            if status == 200:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.25)
    raise TimeoutError("gateway did not become ready")


def _wait_for_phase(
    base_url: str,
    process: subprocess.Popen,
    task_id: str,
    phase: str,
    *,
    timeout: float = 180,
) -> tuple[dict, set[str]]:
    deadline = time.monotonic() + timeout
    observed = set()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"gateway exited early with {process.returncode}")
        status, payload = _request(
            base_url,
            "GET",
            f"/v1/codex/tasks/{task_id}",
            conversation="phase1-process-client",
        )
        if status == 200:
            observed.add(payload["phase"])
            if payload["phase"] == phase:
                return payload, observed
        time.sleep(0.1)
    raise TimeoutError(f"task {task_id} did not reach {phase}")


def _stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(
    os.environ.get("HERMES_CODEX_BRIDGE_PROCESS_E2E") != "1",
    reason="set HERMES_CODEX_BRIDGE_PROCESS_E2E=1 for real Gateway/Codex E2E",
)
def test_authenticated_http_question_survives_full_gateway_process_restart(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    hermes_home = tmp_path / "hermes-home"
    workspace = tmp_path / "workspace"
    hermes_home.mkdir()
    workspace.mkdir()
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("must remain unchanged", encoding="utf-8")
    config = (
        "gateway:\n"
        "  api_server:\n"
        "    enabled: true\n"
        "    host: 127.0.0.1\n"
        f"    port: {port}\n"
        f"    key: {API_KEY}\n"
        "codex_bridge:\n"
        "  enabled: true\n"
        "  allowed_origins: [api_server]\n"
        f"  workspace_allowlist: [{json.dumps(str(workspace))}]\n"
        f"  default_workspace: {json.dumps(str(workspace))}\n"
        "  sandbox: read-only\n"
        "  collaboration_mode: plan\n"
        "  stale_recovery_seconds: 1\n"
        "legacy_hermes_workers:\n"
        "  auto_dispatch_enabled: false\n"
    )
    (hermes_home / "config.yaml").write_text(config, encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONUTF8"] = "1"
    command = [
        _hermes_executable(),
        "gateway",
        "run",
        "--force",
        "--accept-hooks",
        "--external-supervisor",
    ]
    process = None
    stdout = (hermes_home / "gateway.stdout.log").open("w", encoding="utf-8")
    stderr = (hermes_home / "gateway.stderr.log").open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        _wait_ready(base_url, process)
        initial_body = {
            "input": (
                "Before planning anything else, call request_user_input exactly "
                "once. Ask one question with header Environment, id environment, "
                "question Choose the deployment environment., and options Staging "
                "described Safe test target and Production described User-impacting "
                "target. Do not inspect or modify files."
            ),
            "workspace": str(workspace),
        }
        status, initial = _request(
            base_url,
            "POST",
            "/v1/codex/tasks",
            conversation="phase1-process-client",
            idempotency_key="phase1-process-initial",
            body=initial_body,
        )
        assert status == 202
        assert initial["phase"] in {"captured", "working"}
        task_id = initial["task_id"]
        initial, observed_phases = _wait_for_phase(
            base_url, process, task_id, "needs_user"
        )
        duplicate_status, duplicate = _request(
            base_url,
            "POST",
            "/v1/codex/tasks",
            conversation="phase1-process-client",
            idempotency_key="phase1-process-initial",
            body=initial_body,
        )
        assert duplicate_status == 202
        assert initial["phase"] == "needs_user"
        assert initial["prompt_id"] == duplicate["prompt_id"]
        assert 2 <= len(initial["events"]) <= 4
        assert "working" in observed_phases
        assert sentinel.read_text(encoding="utf-8") == "must remain unchanged"
        prompt_id = initial["prompt_id"]
        db_path = hermes_home / "codex_bridge" / "state.db"
        with sqlite3.connect(db_path) as db:
            thread_before = db.execute(
                "SELECT codex_thread_id FROM bridge_jobs WHERE hermes_job_id = ?",
                (task_id,),
            ).fetchone()[0]

        _stop(process)
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        _wait_ready(base_url, process)
        status, persisted = _request(
            base_url,
            "GET",
            f"/v1/codex/tasks/{task_id}",
            conversation="phase1-process-client",
        )
        assert status == 200
        assert persisted["phase"] == "needs_user"
        assert persisted["prompt_id"] == prompt_id

        wrong_status, _wrong = _request(
            base_url,
            "POST",
            f"/v1/codex/tasks/{task_id}/reply",
            conversation="phase1-other-client",
            idempotency_key="phase1-wrong-reply",
            body={"prompt_id": prompt_id, "answer": "Staging"},
        )
        assert wrong_status == 409
        reply_body = {
            "prompt_id": prompt_id,
            "answer": (
                "Staging. Continue from the pending question and finish with a "
                "concise confirmation. Do not inspect or modify files."
            ),
        }
        reply_status, completed = _request(
            base_url,
            "POST",
            f"/v1/codex/tasks/{task_id}/reply",
            conversation="phase1-process-client",
            idempotency_key="phase1-process-reply",
            body=reply_body,
        )
        duplicate_reply_status, duplicate_completed = _request(
            base_url,
            "POST",
            f"/v1/codex/tasks/{task_id}/reply",
            conversation="phase1-process-client",
            idempotency_key="phase1-process-reply",
            body=reply_body,
        )
        assert reply_status == duplicate_reply_status == 200
        assert completed["phase"] == "done"
        assert completed["result"] == duplicate_completed["result"]
        assert "Staging" in completed["result"]
        assert sentinel.read_text(encoding="utf-8") == "must remain unchanged"
        with sqlite3.connect(db_path) as db:
            thread_after = db.execute(
                "SELECT codex_thread_id FROM bridge_jobs WHERE hermes_job_id = ?",
                (task_id,),
            ).fetchone()[0]
            reply_count = db.execute(
                "SELECT COUNT(*) FROM bridge_replies WHERE hermes_job_id = ?",
                (task_id,),
            ).fetchone()[0]
        assert thread_after == thread_before
        assert reply_count == 1
        assert all(
            "reasoning" not in json.dumps(event).lower()
            for event in completed["events"]
        )
    finally:
        _stop(process)
        stdout.close()
        stderr.close()
