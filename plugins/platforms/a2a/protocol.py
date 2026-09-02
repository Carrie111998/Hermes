"""
A2A protocol helpers — Agent Card construction, JSON-RPC framing, task store,
and disk-backed conversation persistence.

Wire shape follows A2A Protocol v1.0 (JSON-RPC 2.0 binding over HTTP):
  - Agent Card served at GET /.well-known/agent-card.json (canonical v1.0; legacy agent.json also answers)
  - Tasks via POST {jsonrpc:"2.0", method:"message/send", params:{...}}
  - Streaming via ``message/stream`` → SSE; events are StreamResponse objects
    discriminated by member presence (``statusUpdate`` / ``artifactUpdate``),
    stream closure signals the terminal state (no ``final`` field in v1.0)
  - Task states / message roles are v1.0 SCREAMING_SNAKE_CASE enums
  - Parts are the v1.0 unified shape ({"text": ..., "mediaType": ...}),
    discriminated by member presence (no ``kind`` field)
  - Push notification configs carry ``configId`` + ``createdAt`` and can be
    passed inline in ``message/send`` via configuration.taskPushNotificationConfig

We deliberately implement the subset of A2A needed for text task exchange with
stdlib only (no a2a-sdk). ``extract_text`` stays tolerant of v0.3 peers.
"""

from __future__ import annotations

import json
import copy
import os
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

PROTOCOL_VERSION = "1.0"

# A2A v1.0 task lifecycle states.
STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_WORKING = "TASK_STATE_WORKING"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"
STATE_CANCELED = "TASK_STATE_CANCELED"
STATE_REJECTED = "TASK_STATE_REJECTED"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELED, STATE_REJECTED})

# A2A v1.0 message roles.
ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

# The agent starts its reply with this marker when it needs clarification from
# the peer before it can complete the task; the adapter maps such replies to
# TASK_STATE_INPUT_REQUIRED (marker stripped, text in status.message).
INPUT_REQUIRED_MARKER = "[INPUT_REQUIRED]"

# JSON-RPC / A2A error codes.
# -32001..-32003 are A2A spec-defined and used only with their spec semantics.
# Custom errors live at -32050..-32059 (JSON-RPC implementation-defined server
# error space, clear of the A2A-reserved block).
ERR_PARSE = -32700
ERR_INVALID_PARAMS = -32602
ERR_METHOD_NOT_FOUND = -32601
ERR_TASK_NOT_FOUND = -32001        # A2A spec: TaskNotFoundError
ERR_TASK_NOT_CANCELABLE = -32002   # A2A spec: TaskNotCancelableError
ERR_PUSH_NOT_SUPPORTED = -32003    # A2A spec: PushNotificationNotSupportedError
ERR_UNSUPPORTED_OPERATION = -32004 # A2A spec: UnsupportedOperationError
ERR_UNAUTHORIZED = -32050
ERR_RATE_LIMITED = -32051
ERR_UNTRUSTED_PEER = -32052

# Maximum turns an A2A conversation can have before anti-loop kicks in.
# Default 5, configurable via A2A_MAX_PINGPONG_TURNS env (max 20).
_DEFAULT_MAX_PINGPONG = 5
_HARD_MAX_PINGPONG = 20


def max_pingpong_turns() -> int:
    try:
        v = int(os.getenv("A2A_MAX_PINGPONG_TURNS", str(_DEFAULT_MAX_PINGPONG)))
        return max(1, min(v, _HARD_MAX_PINGPONG))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_PINGPONG


def now_iso() -> str:
    """ISO 8601 UTC timestamp with millisecond precision (A2A v1.0)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# Agent Card (v1.0)
# --------------------------------------------------------------------------

def build_agent_card(
    *,
    name: str,
    url: str,
    description: str,
    skills: Optional[list[dict]] = None,
    streaming: bool = False,
    push_notifications: bool = False,
    auth_required: bool = False,
    tenant: str = "",
) -> dict:
    """Construct an A2A v1.0 Agent Card document.

    ``tenant`` is the optional v1.0 multi-tenancy routing key advertised on
    AgentInterface. When present, clients MUST echo it in request params.
    """
    iface: dict[str, Any] = {
        "url": url,
        "protocolBinding": "JSONRPC",
        "protocolVersion": PROTOCOL_VERSION,
    }
    if tenant:
        iface["tenant"] = tenant

    card: dict[str, Any] = {
        "name": name,
        "description": description,
        "url": url,  # convenience for pre-1.0 clients; canonical is supportedInterfaces
        "version": "1.0.0",
        "provider": {
            "organization": os.getenv("A2A_PROVIDER_ORG", "Hermes Agent"),
            "url": os.getenv("A2A_PROVIDER_URL", "") or url,
        },
        "supportedInterfaces": [iface],
        "capabilities": {
            "streaming": streaming,
            "pushNotifications": push_notifications,
            "stateTransitionHistory": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills or [],
    }
    if auth_required:
        card["securitySchemes"] = {
            "bearer": {"type": "http", "scheme": "bearer"}
        }
        card["security"] = [{"bearer": []}]
    return card


def skills_from_toolsets(toolsets: "list[str] | dict[str, list[str]] | None") -> list[dict]:
    """Derive A2A skill descriptors from the agent's toolsets.

    Accepts either a plain list of toolset names, or a mapping of toolset name
    → tool names (built from the live tool registry for dynamic Agent Cards —
    tool names become tags so peers can match tasks to us).
    """
    skills = []
    if isinstance(toolsets, dict):
        for ts_name in sorted(toolsets.keys()):
            tool_names = [str(t) for t in (toolsets[ts_name] or [])]
            skills.append({
                "id": f"toolset.{ts_name}",
                "name": ts_name,
                "description": f"Hermes '{ts_name}' capabilities",
                "tags": [ts_name] + tool_names[:10],
            })
    else:
        for ts in sorted(set(toolsets or [])):
            skills.append({
                "id": f"toolset.{ts}",
                "name": ts,
                "description": f"Hermes '{ts}' capabilities",
                "tags": [ts],
            })
    if not skills:
        skills.append({
            "id": "general",
            "name": "general",
            "description": "General-purpose conversational agent",
            "tags": ["general"],
        })
    return skills


# --------------------------------------------------------------------------
# JSON-RPC framing
# --------------------------------------------------------------------------

def jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def send_message_response(payload: dict) -> dict:
    """A2A v1.0 SendMessageResponse oneof wrapper.

    The JSON-RPC ``SendMessage`` result is not a bare Task/Message; it is a
    wrapper containing exactly one of ``task`` or ``message``. Legacy methods
    still return bare payloads for compatibility.
    """
    if isinstance(payload, dict) and payload.get("status") and payload.get("id"):
        return {"task": payload}
    return {"message": payload}


def unwrap_send_message_response(result: Any) -> Any:
    """Return the Task/Message inside a v1.0 response, or pass legacy through."""
    if isinstance(result, dict):
        if isinstance(result.get("task"), dict):
            return result["task"]
        if isinstance(result.get("message"), dict):
            return result["message"]
    return result


def stream_task(task: dict) -> dict:
    """v1.0 StreamResponse with a task member."""
    return {"task": task}


def stream_message(message: dict) -> dict:
    """v1.0 StreamResponse with a message member."""
    return {"message": message}


def new_task_id() -> str:
    return "task-" + uuid.uuid4().hex[:16]


def new_context_id() -> str:
    return "ctx-" + uuid.uuid4().hex[:16]


def text_part(text: str) -> dict:
    """Build a v1.0 text Part (member-presence discriminated, no ``kind``)."""
    return {"text": text, "mediaType": "text/plain"}


def file_part(url: str = "", raw: str = "", filename: str = "",
              media_type: str = "application/octet-stream") -> dict:
    """Build a v1.0 file Part.

    Either ``url`` (file reference) or ``raw`` (base64-encoded bytes) must be
    provided. Discrimination is by member presence — no ``kind`` field.
    """
    part: dict[str, Any] = {"mediaType": media_type}
    if filename:
        part["filename"] = filename
    if url:
        part["url"] = url
    elif raw:
        part["raw"] = raw
    return part


def data_part(data: Any, media_type: str = "application/json") -> dict:
    """Build a v1.0 data Part (structured data, no ``kind`` field)."""
    return {"data": data, "mediaType": media_type}


def text_message(
    role: str,
    text: str,
    context_id: str = "",
    message_id: str = "",
) -> dict:
    """Build an A2A v1.0 Message with a single text Part."""
    msg: dict[str, Any] = {
        "role": role,  # ROLE_USER | ROLE_AGENT
        "parts": [text_part(text)],
        "messageId": message_id or uuid.uuid4().hex,
    }
    if context_id:
        msg["contextId"] = context_id
    return msg


def message_with_parts(role: str, parts: list[dict], context_id: str = "") -> dict:
    """Build an A2A v1.0 Message with arbitrary Parts (text, file, data)."""
    msg: dict[str, Any] = {
        "role": role,
        "parts": parts,
        "messageId": uuid.uuid4().hex,
    }
    if context_id:
        msg["contextId"] = context_id
    return msg


def extract_text(message_or_params: dict) -> str:
    """Pull concatenated text from an A2A Message / Task-result / params payload.

    v1.0 Parts carry a ``text`` member directly; v0.3 used ``kind: "text"``
    and some pre-0.3 peers used ``type``. All three shapes put the payload in
    ``part["text"]``, so presence of a string ``text`` member is the test.

    File and data Parts are rendered into the text stream so the agent sees
    them: file Parts with a URL include the URL and filename; data Parts
    include their JSON-serialised content. Raw (base64) file Parts are noted
    but not decoded (the agent can't act on binary inline).
    """
    msg = message_or_params.get("message", message_or_params)
    parts = msg.get("parts", []) if isinstance(msg, dict) else []
    chunks = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        # v1.0 text part (member-presence discrimination)
        txt = part.get("text")
        if isinstance(txt, str):
            chunks.append(txt)
            continue
        # v0.3 compatibility: kind == "text"
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
            continue
        # v1.0 file part with URL
        url = part.get("url")
        if isinstance(url, str) and url:
            fname = part.get("filename") or part.get("name") or ""
            mtype = part.get("mediaType") or part.get("mimeType") or ""
            label = f"[file: {fname}]" if fname else "[file]"
            chunks.append(f"{label} {url}" + (f" ({mtype})" if mtype else ""))
            continue
        # v0.3 file part with nested file.fileWithUri
        v03_file = part.get("file")
        if isinstance(v03_file, dict) and isinstance(v03_file.get("fileWithUri"), str):
            uri = v03_file["fileWithUri"]
            fname = v03_file.get("name") or ""
            mtype = v03_file.get("mimeType") or ""
            label = f"[file: {fname}]" if fname else "[file]"
            chunks.append(f"{label} {uri}" + (f" ({mtype})" if mtype else ""))
            continue
        # v1.0 file part with raw bytes (base64) — note but don't decode
        if isinstance(part.get("raw"), str):
            fname = part.get("filename") or ""
            mtype = part.get("mediaType") or ""
            label = f"[file: {fname}]" if fname else "[file]"
            size_note = f"{len(part['raw'])} bytes base64-encoded"
            chunks.append(f"{label} {size_note}" + (f" ({mtype})" if mtype else ""))
            continue
        # v1.0 data part — include JSON content
        data = part.get("data")
        if data is not None:
            try:
                rendered = json.dumps(data, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                rendered = str(data)
            mtype = part.get("mediaType") or "application/json"
            chunks.append(f"[data ({mtype})]\n{rendered}")
            continue
        # v0.3 data part: kind == "data"
        if part.get("kind") == "data" and part.get("data") is not None:
            try:
                rendered = json.dumps(part["data"], ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                rendered = str(part["data"])
            chunks.append(f"[data]\n{rendered}")
            continue
    return "\n".join(chunks).strip()


def extract_context_id(params: dict) -> str:
    """v1.0 puts contextId inside the Message; tolerate legacy top-level."""
    msg = params.get("message") or {}
    ctx = ""
    if isinstance(msg, dict):
        ctx = str(msg.get("contextId") or "")
    return ctx or str(params.get("contextId") or "")


def build_task(
    task_id: str,
    context_id: str,
    state: str,
    agent_text: str = "",
    *,
    created_at: str = "",
    status_timestamp: str = "",
    artifact_id: str = "",
    status_message_id: str = "",
) -> dict:
    """Build an A2A v1.0 Task object for a message/send result.

    ``created_at`` is accepted for call-site compatibility but not serialized —
    the A2A v1.0 ``Task`` proto (``lf.a2a.v1.Task``) has no ``createdAt`` or
    ``lastModified`` field.  Strict ProtoJSON parsers (e.g. a2a-sdk 1.1.0)
    reject unknown fields, so we must not include them.  The spec's §5.6.1
    timestamp-format example mentions them but they are not in the proto.
    ``status_timestamp``, ``artifact_id``, and ``status_message_id`` let stored
    tasks render the same wire object on every side-effect-free GetTask poll;
    one-shot responses omit them and receive fresh values.
    """
    now = status_timestamp or now_iso()
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": now},
    }
    if agent_text:
        task["status"]["message"] = text_message(
            ROLE_AGENT,
            agent_text,
            context_id,
            message_id=status_message_id,
        )
        if state == STATE_COMPLETED:
            task["artifacts"] = [{
                "artifactId": artifact_id or uuid.uuid4().hex,
                "parts": [text_part(agent_text)],
            }]
    return task


# --------------------------------------------------------------------------
# Streaming (v1.0 StreamResponse events)
# --------------------------------------------------------------------------

def status_update(task_id: str, context_id: str, state: str, text: str = "") -> dict:
    """v1.0 StreamResponse with a statusUpdate member."""
    status: dict[str, Any] = {"state": state, "timestamp": now_iso()}
    if text:
        status["message"] = text_message(ROLE_AGENT, text, context_id)
    return {"statusUpdate": {"taskId": task_id, "contextId": context_id, "status": status}}


def artifact_update(task_id: str, context_id: str, text: str) -> dict:
    """v1.0 StreamResponse with an artifactUpdate member."""
    return {
        "artifactUpdate": {
            "taskId": task_id,
            "contextId": context_id,
            "artifact": {
                "artifactId": uuid.uuid4().hex,
                "parts": [text_part(text)],
            },
        }
    }


def sse_data(payload: dict, req_id: Any = None) -> str:
    """Encode one StreamResponse as a JSON-RPC-wrapped SSE data frame.

    A2A v1.0 §9.4 requires each SSE frame to be a full JSON-RPC response:
    ``{"jsonrpc":"2.0","id":<req_id>,"result":{StreamResponse}}``.  Emitting a
    bare StreamResponse (the REST binding shape) breaks JSON-RPC clients that
    expect the envelope, including the official a2a-sdk.
    """
    if req_id is not None:
        envelope = jsonrpc_result(req_id, payload)
    else:
        envelope = payload  # legacy/fallback — no envelope
    return f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"


def sse_done() -> str:
    """SSE stream-closure marker — a comment, not a parseable data frame.

    A2A v1.0 signals terminal state by closing the stream.  Emitting
    ``data: {}`` causes JSON-RPC clients to try parsing an empty response and
    fail.  An SSE comment line (``: done``) is ignored by all SSE parsers.
    """
    return ": done\n\n"


# --------------------------------------------------------------------------
# Anti-loop ping-pong protection (per-adapter instance)
# --------------------------------------------------------------------------

class TurnTracker:
    """Counts inbound turns per context_id to stop infinite agent↔agent loops.

    A "turn" is one inbound message/send from a peer. When the count exceeds
    max_pingpong_turns(), the adapter rejects further messages for that context.
    """

    _TTL = 3600  # prune contexts idle longer than 1 hour

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._timestamps: dict[str, float] = {}
        self._lock = threading.Lock()

    def track(self, context_id: str) -> int:
        """Increment and return the turn count; prunes stale contexts."""
        with self._lock:
            now = time.time()
            stale = [cid for cid, ts in self._timestamps.items() if now - ts > self._TTL]
            for cid in stale:
                self._counts.pop(cid, None)
                self._timestamps.pop(cid, None)
            self._counts[context_id] += 1
            self._timestamps[context_id] = now
            return self._counts[context_id]

    def reset(self, context_id: str) -> None:
        """Reset turn count for a context (e.g. after explicit cancel)."""
        with self._lock:
            self._counts.pop(context_id, None)
            self._timestamps.pop(context_id, None)


# --------------------------------------------------------------------------
# Rate limiting (sliding window per authenticated peer identity)
# --------------------------------------------------------------------------

_RATE_LIMIT_DEFAULT = 60  # requests per minute
_RATE_WINDOW = 60.0  # seconds


def _rate_limit_per_minute() -> int:
    try:
        return max(1, int(os.getenv("A2A_RATE_LIMIT", str(_RATE_LIMIT_DEFAULT))))
    except (ValueError, TypeError):
        return _RATE_LIMIT_DEFAULT


class RateLimiter:
    """Sliding-window request limiter, one bucket per authenticated identity."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, identity: str) -> bool:
        with self._lock:
            limit = _rate_limit_per_minute()
            now = time.time()
            bucket = self._buckets[identity]
            while bucket and now - bucket[0] > _RATE_WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


# --------------------------------------------------------------------------
# Metrics collection
# --------------------------------------------------------------------------

# Module-level singleton shared by the inbound adapter and the outbound client
# tools so /metrics and a2a_list report both directions. Not persisted.
class Metrics:
    """Simple counters for A2A operations."""

    def __init__(self) -> None:
        self.inbound_total = 0
        self.outbound_total = 0
        self.streams_started = 0
        self.push_sent = 0
        self.push_failed = 0
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.anti_loop_triggers = 0
        self.rate_limit_triggers = 0
        self._start_time = time.time()
        # Rolling latency tracking (last 100 completed inbound tasks)
        self._latencies: deque[float] = deque(maxlen=100)

    def record_latency(self, seconds: float) -> None:
        self._latencies.append(seconds)

    def avg_latency(self) -> float:
        if not self._latencies:
            return 0.0
        return sum(self._latencies) / len(self._latencies)

    def snapshot(self) -> dict[str, Any]:
        uptime = time.time() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "inbound_total": self.inbound_total,
            "outbound_total": self.outbound_total,
            "streams_started": self.streams_started,
            "push_sent": self.push_sent,
            "push_failed": self.push_failed,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "anti_loop_triggers": self.anti_loop_triggers,
            "rate_limit_triggers": self.rate_limit_triggers,
            "avg_latency_ms": round(self.avg_latency() * 1000, 1),
        }


metrics = Metrics()


# --------------------------------------------------------------------------
# Task store — active AND finalized tasks (queryable via tasks/get, tasks/list)
# --------------------------------------------------------------------------

class TaskStore:
    """In-memory store of active and finalized A2A tasks.

    Finalized records include terminal and interrupted states and remain
    queryable until bounded retention trims them. Records carry the routed
    agent slug and tenant. All read/write helpers accept optional scope values
    and return not-found when the task exists but is not visible in that scope,
    satisfying the spec's authorization scoping rule.
    """

    _MAX_TERMINAL = 500

    def __init__(self) -> None:
        self._tasks: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._watchers: dict[str, list[Future]] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._completion_sequence = 0

    @staticmethod
    def _in_scope(rec: dict, agent_slug: str = "", tenant: str = "") -> bool:
        if agent_slug and rec.get("agent_slug", "") != agent_slug:
            return False
        if tenant and rec.get("tenant", "") != tenant:
            return False
        return True

    def create(self, task_id: str, context_id: str, peer: str,
               agent_slug: str = "", tenant: str = "") -> dict:
        created_iso = now_iso()
        rec = {
            "task_id": task_id,
            "context_id": context_id,
            "peer": peer,
            "agent_slug": agent_slug or "",
            "tenant": tenant or "",
            "state": STATE_SUBMITTED,
            "reply": "",
            "progress": "",
            "created_at": time.time(),
            "created_iso": created_iso,
            "status_iso": created_iso,
            "revision": 0,
            "artifact_id": uuid.uuid4().hex,
            "status_message_id": uuid.uuid4().hex,
            "push_url": "",
            "push_config_id": "",
        }
        with self._lock:
            self._tasks[task_id] = rec
        return dict(rec)

    def set_state(self, task_id: str, state: str) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec and rec.get("completed_at") is None:
                rec["state"] = state
                rec["status_iso"] = now_iso()
                rec["revision"] = int(rec.get("revision", 0)) + 1
                self._condition.notify_all()

    def set_progress(self, task_id: str, text: str) -> bool:
        """Replace the latest non-terminal progress snapshot for a task."""
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or rec.get("completed_at") is not None:
                return False
            rec["progress"] = text
            rec["status_iso"] = now_iso()
            rec["revision"] = int(rec.get("revision", 0)) + 1
            self._condition.notify_all()
            return True

    def set_push_config(self, task_id: str, url: str,
                        agent_slug: str = "", tenant: str = "") -> Optional[dict]:
        """Attach a push notification config; returns the stored config or None."""
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant):
                return None
            rec["push_url"] = url
            rec["push_config_id"] = "cfg-" + uuid.uuid4().hex[:12]
            return self._push_config_view(rec)

    @staticmethod
    def _push_config_view(rec: dict) -> dict:
        """Build the JSON-RPC result for a push notification config."""
        return {
            "configId": rec.get("push_config_id") or "",
            "taskId": rec["task_id"],
            "createdAt": rec.get("created_iso", ""),
            "pushNotificationConfig": {"url": rec.get("push_url") or ""},
        }

    def get_push_config(self, task_id: str, config_id: str = "",
                        agent_slug: str = "", tenant: str = "") -> Optional[dict]:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant) or not rec.get("push_url"):
                return None
            if config_id and rec.get("push_config_id") != config_id:
                return None
            return self._push_config_view(rec)

    def list_push_configs(self, task_id: str, agent_slug: str = "", tenant: str = "") -> list[dict]:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant) or not rec.get("push_url"):
                return []
            return [self._push_config_view(rec)]

    def delete_push_config(self, task_id: str, config_id: str = "",
                           agent_slug: str = "", tenant: str = "") -> bool:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant) or not rec.get("push_url"):
                return False
            if config_id and rec.get("push_config_id") != config_id:
                return False
            rec["push_url"] = ""
            rec["push_config_id"] = ""
            return True

    def pop_push_url(self, task_id: str) -> str:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec:
                return ""
            url, rec["push_url"] = rec["push_url"], ""
            return url

    def get(self, task_id: str, agent_slug: str = "", tenant: str = "") -> Optional[dict]:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant):
                return None
            return dict(rec)

    def complete(self, task_id: str, state: str, reply: str = "") -> Optional[dict]:
        """Finalize one dispatch cycle. Idempotent, including interrupted states."""
        watchers: list[Future] = []
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec:
                return None
            if rec.get("completed_at") is not None:
                can_cancel_interrupted = (
                    rec.get("state") in {STATE_INPUT_REQUIRED, STATE_AUTH_REQUIRED}
                    and state == STATE_CANCELED
                )
                if not can_cancel_interrupted:
                    return None
            rec["state"] = state
            rec["reply"] = reply
            rec["completed_at"] = time.time()
            self._completion_sequence += 1
            rec["completion_seq"] = self._completion_sequence
            rec["status_iso"] = now_iso()
            rec["revision"] = int(rec.get("revision", 0)) + 1
            watchers = self._watchers.pop(task_id, [])
            self._trim_locked()
            self._condition.notify_all()
            out = dict(rec)
        for fut in watchers:
            if not fut.done():
                fut.set_result((state, reply))
        return out

    def watch(self, task_id: str, agent_slug: str = "", tenant: str = "") -> Optional[Future]:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant):
                return None
            fut: Future = Future()
            if rec.get("completed_at") is not None:
                fut.set_result((rec["state"], rec.get("reply", "")))
            else:
                self._watchers.setdefault(task_id, []).append(fut)
            return fut

    def wait_for_update(
        self,
        task_id: str,
        revision: int,
        timeout: float,
        agent_slug: str = "",
        tenant: str = "",
    ) -> Optional[dict]:
        """Wait until a task's status snapshot changes, then return it.

        A timeout returns the current unchanged record so SSE callers can emit
        a keepalive without losing their subscription.
        """
        with self._condition:
            def changed() -> bool:
                current = self._tasks.get(task_id)
                return (
                    current is None
                    or not self._in_scope(current, agent_slug, tenant)
                    or int(current.get("revision", 0)) != revision
                )

            self._condition.wait_for(changed, timeout=max(0.0, timeout))
            rec = self._tasks.get(task_id)
            if not rec or not self._in_scope(rec, agent_slug, tenant):
                return None
            return dict(rec)

    def list(
        self,
        context_id: str = "",
        state: str = "",
        page_size: int = 50,
        offset: int = 0,
        agent_slug: str = "",
        tenant: str = "",
        with_total: bool = False,
    ):
        """Filtered task page (newest first).

        Historical API returns ``(records, next_offset)``. v1.0 ListTasks needs
        ``totalSize``, so callers can opt into ``(records, next_offset, total)``.
        """
        page_size = max(1, min(int(page_size or 50), 100))
        with self._lock:
            recs = [dict(r) for r in reversed(self._tasks.values())]
        if agent_slug or tenant:
            recs = [r for r in recs if self._in_scope(r, agent_slug, tenant)]
        if context_id:
            recs = [r for r in recs if r["context_id"] == context_id]
        if state:
            recs = [r for r in recs if r["state"] == state]
        total = len(recs)
        page = recs[offset:offset + page_size]
        next_offset = offset + page_size if offset + page_size < total else 0
        if with_total:
            return page, next_offset, total
        return page, next_offset

    def orphan_ids(self, timeout_seconds: int = 300,
                   skip: Optional[set[str]] = None) -> List[str]:
        skip = skip or set()
        with self._lock:
            now = time.time()
            return [
                tid for tid, rec in self._tasks.items()
                if tid not in skip
                and rec.get("completed_at") is None
                and now - rec["created_at"] > timeout_seconds
            ]

    def fail_orphans(self, timeout_seconds: int = 300,
                     skip: Optional[set[str]] = None) -> List[str]:
        stale = self.orphan_ids(timeout_seconds, skip)
        failed = []
        for tid in stale:
            if self.complete(tid, STATE_FAILED, "[task orphaned — no reply produced]"):
                failed.append(tid)
        return failed

    def _trim_locked(self) -> None:
        terminal = sorted(
            (
                (tid, int(rec.get("completion_seq") or 0))
                for tid, rec in self._tasks.items()
                if rec.get("completed_at") is not None
            ),
            key=lambda item: item[1],
        )
        excess = len(terminal) - self._MAX_TERMINAL
        for tid, _completed_at in terminal[:max(0, excess)]:
            self._tasks.pop(tid, None)

    @staticmethod
    def to_task(rec: dict, history_length: Optional[int] = None, include_artifacts: bool = True) -> dict:
        """Render a stored record as an A2A v1.0 Task object."""
        text = rec.get("reply", "")
        if rec.get("state") in (STATE_SUBMITTED, STATE_WORKING):
            text = rec.get("progress", "")
        task = build_task(
            rec["task_id"],
            rec["context_id"],
            rec["state"],
            text,
            created_at=rec.get("created_iso", ""),
            status_timestamp=rec.get("status_iso", ""),
            artifact_id=rec.get("artifact_id", ""),
            status_message_id=rec.get("status_message_id", ""),
        )
        if not include_artifacts:
            task.pop("artifacts", None)
        if history_length == 0:
            task.pop("history", None)
        return copy.deepcopy(task)

# --------------------------------------------------------------------------
# Conversation persistence (outside the context-compaction pipeline)
# --------------------------------------------------------------------------

_conversation_lock = threading.Lock()


@contextmanager
def _conversation_file_lock(path: Path):
    """Serialize a conversation read-modify-append across Hermes processes."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _conv_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        base = Path(get_hermes_home())
    except Exception:
        base = Path(os.path.expanduser("~/.hermes"))
    return base / "a2a_conversations"


def _safe_name(context_id: str) -> str:
    return "".join(c for c in (context_id or "default") if c.isalnum() or c in "-_") or "default"


def persist_message(context_id: str, role: str, text: str, task_id: str = "") -> None:
    """Append one message to the context's on-disk conversation log."""
    try:
        d = _conv_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "role": role, "text": text, "task_id": task_id}
        with _conversation_lock:
            with (d / f"{_safe_name(context_id)}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def persist_message_once(context_id: str, role: str, text: str,
                         task_id: str) -> bool:
    """Append a task result once, returning whether a record was written."""
    if not task_id:
        return False
    try:
        directory = _conv_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_name(context_id)}.jsonl"
        with _conversation_lock:
            with _conversation_file_lock(path):
                if path.exists():
                    with path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                existing = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if (
                                existing.get("role") == role
                                and existing.get("task_id") == task_id
                            ):
                                return False
                record = {
                    "ts": time.time(),
                    "role": role,
                    "text": text,
                    "task_id": task_id,
                }
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load_conversation(context_id: str, limit: int = 50) -> list[dict]:
    """Load the last *limit* messages for a context (empty list if none)."""
    path = _conv_dir() / f"{_safe_name(context_id)}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return out[-limit:]


def list_conversations() -> list[str]:
    """Return known context-ids that have persisted conversations."""
    d = _conv_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.jsonl"))
