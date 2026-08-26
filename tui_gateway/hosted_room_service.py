"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_room_links
from gateway import hosted_rooms
from gateway.hosted_room_peer import GatewayRoomCatalog
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
from tui_gateway.hosted_room_peer_transport import (
    HostedRoomPeerClient,
    PeerHostedRoomTransport,
    PeerMemberRoute,
)


class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(
        self,
        server: ModuleType,
        *,
        db_path: Path | str | None = None,
        peer_routes: Mapping[tuple[str, str], PeerMemberRoute] | None = None,
        peer_clients: Mapping[str, HostedRoomPeerClient] | None = None,
    ) -> None:
        self.server = server
        self.db_path = Path(db_path or hosted_rooms.default_db_path())
        self._policy_lock = threading.RLock()
        self.rpc = HostedRoomServerRPC(server)
        self._link_load_error = None
        self._peer_route_status: dict[tuple[str, str], str] = {}
        self.peer_routes = {}
        self.peer_clients = {}
        try:
            for stored in hosted_room_links.load_room_links(self.db_path):
                client = PeerRunsHTTPClient(
                    base_url=stored.target_url,
                    api_key="",
                    receipt_db_path=self.db_path,
                )
                route = PeerMemberRoute(
                    home_install_id=hosted_rooms.local_authority_gateway_id(),
                    member_id=stored.member_id,
                    target_install_id=stored.catalog.installation_id,
                    target_profile=stored.target_profile,
                    capability_digest=stored.catalog.catalog_digest,
                    cancellation_scope_id=stored.cancellation_scope_id,
                    trace_id=stored.trace_id,
                    grant=stored.grant,
                )
                self.peer_routes[(stored.room_id, stored.member_id)] = route
                self.peer_clients[stored.catalog.installation_id] = client
                self._peer_route_status[(stored.room_id, stored.member_id)] = (
                    stored.status
                )
        except Exception as exc:
            self._link_load_error = str(exc)
        self.peer_routes.update(peer_routes or {})
        self.peer_clients.update(peer_clients or {})
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path,
            rooms=self.bindings,
            rpc=self.rpc,
            transport_resolver=self._resolve_member_transport,
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
            profiles.update(
                path.name for path in profiles_dir.iterdir() if path.is_dir()
            )
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

    def register_peer_route(
        self,
        *,
        room_id: str,
        member_id: str,
        route: PeerMemberRoute,
        client: HostedRoomPeerClient,
        target_url: str | None = None,
        catalog: GatewayRoomCatalog | None = None,
    ) -> None:
        """Register one verified route and optionally persist its scoped grant."""
        bind_store = getattr(client, "bind_receipt_store", None)
        if callable(bind_store):
            bind_store(self.db_path)
        if target_url is not None and catalog is not None:
            hosted_room_links.save_room_link(
                self.db_path,
                hosted_room_links.make_stored_link(
                    room_id=room_id,
                    member_id=member_id,
                    target_url=target_url,
                    target_profile=route.target_profile,
                    grant=route.grant,
                    catalog=catalog,
                    cancellation_scope_id=route.cancellation_scope_id,
                    trace_id=route.trace_id,
                ),
            )
        # Persistence is the publication boundary. A failed disk write must
        # never leave a process-local route that disappears after restart.
        with self._policy_lock:
            self.peer_routes[(room_id, member_id)] = route
            self.peer_clients[route.target_install_id] = client
            self._peer_route_status[(room_id, member_id)] = "ready"
        self.runtime.wakeup()

    def revoke_room_routes(self, room_id: str) -> int:
        """Revoke and forget every scoped peer route for one room.

        The remote revocation is the boundary: if a target is unreachable the
        room remains intact and the user may retry rather than receiving a
        false successful disband while a grant is still live.
        """
        with self._policy_lock:
            routes = [
                (key, route)
                for key, route in self.peer_routes.items()
                if key[0] == room_id
            ]
        for _key, route in routes:
            client = self.peer_clients.get(route.target_install_id)
            revoke = getattr(client, "revoke_grant", None)
            if not callable(revoke):
                raise RuntimeError("peer room grant cannot be revoked safely")
            revoke(grant=route.grant)

        hosted_rooms.delete_room_link_records(self.db_path, room_id=room_id)
        with self._policy_lock:
            for key, route in routes:
                self.peer_routes.pop(key, None)
                self._peer_route_status.pop(key, None)
                if not any(
                    candidate.target_install_id == route.target_install_id
                    for candidate in self.peer_routes.values()
                ):
                    self.peer_clients.pop(route.target_install_id, None)
        return len(routes)

    def _resolve_member_transport(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
    ):
        payload = task.get("payload", {})
        member_id = str(
            payload.get("target_member_id") or payload.get("target_profile") or ""
        )
        route = self.peer_routes.get((binding.room_id, member_id))
        if route is None:
            if self._member_is_peer(binding.room_id, member_id):
                raise RuntimeError("peer room route is unavailable")
            return self.rpc
        client = self.peer_clients.get(route.target_install_id)
        if client is None:
            raise RuntimeError("peer room client is unavailable")
        return PeerHostedRoomTransport(
            binding=binding,
            route=route,
            client=_RouteStatusPeerClient(
                client,
                on_ready=lambda: self._set_route_status(
                    binding.room_id, member_id, "ready"
                ),
                on_reauthorization=lambda: self._set_route_status(
                    binding.room_id, member_id, "needs_reauthorization"
                ),
                on_refreshed=lambda grant: self._rotate_route_grant(
                    binding.room_id, member_id, grant
                ),
            ),
            source_event_seq=int(payload.get("source_event_seq") or 0),
            task_id=getattr(task.get("identity"), "task_id", None),
            execution_generation=int(task.get("execution_generation") or 0),
        )

    def _member_is_peer(self, room_id: str, member_id: str) -> bool:
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        for member in room.get("members") or []:
            if not isinstance(member, Mapping):
                continue
            if str(member.get("member_id") or member.get("profile") or "") != member_id:
                continue
            target = member.get("target")
            return isinstance(target, Mapping) and target.get("kind") == "peer"
        return False

    def _set_route_status(self, room_id: str, member_id: str, status: str) -> None:
        key = (room_id, member_id)
        with self._policy_lock:
            if self._peer_route_status.get(key) == status:
                return
            self._peer_route_status[key] = status
        hosted_room_links.mark_room_link_status(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
            status=status,
        )

    def _rotate_route_grant(
        self, room_id: str, member_id: str, grant: str
    ) -> None:
        """Persist a target-refreshed scoped grant before publishing it live."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            raise RuntimeError("peer room route is unavailable")
        stored = next(
            (
                link
                for link in hosted_room_links.load_room_links(self.db_path)
                if (link.room_id, link.member_id) == key
            ),
            None,
        )
        if stored is None:
            raise RuntimeError("peer room route cannot be renewed before persistence")
        rotated_route = replace(route, grant=grant)
        hosted_room_links.save_room_link(
            self.db_path,
            hosted_room_links.make_stored_link(
                room_id=room_id,
                member_id=member_id,
                target_url=stored.target_url,
                target_profile=stored.target_profile,
                grant=grant,
                catalog=stored.catalog,
                cancellation_scope_id=stored.cancellation_scope_id,
                trace_id=stored.trace_id,
            ),
        )
        with self._policy_lock:
            self.peer_routes[key] = rotated_route
            self._peer_route_status[key] = "ready"

    def _route_statuses(self, room_id: str | None = None) -> list[dict[str, str]]:
        with self._policy_lock:
            rows = [
                {
                    "room_id": key[0],
                    "member_id": key[1],
                    "status": status,
                }
                for key, status in self._peer_route_status.items()
                if room_id is None or key[0] == room_id
            ]
        return sorted(rows, key=lambda row: (row["room_id"], row["member_id"]))

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
                    "target": dict(member.target or {}),
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
            (
                candidate
                for candidate in self.bindings()
                if candidate.room_id == room_id
            ),
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
        runtime = {**runtime, "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime = {**runtime, "link_load_error": self._link_load_error}
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
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate")),
            "counts": dict(counts),
            "peer_routes": self._route_statuses(room_id),
        }


class _RouteStatusPeerClient:
    """Classify scoped-auth failures without exposing route credentials."""

    def __init__(
        self, client, *, on_ready, on_reauthorization, on_refreshed
    ) -> None:
        self._client = client
        self._on_ready = on_ready
        self._on_reauthorization = on_reauthorization
        self._on_refreshed = on_refreshed

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
            if name == "dispatch" and "grant" in kwargs:
                from gateway.hosted_room_peer import (
                    room_grant_needs_dispatch_refresh,
                )

                grant = kwargs["grant"]
                if room_grant_needs_dispatch_refresh(grant):
                    refresh = getattr(self._client, "refresh_grant", None)
                    if callable(refresh):
                        try:
                            refreshed = refresh(grant=grant)
                        except Exception as exc:
                            if room_grant_needs_dispatch_refresh(
                                grant, leeway_seconds=0
                            ):
                                self._on_reauthorization()
                                raise
                        else:
                            replacement = str(refreshed.get("grant") or "")
                            if not replacement:
                                raise RuntimeError(
                                    "peer returned no refreshed room grant"
                                )
                            self._on_refreshed(replacement)
                            kwargs = {**kwargs, "grant": replacement}
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                if bool(getattr(exc, "needs_reauthorization", False)):
                    self._on_reauthorization()
                raise
            self._on_ready()
            return result

        return tracked
