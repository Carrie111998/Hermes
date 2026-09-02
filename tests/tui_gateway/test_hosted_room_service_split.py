"""Runtime contracts across the hosted-room coordinator/artifact split."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway.hosted_room_attachments import AttachmentNotFoundError
from tui_gateway.hosted_room_service import HostedRoomService


def _service(db_path):
    server = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())
    return HostedRoomService(server, db_path=db_path, artifact_clock=lambda: 10.0)


@pytest.fixture
def service(tmp_path):
    (tmp_path / "profiles" / "ops").mkdir(parents=True)
    instance = _service(tmp_path / "state.db")
    instance.create_room(
        room_id="room-split",
        name="Artifact split",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    return instance


def test_coordinator_send_reaches_bound_artifact_loader(service):
    data = b"Review notes"
    stored = service.put_attachment(
        room_id="room-split",
        upload_id="notes-upload",
        kind="file",
        name="notes.txt",
        mime="text/plain",
        data=data,
    )
    manifest = {
        key: stored[key] for key in ("attachment_id", "kind", "name", "size", "mime")
    }
    actor = {"kind": "user", "id": "trusted-test-sender"}
    event = service.send_server_owned(
        room_id="room-split",
        event_id="notes-event",
        payload={"text": "Review these notes", "attachments": [manifest]},
        actor=actor,
    )
    assert event["actor"] == actor

    binding = next(item for item in service.bindings() if item.room_id == "room-split")
    task = {
        "payload": {
            "target_member_id": "ops",
            "target_profile": "ops",
            "attachments": event["payload"]["attachments"],
        }
    }
    assert list(service.runtime.attachment_loader(binding, task)) == [(manifest, data)]
    assert (
        service.read_attachment(
            room_id="room-split",
            attachment_id=stored["attachment_id"],
            recipient_member_id=None,
            event_id="notes-event",
            viewer=True,
        ).data
        == data
    )
    with pytest.raises(AttachmentNotFoundError):
        service.read_attachment(
            room_id="room-split",
            attachment_id=stored["attachment_id"],
            recipient_member_id="outsider",
            event_id="notes-event",
        )

    task["payload"]["attachments"] = [{**manifest, "name": "changed.txt"}]
    with pytest.raises(RuntimeError, match="metadata changed"):
        list(service.runtime.attachment_loader(binding, task))


def test_artifact_retries_survive_service_restart_and_unblock_only_the_member(service):
    tasks = [
        {
            "identity": driver.TaskIdentity(
                "room-split", f"task-{member_id}", "thread-split", "turn-split"
            ),
            "execution_generation": 1,
            "payload": {"target_member_id": member_id},
        }
        for member_id in ("default", "ops")
    ]
    for task in tasks:
        service._defer_artifact_retry(
            task, RuntimeError("route blocked"), permanent=True
        )

    recovered = _service(service.db_path)
    assert recovered._artifact_retry_keys("room-split") == {
        ("room-split", task["identity"].task_id, 1) for task in tasks
    }
    assert all(not recovered._artifact_retry_due(task) for task in tasks)
    recovered._unblock_artifact_retries("room-split", "default")
    assert recovered._artifact_retry_due(tasks[0])
    assert not recovered._artifact_retry_due(tasks[1])
    recovered._clear_artifact_retry(tasks[0])
    assert recovered._artifact_retry_keys("room-split") == {
        ("room-split", tasks[1]["identity"].task_id, 1)
    }
    recovered.retire_room_artifacts("room-split")
    assert recovered._artifact_retry_keys("room-split") == set()


@pytest.mark.parametrize(
    "module_path",
    ["tui_gateway.hosted_room_service", "tui_gateway.hosted_room_artifact_service"],
)
def test_poppler_probe_patch_reaches_extracted_upload(
    service, monkeypatch, module_path
):
    probe = Mock(return_value=None)
    monkeypatch.setattr(f"{module_path}.shutil.which", probe)

    with pytest.raises(hosted_rooms.HostedRoomError, match="Poppler"):
        service.put_attachment(
            room_id="room-split",
            upload_id="pdf-upload",
            kind="pdf",
            name="notes.pdf",
            mime="application/pdf",
            data=b"%PDF-1.7\n%%EOF\n",
        )
    probe.assert_called_once_with("pdftoppm")
