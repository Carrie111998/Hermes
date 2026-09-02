"""In-process session adapter for the hosted room driver.

The room worker must not depend on a Desktop/WebSocket transport, but it should
still use the same session handlers as every other TUI/Desktop turn. This
adapter calls the installed handler registry directly and keeps the extra
task proof as an in-process-only Python object that JSON clients cannot forge.
"""

from __future__ import annotations

import base64
import itertools
import stat
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from gateway import hosted_room_driver as state


class HostedRoomSessionError(RuntimeError):
    """Raised when an in-process session operation is rejected."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed: {message}")
        self.method = method
        self.code = code


class HostedRoomServerRPC:
    """Normalize the installed server handlers for :class:`HostedRoomRuntime`."""

    def __init__(self, server: ModuleType) -> None:
        self.server = server
        self._ids = itertools.count(1)
        self._attachment_lock = threading.Lock()
        self._staged_attachments: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._attachment_attempts: dict[tuple[str, int], tuple[str, ...]] = {}

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self.server._methods[method]
        envelope = handler(f"hosted-room-{next(self._ids)}", params)
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            raise HostedRoomSessionError(
                method,
                int(error.get("code") or 5000),
                str(error.get("message") or "gateway rejected the request"),
            )
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(result, dict):
            raise HostedRoomSessionError(method, 5000, "gateway returned no result")
        return result

    def resolve_exact(
        self, *, profile: str, title: str, source: str
    ) -> Mapping[str, Any] | None:
        del source
        result = self._call(
            "session.list",
            {"profile": profile, "title": title, "include_hidden": True},
        )
        rows = result.get("sessions")
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        session_id = row.get("resolved_id") or row.get("id")
        return {"session_id": session_id, "title": row.get("title") or title}

    def create(self, *, profile: str, title: str, source: str) -> Mapping[str, Any]:
        return self._call(
            "session.create",
            {
                "profile": profile,
                "title": title,
                "source": source,
                "hidden": True,
                "room_plumbing": True,
                "follow_profile_config": True,
                "close_on_disconnect": False,
            },
        )

    def resume(
        self, *, profile: str, session_id: str, source: str
    ) -> Mapping[str, Any]:
        return self._call(
            "session.resume",
            {
                "profile": profile,
                "session_id": session_id,
                "omit_messages": True,
                "source": source,
            },
        )

    def submit(
        self,
        *,
        profile: str,
        session_id: str,
        prompt: str,
        source: str,
        task: state.TaskIdentity,
        execution_generation: int,
        on_terminal: Callable[[Mapping[str, Any]], None],
    ) -> Mapping[str, Any]:
        try:
            return self._call(
                "prompt.submit",
                {
                    "profile": profile,
                    "session_id": session_id,
                    "text": prompt,
                    "source": source,
                    "_hosted_task": {
                        "room_id": task.room_id,
                        "task_id": task.task_id,
                        "thread_id": task.thread_id,
                        "turn_id": task.turn_id,
                        "execution_generation": execution_generation,
                    },
                    "_hosted_terminal_callback": on_terminal,
                },
            )
        except HostedRoomSessionError as exc:
            # In-process prompt.submit error envelopes are returned before the
            # background turn is admitted. Preserve that proof so the driver
            # can defer or requeue without waiting out an ambiguity lease.
            exc.not_admitted = True
            raise

    def stage_attachment(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        attachment: Mapping[str, Any],
        data: bytes,
        execution_generation: int,
    ) -> Mapping[str, Any]:
        """Stage canonical bytes through the existing session attachment RPCs."""

        self.begin_attachment_staging(
            profile=profile,
            session_id=session_id,
            source=source,
            execution_generation=execution_generation,
        )
        attachment_id = str(attachment.get("attachment_id") or "")
        key = (session_id, attachment_id, int(execution_generation))
        with self._attachment_lock:
            cached = self._staged_attachments.get(key)
            if cached is not None:
                return dict(cached)

        encoded = base64.b64encode(data).decode("ascii")
        kind = str(attachment.get("kind") or "")
        name = str(attachment.get("name") or "attachment")
        mime = str(attachment.get("mime") or "application/octet-stream")
        if kind == "image":
            result = self._call(
                "image.attach_bytes",
                {
                    "session_id": session_id,
                    "content_base64": encoded,
                    "filename": name,
                },
            )
        elif kind == "pdf":
            result = self._call(
                "pdf.attach",
                {
                    "session_id": session_id,
                    "content_base64": encoded,
                    "filename": name,
                },
            )
        elif kind == "file":
            result = self._call(
                "file.attach",
                {
                    "session_id": session_id,
                    "data_url": f"data:{mime};base64,{encoded}",
                    "name": name,
                },
            )
        else:
            raise HostedRoomSessionError(
                "attachment.stage", 4016, "unsupported hosted attachment kind"
            )
        if result.get("attached") is not True:
            raise HostedRoomSessionError(
                f"{kind}.attach", 5028, "attachment staging was not acknowledged"
            )
        with self._attachment_lock:
            self._staged_attachments[key] = dict(result)
        return result

    def begin_attachment_staging(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        execution_generation: int,
    ) -> None:
        """Capture the canonical session's pending-image queue once."""

        del profile, source
        attempt_key = (session_id, int(execution_generation))
        with self._attachment_lock:
            if attempt_key in self._attachment_attempts:
                return
        record = self._session_record(session_id)
        if record is None:
            raise HostedRoomSessionError(
                "attachment.stage", 4007, "session not found"
            )
        lock = record.get("history_lock")
        if lock is None:
            raise HostedRoomSessionError(
                "attachment.stage", 5000, "session has no history lock"
            )
        with lock:
            snapshot = tuple(str(path) for path in record.get("attached_images", []))
        with self._attachment_lock:
            self._attachment_attempts.setdefault(attempt_key, snapshot)

    def commit_attachment_staging(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        execution_generation: int,
    ) -> None:
        """Drop attempt bookkeeping after prompt admission becomes ambiguous."""

        del profile, source
        self._forget_attachment_attempt(session_id, execution_generation)

    def rollback_attachment_staging(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        execution_generation: int,
    ) -> None:
        """Restore the pending-image queue after a pre-submit failure."""

        del profile, source
        attempt_key = (session_id, int(execution_generation))
        with self._attachment_lock:
            snapshot = self._attachment_attempts.pop(attempt_key, None)
            stale_keys = [
                key
                for key in self._staged_attachments
                if key[0] == session_id and key[2] == int(execution_generation)
            ]
            staged_results = [
                dict(self._staged_attachments.get(key) or {}) for key in stale_keys
            ]
            for key in stale_keys:
                self._staged_attachments.pop(key, None)
        for result in staged_results:
            self._remove_attempt_uploaded_file(session_id, result)
        if snapshot is None:
            return
        record = self._session_record(session_id)
        if record is None:
            return
        lock = record.get("history_lock")
        if lock is None:
            return
        with lock:
            record["attached_images"] = list(snapshot)

    def _remove_attempt_uploaded_file(
        self, session_id: str, result: Mapping[str, Any]
    ) -> None:
        """Delete only files materialized by this failed staging attempt."""

        if result.get("uploaded") is not True:
            return
        raw_path = str(result.get("path") or "")
        if not raw_path:
            return
        record = self._session_record(session_id)
        attachment_dir = getattr(self.server, "_desktop_attachment_dir", None)
        if record is None or not callable(attachment_dir):
            return
        try:
            root = Path(attachment_dir(record)).resolve()
            candidate = Path(raw_path).resolve()
            candidate.relative_to(root)
            info = candidate.lstat()
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                candidate.unlink(missing_ok=True)
        except (FileNotFoundError, OSError, ValueError):
            return

    def _forget_attachment_attempt(
        self, session_id: str, execution_generation: int
    ) -> None:
        attempt_key = (session_id, int(execution_generation))
        with self._attachment_lock:
            self._attachment_attempts.pop(attempt_key, None)
            stale_keys = [
                key
                for key in self._staged_attachments
                if key[0] == session_id and key[2] == int(execution_generation)
            ]
            for key in stale_keys:
                self._staged_attachments.pop(key, None)

    def history(
        self, *, profile: str, session_id: str, source: str
    ) -> Sequence[Mapping[str, Any]]:
        del source
        result = self._call(
            "session.history",
            {"profile": profile, "session_id": session_id},
        )
        rows = result.get("messages")
        return tuple(row for row in rows if isinstance(row, dict)) if isinstance(rows, list) else ()

    def _session_record(self, session_id: str) -> dict[str, Any] | None:
        with self.server._sessions_lock:
            record = self.server._sessions.get(session_id)
            if record is not None:
                return record
            for candidate in self.server._sessions.values():
                if str(candidate.get("session_key") or "") == session_id:
                    return candidate
        return None

    def info(self, *, profile: str, session_id: str, source: str) -> Mapping[str, Any]:
        del profile, source
        record = self._session_record(session_id)
        if record is None:
            return {"active": False, "task_id": None}
        lock = record.get("history_lock")
        if not isinstance(lock, type(threading.Lock())):
            return {"active": bool(record.get("running")), "task_id": None}
        with lock:
            task = record.get("_hosted_room_task")
            result = {
                "active": bool(record.get("running")),
                "task_id": task.get("task_id") if isinstance(task, dict) else None,
            }
            pending_reader = getattr(
                self.server, "_pending_approval_request_payload", None
            )
            pending = (
                pending_reader(str(record.get("session_key") or ""))
                if callable(pending_reader)
                else None
            )
            if pending:
                result["status"] = "waiting_for_approval"
                result["pending_approval"] = pending
            return result

    def approve(
        self,
        *,
        session_id: str,
        request_id: str,
        choice: str,
    ) -> Mapping[str, Any]:
        """Resolve one exact local room approval without broad policy changes."""
        return self._call(
            "approval.respond",
            {
                "session_id": session_id,
                "request_id": request_id,
                "choice": choice,
                "all": False,
            },
        )

    def interrupt(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        expected_task_id: str,
    ) -> Mapping[str, Any] | None:
        del source
        return self._call(
            "session.interrupt",
            {
                "profile": profile,
                "session_id": session_id,
                "expected_hosted_task_id": expected_task_id,
            },
        )
