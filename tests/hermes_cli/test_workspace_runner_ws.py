import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from hermes_cli.runner_protocol import RunnerEvent, sign_envelope, verify_envelope
from hermes_cli.web_routers import workspace_runners


def test_runner_enrollment_reconnect_binding_sync_and_revocation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace_runners.reset_workspace_runner_state_for_tests()
    app = FastAPI()
    app.include_router(workspace_runners.router)

    with TestClient(app) as client:
        enrollment = client.post(
            "/api/workspace/runners/enroll",
            json={"label": "Studio Mac"},
        ).json()

        with client.websocket_connect(
            f"/api/workspace/runners/connect?runner_id={enrollment['runner_id']}",
            headers={"Authorization": f"Enrollment {enrollment['enrollment_token']}"},
        ) as socket:
            enrolled = socket.receive_json()
            assert enrolled["type"] == "enrolled"
            assert enrolled["device_token"]
            assert enrolled["command_key"]
            socket.send_json(
                {
                    "type": "hello",
                    "bindings": [
                        {
                            "binding_id": "binding-1",
                            "label": "Launch",
                            "project_id": "project-1",
                            "revoked": False,
                        }
                    ],
                }
            )
            assert socket.receive_json()["type"] == "hello.ack"
            socket.send_json({"type": "heartbeat"})
            assert socket.receive_json()["type"] == "heartbeat.ack"

        runners = client.get("/api/workspace/runners").json()["runners"]
        assert runners[0]["status"] == "online"
        assert runners[0]["bindings"][0]["binding_id"] == "binding-1"
        assert "path" not in repr(runners)

        with client.websocket_connect(
            f"/api/workspace/runners/connect?runner_id={enrollment['runner_id']}",
            headers={"Authorization": f"Runner {enrolled['device_token']}"},
        ) as socket:
            assert socket.receive_json()["type"] == "connected"

        revoked = client.post(
            f"/api/workspace/runners/{enrollment['runner_id']}/revoke"
        )
        assert revoked.status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                f"/api/workspace/runners/connect?runner_id={enrollment['runner_id']}",
                headers={"Authorization": f"Runner {enrolled['device_token']}"},
            ):
                pass
        assert closed.value.code == 4401

    workspace_runners.reset_workspace_runner_state_for_tests()


def test_dashboard_dispatches_signed_command_and_persists_result(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace_runners.reset_workspace_runner_state_for_tests()
    app = FastAPI()
    app.include_router(workspace_runners.router)

    with TestClient(app) as client:
        enrollment = client.post(
            "/api/workspace/runners/enroll",
            json={"label": "Studio Mac"},
        ).json()
        with client.websocket_connect(
            f"/api/workspace/runners/connect?runner_id={enrollment['runner_id']}",
            headers={"Authorization": f"Enrollment {enrollment['enrollment_token']}"},
        ) as socket:
            enrolled = socket.receive_json()
            key = base64.urlsafe_b64decode(enrolled["command_key"])
            socket.send_json(
                {
                    "type": "hello",
                    "bindings": [
                        {
                            "binding_id": "binding-1",
                            "label": "Launch",
                            "project_id": "project-1",
                            "revoked": False,
                        }
                    ],
                }
            )
            assert socket.receive_json()["type"] == "hello.ack"

            with ThreadPoolExecutor(max_workers=1) as executor:
                submitted = executor.submit(
                    client.post,
                    f"/api/workspace/runners/{enrollment['runner_id']}/commands",
                    json={
                        "attempt_id": "attempt-1",
                        "binding_id": "binding-1",
                        "method": "fs.write",
                        "params": {"path": "notes/result.txt", "text": "ok"},
                        "run_id": "run-1",
                    },
                )
                try:
                    premature = submitted.result(timeout=0.1)
                except FutureTimeoutError:
                    pass
                else:
                    assert premature.status_code == 200

                acquire = socket.receive_json()
                acquire_payload = verify_envelope(acquire["envelope"], key)
                assert acquire["type"] == "control"
                assert acquire_payload["method"] == "lease.acquire"
                socket.send_json(
                    {
                        "envelope": sign_envelope(
                            {
                                "ok": True,
                                "request_id": acquire["request_id"],
                                "result": {
                                    "binding_id": "binding-1",
                                    "expected_head": None,
                                    "expires_at": 9999999999,
                                    "fencing_token": 1,
                                    "lease_id": "lease-1",
                                    "owner": "run-1",
                                },
                            },
                            key,
                        ),
                        "request_id": acquire["request_id"],
                        "type": "response",
                    }
                )

                command = socket.receive_json()
                command_payload = verify_envelope(command["envelope"], key)
                assert command["type"] == "command"
                assert command_payload["method"] == "fs.write"
                assert command_payload["params"]["path"] == "notes/result.txt"
                socket.send_json(
                    {
                        "command_id": command["command_id"],
                        "envelope": sign_envelope(
                            {
                                "accepted_at": 123.0,
                                "command_id": command["command_id"],
                                "state": "accepted",
                            },
                            key,
                        ),
                        "type": "command.ack",
                    }
                )
                registry = workspace_runners.get_workspace_runner_registry()
                deadline = time.monotonic() + 1
                acknowledged = registry.command_status(
                    enrollment["runner_id"], command["command_id"]
                )
                while acknowledged["state"] != "acknowledged" and time.monotonic() < deadline:
                    time.sleep(0.01)
                    acknowledged = registry.command_status(
                        enrollment["runner_id"], command["command_id"]
                    )
                assert acknowledged["state"] == "acknowledged"
                assert acknowledged["sent_at"] is not None
                assert acknowledged["acknowledged_at"] is not None

                event = RunnerEvent.create(
                    attempt_id="attempt-1",
                    event_type="run.accepted",
                    payload={"command_id": command["command_id"]},
                    run_id="run-1",
                    sequence=1,
                )
                socket.send_json(
                    {
                        "envelope": sign_envelope({"events": [event.to_dict()]}, key),
                        "type": "event.batch",
                    }
                )
                event_ack = socket.receive_json()
                assert event_ack["type"] == "event.ack"
                assert verify_envelope(event_ack["envelope"], key)["event_ids"] == [
                    event.event_id
                ]
                stored_events = workspace_runners.get_workspace_runner_registry().list_events(
                    enrollment["runner_id"], "attempt-1"
                )
                assert stored_events == [event.to_dict()]
                socket.send_json(
                    {
                        "command_id": command["command_id"],
                        "envelope": sign_envelope(
                            {
                                "command_id": command["command_id"],
                                "ok": True,
                                "replayed": False,
                                "result": {"written": True},
                                "state": "completed",
                            },
                            key,
                        ),
                        "type": "command.result",
                    }
                )

                release = socket.receive_json()
                release_payload = verify_envelope(release["envelope"], key)
                assert release_payload["method"] == "lease.release"
                socket.send_json(
                    {
                        "envelope": sign_envelope(
                            {
                                "ok": True,
                                "request_id": release["request_id"],
                                "result": {"released": True},
                            },
                            key,
                        ),
                        "request_id": release["request_id"],
                        "type": "response",
                    }
                )
                response = submitted.result(timeout=5)

            assert response.status_code == 200
            body = response.json()
            assert body["state"] == "completed"
            assert body["result"]["command_id"] == body["command_id"]
            assert body["result"]["ok"] is True
            assert body["result"]["result"] == {"written": True}
            status = client.get(
                f"/api/workspace/runners/{enrollment['runner_id']}/commands/{body['command_id']}"
            ).json()
            assert status["state"] == "completed"

    workspace_runners.reset_workspace_runner_state_for_tests()
