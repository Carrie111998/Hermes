import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli.runner_process import RunnerProcessServer
from hermes_cli.runner_protocol import RunnerCommand, sign_envelope


class RunnerProcess:
    def __init__(self, database: Path, key: bytes):
        env = {**os.environ, "HERMES_RUNNER_DEVICE_KEY": base64.b64encode(key).decode("ascii")}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.runner_process", "--state", str(database)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.next_request = 0

    def call(self, method: str, params: dict) -> dict:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.next_request += 1
        request_id = f"request-{self.next_request}"
        self.process.stdin.write(
            json.dumps({"method": method, "params": params, "request_id": request_id}) + "\n"
        )
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            assert line, self.process.stderr.read() if self.process.stderr else "runner exited"
            response = json.loads(line)
            if response.get("request_id") == request_id:
                return response

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        self.process.terminate()
        self.process.wait(timeout=5)


def test_runner_process_executes_signed_opaque_commands_and_survives_restart(tmp_path):
    key = b"k" * 32
    database = tmp_path / "runner.db"
    root = tmp_path / "repo"
    root.mkdir()
    runner = RunnerProcess(database, key)

    registered = runner.call(
        "binding.register",
        {"label": "Repo", "path": str(root), "project_id": "project-1"},
    )
    assert registered["ok"] is True
    binding = registered["result"]
    assert "path" not in binding and "root_path" not in binding

    lease_response = runner.call(
        "lease.acquire",
        {
            "binding_id": binding["binding_id"],
            "expected_head": None,
            "owner": "run-1",
            "ttl_seconds": 60,
        },
    )
    lease = lease_response["result"]
    command = RunnerCommand.create(
        attempt_id="attempt-1",
        binding_id=binding["binding_id"],
        command_id="write-1",
        fencing_token=lease["fencing_token"],
        lease_id=lease["lease_id"],
        method="fs.write",
        params={"path": "result.txt", "text": "hello"},
        run_id="run-1",
    )
    response = runner.call("command.execute", {"envelope": sign_envelope(command.to_dict(), key)})
    assert response["ok"] is True
    assert (root / "result.txt").read_text() == "hello"
    runner.close()

    restarted = RunnerProcess(database, key)
    bindings = restarted.call("binding.list", {})
    assert bindings["result"] == [binding]
    events = restarted.call("events.pending", {"attempt_id": "attempt-1"})
    assert [event["sequence"] for event in events["result"]] == [1, 2, 3]

    revoked = restarted.call("binding.revoke", {"binding_id": binding["binding_id"]})
    assert revoked["ok"] is True
    rejected = restarted.call("command.execute", {"envelope": sign_envelope(command.to_dict(), key)})
    assert rejected["ok"] is False
    assert "revoked" in rejected["error"] or "consumed" in rejected["error"]
    restarted.close()


def test_runner_process_rejects_unsigned_or_tampered_command(tmp_path):
    key = b"k" * 32
    runner = RunnerProcess(tmp_path / "runner.db", key)

    response = runner.call(
        "command.execute",
        {"envelope": {"payload": {"method": "fs.read"}, "signature": "0" * 64}},
    )

    assert response["ok"] is False
    assert "signature" in response["error"]
    runner.close()


def test_runner_process_dispatches_optional_codex_worker_without_remote_tool_paths(tmp_path):
    class FakeCodexWorker:
        def __init__(self):
            self.calls = []

        def run(self, *, prompt, workdir, timeout_seconds):
            self.calls.append((prompt, Path(workdir), timeout_seconds))
            return {"ok": True, "version": "0.146.1"}

    key = b"k" * 32
    root = tmp_path / "repo"
    root.mkdir()
    worker = FakeCodexWorker()
    server = RunnerProcessServer(
        state_path=tmp_path / "runner.db",
        device_key=key,
        codex_worker=worker,
    )
    binding = server.dispatch(
        "binding.register",
        {"label": "Repo", "path": str(root), "project_id": "project-1"},
    )
    lease = server.dispatch(
        "lease.acquire",
        {
            "binding_id": binding["binding_id"],
            "expected_head": None,
            "owner": "run-1",
            "ttl_seconds": 60,
        },
    )
    command = RunnerCommand.create(
        attempt_id="attempt-codex",
        binding_id=binding["binding_id"],
        command_id="codex-1",
        fencing_token=lease["fencing_token"],
        lease_id=lease["lease_id"],
        method="worker.codex",
        params={"prompt": "Update the page", "timeout_seconds": 30},
        run_id="run-1",
    )

    result = server.dispatch(
        "command.execute",
        {"envelope": sign_envelope(command.to_dict(), key)},
    )

    assert result["ok"] is True
    assert result["result"]["version"] == "0.146.1"
    assert worker.calls == [("Update the page", root.resolve(), 30.0)]
    server.close()
