"""Exercise scoped cross-host dispatch, home restart, replay, and revoke."""

import os
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from gateway import hosted_rooms
from gateway import hosted_room_driver
from gateway.hosted_room_peer import GatewayRoomCatalog
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
from tui_gateway.hosted_room_peer_transport import PeerMemberRoute
from tui_gateway.hosted_room_service import HostedRoomService


class LocalRPC:
    def resolve_exact(self, **_kwargs):
        return None

    def create(self, **_kwargs):
        return {"session_id": "local-session"}

    def resume(self, **kwargs):
        return {"session_id": kwargs["session_id"]}

    def submit(self, **kwargs):
        kwargs["on_terminal"]({"status": "settled", "text": "LOCAL_UAT_REPLY"})
        return {"accepted": True}

    def history(self, **_kwargs):
        return []

    def info(self, **_kwargs):
        return {"active": False, "task_id": None}

    def interrupt(self, **_kwargs):
        return {"interrupted": True}


def server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("UAT condition timed out")


db = Path("/opt/data/state.db")
room_id = f"room-uat-{uuid.uuid4().hex[:12]}"
target_url = os.environ["TARGET_URL"].rstrip("/")
target_key = os.environ["TARGET_API_KEY"]
home_install = hosted_rooms.local_authority_gateway_id()
admin = PeerRunsHTTPClient(base_url=target_url, api_key=target_key)
invitation = admin.issue_invitation(
    room_id=room_id,
    home_install_id=home_install,
    authority_gateway_id=home_install,
    authority_epoch=1,
    member_id="member-peer",
    grant_id=f"grant-{room_id}",
)
catalog = GatewayRoomCatalog.from_mapping(invitation["catalog"])
scoped = PeerRunsHTTPClient(base_url=target_url, api_key="", receipt_db_path=db)
probe = scoped.probe(grant=invitation["grant"])
assert probe["catalog"] == invitation["catalog"]
route = PeerMemberRoute(
    home_install_id=home_install,
    member_id="member-peer",
    target_install_id=catalog.installation_id,
    target_profile=invitation["target_profile"],
    capability_digest=catalog.catalog_digest,
    cancellation_scope_id=f"cancel-{room_id}",
    trace_id=f"trace-{room_id}",
    grant=invitation["grant"],
)


def build_service():
    service = HostedRoomService(server(), db_path=db)
    service.rpc = LocalRPC()
    service.runtime.rpc = service.rpc
    service.runtime.lease_ttl_seconds = 1
    service.runtime.poll_interval_seconds = 0.05
    service.local_profiles = lambda: ("default",)
    return service


home = build_service()
home.register_peer_route(
    room_id=room_id,
    member_id="member-peer",
    route=route,
    client=scoped,
    target_url=target_url,
    catalog=catalog,
)
home.create_room(
    room_id=room_id,
    name="Autonomous room UAT",
    members=[
        {"member_id": "local", "profile": "default", "handle": "local"},
        {
            "member_id": "member-peer",
            "profile": "default",
            "handle": "reviewer",
            "target": {
                "kind": "peer",
                "peer_id": catalog.installation_id,
                "installation_id": catalog.installation_id,
                "profile": "default",
                "capability_digest": catalog.catalog_digest,
            },
        },
    ],
)
home.start()
home.send(
    room_id=room_id,
    event_id="user-uat-1",
    payload={"text": "@reviewer verify continuity", "thread_id": "thread-uat"},
)
try:
    wait_for(
        lambda: bool(
            hosted_rooms.list_remote_run_receipts(db, room_id=room_id)
        )
    )
except RuntimeError:
    print("DISPATCH_UAT_TASKS", hosted_room_driver.list_tasks(db, room_id=room_id))
    print("DISPATCH_UAT_EVENTS", home._events(room_id))
    print("DISPATCH_UAT_RUNTIME", home.runtime.status())
    raise
home.stop(timeout=0.05)

restarted = build_service()
restarted.start()
wait_for(
    lambda: any(
        event["kind"] == "message.member"
        and event["payload"].get("text") == "REMOTE_UAT_REPLY"
        for event in restarted._events(room_id)
    )
)
events = restarted._events(room_id)
replies = [event for event in events if event["kind"] == "message.member"]
assert len(replies) == 1
restarted.send(
    room_id=room_id,
    event_id="user-uat-stop",
    payload={"text": "@reviewer start another turn", "thread_id": "thread-stop"},
)
try:
    wait_for(
        lambda: any(
            task["status"] == "running"
            and task["identity"].thread_id == "thread-stop"
            for task in hosted_room_driver.list_tasks(db, room_id=room_id)
        ),
        timeout=3,
    )
except RuntimeError:
    print("STOP_UAT_TASKS", hosted_room_driver.list_tasks(db, room_id=room_id))
    print("STOP_UAT_EVENTS", restarted._events(room_id))
    print("STOP_UAT_RUNTIME", restarted.runtime.status())
    raise
assert restarted.stop_room(
    room_id,
    cancel_id="uat-stop",
    require_acknowledged=False,
) == 1
wait_for(
    lambda: any(
        task["status"] == "cancelled"
        for task in hosted_room_driver.list_tasks(db, room_id=room_id)
    )
)
cancelled_task = next(
    task
    for task in hosted_room_driver.list_tasks(db, room_id=room_id)
    if task["status"] == "cancelled"
)
receipt = hosted_rooms.remote_run_receipt(
    db,
    task_id=cancelled_task["identity"].task_id,
    execution_generation=cancelled_task["execution_generation"],
)
assert receipt is not None
scoped.bind_observation(
    task_id=cancelled_task["identity"].task_id,
    execution_generation=cancelled_task["execution_generation"],
)
target_status = scoped.status(
    room_id=room_id,
    profile=receipt["target_profile"],
    session_id=receipt["session_id"],
    grant=invitation["grant"],
)
assert target_status["status"] in {"cancelled", "interrupted"}
# A second acknowledged Stop sees no remaining live work; disband can now
# revoke the route without racing an executor that is still running.
assert restarted.stop_room(
    room_id,
    cancel_id="uat-stop-confirm",
    require_acknowledged=True,
) == 0
assert restarted.stop(timeout=2)
assert restarted.revoke_room_routes(room_id) == 1
print(
    "UAT_OK remote_reply=1 restart_recovered=1 "
    "stop_acknowledged=1 target_terminal=1 scoped_route_revoked=1"
)
