"""Artifact ownership for hosted Group Chat services."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway.hosted_room_attachments import AttachmentData, MAX_ATTACHMENTS_PER_MESSAGE
from gateway.hosted_room_artifacts import (
    RoomArtifactOutbox,
    RoomArtifactScope,
    validate_terminal_artifact_manifest,
)
from tui_gateway.hosted_room_driver import HostedRoomBinding
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPError


class _ArtifactRetirementBlocked(RuntimeError):
    """Private peer bytes need a repaired scoped route before retirement."""


class HostedRoomArtifactMixin:
    def retire_room_artifacts(self, room_id: str) -> None:
        """Delete every local private output and retry before room tombstoning."""

        with self._policy_lock:
            for task in driver.list_tasks(self.db_path, room_id=room_id):
                self._discard_cancelled_task_artifacts(room_id, task)
            with self._artifact_retry_connection() as conn:
                conn.execute(
                    "DELETE FROM hosted_room_artifact_retries WHERE room_id=?",
                    (room_id,),
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
    ) -> bool:
        changed = False
        local_profiles = self.local_profiles()
        retry_keys = self._artifact_retry_keys(str(room["room_id"]))
        for status in ("deferred", "settled", "failed", "cancelled"):
            for task in driver.list_tasks(
                self.db_path,
                room_id=str(room["room_id"]),
                status=status,
            ):
                identity = task["identity"]
                publication_exists = self.policy_checkpoint.publication_exists(
                    room_id=str(room["room_id"]),
                    task_id=identity.task_id,
                    status=status,
                    execution_generation=int(task["execution_generation"]),
                )
                retry_key = self._artifact_retry_key(task)
                result = task.get("result")
                has_artifact_ack = (
                    isinstance(result, Mapping) and bool(result.get("artifacts"))
                )
                if (
                    publication_exists
                    and retry_key not in retry_keys
                    and not has_artifact_ack
                ):
                    continue
                task_events = self.policy_checkpoint.events_for_task(
                    room_id=str(room["room_id"]),
                    source_event_seq=int(task["payload"]["source_event_seq"]),
                )
                if status == "cancelled":
                    self._discard_cancelled_task_artifacts(
                        str(room["room_id"]),
                        task,
                    )
                if not self._artifact_retry_due(task):
                    continue
                publication_status = status
                if publication_exists and has_artifact_ack:
                    plan = self._published_artifact_plan(
                        room,
                        task,
                        local_profiles=local_profiles,
                    )
                else:
                    plan = discussion.reconstruct_task_plan(
                        room,
                        task_events,
                        task,
                        local_profiles=local_profiles,
                    )
                try:
                    result, acknowledge_artifacts = self._import_terminal_artifacts(
                        room=room,
                        task=task,
                        plan=plan,
                        events=task_events,
                    )
                except Exception as exc:
                    if bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, (ConnectionError, OSError, TimeoutError)
                    ):
                        self._defer_artifact_retry(task, exc)
                        continue
                    if isinstance(exc, _ArtifactRetirementBlocked):
                        self._defer_artifact_retry(task, exc, permanent=True)
                        continue
                    try:
                        self._retire_failed_terminal_artifacts(
                            room=room,
                            task=task,
                            plan=plan,
                        )
                    except Exception as retirement_exc:
                        self._defer_artifact_retry(
                            task,
                            retirement_exc,
                            permanent=not (
                                bool(getattr(retirement_exc, "retryable", False))
                                or isinstance(
                                    retirement_exc,
                                    (ConnectionError, OSError, TimeoutError),
                                )
                            ),
                        )
                        continue
                    result = {
                        "error": "A Group Chat file could not be verified.",
                        "reason_code": "artifact_verification_failed",
                    }
                    acknowledge_artifacts = lambda: None
                    publication_status = "failed"
                if publication_exists:
                    if isinstance(result, Mapping) and result.get("attachments"):
                        digest = plan.identity.task_id.removeprefix("dtask:")
                        self.attachments.retain_event(
                            room_id=str(room["room_id"]),
                            event_id=f"dmessage:{digest}",
                        )
                        try:
                            acknowledge_artifacts()
                        except Exception as exc:
                            self._defer_artifact_retry(
                                task,
                                exc,
                                permanent=not (
                                    bool(getattr(exc, "retryable", False))
                                    or isinstance(exc, (ConnectionError, OSError, TimeoutError))
                                ),
                            )
                            continue
                    self._clear_artifact_retry(task)
                    continue
                publication = discussion.plan_publication(
                    room,
                    task_events,
                    plan,
                    status=publication_status,
                    result=result,
                    execution_generation=(
                        int(task["execution_generation"])
                        if status == "deferred"
                        else None
                    ),
                    local_profiles=local_profiles,
                )
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
                self._clear_artifact_retry(task)
                changed = True
        return changed

    @staticmethod
    def _published_artifact_plan(
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        *,
        local_profiles: Iterable[str],
    ) -> discussion.DiscussionTaskPlan:
        """Recover the immutable artifact scope after policy compaction.

        A published member message no longer needs its source prompt to be
        reconstructed. The private artifact acknowledgement can outlive that
        bounded policy projection, so derive only its frozen target from the
        admitted task and room roster.
        """

        validated_room = discussion.validate_room(
            room,
            local_profiles=local_profiles,
        )
        identity = task.get("identity")
        payload = task.get("payload")
        if not isinstance(identity, driver.TaskIdentity) or not isinstance(
            payload, Mapping
        ):
            raise discussion.DiscussionReconstructionError(
                "published artifact task shape changed"
            )
        target_member_id = str(payload.get("target_member_id") or "")
        target_profile = str(payload.get("target_profile") or "")
        member_index = next(
            (
                index
                for index, member in enumerate(validated_room.members)
                if (
                    member.member_id == target_member_id
                    if target_member_id
                    else member.profile == target_profile
                )
            ),
            -1,
        )
        if member_index < 0:
            raise discussion.DiscussionReconstructionError(
                "published artifact target is no longer in the frozen roster"
            )
        return discussion.DiscussionTaskPlan(
            identity=identity,
            payload=payload,
            discussion_event_id="",
            member=validated_room.members[member_index],
            member_index=member_index,
            round_index=0,
            seen_through_seq=0,
        )

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
                outbox.discard_durably(scope)
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
                return normalized, lambda: outbox.acknowledge(
                    scope,
                    artifact_ids,
                    message_event_id=message_event_id,
                )
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
        recipient_ids = tuple(plan.payload.get("recipient_member_ids") or ())
        if not recipient_ids:
            raise RuntimeError("room output artifact recipient roster is missing")
        normalized = dict(result)
        normalized["attachments"] = self.attachments.commit_message(
            room_id=str(room["room_id"]),
            event_id=event_id,
            manifest=canonical,
            recipient_member_ids=recipient_ids,
            viewer_access=True,
            hold_until_event=True,
        )
        if outbox is not None:
            return normalized, lambda: outbox.acknowledge(
                scope,
                artifact_ids,
                message_event_id=message_event_id,
            )
        run_id = str(result.get("run_id") or "")
        return normalized, lambda: peer_client.acknowledge_artifacts(
            run_id=run_id,
            artifact_ids=artifact_ids,
            manifest_digest=result["artifacts"]["manifest_digest"],
            message_event_id=message_event_id,
            grant=peer_route.grant,
        )

    def _retire_failed_terminal_artifacts(
        self,
        *,
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        plan: discussion.DiscussionTaskPlan,
    ) -> None:
        """Confirm exact source-byte retirement before publishing failure."""

        result = task.get("result")
        if not isinstance(result, Mapping) or not result.get("artifacts"):
            return
        target = plan.member.target or {
            "kind": "local",
            "profile": plan.member.profile,
        }
        if target.get("kind") == "local":
            installation = hosted_rooms.local_authority_gateway_id()
            scope = RoomArtifactScope.from_mapping({
                "room_id": room["room_id"],
                "task_id": plan.identity.task_id,
                "execution_generation": int(task["execution_generation"]),
                "member_id": plan.member.member_id,
                "target_profile": plan.member.profile,
                "home_install_id": installation,
                "target_install_id": installation,
                "authority_gateway_id": room["authority_gateway_id"],
                "authority_epoch": int(room["authority_epoch"]),
            })
            profile_root = (
                self.root
                if plan.member.profile == "default"
                else self.root / "profiles" / plan.member.profile
            )
            RoomArtifactOutbox(profile_root / "state.db").discard_durably(scope)
            return
        if target.get("kind") != "peer":
            raise _ArtifactRetirementBlocked(
                "room output artifact target cannot be retired"
            )
        key = (str(room["room_id"]), plan.member.member_id)
        route = self.peer_routes.get(key)
        client = self.peer_clients.get(key)
        run_id = str(result.get("run_id") or "")
        if route is None or client is None or not run_id:
            raise _ArtifactRetirementBlocked(
                "peer room output artifact route needs repair"
            )
        client = self._tracked_peer_client(
            str(room["room_id"]),
            plan.member.member_id,
            client,
        )
        try:
            retirement = client.discard_artifacts(run_id=run_id, grant=route.grant)
            removed = retirement.get("removed") if isinstance(retirement, Mapping) else None
            if (
                not isinstance(retirement, Mapping)
                or retirement.get("discarded") is not True
                or isinstance(removed, bool)
                or not isinstance(removed, int)
                or not 0 <= removed <= MAX_ATTACHMENTS_PER_MESSAGE
            ):
                raise PeerRunsHTTPError(
                    "peer returned an invalid artifact retirement receipt"
                )
        except Exception as exc:
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

        self._owned_room(room_id)
        if kind == "pdf" and shutil.which("pdftoppm") is None:
            raise hosted_rooms.HostedRoomError(
                "This gateway cannot receive PDFs until Poppler is installed."
            )
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
        recipient_member_id: str | None,
        event_id: str | None = None,
        viewer: bool = False,
    ) -> AttachmentData:
        """Return verified bytes only when send-time recipient ownership permits it."""

        return self.attachments.read(
            room_id=room_id,
            attachment_id=attachment_id,
            recipient_member_id=recipient_member_id,
            event_id=event_id,
            viewer=viewer,
        )

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
        room = hosted_rooms.room_state(
            self.db_path,
            room_id=room_id,
            include_disbanded=True,
        )
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
        RoomArtifactOutbox(profile_root / "state.db").discard_durably(scope)
