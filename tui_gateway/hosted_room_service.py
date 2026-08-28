"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_room_links
from gateway import hosted_rooms
from gateway.hosted_room_attachments import (
    AttachmentData,
    HostedRoomAttachmentStore,
)
from gateway.hosted_room_artifacts import (
    RoomArtifactOutbox,
    RoomArtifactScope,
    validate_terminal_artifact_manifest,
)
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    PROTOCOL_VERSION,
    attachment_manifest_digest,
)
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
from tui_gateway.hosted_room_peer_transport import (
    HostedRoomPeerClient,
    PeerHostedRoomTransport,
    PeerMemberRoute,
)


class _ArtifactRetirementBlocked(RuntimeError):
    """Private peer bytes need a repaired scoped route before retirement."""


class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(
        self,
        server: ModuleType,
        *,
        db_path: Path | str | None = None,
        peer_routes: Mapping[tuple[str, str], PeerMemberRoute] | None = None,
        peer_clients: Mapping[Any, HostedRoomPeerClient] | None = None,
        artifact_clock: Callable[[], float] = time.time,
        artifact_retry_min_seconds: float = 1.0,
        artifact_retry_max_seconds: float = 60.0,
    ) -> None:
        self.server = server
        self.db_path = Path(db_path or hosted_rooms.default_db_path())
        self._policy_lock = threading.RLock()
        self.attachments = HostedRoomAttachmentStore(self.db_path)
        self.rpc = HostedRoomServerRPC(server)
        self._link_load_error = None
        self._peer_route_status: dict[tuple[str, str], str] = {}
        self._pending_actions: dict[tuple[str, str], dict[str, Any]] = {}
        self._prepared_room_cursors: dict[
            str,
            tuple[int, int, tuple[tuple[str, int], ...]],
        ] = {}
        self._artifact_clock = artifact_clock
        self._artifact_retry_min_seconds = max(0.01, float(artifact_retry_min_seconds))
        self._artifact_retry_max_seconds = max(
            self._artifact_retry_min_seconds,
            float(artifact_retry_max_seconds),
        )
        self._prepare_artifact_retry_store()
        self.peer_routes = {}
        self.peer_clients = {}
        try:
            stored_links, load_errors = hosted_room_links.load_room_links_tolerant(
                self.db_path
            )
            errors = list(load_errors)
            for stored in stored_links:
                if PROTOCOL_VERSION not in stored.catalog.protocol_versions:
                    errors.append(
                        f"{stored.room_id}:{stored.member_id}:protocol-upgrade-required"
                    )
                    continue
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
                    attachments=stored.catalog.attachments,
                )
                self.peer_routes[(stored.room_id, stored.member_id)] = route
                self.peer_clients[(stored.room_id, stored.member_id)] = client
                self._peer_route_status[(stored.room_id, stored.member_id)] = (
                    stored.status
                )
            if errors:
                self._link_load_error = ",".join(errors)
        except Exception as exc:
            self._link_load_error = str(exc)
        supplied_routes = dict(peer_routes or {})
        supplied_clients = dict(peer_clients or {})
        self.peer_routes.update(supplied_routes)
        for key, route in supplied_routes.items():
            client = supplied_clients.get(key)
            if client is None:
                client = supplied_clients.get(route.target_install_id)
            if client is not None:
                self.peer_clients[key] = client
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path,
            rooms=self.bindings,
            rpc=self.rpc,
            transport_resolver=self._resolve_member_transport,
            turn_lock=self._turn_lock,
            prepare_room=self.prepare_room,
            publish_terminal=self.publish_terminal,
            pending_action=self._set_pending_action,
            attachment_loader=self._load_task_attachments,
            # Local sends and terminal callbacks wake the worker immediately.
            # This poll exists only for room events written by sibling gateway
            # processes, so 1s keeps cross-process control responsive without
            # opening several SQLite handles per room ten times each second.
            poll_interval_seconds=1.0,
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
        self.attachments.prune()
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
            self.peer_clients[(room_id, member_id)] = client
            self._peer_route_status[(room_id, member_id)] = "ready"
        self._unblock_artifact_retries(room_id, member_id)
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
        for key, route in routes:
            client = self.peer_clients.get(key)
            revoke = getattr(client, "revoke_grant", None)
            if not callable(revoke):
                raise RuntimeError("peer room grant cannot be revoked safely")
            revoke(grant=route.grant)

        hosted_rooms.delete_room_link_records(self.db_path, room_id=room_id)
        with self._policy_lock:
            for key, route in routes:
                self.peer_routes.pop(key, None)
                self._peer_route_status.pop(key, None)
                self.peer_clients.pop(key, None)
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
        client = self.peer_clients.get((binding.room_id, member_id))
        if client is None:
            raise RuntimeError("peer room client is unavailable")
        identity = task.get("identity")
        execution_generation = int(task.get("execution_generation") or 0)
        bind_observation = getattr(client, "bind_observation", None)
        if (
            callable(bind_observation)
            and isinstance(identity, driver.TaskIdentity)
            and execution_generation > 0
        ):
            bind_observation(
                task_id=identity.task_id,
                execution_generation=execution_generation,
            )
        tracked_client = self._tracked_peer_client(
            binding.room_id,
            member_id,
            client,
        )
        if task.get("payload", {}).get("attachments"):
            route = self._refresh_peer_attachment_catalog(
                binding.room_id,
                member_id,
                route,
                tracked_client,
            )
            if not route.attachments:
                raise RuntimeError(
                    "The target gateway needs an update before it can receive files in this Group Chat."
                )
        self._recover_peer_admission(binding, task, route, tracked_client)
        return PeerHostedRoomTransport(
            binding=binding,
            route=route,
            client=tracked_client,
            source_event_seq=int(payload.get("source_event_seq") or 0),
            task_id=getattr(task.get("identity"), "task_id", None),
            execution_generation=int(task.get("execution_generation") or 0),
        )

    def _tracked_peer_client(
        self,
        room_id: str,
        member_id: str,
        client: HostedRoomPeerClient,
    ) -> "_RouteStatusPeerClient":
        return _RouteStatusPeerClient(
            client,
            on_ready=lambda: self._set_route_status(
                room_id, member_id, "ready"
            ),
            on_reauthorization=lambda: self._set_route_status(
                room_id, member_id, "needs_reauthorization"
            ),
            on_unavailable=lambda: self._set_route_status(
                room_id, member_id, "unavailable"
            ),
            on_refreshed=lambda grant: self._rotate_route_grant(
                room_id, member_id, grant
            ),
        )

    def _recover_peer_admission(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
        route: PeerMemberRoute,
        client: Any,
    ) -> None:
        """Rediscover an admitted peer run without advancing its generation."""
        recover = getattr(client, "recover_dispatch", None)
        identity = task.get("identity")
        payload = task.get("payload")
        execution_generation = int(task.get("execution_generation") or 0)
        if (
            not callable(recover)
            or not isinstance(identity, driver.TaskIdentity)
            or not isinstance(payload, Mapping)
            or execution_generation < 1
            or task.get("status") not in {"running", "indeterminate", "stopping"}
        ):
            return
        prompt = payload.get("prompt")
        source_event_seq = int(payload.get("source_event_seq") or 0)
        if not isinstance(prompt, str) or source_event_seq < 1 or not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        attachment_manifest = []
        attachment_payloads = []
        for attachment, data in self._load_task_attachments(binding, task):
            manifest_item = {
                **dict(attachment),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            attachment_manifest.append(manifest_item)
            attachment_payloads.append({**manifest_item, "data": data})
        dispatch = HostedMemberDispatch.from_mapping({
            "protocol_version": PROTOCOL_VERSION,
            "room_id": identity.room_id,
            "home_install_id": route.home_install_id,
            "authority_gateway_id": binding.gateway_id,
            "authority_epoch": binding.authority_epoch,
            "member_id": route.member_id,
            "target_install_id": route.target_install_id,
            "target_profile": route.target_profile,
            "task_id": identity.task_id,
            "execution_generation": execution_generation,
            "source_event_seq": source_event_seq,
            "cancellation_scope_id": route.cancellation_scope_id,
            "prompt": prompt,
            "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "capability_digest": route.capability_digest,
            "trace_id": route.trace_id,
            **(
                {
                    "attachment_manifest_digest": attachment_manifest_digest(
                        attachment_manifest
                    )
                }
                if attachment_manifest
                else {}
            ),
        })
        if attachment_payloads:
            stage = getattr(client, "stage_attachments", None)
            if not callable(stage):
                raise RuntimeError(
                    "The target gateway needs an update before it can receive files in this Group Chat."
                )
            stage(
                dispatch=dispatch.as_mapping(),
                attachments=attachment_payloads,
                grant=route.grant,
            )
        recover(dispatch=dispatch.as_mapping(), grant=route.grant)

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

    def _set_pending_action(
        self, room_id: str, member_id: str, action: Mapping[str, Any] | None
    ) -> None:
        key = (room_id, member_id)
        with self._policy_lock:
            if action is None:
                self._pending_actions.pop(key, None)
            else:
                self._pending_actions[key] = {**action, "member_id": member_id}

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
        self._unblock_artifact_retries(room_id, member_id)

    def _refresh_peer_attachment_catalog(
        self,
        room_id: str,
        member_id: str,
        route: PeerMemberRoute,
        client: Any,
    ) -> PeerMemberRoute:
        """Re-probe binary support so upgrades and downgrades fail truthfully."""

        probe = getattr(client, "probe", None)
        if not callable(probe):
            return route
        result = probe(grant=route.grant)
        catalog = GatewayRoomCatalog.from_mapping(result.get("catalog"))
        if (
            catalog.installation_id != route.target_install_id
            or not catalog.persistent_process
            or not catalog.text
        ):
            raise RuntimeError("peer room capability identity changed")
        key = (room_id, member_id)
        stored = next(
            (
                link
                for link in hosted_room_links.load_room_links(self.db_path)
                if (link.room_id, link.member_id) == key
            ),
            None,
        )
        if stored is None:
            # Explicitly supplied in-process routes are valid for tests and
            # ephemeral callers, but cannot publish a refreshed catalog.
            return replace(
                route,
                capability_digest=catalog.catalog_digest,
                attachments=catalog.attachments,
            )
        refreshed = replace(
            route,
            capability_digest=catalog.catalog_digest,
            attachments=catalog.attachments,
        )
        if (
            stored.catalog.catalog_digest != catalog.catalog_digest
            or stored.catalog.attachments != catalog.attachments
        ):
            hosted_room_links.save_room_link(
                self.db_path,
                hosted_room_links.make_stored_link(
                    room_id=room_id,
                    member_id=member_id,
                    target_url=stored.target_url,
                    target_profile=stored.target_profile,
                    grant=route.grant,
                    catalog=catalog,
                    cancellation_scope_id=stored.cancellation_scope_id,
                    trace_id=stored.trace_id,
                ),
            )
            with self._policy_lock:
                self.peer_routes[key] = refreshed
                self._peer_route_status[key] = "ready"
        return refreshed

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

    def _load_task_attachments(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
    ) -> Iterator[tuple[Mapping[str, Any], bytes]]:
        """Resolve task manifests to verified bytes for one frozen room member."""

        manifests = task.get("payload", {}).get("attachments") or []
        if not manifests:
            return
        room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
        target_member_id = str(
            task.get("payload", {}).get("target_member_id") or ""
        )
        profile = str(task.get("payload", {}).get("target_profile") or "")
        member = next(
            (
                candidate
                for candidate in room["members"]
                if (
                    str(candidate.get("member_id") or "") == target_member_id
                    if target_member_id
                    else str(candidate.get("profile") or "") == profile
                )
            ),
            None,
        )
        if member is None:
            raise RuntimeError("hosted attachment target is not in the frozen roster")
        member_id = str(member.get("member_id") or "")
        for manifest in manifests:
            stored = self.attachments.read(
                room_id=binding.room_id,
                attachment_id=manifest.get("attachment_id"),
                recipient_member_id=member_id,
            )
            safe = {
                "attachment_id": stored.attachment["attachment_id"],
                "kind": stored.attachment["kind"],
                "name": stored.attachment["name"],
                "size": stored.attachment["size"],
                "mime": stored.attachment["mime"],
            }
            if safe != dict(manifest):
                raise RuntimeError(
                    "hosted attachment metadata changed after task admission"
                )
            yield safe, stored.data

    def _publish_terminal_tasks(
        self,
        room: Mapping[str, Any],
        events: list[dict[str, Any]],
    ) -> bool:
        changed = False
        local_profiles = self.local_profiles()
        published_task_ids = {
            str(event.get("payload", {}).get("task_id") or "")
            for event in events
            if event.get("kind")
            in {"turn.settled", "turn.failed", "turn.cancelled"}
        }
        retry_keys = self._artifact_retry_keys(str(room["room_id"]))
        for status in ("settled", "failed", "cancelled"):
            for task in driver.list_tasks(
                self.db_path,
                room_id=str(room["room_id"]),
                status=status,
            ):
                identity = task["identity"]
                retry_key = self._artifact_retry_key(task)
                if (
                    identity.task_id in published_task_ids
                    and retry_key not in retry_keys
                ):
                    continue
                if status == "cancelled":
                    self._discard_cancelled_task_artifacts(
                        str(room["room_id"]),
                        task,
                    )
                if not self._artifact_retry_due(task):
                    continue
                publication_status = status
                keep_artifact_retry = False
                plan = discussion.reconstruct_task_plan(
                    room,
                    events,
                    task,
                    local_profiles=local_profiles,
                )
                try:
                    result, acknowledge_artifacts = self._import_terminal_artifacts(
                        room=room,
                        task=task,
                        plan=plan,
                        events=events,
                    )
                except Exception as exc:
                    if bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, (ConnectionError, OSError, TimeoutError)
                    ):
                        self._defer_artifact_retry(task, exc)
                        continue
                    if isinstance(exc, _ArtifactRetirementBlocked):
                        self._defer_artifact_retry(task, exc, permanent=True)
                        keep_artifact_retry = True
                    result = {
                        "error": "A Group Chat file could not be verified.",
                        "reason_code": "artifact_verification_failed",
                    }
                    acknowledge_artifacts = lambda: None
                    publication_status = "failed"
                publication = discussion.plan_publication(
                    room,
                    events,
                    plan,
                    status=publication_status,
                    result=result,
                    local_profiles=local_profiles,
                )
                before = len(events)
                self._append_plan(str(room["room_id"]), publication)
                if isinstance(result, Mapping) and result.get("attachments"):
                    digest = plan.identity.task_id.removeprefix("dtask:")
                    self.attachments.retain_event(
                        room_id=str(room["room_id"]),
                        event_id=f"dmessage:{digest}",
                    )
                    try:
                        acknowledge_artifacts()
                    except Exception as exc:
                        if bool(getattr(exc, "retryable", False)) or isinstance(
                            exc, (ConnectionError, OSError, TimeoutError)
                        ):
                            self._defer_artifact_retry(task, exc)
                            continue
                        self._defer_artifact_retry(task, exc, permanent=True)
                        continue
                if not keep_artifact_retry:
                    self._clear_artifact_retry(task)
                events = self._events(str(room["room_id"]))
                changed = changed or len(events) > before
        return changed

    def _artifact_retry_connection(self) -> sqlite3.Connection:
        from hermes_state import apply_wal_with_fallback

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="state.db (room artifact retries)")
        return conn

    def _prepare_artifact_retry_store(self) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_artifact_retries (
                    room_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    execution_generation INTEGER NOT NULL,
                    member_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(room_id, task_id, execution_generation)
                )"""
            )
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(hosted_room_artifact_retries)"
                ).fetchall()
            }
            if "member_id" not in columns:
                conn.execute(
                    """ALTER TABLE hosted_room_artifact_retries
                       ADD COLUMN member_id TEXT NOT NULL DEFAULT ''"""
                )
            empty = conn.execute(
                """SELECT room_id, task_id, execution_generation
                   FROM hosted_room_artifact_retries WHERE member_id=''"""
            ).fetchall()
            has_driver_tasks = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='hosted_room_driver_tasks'"""
            ).fetchone()
            if has_driver_tasks is None:
                return
            for row in empty:
                task = conn.execute(
                    """SELECT payload_json FROM hosted_room_driver_tasks
                       WHERE room_id=? AND task_id=?""",
                    (row["room_id"], row["task_id"]),
                ).fetchone()
                if task is None:
                    continue
                try:
                    payload = json.loads(task["payload_json"])
                    member_id = str(
                        payload.get("target_member_id")
                        or payload.get("target_profile")
                        or ""
                    )
                except Exception:
                    continue
                if member_id:
                    conn.execute(
                        """UPDATE hosted_room_artifact_retries SET member_id=?
                           WHERE room_id=? AND task_id=? AND execution_generation=?""",
                        (
                            member_id,
                            row["room_id"],
                            row["task_id"],
                            row["execution_generation"],
                        ),
                    )

    @staticmethod
    def _artifact_retry_key(task: Mapping[str, Any]) -> tuple[str, str, int]:
        identity = task["identity"]
        return (
            identity.room_id,
            identity.task_id,
            int(task["execution_generation"]),
        )

    def _artifact_retry_keys(self, room_id: str) -> set[tuple[str, str, int]]:
        with self._artifact_retry_connection() as conn:
            rows = conn.execute(
                """SELECT room_id, task_id, execution_generation
                   FROM hosted_room_artifact_retries WHERE room_id=?""",
                (room_id,),
            ).fetchall()
        return {
            (str(row["room_id"]), str(row["task_id"]), int(row["execution_generation"]))
            for row in rows
        }

    def _room_task_status_signature(self, room_id: str) -> tuple[tuple[str, int], ...]:
        with self._artifact_retry_connection() as conn:
            try:
                rows = conn.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM hosted_room_driver_tasks
                       WHERE room_id=? GROUP BY status ORDER BY status""",
                    (room_id,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                rows = ()
        return tuple((str(row["status"]), int(row["count"])) for row in rows)

    def _artifact_retry_due(self, task: Mapping[str, Any]) -> bool:
        key = self._artifact_retry_key(task)
        with self._artifact_retry_connection() as conn:
            row = conn.execute(
                """SELECT next_attempt_at, blocked FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                key,
            ).fetchone()
        return row is None or (
            not bool(row["blocked"])
            and self._artifact_clock() >= float(row["next_attempt_at"])
        )

    def _defer_artifact_retry(
        self,
        task: Mapping[str, Any],
        _exc: Exception,
        *,
        permanent: bool = False,
    ) -> None:
        key = self._artifact_retry_key(task)
        now = float(self._artifact_clock())
        with self._artifact_retry_connection() as conn:
            row = conn.execute(
                """SELECT attempts FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                key,
            ).fetchone()
            attempts = 1 if row is None else int(row["attempts"]) + 1
            delay = min(
                self._artifact_retry_max_seconds,
                self._artifact_retry_min_seconds * (2 ** min(attempts - 1, 16)),
            )
            conn.execute(
                """INSERT INTO hosted_room_artifact_retries
                   (room_id, task_id, execution_generation, member_id, attempts,
                    next_attempt_at, blocked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(room_id, task_id, execution_generation) DO UPDATE SET
                    attempts=excluded.attempts,
                    next_attempt_at=excluded.next_attempt_at,
                    blocked=excluded.blocked,
                    updated_at=excluded.updated_at""",
                (
                    *key,
                    str(
                        task.get("payload", {}).get("target_member_id")
                        or task.get("payload", {}).get("target_profile")
                        or "unknown"
                    ),
                    attempts,
                    now + delay,
                    int(permanent),
                    now,
                ),
            )

    def _clear_artifact_retry(self, task: Mapping[str, Any]) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """DELETE FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                self._artifact_retry_key(task),
            )

    def _unblock_artifact_retries(self, room_id: str, member_id: str) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """UPDATE hosted_room_artifact_retries
                      SET blocked=0, next_attempt_at=0, updated_at=?
                    WHERE room_id=? AND member_id=? AND blocked=1""",
                (float(self._artifact_clock()), room_id, member_id),
            )

    def _import_terminal_artifacts(
        self,
        *,
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        plan: discussion.DiscussionTaskPlan,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | Any, Any]:
        result = task.get("result")
        if not isinstance(result, Mapping) or not result.get("artifacts"):
            return result, lambda: None
        items = validate_terminal_artifact_manifest(result["artifacts"])
        if not items:
            return result, lambda: None
        target = plan.member.target or {"kind": "local", "profile": plan.member.profile}
        local_install = hosted_rooms.local_authority_gateway_id()
        peer_route = None
        peer_client = None
        if target.get("kind") == "peer":
            key = (str(room["room_id"]), plan.member.member_id)
            peer_route = self.peer_routes.get(key)
            peer_client = self.peer_clients.get(key)
            if peer_route is None or peer_client is None:
                raise RuntimeError("peer room output artifact route is unavailable")
            target_install = peer_route.target_install_id
            home_install = peer_route.home_install_id
        elif target.get("kind") == "local":
            target_install = local_install
            home_install = local_install
        else:
            raise RuntimeError("room output artifact target is invalid")
        if peer_client is not None:
            peer_client = self._tracked_peer_client(
                str(room["room_id"]),
                plan.member.member_id,
                peer_client,
            )
        scope = RoomArtifactScope.from_mapping({
            "room_id": room["room_id"],
            "task_id": plan.identity.task_id,
            "execution_generation": int(task["execution_generation"]),
            "member_id": plan.member.member_id,
            "target_profile": plan.member.profile,
            "home_install_id": home_install,
            "target_install_id": target_install,
            "authority_gateway_id": room["authority_gateway_id"],
            "authority_epoch": int(room["authority_epoch"]),
        })
        outbox = None
        if target.get("kind") == "local":
            profile_root = (
                self.root
                if plan.member.profile == "default"
                else self.root / "profiles" / plan.member.profile
            )
            outbox = RoomArtifactOutbox(profile_root / "state.db")
        artifact_ids = tuple(item["artifact_id"] for item in items)
        message_event_id = f"dmessage:{plan.identity.task_id.removeprefix('dtask:')}"
        recipient_ids = tuple(plan.payload.get("recipient_member_ids") or ())
        if not recipient_ids:
            # Pre-file-handoff tasks have no admission-time recipient snapshot.
            # Their text replies remain replayable after upgrade, but publishing
            # a file would otherwise grant bytes using today's room membership.
            # Retire the exact private outbox first, then fail the visible turn.
            if outbox is not None:
                outbox.discard(scope)
            else:
                run_id = str(result.get("run_id") or "")
                if not run_id:
                    raise RuntimeError("peer room output artifact run is missing")
                try:
                    peer_client.acknowledge_artifacts(
                        run_id=run_id,
                        artifact_ids=artifact_ids,
                        manifest_digest=result["artifacts"]["manifest_digest"],
                        message_event_id=message_event_id,
                        grant=peer_route.grant,
                    )
                except Exception as exc:
                    # Do not publish a terminal failure while private bytes are
                    # still live on the peer. Keep the bounded retry active so
                    # route refresh or reconnection can finish retirement.
                    if bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, (ConnectionError, OSError, TimeoutError)
                    ):
                        raise
                    self._set_route_status(
                        str(room["room_id"]),
                        plan.member.member_id,
                        "needs_reauthorization",
                    )
                    raise _ArtifactRetirementBlocked(
                        "peer room output artifact retirement needs a repaired route"
                    ) from exc
            raise RuntimeError("room output artifact recipient roster is missing")
        existing_message = next(
            (
                event
                for event in events
                if event.get("event_id") == message_event_id
                and event.get("kind") == "message.member"
            ),
            None,
        )
        if existing_message is not None:
            existing_attachments = existing_message.get("payload", {}).get("attachments")
            if not isinstance(existing_attachments, list) or not existing_attachments:
                raise RuntimeError("room output artifact publication changed after commit")
            normalized = dict(result)
            normalized["attachments"] = existing_attachments
            if outbox is not None:
                return normalized, lambda: outbox.acknowledge(scope, artifact_ids)
            run_id = str(result.get("run_id") or "")
            return normalized, lambda: peer_client.acknowledge_artifacts(
                run_id=run_id,
                artifact_ids=artifact_ids,
                manifest_digest=result["artifacts"]["manifest_digest"],
                message_event_id=message_event_id,
                grant=peer_route.grant,
            )

        canonical: list[dict[str, Any]] = []
        for item in items:
            if outbox is not None:
                metadata, data = outbox.read(scope, item["artifact_id"])
                if metadata != item:
                    raise RuntimeError(
                        "room output artifact metadata changed before import"
                    )
            else:
                run_id = str(result.get("run_id") or "")
                if not run_id:
                    raise RuntimeError("peer room output artifact run is missing")
                data = peer_client.read_artifact(
                    run_id=run_id,
                    artifact_id=item["artifact_id"],
                    grant=peer_route.grant,
                )
                if (
                    len(data) != item["size"]
                    or hashlib.sha256(data).hexdigest() != item["sha256"]
                ):
                    raise RuntimeError("peer room output artifact failed verification")
            upload_digest = hashlib.sha256(
                (
                    f"{plan.identity.task_id}\0{task['execution_generation']}\0"
                    f"{item['artifact_id']}"
                ).encode()
            ).hexdigest()[:32]
            stored = self.attachments.put(
                room_id=str(room["room_id"]),
                upload_id=f"bot-output:{upload_digest}",
                kind=item["kind"],
                name=item["name"],
                mime=item["mime"],
                data=data,
            )
            if stored["sha256"] != item["sha256"]:
                raise RuntimeError("room output artifact digest changed during import")
            canonical.append({
                key: stored[key]
                for key in ("attachment_id", "kind", "name", "size", "mime")
            })

        digest = plan.identity.task_id.removeprefix("dtask:")
        event_id = f"dmessage:{digest}"
        normalized = dict(result)
        normalized["attachments"] = self.attachments.commit_message(
            room_id=str(room["room_id"]),
            event_id=event_id,
            manifest=canonical,
            recipient_member_ids=(*recipient_ids, "viewer"),
            hold_until_event=True,
        )
        if outbox is not None:
            return normalized, lambda: outbox.acknowledge(scope, artifact_ids)
        run_id = str(result.get("run_id") or "")
        return normalized, lambda: peer_client.acknowledge_artifacts(
            run_id=run_id,
            artifact_ids=artifact_ids,
            manifest_digest=result["artifacts"]["manifest_digest"],
            message_event_id=message_event_id,
            grant=peer_route.grant,
        )

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

    def prepare_room(
        self,
        binding: HostedRoomBinding,
        *,
        force: bool = False,
    ) -> None:
        with self._policy_lock:
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
            cursor = (
                int(room["revision"]),
                int(room["latest_seq"]),
                self._room_task_status_signature(binding.room_id),
            )
            if (
                not force
                and self._prepared_room_cursors.get(binding.room_id) == cursor
                and not self._artifact_retry_keys(binding.room_id)
            ):
                return
            events = self._events(binding.room_id)
            if self._publish_terminal_tasks(room, events):
                events = self._events(binding.room_id)
            decision = discussion.plan_next_task(
                room,
                events,
                local_profiles=self.local_profiles(),
            )
            if decision.status == "task" and decision.task is not None:
                existing = next(
                    (
                        task
                        for task in driver.list_tasks(
                            self.db_path,
                            room_id=binding.room_id,
                        )
                        if task["status"] not in driver.TERMINAL_STATUSES
                        and task["identity"].thread_id
                        == decision.task.identity.thread_id
                        and task["identity"].turn_id == decision.task.identity.turn_id
                    ),
                    None,
                )
                if existing is not None:
                    # A roster edit can change the prompt and task hash while
                    # the immutable room/thread/turn coordinate stays the same.
                    # Resume the admitted task after strict reconstruction;
                    # admitting the rehashed candidate would collide with the
                    # driver's unique turn fence and brick the room.
                    discussion.reconstruct_task_plan(
                        room,
                        events,
                        existing,
                        local_profiles=self.local_profiles(),
                    )
                else:
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
            # Cache only the snapshot this pass actually evaluated. Another
            # process can append a user event while terminal publication is in
            # progress; caching a fresh end-of-pass cursor would claim that
            # unseen event was processed and strand it forever. Our own task or
            # event writes intentionally cause one cheap follow-up pass.
            self._prepared_room_cursors[binding.room_id] = cursor

    def publish_terminal(
        self,
        binding: HostedRoomBinding,
        _task: Mapping[str, Any],
    ) -> None:
        self.prepare_room(binding, force=True)
        self.runtime.wakeup()

    def discard_output_artifacts(self, room_id: str) -> int:
        """Purge local-profile output outboxes after a room is disbanded."""

        changed = 0
        for profile in self.local_profiles():
            profile_root = self.root if profile == "default" else self.root / "profiles" / profile
            changed += RoomArtifactOutbox(profile_root / "state.db").discard_room(room_id)
        return changed

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

    def put_attachment(
        self,
        *,
        room_id: str,
        upload_id: str,
        kind: str,
        name: str,
        mime: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Store one bounded upload for a live room without exposing its path."""

        hosted_rooms.room_state(self.db_path, room_id=room_id)
        return self.attachments.put(
            room_id=room_id,
            upload_id=upload_id,
            kind=kind,
            name=name,
            mime=mime,
            data=data,
        )

    def read_attachment(
        self,
        *,
        room_id: str,
        attachment_id: str,
        recipient_member_id: str,
        event_id: str | None = None,
    ) -> AttachmentData:
        """Return verified bytes only when send-time recipient ownership permits it."""

        return self.attachments.read(
            room_id=room_id,
            attachment_id=attachment_id,
            recipient_member_id=recipient_member_id,
            event_id=event_id,
        )

    def send(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
        actor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        member_ids = tuple(
            str(member.get("member_id") or member.get("profile") or "")
            for member in room["members"]
        )
        if isinstance(payload, Mapping) and "thread_id" not in payload:
            payload = {**payload, "thread_id": event_id}
        normalized = discussion.validate_user_payload(
            payload,
            member_ids=member_ids,
        )
        if normalized.get("attachments"):
            normalized["attachments"] = self.attachments.commit_message(
                room_id=room_id,
                event_id=event_id,
                manifest=normalized["attachments"],
                recipient_member_ids=(*member_ids, "viewer"),
                hold_until_event=True,
            )
        event = hosted_rooms.append_event(
            self.db_path,
            room_id=room_id,
            event_id=event_id,
            kind="message.user",
            actor=actor or {"kind": "user", "id": "desktop"},
            payload=normalized,
        )
        if normalized.get("attachments"):
            self.attachments.retain_event(room_id=room_id, event_id=event_id)
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

    def stop_room(
        self,
        room_id: str,
        *,
        cancel_id: str,
        require_acknowledged: bool = False,
    ) -> int:
        hosted_rooms.request_room_stop(
            self.db_path,
            room_id=room_id,
            cancel_id=cancel_id,
        )
        cancelled = 0
        pending = 0
        with self._policy_lock:
            tasks = {}
            for status in ("queued", "running", "indeterminate", "stopping"):
                for task in driver.list_tasks(
                    self.db_path,
                    room_id=room_id,
                    status=status,
                ):
                    identity = task["identity"]
                    tasks[(identity.room_id, identity.task_id)] = task
            for task in tasks.values():
                task_cancel_id = (
                    str(task.get("cancel_id") or "")
                    if task.get("status") == "stopping"
                    else ""
                )
                result = self.runtime.cancel(
                    task["identity"], cancel_id=task_cancel_id or cancel_id
                )
                cancelled += 1
                if result["status"] == "stopping":
                    pending += 1
                else:
                    self._discard_cancelled_task_artifacts(room_id, task)
        if require_acknowledged and pending:
            raise RuntimeError(
                "room work is still stopping; retry deletion after Stop completes"
            )
        self.runtime.wakeup()
        return cancelled

    def _discard_cancelled_task_artifacts(
        self,
        room_id: str,
        task: Mapping[str, Any],
    ) -> None:
        generation = int(task.get("execution_generation") or 0)
        if generation < 1:
            return
        payload = task.get("payload") or {}
        member_id = str(
            payload.get("target_member_id") or payload.get("target_profile") or ""
        )
        if not member_id or (room_id, member_id) in self.peer_routes:
            return
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        profile = str(payload.get("target_profile") or "default")
        installation = hosted_rooms.local_authority_gateway_id()
        scope = RoomArtifactScope.from_mapping({
            "room_id": room_id,
            "task_id": task["identity"].task_id,
            "execution_generation": generation,
            "member_id": member_id,
            "target_profile": profile,
            "home_install_id": installation,
            "target_install_id": installation,
            "authority_gateway_id": room["authority_gateway_id"],
            "authority_epoch": int(room["authority_epoch"]),
        })
        profile_root = (
            self.root
            if profile == "default"
            else self.root / "profiles" / profile
        )
        RoomArtifactOutbox(profile_root / "state.db").discard(scope)

    def retry_room_task(self, room_id: str, *, task_id: str) -> dict[str, Any]:
        """Retry one indeterminate task only after explicit user action."""
        task = next(
            (
                candidate
                for candidate in driver.list_tasks(
                    self.db_path, room_id=room_id, status="indeterminate"
                )
                if candidate["identity"].task_id == task_id
            ),
            None,
        )
        if task is None:
            raise driver.InvalidTaskTransitionError(
                "no indeterminate room task matches task_id"
            )
        return self.runtime.retry_indeterminate(task["identity"])

    def approve_room_task(
        self,
        room_id: str,
        *,
        member_id: str,
        task_id: str,
        execution_generation: int,
        choice: str,
        request_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Resolve one exact local or peer approval and wake room observation."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        client = self.peer_clients.get(key)
        with self._policy_lock:
            action = self._pending_actions.get(key)
        requested_approval_id = str(request_id or "")
        pending_approval_id = str((action or {}).get("request_id") or "")
        if (
            action is None
            or action.get("task_id") != task_id
            or int(action.get("execution_generation") or 0)
            != execution_generation
            or not requested_approval_id
            or not pending_approval_id
            or requested_approval_id != pending_approval_id
        ):
            raise RuntimeError("room approval is no longer pending")
        if choice not in {"once", "deny"}:
            raise RuntimeError("room approval choice must be once or deny")
        approve = getattr(client, "approve_receipt", None)
        if route is not None and callable(approve):
            result = approve(
                task_id=task_id,
                execution_generation=execution_generation,
                request_id=requested_approval_id,
                choice=choice,
                grant=route.grant,
            )
        else:
            session_id = str(action.get("session_id") or "")
            if not session_id:
                raise RuntimeError("local room approval identity is unavailable")
            result = self.rpc.approve(
                session_id=session_id,
                request_id=requested_approval_id,
                choice=choice,
            )
        if result is None:
            raise RuntimeError("room approval target is unavailable")
        with self._policy_lock:
            current = self._pending_actions.get(key)
            if (
                current is not None
                and str(current.get("request_id") or "") == requested_approval_id
                and current.get("task_id") == task_id
                and int(current.get("execution_generation") or 0)
                == execution_generation
            ):
                self._pending_actions.pop(key, None)
        self.runtime.wakeup()
        return result

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = self.runtime.status()
        runtime = {**runtime, "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime = {**runtime, "link_load_error": self._link_load_error}
        if room_id is None:
            return runtime
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            {
                "kind": "retry",
                "task_id": task["identity"].task_id,
            }
            for task in tasks
            if task["status"] == "indeterminate"
        ]
        with self._policy_lock:
            pending_actions.extend(
                dict(action)
                for (action_room_id, _member_id), action in self._pending_actions.items()
                if action_room_id == room_id
            )
        return {
            "running": runtime["running"],
            "working": bool(
                counts.get("running")
                or counts.get("queued")
                or counts.get("stopping")
            ),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts),
            "pending_actions": pending_actions,
            "peer_routes": self._route_statuses(room_id),
        }


class _RouteStatusPeerClient:
    """Classify scoped-auth failures without exposing route credentials."""

    def __init__(
        self,
        client,
        *,
        on_ready,
        on_reauthorization,
        on_unavailable,
        on_refreshed,
    ) -> None:
        self._client = client
        self._on_ready = on_ready
        self._on_reauthorization = on_reauthorization
        self._on_unavailable = on_unavailable
        self._on_refreshed = on_refreshed

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
            if name in {
                "acknowledge_artifacts",
                "dispatch",
                "read_artifact",
                "recover_dispatch",
                "stage_attachments",
            } and "grant" in kwargs:
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
                elif bool(getattr(exc, "not_admitted", False)):
                    self._on_unavailable()
                raise
            if name != "prepare":
                self._on_ready()
            return result

        return tracked
