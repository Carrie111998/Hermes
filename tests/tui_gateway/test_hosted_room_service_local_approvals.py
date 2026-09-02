"""Local approval identity and session routing for hosted Group Chats."""

from __future__ import annotations

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_room_messaging_approvals as approvals
from tests.tui_gateway.test_hosted_room_messaging_approvals import _create_local_room
from tests.tui_gateway.test_hosted_room_service import _FakeRPC, _server
from tui_gateway.hosted_room_driver import room_session_title
from tui_gateway.hosted_room_service import HostedRoomService


class _ApprovalRPC(_FakeRPC):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions = []
        self.resumes = []

    def resolve_exact(self, *, profile, title, source):
        self.resolutions.append((profile, title, source))
        return super().resolve_exact(profile=profile, title=title, source=source)

    def resume(self, *, profile, session_id, source):
        self.resumes.append((profile, session_id, source))
        return super().resume(profile=profile, session_id=session_id, source=source)

    def approve(self, *, session_id, request_id, choice):
        self.approvals.append({
            "session_id": session_id,
            "request_id": request_id,
            "choice": choice,
        })
        return {"resolved": 1}


@pytest.fixture
def local_approval_task(tmp_path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _ApprovalRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    _create_local_room(service)
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        db,
        room_id=binding.room_id,
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation=service.runtime.process_generation,
        ttl_seconds=30,
        clock=service.runtime.clock,
    )
    service.runtime._leases[binding.room_id] = lease
    driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=task["cancel_generation"],
        clock=service.runtime.clock,
    )
    task = driver.get_task(db, task["identity"])
    assert task["status"] == "running"
    assert task["payload"]["target_member_id"] == "ops"
    session = service.runtime._resolve_or_create(rpc, "ops", binding.room_id)
    assert rpc.resolutions == [("ops", room_session_title("room-1"), "bot_room")]
    rpc.resolutions.clear()
    return service, rpc, binding, task, session["session_id"]


def _report_approval(service, binding, task, session_id, request_id):
    service.runtime._report_pending_action(
        binding,
        task,
        session_id=session_id,
        info={
            "pending_approval": {
                "request_id": request_id,
                "description": "Run focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "always", "deny"],
            }
        },
    )
    pending = approvals.list_pending_approvals(service.db_path, room_id="room-1")
    assert len(pending) == 1
    assert pending[0]["authority_gateway_id"] == binding.gateway_id
    assert pending[0]["authority_epoch"] == binding.authority_epoch
    assert pending[0]["observer_generation"] == service.runtime.process_generation
    assert pending[0]["observer_lease_generation"] == (
        service.runtime._leases[binding.room_id].lease_generation
    )
    assert pending[0]["task_id"] == task["identity"].task_id
    assert pending[0]["execution_generation"] == task["execution_generation"]
    assert pending[0]["session_id"] == session_id
    assert pending[0]["request_id"] == request_id
    return pending


@pytest.mark.parametrize(
    "mismatched_field", ["task_id", "execution_generation", "request_id"]
)
def test_local_pending_approval_requires_exact_task_generation_and_request(
    local_approval_task, mismatched_field
):
    service, rpc, binding, task, session_id = local_approval_task
    pending = _report_approval(service, binding, task, session_id, "approval-1")
    actions = service.status("room-1")["pending_actions"]
    assert len(actions) == 1
    assert actions[0]["member_id"] == "ops"
    assert actions[0]["approval"]["choices"] == ["once", "deny"]
    request = {
        "member_id": "ops",
        "task_id": task["identity"].task_id,
        "execution_generation": task["execution_generation"],
        "choice": "once",
        "request_id": "approval-1",
    }
    mismatches = {
        "task_id": "wrong-task",
        "execution_generation": task["execution_generation"] + 1,
        "request_id": "wrong-request",
    }
    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1", **{**request, mismatched_field: mismatches[mismatched_field]}
        )

    assert rpc.approvals == []
    assert rpc.resumes == []
    assert service.status("room-1")["pending_actions"] == actions
    assert (
        approvals.list_pending_approvals(service.db_path, room_id="room-1") == pending
    )
    assert service.approve_room_task("room-1", **request) == {"resolved": 1}
    assert rpc.approvals == [
        {"session_id": session_id, "request_id": "approval-1", "choice": "once"}
    ]
    assert service.status("room-1")["pending_actions"] == []
    assert approvals.list_pending_approvals(service.db_path, room_id="room-1") == []


def test_local_room_approval_uses_the_exact_hidden_session(local_approval_task):
    service, rpc, binding, task, session_id = local_approval_task
    _report_approval(service, binding, task, session_id, "approval-local-1")
    # A later title lookup must not redirect an already-observed approval.
    rpc.sessions[("ops", room_session_title("room-1"))] = {
        "session_id": "replacement-session",
        "title": room_session_title("room-1"),
    }

    assert service.approve_room_task(
        "room-1",
        member_id="ops",
        task_id=task["identity"].task_id,
        execution_generation=task["execution_generation"],
        choice="once",
        request_id="approval-local-1",
    ) == {"resolved": 1}
    assert rpc.resolutions == []
    assert rpc.resumes == [("ops", session_id, "bot_room")]
    assert rpc.approvals == [
        {
            "session_id": session_id,
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []
    assert approvals.list_pending_approvals(service.db_path, room_id="room-1") == []


def test_stale_local_approval_cannot_resolve_replacement_request(local_approval_task):
    service, rpc, binding, task, session_id = local_approval_task
    _report_approval(service, binding, task, session_id, "approval-A")
    pending = _report_approval(service, binding, task, session_id, "approval-B")

    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="ops",
            task_id=task["identity"].task_id,
            execution_generation=task["execution_generation"],
            choice="once",
            request_id="approval-A",
        )

    assert rpc.approvals == []
    assert rpc.resumes == []
    assert service.status("room-1")["pending_actions"][0]["request_id"] == (
        "approval-B"
    )
    assert (
        approvals.list_pending_approvals(service.db_path, room_id="room-1") == pending
    )
    assert service.approve_room_task(
        "room-1",
        member_id="ops",
        task_id=task["identity"].task_id,
        execution_generation=task["execution_generation"],
        choice="deny",
        request_id="approval-B",
    ) == {"resolved": 1}
    assert rpc.approvals == [
        {"session_id": session_id, "request_id": "approval-B", "choice": "deny"}
    ]
    assert service.status("room-1")["pending_actions"] == []
    assert approvals.list_pending_approvals(service.db_path, room_id="room-1") == []
