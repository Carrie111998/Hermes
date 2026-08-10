from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
SESSION_TOKEN = "integration-session-token"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    authenticated: bool = True,
    timeout: float = 10,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["X-Hermes-Session-Token"] = SESSION_TOKEN
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_for(predicate, *, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(0.05)
    if last_error is not None:
        raise AssertionError(f"condition was not met: {last_error}") from last_error
    raise AssertionError("condition was not met before timeout")


def _stop(process: subprocess.Popen[bytes] | None, *, force: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    if force:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _device_command_state(database: Path, command_id: str) -> str | None:
    if not database.exists():
        return None
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state FROM commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
    return str(row[0]) if row else None


@pytest.mark.skipif(sys.platform != "darwin", reason="terminal fault injection requires sandbox-exec")
def test_production_control_plane_and_remote_runner_dispatch_ack_and_replay(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    control_home = tmp_path / "control-home"
    control_home.mkdir()
    repository = tmp_path / "device-repository"
    repository.mkdir()
    binding_config = tmp_path / "bindings.json"
    binding_config.write_text(
        json.dumps(
            [
                {
                    "binding_id": "binding-1",
                    "label": "Device repository",
                    "path": str(repository),
                    "project_id": "project-1",
                }
            ]
        ),
        encoding="utf-8",
    )
    binding_config.chmod(0o600)
    credentials = tmp_path / "runner-credentials.json"
    runner_state = tmp_path / "runner-state.db"
    server_log_path = tmp_path / "server.log"
    runner_log_path = tmp_path / "runner.log"
    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_DASHBOARD_SESSION_TOKEN": SESSION_TOKEN,
            "HERMES_HOME": str(control_home),
            "HERMES_WEB_DIST": str(ROOT / "web" / "dist"),
            "PYTHONPATH": str(ROOT),
        }
    )
    server: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    server_log = server_log_path.open("wb")
    runner_log = runner_log_path.open("ab")
    try:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "hermes_cli.web_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
            stderr=subprocess.STDOUT,
            stdout=server_log,
            start_new_session=True,
        )
        _wait_for(
            lambda: _json_request(f"{base_url}/api/status", authenticated=False)[0] == 200,
            timeout=60,
        )
        status, enrollment = _json_request(
            f"{base_url}/api/workspace/runners/enroll",
            method="POST",
            payload={"label": "Integration runner"},
        )
        assert status == 200
        runner_id = enrollment["runner_id"]

        runner_environment = environment.copy()
        runner_environment["HERMES_RUNNER_ENROLLMENT_TOKEN"] = enrollment["enrollment_token"]
        runner_command = [
            sys.executable,
            "-m",
            "hermes_cli.runner_remote",
            "--bindings",
            str(binding_config),
            "--credentials",
            str(credentials),
            "--runner-id",
            runner_id,
            "--server",
            f"ws://127.0.0.1:{port}/api/workspace/runners/connect",
            "--state",
            str(runner_state),
        ]
        runner = subprocess.Popen(
            runner_command,
            cwd=ROOT,
            env=runner_environment,
            stderr=subprocess.STDOUT,
            stdout=runner_log,
            start_new_session=True,
        )

        def connected() -> dict[str, Any] | None:
            code, body = _json_request(f"{base_url}/api/workspace/runners")
            if code != 200:
                return None
            match = next(
                (item for item in body["runners"] if item["runner_id"] == runner_id),
                None,
            )
            if match and match["status"] == "online" and match["bindings"]:
                return match
            return None

        assert _wait_for(connected, timeout=30)["bindings"][0]["binding_id"] == "binding-1"
        status, written = _json_request(
            f"{base_url}/api/workspace/runners/{runner_id}/commands",
            method="POST",
            payload={
                "attempt_id": "attempt-write",
                "binding_id": "binding-1",
                "command_id": "command-write",
                "method": "fs.write",
                "params": {"path": "remote/result.txt", "text": "from remote runner"},
                "run_id": "run-write",
            },
            timeout=20,
        )
        assert status == 200
        assert written["state"] == "completed"
        assert (repository / "remote" / "result.txt").read_text() == "from remote runner"

        replay_id = "command-replay-after-crash"
        with ThreadPoolExecutor(max_workers=1) as executor:
            submitted = executor.submit(
                _json_request,
                f"{base_url}/api/workspace/runners/{runner_id}/commands",
                method="POST",
                payload={
                    "attempt_id": "attempt-replay",
                    "binding_id": "binding-1",
                    "command_id": replay_id,
                    "method": "terminal.run",
                    "params": {
                        "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                        "cwd": ".",
                        "timeout_seconds": 10,
                    },
                    "run_id": "run-replay",
                    "timeout_seconds": 15,
                },
                timeout=25,
            )
            observed_state = _wait_for(
                lambda: _device_command_state(runner_state, replay_id),
                timeout=10,
            )
            if observed_state != "accepted":
                _, central_state = _json_request(
                    f"{base_url}/api/workspace/runners/{runner_id}/commands/{replay_id}"
                )
                raise AssertionError(
                    f"fault command was not durably accepted: device={observed_state}, "
                    f"central={central_state}"
                )
            _stop(runner, force=True)
            runner = None
            response_status, replay_submitted = submitted.result(timeout=15)
        assert response_status == 202
        assert replay_submitted["state"] == "acknowledged"
        assert replay_submitted["acknowledged_at"] is not None

        runner = subprocess.Popen(
            runner_command,
            cwd=ROOT,
            env=environment,
            stderr=subprocess.STDOUT,
            stdout=runner_log,
            start_new_session=True,
        )

        def reconciled() -> dict[str, Any] | None:
            code, body = _json_request(
                f"{base_url}/api/workspace/runners/{runner_id}/commands/{replay_id}"
            )
            return body if code == 200 and body.get("state") == "uncertain" else None

        try:
            replayed = _wait_for(reconciled, timeout=30)
        except AssertionError as exc:
            _, central_command = _json_request(
                f"{base_url}/api/workspace/runners/{runner_id}/commands/{replay_id}"
            )
            _, central_runners = _json_request(f"{base_url}/api/workspace/runners")
            raise AssertionError(
                "replay did not reconcile: "
                f"device={_device_command_state(runner_state, replay_id)}, "
                f"runner_exit={runner.poll() if runner else None}, "
                f"central_command={central_command}, runners={central_runners}"
            ) from exc
        assert replayed["result"]["state"] == "uncertain"
        assert replayed["result"]["replayed"] is True
        assert _device_command_state(runner_state, replay_id) == "uncertain"
        abandon_status, abandoned = _json_request(
            f"{base_url}/api/workspace/runners/{runner_id}/commands/{replay_id}/reconcile",
            method="POST",
            payload={"decision": "abandon"},
        )
        assert abandon_status == 200
        assert abandoned["state"] == "abandoned"
        assert abandoned["reconciliation"]["decision"] == "abandon"
    except Exception:
        server_log.flush()
        runner_log.flush()
        details = "\n--- server ---\n" + server_log_path.read_text(errors="replace")
        details += "\n--- runner ---\n" + runner_log_path.read_text(errors="replace")
        pytest.fail(details, pytrace=True)
    finally:
        _stop(runner)
        _stop(server)
        runner_log.close()
        server_log.close()
