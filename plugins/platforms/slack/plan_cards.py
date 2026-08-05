"""Native Slack plan-card rendering and durable projection state.

The todo tool remains authoritative. This module only stores the latest desired
Slack projection, the last applied projection, and the reverse route needed to
validate interactive payloads.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import random
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence


STATE_VERSION = 1
MAX_INTERACTIVE_TASKS = 10
MAX_USER_TASK_CONTENT_LENGTH = 3000
MAX_NATIVE_TASK_TITLE_LENGTH = 3000
MAX_ACTION_DEDUPE_IDS = 512
USER_TASK_ID_PREFIX = "user:"
INTERACTIVE_USER_TASK_STATUSES = frozenset({"in_progress", "completed"})
PLAN_ACTION_COMPLETE_REOPEN = "complete_reopen"
PLAN_ACTION_CANCEL = "cancel"
PLAN_ACTION_ADD_USER_TASK = "add_user_task"
PLAN_MUTATION_ACTIONS = frozenset({
    PLAN_ACTION_COMPLETE_REOPEN,
    PLAN_ACTION_CANCEL,
    PLAN_ACTION_ADD_USER_TASK,
})
_NATIVE_TITLE_TRUNCATION_SUFFIX = "... [truncated]"
_STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In progress",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
_NATIVE_STATUSES = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "complete",
    "cancelled": "error",
}
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _path_thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


def _json_loads_prefix(value: str) -> Any:
    text = value.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        data, _ = json.JSONDecoder().raw_decode(text)
        return data


def is_user_task_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(USER_TASK_ID_PREFIX)
        and len(value) > len(USER_TASK_ID_PREFIX)
    )


def is_interactive_user_task(task: Mapping[str, Any]) -> bool:
    return (
        is_user_task_id(task.get("id"))
        and task.get("status") in INTERACTIVE_USER_TASK_STATUSES
    )


def normalize_todos(todos: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in todos:
        if not isinstance(item, Mapping):
            continue
        task_id = str(item.get("id") or "")
        if not task_id:
            continue
        content = str(item.get("content") or "").strip() or "(no description)"
        status = str(item.get("status") or "pending").strip().lower()
        if status not in _NATIVE_STATUSES:
            status = "pending"
        normalized.append({"id": task_id, "content": content, "status": status})
    return normalized


def parse_todo_result(result: Any) -> Optional[list[dict[str, str]]]:
    """Return the full normalized todo list from a successful tool result."""
    if not isinstance(result, str) or not result.strip():
        return None
    try:
        data = _json_loads_prefix(result)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("todos"), list):
        return None
    return normalize_todos(data["todos"])


def snapshot_hash(todos: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(normalize_todos(todos), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_action_value(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_action_value(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, str) or not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def sign_private_metadata(payload: Mapping[str, Any], secret: Optional[bytes]) -> Optional[str]:
    if not secret:
        return None
    body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    signature = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return json.dumps({"payload": dict(payload), "signature": signature}, separators=(",", ":"))


def verify_private_metadata(value: Any, secret: Optional[bytes]) -> Optional[dict[str, Any]]:
    if not secret or not isinstance(value, str):
        return None
    try:
        envelope = json.loads(value)
        payload = envelope["payload"]
        signature = str(envelope["signature"])
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    except Exception:
        return None
    if not isinstance(payload, dict) or not hmac.compare_digest(signature, expected):
        return None
    return payload


@dataclass(frozen=True)
class RenderedPlan:
    text: str
    native_blocks: list[dict[str, Any]]
    fallback_blocks: list[dict[str, Any]]


class _RetryScheduleKind(Enum):
    NO_WORK = "no_work"
    DUE_NOW = "due_now"
    DUE_AT = "due_at"


@dataclass(frozen=True)
class _RetrySchedule:
    kind: _RetryScheduleKind
    deadline: Optional[float] = None


def _plain_text(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text[:3000], "emoji": True}


def _native_task_title(task: Mapping[str, str]) -> str:
    title = task["content"]
    if task["status"] == "cancelled":
        title = f"[cancelled] {title}"
    if len(title) <= MAX_NATIVE_TASK_TITLE_LENGTH:
        return title
    prefix_length = MAX_NATIVE_TASK_TITLE_LENGTH - len(_NATIVE_TITLE_TRUNCATION_SUFFIX)
    return title[:prefix_length] + _NATIVE_TITLE_TRUNCATION_SUFFIX


def _control_blocks(
    todos: list[dict[str, str]],
    *,
    revision: int,
    desired_hash: str,
    signing_secret: Optional[bytes],
    action_context: Optional[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    base = dict(action_context or {})
    base.update({"revision": revision, "snapshot_hash": desired_hash})
    encoded = encode_action_value(base)
    user_tasks = [task for task in todos if is_interactive_user_task(task)]
    over_capacity = len(user_tasks) > MAX_INTERACTIVE_TASKS
    completed = {
        task["id"] for task in user_tasks if task["status"] == "completed"
    }
    checkbox_options = [
        {
            "text": _plain_text(task["content"][:75]),
            "value": task["id"],
        }
        for task in user_tasks
    ]
    elements: list[dict[str, Any]] = []
    if checkbox_options and not over_capacity:
        checkbox: dict[str, Any] = {
            "type": "checkboxes",
            "action_id": "hermes_plan_complete",
            "options": checkbox_options,
            "confirm": {
                "title": _plain_text("Update tasks"),
                "text": _plain_text("Complete checked tasks and reopen unchecked tasks?"),
                "confirm": _plain_text("Update"),
                "deny": _plain_text("Back"),
            },
        }
        selected = [option for option in checkbox_options if option["value"] in completed]
        if selected:
            checkbox["initial_options"] = selected
        elements.append(checkbox)
    cancel_options = [
        {"text": _plain_text(task["content"][:75]), "value": task["id"]}
        for task in user_tasks
        if task["status"] == "in_progress"
    ]
    if cancel_options and not over_capacity:
        elements.append({
            "type": "static_select",
            "action_id": "hermes_plan_cancel",
            "placeholder": _plain_text("Cancel task"),
            "options": cancel_options,
        })
    if (
        signing_secret
        and not over_capacity
        and len(user_tasks) < MAX_INTERACTIVE_TASKS
    ):
        elements.append({
            "type": "button",
            "action_id": "hermes_plan_add",
            "text": _plain_text("Add user task"),
            "value": encoded,
        })
    elements.append({
        "type": "button",
        "action_id": "hermes_plan_refresh",
        "text": _plain_text("Refresh"),
        "value": encoded,
    })
    if not elements:
        return []
    return [{
        "type": "actions",
        "block_id": f"hermes-plan-controls-r{revision}-{desired_hash[:10]}",
        "elements": elements,
    }]


def build_plan_blocks(
    todos: Sequence[Mapping[str, Any]],
    *,
    revision: int,
    snapshot_hash: str,
    signing_secret: Optional[bytes],
    action_context: Optional[Mapping[str, Any]] = None,
) -> RenderedPlan:
    tasks = normalize_todos(todos)
    count = len(tasks)
    text = f"Hermes plan: {count} task{'s' if count != 1 else ''}"
    native_tasks = []
    for index, task in enumerate(tasks):
        native_tasks.append({
            "type": "task_card",
            "block_id": f"hermes-task-{index}-{task['id'][:70]}-r{revision}-{snapshot_hash[:8]}",
            "task_id": task["id"],
            "title": _native_task_title(task),
            "status": _NATIVE_STATUSES[task["status"]],
        })
    native = [{
        "type": "plan",
        "block_id": f"hermes-plan-r{revision}-{snapshot_hash[:10]}",
        "title": "Hermes plan",
        "tasks": native_tasks,
    }]
    native.extend(_control_blocks(
        tasks,
        revision=revision,
        desired_hash=snapshot_hash,
        signing_secret=signing_secret,
        action_context=action_context,
    ))

    lines = [f"*Hermes plan* ({count} tasks)"]
    for task in tasks:
        label = _STATUS_LABELS[task["status"]]
        lines.append(f"• *{label}* `{task['id']}` — {task['content']}")
    user_task_count = sum(is_interactive_user_task(task) for task in tasks)
    if user_task_count > MAX_INTERACTIVE_TASKS:
        lines.append(
            f"_User task mutation controls unavailable: this plan has {user_task_count} "
            f"mutable user tasks; the safe control limit is {MAX_INTERACTIVE_TASKS}. Refresh remains available._"
        )
    fallback_text = "\n".join(lines)
    chunks = [
        fallback_text[index:index + 3000]
        for index in range(0, len(fallback_text), 3000)
    ]
    control_blocks = _control_blocks(
        tasks,
        revision=revision,
        desired_hash=snapshot_hash,
        signing_secret=signing_secret,
        action_context=action_context,
    )
    max_fallback_blocks = 50
    if len(control_blocks) > max_fallback_blocks:
        control_blocks = []
    text_block_budget = max_fallback_blocks - len(control_blocks)
    truncated = len(chunks) > text_block_budget
    section_limit = text_block_budget
    if truncated and text_block_budget:
        section_limit -= 1
    fallback = [
        {
            "type": "section",
            "block_id": f"hermes-plan-fallback-r{revision}-{index}-{snapshot_hash[:8]}",
            "text": {"type": "mrkdwn", "text": chunk},
        }
        for index, chunk in enumerate(chunks[:section_limit])
    ]
    if truncated and text_block_budget:
        fallback.append({
            "type": "context",
            "block_id": f"hermes-plan-overflow-r{revision}-{snapshot_hash[:8]}",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    "Slack display truncated this plan because it exceeds block/text limits. "
                    "The complete task truth remains in Hermes todo."
                ),
            }],
        })
    if len(fallback) + len(control_blocks) <= max_fallback_blocks:
        fallback.extend(control_blocks)
    return RenderedPlan(text=text, native_blocks=native, fallback_blocks=fallback)


class PlanCardStore:
    """Native Slack plugin authority for its current desired Todo projection.

    This is durable Slack projection state, not a replacement for or claim of
    authority over the core TodoStore.
    """

    def __init__(self, hermes_home: Path | str):
        root = Path(hermes_home)
        self.state_path = root / "gateway" / "slack_plan_cards.json"
        self.lock_path = root / "gateway" / "slack_plan_cards.lock"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = _path_thread_lock(self.lock_path)
        with thread_lock:
            handle = self.lock_path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    if os.name == "nt":  # pragma: no cover
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _read_unlocked(self, *, strict: bool = False) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": STATE_VERSION, "sessions": {}, "routes": {}, "action_ids": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
                raise ValueError("invalid root")
            data.setdefault("routes", {})
            data.setdefault("action_ids", [])
            return data
        except Exception as exc:
            if strict:
                raise ValueError(f"Slack plan-card state is corrupt: {exc}") from exc
            return {"version": STATE_VERSION, "sessions": {}, "routes": {}, "action_ids": [], "_corrupt": True}

    def _write_unlocked(self, data: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.state_path)
            if hasattr(os, "O_DIRECTORY"):
                dir_fd = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _quarantine_corrupt_unlocked(self) -> None:
        if not self.state_path.exists():
            return
        suffix = f".corrupt.{int(time.time() * 1000)}.{os.getpid()}"
        os.replace(self.state_path, self.state_path.with_name(self.state_path.name + suffix))

    @staticmethod
    def _route_key(team_id: str, channel_id: str, message_ts: str) -> str:
        return "\x1f".join((str(team_id), str(channel_id), str(message_ts)))

    @staticmethod
    def _route_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        defaults = {"chat_type": "group"}
        for key in (
            "session_key", "session_id", "team_id", "channel_id",
            "thread_ts", "route_user_id", "chat_type", "profile",
        ):
            default = defaults.get(key, "")
            left_value = str(left.get(key) or "") if key in left else default
            right_value = str(right.get(key) or "") if key in right else default
            if left_value != right_value:
                return False
        return True

    @staticmethod
    def _retired_anchor_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "anchor_id": str(uuid.uuid4()),
            "session_key": str(state.get("session_key") or ""),
            "session_id": str(state.get("session_id") or ""),
            "team_id": str(state.get("team_id") or ""),
            "channel_id": str(state.get("channel_id") or ""),
            "thread_ts": str(state.get("thread_ts") or ""),
            "route_user_id": str(state.get("route_user_id") or ""),
            "profile": state.get("profile"),
            "chat_type": (
                str(state.get("chat_type") or "")
                if "chat_type" in state
                else "group"
            ),
            "message_ts": str(state.get("message_ts") or ""),
            "client_msg_id": str(state.get("client_msg_id") or ""),
            "create_attempted_at": float(state.get("create_attempted_at") or 0),
            "retry_count": 0,
            "next_retry_at": 0,
            "retired_at": time.time(),
        }

    def record_desired_snapshot(
        self,
        route: Mapping[str, Any],
        todos: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        session_key = str(route.get("session_key") or "").strip()
        if not session_key:
            raise ValueError("session_key is required")
        normalized = normalize_todos(todos)
        desired_hash = snapshot_hash(normalized)
        now = time.time()
        with self._locked():
            try:
                data = self._read_unlocked(strict=True)
            except ValueError:
                # Todo is authoritative. Quarantine unreadable projection state
                # and rebuild from this full successful snapshot; interactive
                # reads remain fail-closed until this recovery write occurs.
                self._quarantine_corrupt_unlocked()
                data = {"version": STATE_VERSION, "sessions": {}, "routes": {}, "action_ids": []}
            prior = dict(data["sessions"].get(session_key) or {})
            prior_message_ts = str(prior.get("message_ts") or "")
            def route_value(key: str, default: str = "") -> str:
                if key in route:
                    return str(route.get(key) or "")
                if key in prior:
                    return str(prior.get(key) or "")
                return default

            new_session_id = route_value("session_id")
            new_team_id = route_value("team_id")
            new_channel_id = route_value("channel_id")
            new_thread_ts = route_value("thread_ts")
            new_route_user_id = route_value("route_user_id")
            new_chat_type = route_value("chat_type", "group")
            new_profile = route.get("profile") if "profile" in route else prior.get("profile")
            if new_profile is not None:
                new_profile = str(new_profile)
            prior_chat_type = (
                str(prior.get("chat_type") or "")
                if "chat_type" in prior
                else "group"
            )
            route_changed = bool(prior) and any((
                new_session_id != str(prior.get("session_id") or ""),
                new_team_id != str(prior.get("team_id") or ""),
                new_channel_id != str(prior.get("channel_id") or ""),
                new_thread_ts != str(prior.get("thread_ts") or ""),
                new_route_user_id != str(prior.get("route_user_id") or ""),
                new_chat_type != prior_chat_type,
                str(new_profile or "") != str(prior.get("profile") or ""),
            ))
            retired_anchors = list(prior.get("retired_anchors") or [])
            if route_changed:
                if prior_message_ts:
                    data["routes"].pop(self._route_key(
                        prior.get("team_id", ""),
                        prior.get("channel_id", ""),
                        prior_message_ts,
                    ), None)
                if prior_message_ts or float(prior.get("create_attempted_at") or 0):
                    retired_anchors.append(self._retired_anchor_from_state(prior))
            revision = int(prior.get("desired_revision") or 0) + 1
            state = {
                **prior,
                "session_key": session_key,
                "session_id": new_session_id,
                "team_id": new_team_id,
                "channel_id": new_channel_id,
                "thread_ts": new_thread_ts,
                "route_user_id": new_route_user_id,
                "chat_type": new_chat_type,
                "profile": new_profile,
                "message_ts": "" if route_changed else prior_message_ts,
                "client_msg_id": "" if route_changed else str(prior.get("client_msg_id") or ""),
                "create_attempted_at": 0 if route_changed else float(prior.get("create_attempted_at") or 0),
                "retired_anchors": retired_anchors,
                "desired_revision": revision,
                "applied_revision": int(prior.get("applied_revision") or 0),
                "desired_hash": desired_hash,
                "applied_hash": str(prior.get("applied_hash") or ""),
                "applied_render_revision": int(prior.get("applied_render_revision") or 0),
                "last_desired_snapshot": normalized,
                "retry_count": 0,
                "next_retry_at": 0,
                "updated_at": now,
            }
            data["sessions"][session_key] = state
            self._write_unlocked(data)
            return dict(state)

    def get_session(self, session_key: str) -> Optional[dict[str, Any]]:
        with self._locked():
            data = self._read_unlocked()
            if data.get("_corrupt"):
                return None
            state = data["sessions"].get(str(session_key))
            return dict(state) if isinstance(state, dict) else None

    def list_dirty(self, *, now: Optional[float] = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._locked():
            data = self._read_unlocked()
            if data.get("_corrupt"):
                return []
            dirty = []
            for state in data["sessions"].values():
                current_due = (
                    int(state.get("desired_revision") or 0)
                    > int(state.get("applied_revision") or 0)
                    and float(state.get("next_retry_at") or 0) <= current
                )
                retired_due = any(
                    float(anchor.get("next_retry_at") or 0) <= current
                    for anchor in state.get("retired_anchors") or []
                )
                if current_due or retired_due:
                    dirty.append(dict(state))
            return dirty

    def retry_schedule(self, *, now: Optional[float] = None) -> _RetrySchedule:
        """Return the complete current/retired retry schedule snapshot."""
        current = time.time() if now is None else now
        earliest: Optional[float] = None
        due_now = False
        with self._locked():
            data = self._read_unlocked()
            if data.get("_corrupt"):
                return _RetrySchedule(_RetryScheduleKind.NO_WORK)
            for state in data["sessions"].values():
                if int(state.get("desired_revision") or 0) > int(
                    state.get("applied_revision") or 0
                ):
                    deadline = float(state.get("next_retry_at") or 0)
                    if deadline <= current:
                        due_now = True
                    else:
                        earliest = deadline if earliest is None else min(earliest, deadline)
                for anchor in state.get("retired_anchors") or []:
                    deadline = float(anchor.get("next_retry_at") or 0)
                    if deadline <= current:
                        due_now = True
                    else:
                        earliest = deadline if earliest is None else min(earliest, deadline)
        if due_now:
            return _RetrySchedule(_RetryScheduleKind.DUE_NOW)
        if earliest is not None:
            return _RetrySchedule(_RetryScheduleKind.DUE_AT, earliest)
        return _RetrySchedule(_RetryScheduleKind.NO_WORK)

    def prepare_create(
        self,
        session_key: str,
        *,
        expected_route: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Persist one UUID create identity and attempt marker before Slack I/O."""
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if (
                not isinstance(state, dict)
                or not self._route_matches(state, expected_route)
                or str(state.get("message_ts") or "")
            ):
                return None
            was_attempted = bool(float(state.get("create_attempted_at") or 0))
            client_msg_id = str(state.get("client_msg_id") or "")
            if not client_msg_id:
                client_msg_id = str(uuid.uuid4())
                state["client_msg_id"] = client_msg_id
            state["create_attempted_at"] = time.time()
            state["updated_at"] = time.time()
            self._write_unlocked(data)
            return {**state, "was_attempted": was_attempted}

    def mark_applied(
        self,
        session_key: str,
        *,
        revision: int,
        snapshot_hash: str,
        message_ts: str,
        rendered_revision: Optional[int] = None,
        expected_message_ts: Optional[str] = None,
        expected_client_msg_id: Optional[str] = None,
    ) -> bool:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict) or int(state.get("desired_revision") or 0) != int(revision):
                return False
            current_ts = str(state.get("message_ts") or "")
            result_ts = str(message_ts or "")
            if expected_client_msg_id is not None and str(state.get("client_msg_id") or "") != str(expected_client_msg_id):
                return False
            if expected_message_ts is not None and current_ts != str(expected_message_ts):
                same_idempotent_result = (
                    current_ts == result_ts
                    and bool(expected_client_msg_id)
                    and str(state.get("client_msg_id") or "") == str(expected_client_msg_id)
                )
                if not same_idempotent_result:
                    return False
            old_ts = str(state.get("message_ts") or "")
            if old_ts:
                data["routes"].pop(self._route_key(state.get("team_id", ""), state.get("channel_id", ""), old_ts), None)
            state["message_ts"] = str(message_ts)
            if expected_client_msg_id:
                state["client_msg_id"] = str(expected_client_msg_id)
            state["applied_revision"] = int(revision)
            state["applied_hash"] = str(snapshot_hash)
            if rendered_revision is not None or not int(state.get("applied_render_revision") or 0):
                state["applied_render_revision"] = int(
                    revision if rendered_revision is None else rendered_revision
                )
            state["retry_count"] = 0
            state["next_retry_at"] = 0
            state["updated_at"] = time.time()
            data["routes"][self._route_key(state.get("team_id", ""), state.get("channel_id", ""), message_ts)] = session_key
            self._write_unlocked(data)
            return True

    def record_create_result(
        self,
        session_key: str,
        *,
        expected_route: Mapping[str, Any],
        client_msg_id: str,
        message_ts: str,
    ) -> str:
        """Route a completed create to the matching current or retired generation."""
        created_ts = str(message_ts or "")
        if not created_ts:
            return "route_changed"
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return "route_changed"
            if (
                self._route_matches(state, expected_route)
                and str(state.get("client_msg_id") or "") == str(client_msg_id)
            ):
                current_ts = str(state.get("message_ts") or "")
                if current_ts and current_ts != created_ts:
                    return "conflict"
                route_key = self._route_key(
                    state.get("team_id", ""), state.get("channel_id", ""), created_ts
                )
                route_owner = str(data["routes"].get(route_key) or "")
                if route_owner and route_owner != session_key:
                    return "conflict"
                state["message_ts"] = created_ts
                state["updated_at"] = time.time()
                data["routes"][route_key] = session_key
                self._write_unlocked(data)
                return "current"
            for anchor in state.get("retired_anchors") or []:
                if (
                    self._route_matches(anchor, expected_route)
                    and str(anchor.get("client_msg_id") or "") == str(client_msg_id)
                ):
                    retired_ts = str(anchor.get("message_ts") or "")
                    if retired_ts and retired_ts != created_ts:
                        return "conflict"
                    anchor["message_ts"] = created_ts
                    anchor["updated_at"] = time.time()
                    self._write_unlocked(data)
                    return "retired"
            return "route_changed"

    def reset_missing_anchor(self, session_key: str, *, expected_message_ts: str) -> bool:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict) or str(state.get("message_ts") or "") != str(expected_message_ts):
                return False
            data["routes"].pop(self._route_key(
                state.get("team_id", ""), state.get("channel_id", ""), expected_message_ts
            ), None)
            state["message_ts"] = ""
            state["client_msg_id"] = ""
            state["create_attempted_at"] = 0
            state["applied_hash"] = ""
            state["applied_render_revision"] = 0
            state["updated_at"] = time.time()
            self._write_unlocked(data)
            return True

    def list_retired(self, session_key: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._locked():
            data = self._read_unlocked()
            state = data.get("sessions", {}).get(session_key)
            if not isinstance(state, dict):
                return []
            return [
                dict(anchor) for anchor in state.get("retired_anchors") or []
                if float(anchor.get("next_retry_at") or 0) <= current
            ]

    def mark_retired_retry(
        self,
        session_key: str,
        anchor_id: str,
        *,
        base_seconds: float = 1.0,
        max_seconds: float = 60.0,
    ) -> None:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return
            for anchor in state.get("retired_anchors") or []:
                if str(anchor.get("anchor_id") or "") != str(anchor_id):
                    continue
                count = int(anchor.get("retry_count") or 0) + 1
                delay = min(max_seconds, base_seconds * (2 ** min(count - 1, 8)))
                anchor["retry_count"] = count
                anchor["next_retry_at"] = time.time() + delay + random.uniform(0, delay * 0.2)
                self._write_unlocked(data)
                return

    def complete_retired_cleanup(self, session_key: str, anchor_id: str) -> bool:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return False
            before = list(state.get("retired_anchors") or [])
            after = [
                anchor for anchor in before
                if str(anchor.get("anchor_id") or "") != str(anchor_id)
            ]
            if len(after) == len(before):
                return False
            state["retired_anchors"] = after
            state["updated_at"] = time.time()
            self._write_unlocked(data)
            return True

    def retire_orphan_anchor(
        self,
        session_key: str,
        anchor: Mapping[str, Any],
    ) -> bool:
        """Durably queue a conflict-created remote message for cleanup."""
        message_ts = str(anchor.get("message_ts") or "")
        if not message_ts:
            return False
        identity = (
            str(anchor.get("team_id") or ""),
            str(anchor.get("channel_id") or ""),
            message_ts,
            str(anchor.get("client_msg_id") or ""),
        )
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return False
            retired = state.setdefault("retired_anchors", [])
            for existing in retired:
                existing_identity = (
                    str(existing.get("team_id") or ""),
                    str(existing.get("channel_id") or ""),
                    str(existing.get("message_ts") or ""),
                    str(existing.get("client_msg_id") or ""),
                )
                if existing_identity == identity:
                    return False
            candidate = self._retired_anchor_from_state({
                **anchor,
                "session_key": str(anchor.get("session_key") or session_key),
            })
            candidate["message_ts"] = message_ts
            candidate["client_msg_id"] = str(anchor.get("client_msg_id") or "")
            retired.append(candidate)
            state["updated_at"] = time.time()
            self._write_unlocked(data)
            return True

    def mark_retry(self, session_key: str, *, base_seconds: float = 1.0, max_seconds: float = 60.0) -> None:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return
            count = int(state.get("retry_count") or 0) + 1
            delay = min(max_seconds, base_seconds * (2 ** min(count - 1, 8)))
            state["retry_count"] = count
            state["next_retry_at"] = time.time() + delay + random.uniform(0, delay * 0.2)
            self._write_unlocked(data)

    def request_refresh(self, session_key: str) -> Optional[dict[str, Any]]:
        with self._locked():
            data = self._read_unlocked(strict=True)
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return None
            state["desired_revision"] = int(state.get("desired_revision") or 0) + 1
            state["applied_hash"] = ""
            state["next_retry_at"] = 0
            state["updated_at"] = time.time()
            self._write_unlocked(data)
            return dict(state)

    def lookup_route(self, team_id: str, channel_id: str, message_ts: str) -> Optional[dict[str, Any]]:
        with self._locked():
            data = self._read_unlocked()
            if data.get("_corrupt"):
                return None
            session_key = data["routes"].get(self._route_key(team_id, channel_id, message_ts))
            state = data["sessions"].get(session_key) if session_key else None
            return dict(state) if isinstance(state, dict) else None

    def validate_action(self, metadata: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        required = ("session_key", "session_id", "team_id", "channel_id", "message_ts", "revision", "snapshot_hash")
        if any(metadata.get(key) in (None, "") for key in required):
            return None
        with self._locked():
            data = self._read_unlocked()
            if data.get("_corrupt"):
                return None
            route_key = self._route_key(
                str(metadata["team_id"]),
                str(metadata["channel_id"]),
                str(metadata["message_ts"]),
            )
            session_key = str(data["routes"].get(route_key) or "")
            state = data["sessions"].get(session_key)
            if not isinstance(state, dict):
                return None
            comparisons = {
                "session_key": state.get("session_key"),
                "session_id": state.get("session_id"),
                "team_id": state.get("team_id"),
                "channel_id": state.get("channel_id"),
                "thread_ts": state.get("thread_ts"),
                "profile": state.get("profile"),
                "message_ts": state.get("message_ts"),
                "revision": state.get("desired_revision"),
                "snapshot_hash": state.get("desired_hash"),
            }
            if any(str(metadata.get(key)) != str(expected) for key, expected in comparisons.items()):
                return None
            action_kind = metadata.get("action_kind")
            if action_kind not in PLAN_MUTATION_ACTIONS:
                return None
            action_user_id = str(metadata.get("action_user_id") or "")
            if not action_user_id:
                return None
            route_user_id = str(state.get("route_user_id") or "")
            if route_user_id and action_user_id != route_user_id:
                return None
            action_dedupe_id = str(metadata.get("action_dedupe_id") or "")
            if not action_dedupe_id or action_dedupe_id not in {
                str(value) for value in data.get("action_ids") or []
            }:
                return None

            snapshot = state.get("last_desired_snapshot")
            if not isinstance(snapshot, list) or any(
                not isinstance(task, Mapping) for task in snapshot
            ):
                return None
            tasks = normalize_todos(snapshot)
            if len(tasks) != len(snapshot):
                return None
            snapshot_ids = [task["id"] for task in tasks]
            if len(snapshot_ids) != len(set(snapshot_ids)):
                return None
            if snapshot_hash(tasks) != str(state.get("desired_hash") or ""):
                return None
            by_id = {task["id"]: task for task in tasks}
            user_task_count = sum(is_interactive_user_task(task) for task in tasks)
            if user_task_count > MAX_INTERACTIVE_TASKS:
                return None

            def id_list(key: str) -> Optional[list[str]]:
                value = metadata.get(key)
                if not isinstance(value, list) or any(
                    not isinstance(task_id, str) or not task_id for task_id in value
                ):
                    return None
                if len(value) != len(set(value)):
                    return None
                return value

            task_ids = id_list("task_ids")
            if task_ids is None:
                return None
            change_keys = {
                "complete_task_ids", "reopen_task_ids", "cancel_task_ids",
                "add_task_ids", "add_task_content", "complete_task_status",
                "reopen_task_status", "cancel_task_status", "add_task_status",
            }

            if action_kind == PLAN_ACTION_COMPLETE_REOPEN:
                if any(key in metadata for key in change_keys - {
                    "complete_task_ids", "reopen_task_ids",
                    "complete_task_status", "reopen_task_status",
                }):
                    return None
                complete_ids = id_list("complete_task_ids")
                reopen_ids = id_list("reopen_task_ids")
                if (
                    complete_ids is None
                    or reopen_ids is None
                    or metadata.get("complete_task_status") != "completed"
                    or metadata.get("reopen_task_status") != "in_progress"
                ):
                    return None
                combined = complete_ids + reopen_ids
                if not combined or len(combined) != len(set(combined)):
                    return None
                if task_ids != sorted(combined):
                    return None
                if any(
                    not is_user_task_id(task_id)
                    or task_id not in by_id
                    or by_id[task_id]["status"] != "in_progress"
                    for task_id in complete_ids
                ):
                    return None
                if any(
                    not is_user_task_id(task_id)
                    or task_id not in by_id
                    or by_id[task_id]["status"] != "completed"
                    for task_id in reopen_ids
                ):
                    return None
            elif action_kind == PLAN_ACTION_CANCEL:
                if any(key in metadata for key in change_keys - {
                    "cancel_task_ids", "cancel_task_status",
                }):
                    return None
                cancel_ids = id_list("cancel_task_ids")
                if (
                    cancel_ids is None
                    or len(cancel_ids) != 1
                    or task_ids != cancel_ids
                    or metadata.get("cancel_task_status") != "cancelled"
                ):
                    return None
                task_id = cancel_ids[0]
                if (
                    not is_user_task_id(task_id)
                    or task_id not in by_id
                    or by_id[task_id]["status"] != "in_progress"
                ):
                    return None
            else:
                if any(key in metadata for key in change_keys - {
                    "add_task_ids", "add_task_content", "add_task_status",
                }):
                    return None
                add_ids = id_list("add_task_ids")
                content = metadata.get("add_task_content")
                if (
                    add_ids is None
                    or len(add_ids) != 1
                    or task_ids != add_ids
                    or not is_user_task_id(add_ids[0])
                    or add_ids[0] in by_id
                    or user_task_count >= MAX_INTERACTIVE_TASKS
                    or metadata.get("add_task_status") != "in_progress"
                    or not isinstance(content, str)
                    or not content.strip()
                    or len(content) > MAX_USER_TASK_CONTENT_LENGTH
                ):
                    return None
            return dict(state)

    def consume_action_id(self, action_id: str) -> bool:
        if not action_id:
            return False
        with self._locked():
            data = self._read_unlocked(strict=True)
            ids = [str(value) for value in data.get("action_ids") or []]
            if action_id in ids:
                return False
            ids.append(action_id)
            data["action_ids"] = ids[-MAX_ACTION_DEDUPE_IDS:]
            self._write_unlocked(data)
            return True


__all__ = [
    "MAX_INTERACTIVE_TASKS",
    "MAX_USER_TASK_CONTENT_LENGTH",
    "PLAN_ACTION_ADD_USER_TASK",
    "PLAN_ACTION_CANCEL",
    "PLAN_ACTION_COMPLETE_REOPEN",
    "PlanCardStore",
    "RenderedPlan",
    "build_plan_blocks",
    "decode_action_value",
    "encode_action_value",
    "is_interactive_user_task",
    "is_user_task_id",
    "normalize_todos",
    "parse_todo_result",
    "sign_private_metadata",
    "snapshot_hash",
    "USER_TASK_ID_PREFIX",
    "verify_private_metadata",
]
