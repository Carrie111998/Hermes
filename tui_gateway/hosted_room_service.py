"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC


class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(self, server: ModuleType, *, db_path: Path | str | None = None) -> None:
        self.server = server
        self.db_path = Path(db_path or hosted_rooms.default_db_path())
        self._policy_lock = threading.RLock()
        self.rpc = HostedRoomServerRPC(server)
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path,
            rooms=self.bindings,
            rpc=self.rpc,
            turn_lock=self._turn_lock,
            prepare_room=self.prepare_room,
            publish_terminal=self.publish_terminal,
        )

    @property
    def root(self) -> Path:
        return self.db_path.parent

    def local_profiles(self) -> tuple[str, ...]:
        profiles = {"default"}
        profiles_dir = self.root / "profiles"
        if profiles_dir.is_dir():
            profiles.update(path.name for path in profiles_dir.iterdir() if path.is_dir())
        return tuple(sorted(profiles))

    def bindings(self) -> tuple[HostedRoomBinding, ...]:
        return tuple(
            HostedRoomBinding(
                room_id=str(room["room_id"]),
                gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            )
            for room in hosted_rooms.list_rooms(self.db_path)
        )

    @contextlib.contextmanager
    def _turn_lock(self, profile: str) -> Iterator[None]:
        from tools.bot_relay import acquire_turn_lock

        with acquire_turn_lock(self.root, profile):
            yield

    def start(self) -> None:
        self.runtime.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        return self.runtime.stop(timeout=timeout)

    def wakeup(self) -> None:
        self.runtime.wakeup()

    def _events(self, room_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = hosted_rooms.read_events(
                self.db_path,
                room_id=room_id,
                since_seq=cursor,
                limit=hosted_rooms.MAX_LOG_LIMIT,
            )
            rows = page.get("events")
            if isinstance(rows, list):
                events.extend(row for row in rows if isinstance(row, dict))
            next_cursor = int(page.get("cursor") or cursor)
            if not page.get("has_more"):
                return events
            if next_cursor <= cursor:
                raise RuntimeError("hosted room replay cursor did not advance")
            cursor = next_cursor

    def _append_plan(self, room_id: str, plan: discussion.PublicationPlan) -> None:
        for event in plan.events:
            hosted_rooms.append_event(
                self.db_path,
                **event.append_kwargs(room_id),
            )

    def _publish_terminal_tasks(
        self,
        room: Mapping[str, Any],
        events: list[dict[str, Any]],
    ) -> bool:
        changed = False
        local_profiles = self.local_profiles()
        for status in ("settled", "failed", "cancelled"):
            for task in driver.list_tasks(
                self.db_path,
                room_id=str(room["room_id"]),
                status=status,
            ):
                plan = discussion.reconstruct_task_plan(
                    room,
                    events,
                    task,
                    local_profiles=local_profiles,
                )
                publication = discussion.plan_publication(
                    room,
                    events,
                    plan,
                    status=status,
                    result=task.get("result"),
                    local_profiles=local_profiles,
                )
                before = len(events)
                self._append_plan(str(room["room_id"]), publication)
                events = self._events(str(room["room_id"]))
                changed = changed or len(events) > before
        return changed

    def _append_room_status(
        self,
        room: Mapping[str, Any],
        decision: discussion.DiscussionDecision,
    ) -> None:
        if decision.discussion_event_id is None:
            return
        hosted_rooms.append_event(
            self.db_path,
            room_id=str(room["room_id"]),
            event_id=f"dactivity:{decision.discussion_event_id}:{decision.reason}",
            kind="room.activity",
            actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
            payload={
                "status": decision.status,
                "reason_code": decision.reason,
                "thread_id": decision.thread_id,
                "discussion_event_id": decision.discussion_event_id,
            },
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )

    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
            events = self._events(binding.room_id)
            if self._publish_terminal_tasks(room, events):
                events = self._events(binding.room_id)
            decision = discussion.plan_next_task(
                room,
                events,
                local_profiles=self.local_profiles(),
            )
            if decision.status == "task" and decision.task is not None:
                driver.admit_task(
                    self.db_path,
                    decision.task.identity,
                    payload=decision.task.payload,
                    clock=time.time,
                )
                # A stop can race the policy read from another process. Re-read
                # after admission and cancel before the runtime can execute a
                # task whose source event is now behind the room stop fence.
                fresh_events = self._events(binding.room_id)
                stopped_through_seq = max(
                    (
                        int(event["seq"])
                        for event in fresh_events
                        if event.get("kind") == "room.stop_requested"
                    ),
                    default=0,
                )
                if (
                    decision.source_event_seq is not None
                    and decision.source_event_seq < stopped_through_seq
                ):
                    self.runtime.cancel(
                        decision.task.identity,
                        cancel_id=f"stop-fence:{stopped_through_seq}",
                    )
            elif decision.status in {"settled", "bounded"}:
                self._append_room_status(room, decision)

    def publish_terminal(
        self,
        binding: HostedRoomBinding,
        _task: Mapping[str, Any],
    ) -> None:
        self.prepare_room(binding)
        self.runtime.wakeup()

    def create_room(self, *, room_id: str, name: str, members: Any) -> dict[str, Any]:
        normalized = discussion.validate_roster(
            members,
            local_profiles=self.local_profiles(),
        )
        room = hosted_rooms.create_room(
            self.db_path,
            room_id=room_id,
            name=name,
            members=[
                {
                    "member_id": member.member_id,
                    "profile": member.profile,
                    "handle": member.handle,
                    **(
                        {"display_name": member.display_name}
                        if member.display_name
                        else {}
                    ),
                }
                for member in normalized
            ],
            authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        )
        self.runtime.wakeup()
        return room

    def send(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        normalized = discussion.validate_user_payload(payload)
        event = hosted_rooms.append_event(
            self.db_path,
            room_id=room_id,
            event_id=event_id,
            kind="message.user",
            actor={"kind": "user", "id": "desktop"},
            payload=normalized,
        )
        binding = next(
            (candidate for candidate in self.bindings() if candidate.room_id == room_id),
            None,
        )
        if binding is None:
            raise hosted_rooms.RoomNotFoundError("hosted room not found")
        self.prepare_room(binding)
        self.runtime.wakeup()
        return event

    def stop_room(self, room_id: str, *, cancel_id: str) -> int:
        hosted_rooms.request_room_stop(
            self.db_path,
            room_id=room_id,
            cancel_id=cancel_id,
        )
        cancelled = 0
        with self._policy_lock:
            for status in ("queued", "running", "indeterminate"):
                for task in driver.list_tasks(
                    self.db_path,
                    room_id=room_id,
                    status=status,
                ):
                    self.runtime.cancel(task["identity"], cancel_id=cancel_id)
                    cancelled += 1
        self.runtime.wakeup()
        return cancelled

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = self.runtime.status()
        if room_id is None:
            return runtime
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        return {
            "running": runtime["running"],
            "working": bool(
                counts.get("running")
                or counts.get("queued")
                or counts.get("stopping")
            ),
            "blocked": room_id in runtime["blocked_rooms"] or bool(counts.get("indeterminate")),
            "counts": dict(counts),
        }
