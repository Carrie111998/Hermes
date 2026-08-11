"""
Crews — multi-agent crew orchestration backend.

A crew is a named group of specialised Hermes agents working toward a shared
goal. Each member maps to a named persona (Roger the frontender, Ada the QA,
Nova the security reviewer, …) and runs as a profile-scoped `hermes chat -q`
worker so its file system and session history stay isolated in
``~/.hermes/profiles/<name>/``.

This module owns:

* the crew store (``$HERMES_HOME/crews/crews.json``) and the per-crew DAG
  workflow store (``$HERMES_HOME/crews/workflows.json``),
* dispatch — spawning one profile worker per targeted member,
* workflow runs — executing a DAG in topological layers, one worker per task,
* a live ``/events`` WebSocket that streams member/task status + activity so
  the desktop UI can render a real-time feed.

Route prefix (mounted by the dashboard): ``/api/plugins/crews``.

Adapted from the crews + workflow features of the community
`JPeetz/Hermes-Studio` web UI, re-architected for Hermes' plugin surface:
file-backed JSON stores instead of TanStack server routes, and the
battle-tested `hermes chat -q` worker-spawn pattern from the kanban
dispatcher instead of a long-lived SSE server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

router = APIRouter()

# ─── Locations ────────────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _crews_root() -> Path:
    root = _hermes_home() / "crews"
    root.mkdir(parents=True, exist_ok=True)
    return root


CREWS_FILE = None  # resolved lazily via _crews_root()
WORKFLOWS_FILE = None


def _crews_file() -> Path:
    return _crews_root() / "crews.json"


def _workflows_file() -> Path:
    return _crews_root() / "workflows.json"


def _logs_dir(crew_id: str) -> Path:
    d = _crews_root() / "logs" / crew_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_home(name: str) -> Path:
    return _hermes_home() / "profiles" / name


# ─── Personas ─────────────────────────────────────────────────────────────────

PERSONAS: list[dict[str, Any]] = [
    {"id": "roger", "name": "Roger", "role": "Frontend Developer", "emoji": "🎨", "color": "text-blue-400",
     "specialties": ["react", "css", "tailwind", "ui", "ux", "component", "layout", "style", "design", "frontend", "page", "landing"]},
    {"id": "sally", "name": "Sally", "role": "Backend Architect", "emoji": "🏗️", "color": "text-purple-400",
     "specialties": ["api", "server", "database", "backend", "node", "express", "route", "endpoint", "schema", "migration", "sql", "rpc"]},
    {"id": "bill", "name": "Bill", "role": "Marketing Expert", "emoji": "📣", "color": "text-orange-400",
     "specialties": ["marketing", "seo", "content", "copy", "brand", "social", "campaign", "analytics", "growth"]},
    {"id": "ada", "name": "Ada", "role": "QA Engineer", "emoji": "🔍", "color": "text-emerald-400",
     "specialties": ["test", "qa", "bug", "fix", "error", "debug", "lint", "type", "typescript", "validate", "audit"]},
    {"id": "max", "name": "Max", "role": "DevOps Specialist", "emoji": "⚙️", "color": "text-amber-400",
     "specialties": ["deploy", "docker", "ci", "cd", "build", "config", "infra", "monitor", "log", "performance"]},
    {"id": "luna", "name": "Luna", "role": "Research Analyst", "emoji": "🔬", "color": "text-cyan-400",
     "specialties": ["research", "analyze", "compare", "report", "data", "insight", "strategy", "plan", "review"]},
    {"id": "kai", "name": "Kai", "role": "Full-Stack Engineer", "emoji": "⚡", "color": "text-yellow-400",
     "specialties": ["fullstack", "feature", "implement", "build", "create", "scaffold", "refactor", "update", "upgrade"]},
    {"id": "nova", "name": "Nova", "role": "Security Specialist", "emoji": "🛡️", "color": "text-red-400",
     "specialties": ["security", "auth", "permission", "encrypt", "vulnerability", "scan", "protect", "firewall", "token"]},
]

PERSONA_BY_ID = {p["id"]: p for p in PERSONAS}

DEFAULT_ROLE_BY_PERSONA: dict[str, str] = {
    "roger": "executor", "sally": "executor", "kai": "executor", "max": "executor",
    "ada": "reviewer", "luna": "reviewer", "nova": "reviewer", "bill": "specialist",
}

# ─── Templates ────────────────────────────────────────────────────────────────

CREW_TEMPLATES: list[dict[str, Any]] = [
    {"id": "research-team", "name": "Research Team", "category": "Research",
     "goal": "Research the topic, cross-check sources, and produce a cited report.",
     "members": [{"persona": "luna"}, {"persona": "ada"}]},
    {"id": "fullstack-squad", "name": "Full-Stack Squad", "category": "Engineering",
     "goal": "Ship the feature end-to-end: frontend, backend, and integration.",
     "members": [{"persona": "roger"}, {"persona": "sally"}, {"persona": "kai"}]},
    {"id": "code-review-crew", "name": "Code Review Crew", "category": "Engineering",
     "goal": "Review the change for correctness, security, and regressions.",
     "members": [{"persona": "ada"}, {"persona": "nova"}, {"persona": "luna"}]},
    {"id": "ops-team", "name": "Ops Team", "category": "Operations",
     "goal": "Deploy, monitor, and harden the service.",
     "members": [{"persona": "max"}, {"persona": "nova"}]},
]

# ─── Stores ───────────────────────────────────────────────────────────────────

CREW_STATUSES = {"draft", "active", "paused", "complete"}
MEMBER_STATUSES = {"idle", "running", "done", "error"}
TASK_STATUSES = {"idle", "running", "done", "error"}

_store: dict[str, Any] = {"crews": {}}
_workflow_store: dict[str, Any] = {"workflows": {}}
_save_timer: Optional[asyncio.TimerHandle] = None

# ─── Live event bus ───────────────────────────────────────────────────────────

_subscribers: set[WebSocket] = set()
# (crew_id, member_id | task_id) → WorkerRecord for in-flight workers
_workers: dict[tuple[str, str], "WorkerRecord"] = {}
# run_id → RunState for in-flight + recent workflow runs
_runs: dict[str, "RunState"] = {}


class WorkerRecord:
    __slots__ = ("proc", "log_path", "crew_id", "member_id", "task_id", "started_at")

    def __init__(self, proc, log_path: Path, crew_id: str, member_id: Optional[str], task_id: Optional[str]):
        self.proc = proc
        self.log_path = log_path
        self.crew_id = crew_id
        self.member_id = member_id
        self.task_id = task_id
        self.started_at = time.time()


class RunState:
    __slots__ = ("id", "crew_id", "status", "tasks", "started_at", "finished_at")

    def __init__(self, crew_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.crew_id = crew_id
        self.status = "running"  # running | complete | error
        self.tasks: dict[str, str] = {}  # task_id → status
        self.started_at = time.time()
        self.finished_at: Optional[float] = None


def _load_store() -> None:
    global _store, _workflow_store
    try:
        f = _crews_file()
        if f.exists():
            parsed = json.loads(f.read_text("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("crews"), dict):
                _store = parsed
    except Exception:
        pass
    try:
        f = _workflows_file()
        if f.exists():
            parsed = json.loads(f.read_text("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("workflows"), dict):
                # Guard against old data missing x/y positions.
                for wf in parsed["workflows"].values():
                    tasks = wf.get("tasks") or []
                    for i, task in enumerate(tasks):
                        task.setdefault("x", 80 + (i % 4) * 220)
                        task.setdefault("y", 80 + (i // 4) * 120)
                _workflow_store = parsed
    except Exception:
        pass


def _schedule_save() -> None:
    global _save_timer
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync/threadpool context) — write immediately.
        _save_now()
        return

    async def _flush() -> None:
        _save_now()

    if _save_timer is not None:
        _save_timer.cancel()
    _save_timer = loop.call_later(1.0, lambda: asyncio.ensure_future(_flush()))


def _save_now() -> None:
    try:
        _crews_file().write_text(json.dumps(_store, indent=2, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    try:
        _workflows_file().write_text(json.dumps(_workflow_store, indent=2, ensure_ascii=False), "utf-8")
    except Exception:
        pass


# ─── Event broadcast ──────────────────────────────────────────────────────────

def _broadcast(event: dict[str, Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop — nothing to notify (sync/threadpool context)
    payload = json.dumps(event, ensure_ascii=False)
    dead: list[WebSocket] = []
    for ws in list(_subscribers):
        try:
            loop.create_task(ws.send_text(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _subscribers.discard(ws)


def _ws_upgrade_authorized(ws: "WebSocket") -> bool:
    """Delegate to the dashboard's canonical WS auth gate (same as kanban)."""
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        return True  # bare-FastAPI test harness
    return bool(_ws._ws_auth_ok(ws))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _member_dict(member: dict[str, Any]) -> dict[str, Any]:
    return dict(member)


def _crew_dict(crew: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": crew["id"],
        "name": crew["name"],
        "goal": crew["goal"],
        "status": crew["status"],
        "createdAt": crew["createdAt"],
        "updatedAt": crew["updatedAt"],
        "members": [_member_dict(m) for m in crew["members"]],
    }


def _get_crew(crew_id: str) -> Optional[dict[str, Any]]:
    return _store["crews"].get(crew_id)


def _touch(crew: dict[str, Any]) -> None:
    crew["updatedAt"] = int(time.time() * 1000)


def _profile_name_for(member: dict[str, Any]) -> str:
    """Resolve the worker profile for a member — explicit profileName or the
    persona slug. Sanitised so it can never escape the profiles directory."""
    raw = (member.get("profileName") or member["persona"]).strip()
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-") or "agent"
    return sanitized[:64]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv parts (kanban pattern)."""
    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        return [env_bin]
    which = shutil.which("hermes")
    if which:
        return [which]
    return [sys.executable, "-m", "hermes_cli.main"]


def _tail_log(path: Path, max_chars: int = 400) -> str:
    try:
        data = path.read_text("utf-8", errors="replace")
    except Exception:
        return ""
    data = data.strip()
    if len(data) <= max_chars:
        return data
    return "…" + data[-max_chars:]


async def _watch_worker(record: WorkerRecord) -> None:
    """Await a worker subprocess, update statuses, tail the log, broadcast."""
    crew_id, member_id, task_id = record.crew_id, record.member_id, record.task_id
    last_tail = ""

    async def _pulse() -> None:
        nonlocal last_tail
        try:
            tail = _tail_log(record.log_path)
        except Exception:
            tail = ""
        if tail and tail != last_tail:
            last_tail = tail
            _broadcast({
                "type": "activity",
                "crewId": crew_id,
                "memberId": member_id,
                "taskId": task_id,
                "text": tail[-300:],
                "ts": _now_iso(),
            })

    pulse_task = asyncio.ensure_future(_pulse_loop(2.0, _pulse))
    try:
        await asyncio.to_thread(record.proc.wait)
        code = record.proc.returncode
        status = "done" if code == 0 else "error"
        detail = "" if code == 0 else f" (exit {code})"
    except Exception as exc:
        status = "error"
        detail = f" ({exc})"
    finally:
        pulse_task.cancel()
        _workers.pop((crew_id, member_id or task_id or ""), None)

    tail = _tail_log(record.log_path)
    if member_id:
        _set_member_status(crew_id, member_id, status, tail)
    if task_id:
        _set_task_status(crew_id, task_id, status, tail)
    _broadcast({
        "type": "worker_end",
        "crewId": crew_id,
        "memberId": member_id,
        "taskId": task_id,
        "status": status,
        "detail": detail,
        "activity": tail[-300:],
        "ts": _now_iso(),
    })


async def _pulse_loop(interval: float, fn) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await fn()
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


def _spawn_worker(crew: dict[str, Any], member: dict[str, Any], task_text: str,
                  task_id: Optional[str] = None) -> WorkerRecord:
    """Spawn a profile-scoped ``hermes -p <profile> chat -q <task>`` worker."""
    profile = _profile_name_for(member)
    profile_home = _profile_home(profile)
    profile_home.mkdir(parents=True, exist_ok=True)

    cmd = _resolve_hermes_argv() + ["-p", profile, "chat", "-q", task_text]
    if member.get("model"):
        cmd += ["-m", str(member["model"])]

    log_path = _logs_dir(crew["id"]) / f"{task_id or member['id']}.log"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_home)
    env["TERM"] = "dumb"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(profile_home),
            stdin=subprocess.DEVNULL,
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "`hermes` executable not found on PATH. Install Hermes Agent or "
            "activate its venv before dispatching crew work."
        )

    record = WorkerRecord(proc, log_path, crew["id"], member["id"], task_id)
    _workers[(crew["id"], task_id or member["id"])] = record
    asyncio.ensure_future(_watch_worker(record))
    return record


def _set_member_status(crew_id: str, member_id: str, status: str, activity: Optional[str] = None) -> None:
    crew = _get_crew(crew_id)
    if not crew:
        return
    member = next((m for m in crew["members"] if m["id"] == member_id), None)
    if not member:
        return
    if status not in MEMBER_STATUSES:
        return
    member["status"] = status
    if activity is not None:
        member["lastActivity"] = activity
    _touch(crew)
    _schedule_save()


def _set_task_status(crew_id: str, task_id: str, status: str, activity: Optional[str] = None) -> None:
    workflow = _workflow_store["workflows"].get(crew_id)
    if not workflow:
        return
    task = next((t for t in (workflow.get("tasks") or []) if t["id"] == task_id), None)
    if not task:
        return
    task["status"] = status
    if activity is not None:
        task["lastActivity"] = activity
    workflow["updatedAt"] = int(time.time() * 1000)
    _schedule_save()


def _validate_dag(tasks: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Raise HTTPException 400 on unknown refs or cycles (Kahn)."""
    task_ids = {t["id"] for t in tasks}
    for e in edges:
        if e["from"] not in task_ids or e["to"] not in task_ids:
            raise HTTPException(status_code=400, detail="Edge references unknown task id")

    indegree = {tid: 0 for tid in task_ids}
    adj: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indegree[e["to"]] += 1
    queue = [tid for tid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in adj[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(task_ids):
        raise HTTPException(status_code=400, detail="Workflow contains a cycle")


def _topo_layers(tasks: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[list[str]]:
    """Kahn BFS layers — parallel columns of tasks that can run together."""
    task_ids = [t["id"] for t in tasks]
    indegree = {tid: 0 for tid in task_ids}
    adj: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indegree[e["to"]] += 1

    layers: list[list[str]] = []
    frontier = [tid for tid in task_ids if indegree[tid] == 0]
    while frontier:
        layers.append(frontier)
        nxt: list[str] = []
        for node in frontier:
            for child in adj[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        frontier = nxt
    return layers


# ─── Pydantic bodies ──────────────────────────────────────────────────────────

class MemberInput(BaseModel):
    persona: str
    role: Optional[str] = None
    model: Optional[str] = None
    profileName: Optional[str] = None


class CreateCrewBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=2000)
    members: list[MemberInput] = Field(default_factory=list, max_length=8)


class PatchCrewBody(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    status: Optional[str] = None
    memberId: Optional[str] = None
    memberStatus: Optional[str] = None
    lastActivity: Optional[str] = None


class DispatchBody(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    target: Optional[str] = None  # member id, or omitted/"all"


class WorkflowTaskBody(BaseModel):
    id: str
    label: str = ""
    prompt: str = ""
    assigneeId: Optional[str] = None
    x: float = 0
    y: float = 0


class WorkflowEdgeBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class UpsertWorkflowBody(BaseModel):
    tasks: list[WorkflowTaskBody]
    edges: list[WorkflowEdgeBody]


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/personas")
def list_personas() -> dict[str, Any]:
    return {"ok": True, "personas": PERSONAS}


@router.get("/templates")
def list_templates() -> dict[str, Any]:
    return {"ok": True, "templates": CREW_TEMPLATES}


@router.get("/crews")
def list_crews() -> dict[str, Any]:
    crews = sorted(_store["crews"].values(), key=lambda c: c["updatedAt"], reverse=True)
    return {"ok": True, "crews": [_crew_dict(c) for c in crews]}


@router.post("/crews")
async def create_crew(body: CreateCrewBody) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(body.members) > 8:
        raise HTTPException(status_code=400, detail="A crew may have at most 8 members")

    now = int(time.time() * 1000)
    crew = {
        "id": uuid.uuid4().hex,
        "name": name,
        "goal": body.goal.strip(),
        "status": "draft",
        "createdAt": now,
        "updatedAt": now,
        "members": [],
    }
    for m in body.members:
        persona = PERSONA_BY_ID.get(m.persona)
        if persona is None:
            raise HTTPException(status_code=400, detail=f"Unknown persona: {m.persona}")
        crew["members"].append({
            "id": uuid.uuid4().hex,
            "persona": persona["id"],
            "displayName": f"{persona['emoji']} {persona['name']}",
            "roleLabel": persona["role"],
            "color": persona["color"],
            "role": m.role or DEFAULT_ROLE_BY_PERSONA.get(persona["id"], "executor"),
            "model": m.model or None,
            "profileName": m.profileName or None,
            "status": "idle",
            "lastActivity": None,
        })
    _store["crews"][crew["id"]] = crew
    _save_now()
    _broadcast({"type": "crew_updated", "crewId": crew["id"], "ts": _now_iso()})
    return {"ok": True, "crew": _crew_dict(crew)}


@router.get("/crews/{crew_id}")
def get_crew(crew_id: str) -> dict[str, Any]:
    crew = _get_crew(crew_id)
    if not crew:
        raise HTTPException(status_code=404, detail="Crew not found")
    return {"ok": True, "crew": _crew_dict(crew)}


@router.patch("/crews/{crew_id}")
async def patch_crew(crew_id: str, body: PatchCrewBody) -> dict[str, Any]:
    crew = _get_crew(crew_id)
    if not crew:
        raise HTTPException(status_code=404, detail="Crew not found")

    if body.memberId is not None:
        member = next((m for m in crew["members"] if m["id"] == body.memberId), None)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if body.memberStatus is not None:
            if body.memberStatus not in MEMBER_STATUSES:
                raise HTTPException(status_code=400, detail="Invalid member status")
            member["status"] = body.memberStatus
        if body.lastActivity is not None:
            member["lastActivity"] = body.lastActivity
        _touch(crew)
        _schedule_save()
        return {"ok": True, "crew": _crew_dict(crew)}

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        crew["name"] = name
    if body.goal is not None:
        crew["goal"] = body.goal.strip()
    if body.status is not None:
        if body.status not in CREW_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid crew status")
        crew["status"] = body.status
    _touch(crew)
    _schedule_save()
    _broadcast({"type": "crew_updated", "crewId": crew_id, "ts": _now_iso()})
    return {"ok": True, "crew": _crew_dict(crew)}


@router.delete("/crews/{crew_id}")
async def delete_crew(crew_id: str) -> dict[str, Any]:
    if crew_id not in _store["crews"]:
        raise HTTPException(status_code=404, detail="Crew not found")
    del _store["crews"][crew_id]
    _workflow_store["workflows"].pop(crew_id, None)
    _save_now()
    _broadcast({"type": "crew_deleted", "crewId": crew_id, "ts": _now_iso()})
    return {"ok": True}


@router.post("/crews/{crew_id}/clone")
async def clone_crew(crew_id: str) -> dict[str, Any]:
    crew = _get_crew(crew_id)
    if not crew:
        raise HTTPException(status_code=404, detail="Crew not found")
    now = int(time.time() * 1000)
    clone = {
        "id": uuid.uuid4().hex,
        "name": f"{crew['name']} (copy)",
        "goal": crew["goal"],
        "status": "draft",
        "createdAt": now,
        "updatedAt": now,
        "members": [
            {
                "id": uuid.uuid4().hex,
                "persona": m["persona"],
                "displayName": m["displayName"],
                "roleLabel": m["roleLabel"],
                "color": m["color"],
                "role": m.get("role", "executor"),
                "model": m.get("model"),
                "profileName": m.get("profileName"),
                "status": "idle",
                "lastActivity": None,
            }
            for m in crew["members"]
        ],
    }
    _store["crews"][clone["id"]] = clone
    _save_now()
    return {"ok": True, "crew": _crew_dict(clone)}


@router.post("/crews/{crew_id}/dispatch")
async def dispatch_crew(crew_id: str, body: DispatchBody) -> dict[str, Any]:
    crew = _get_crew(crew_id)
    if not crew:
        raise HTTPException(status_code=404, detail="Crew not found")
    task = body.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    target = body.target
    if target and target != "all":
        members = [m for m in crew["members"] if m["id"] == target]
        if not members:
            raise HTTPException(status_code=400, detail="no matching members found")
    else:
        members = list(crew["members"])
    if not members:
        raise HTTPException(status_code=400, detail="crew has no members")

    crew["status"] = "active"
    _touch(crew)

    dispatched: list[str] = []
    for member in members:
        member["status"] = "running"
        dispatched.append(member["id"])
        _broadcast({"type": "member_status", "crewId": crew_id, "memberId": member["id"],
                    "status": "running", "ts": _now_iso()})
        try:
            _spawn_worker(crew, member, task)
        except RuntimeError as exc:
            member["status"] = "error"
            _broadcast({"type": "member_status", "crewId": crew_id, "memberId": member["id"],
                        "status": "error", "detail": str(exc), "ts": _now_iso()})
    _schedule_save()
    return {"ok": True, "dispatched": dispatched, "crewId": crew_id}


@router.get("/crews/{crew_id}/workflow")
def get_workflow(crew_id: str) -> dict[str, Any]:
    if crew_id not in _store["crews"]:
        raise HTTPException(status_code=404, detail="Crew not found")
    workflow = _workflow_store["workflows"].get(crew_id)
    if workflow is None:
        return {"ok": True, "workflow": None}
    return {"ok": True, "workflow": {
        "id": workflow["id"],
        "crewId": workflow["crewId"],
        "tasks": workflow.get("tasks", []),
        "edges": workflow.get("edges", []),
        "createdAt": workflow["createdAt"],
        "updatedAt": workflow["updatedAt"],
    }}


@router.put("/crews/{crew_id}/workflow")
async def upsert_workflow(crew_id: str, body: UpsertWorkflowBody) -> dict[str, Any]:
    if crew_id not in _store["crews"]:
        raise HTTPException(status_code=404, detail="Crew not found")

    tasks = [t.model_dump(by_alias=True) for t in body.tasks]
    edges = [e.model_dump(by_alias=True) for e in body.edges]
    for task in tasks:
        task.pop("from_", None)
        task.pop("status", None)
        task.pop("lastActivity", None)

    _validate_dag(tasks, edges)

    now = int(time.time() * 1000)
    existing = _workflow_store["workflows"].get(crew_id)
    if existing:
        existing.update({"tasks": tasks, "edges": edges, "updatedAt": now})
        workflow = existing
    else:
        workflow = {
            "id": uuid.uuid4().hex,
            "crewId": crew_id,
            "tasks": tasks,
            "edges": edges,
            "createdAt": now,
            "updatedAt": now,
        }
        _workflow_store["workflows"][crew_id] = workflow
    _schedule_save()
    return {"ok": True, "workflow": {
        "id": workflow["id"], "crewId": workflow["crewId"],
        "tasks": workflow["tasks"], "edges": workflow["edges"],
        "createdAt": workflow["createdAt"], "updatedAt": workflow["updatedAt"],
    }}


@router.delete("/crews/{crew_id}/workflow")
async def delete_workflow(crew_id: str) -> dict[str, Any]:
    if crew_id not in _store["crews"]:
        raise HTTPException(status_code=404, detail="Crew not found")
    _workflow_store["workflows"].pop(crew_id, None)
    _schedule_save()
    return {"ok": True}


@router.post("/crews/{crew_id}/workflow/run")
async def run_workflow(crew_id: str) -> dict[str, Any]:
    crew = _get_crew(crew_id)
    if not crew:
        raise HTTPException(status_code=404, detail="Crew not found")
    workflow = _workflow_store["workflows"].get(crew_id)
    if workflow is None or not workflow.get("tasks"):
        raise HTTPException(status_code=400, detail="No workflow defined for this crew")
    tasks = workflow["tasks"]
    edges = workflow["edges"]
    _validate_dag(tasks, edges)

    run = RunState(crew_id)
    for task in tasks:
        run.tasks[task["id"]] = "idle"
        task["status"] = "idle"
        task.pop("lastActivity", None)
    _runs[run.id] = run
    if len(_runs) > 20:
        # Keep only the 20 most recent runs per crew in memory.
        for rid in sorted(_runs, key=lambda r: _runs[r].started_at)[: max(0, len(_runs) - 20)]:
            if _runs[rid].crew_id == crew_id and _runs[rid].status != "running":
                _runs.pop(rid, None)

    crew["status"] = "active"
    _touch(crew)
    _schedule_save()
    _broadcast({"type": "run_started", "crewId": crew_id, "runId": run.id, "ts": _now_iso()})
    asyncio.ensure_future(_execute_workflow_run(crew_id, run, tasks, edges))
    return {"ok": True, "runId": run.id}


async def _execute_workflow_run(crew_id: str, run: RunState, tasks: list[dict[str, Any]],
                                edges: list[dict[str, Any]]) -> None:
    crew = _get_crew(crew_id)
    if not crew:
        run.status = "error"
        return
    member_by_id = {m["id"]: m for m in crew["members"]}
    task_by_id = {t["id"]: t for t in tasks}
    layers = _topo_layers(tasks, edges)

    try:
        for layer in layers:
            futures: list[asyncio.Future] = []
            for task_id in layer:
                task = task_by_id[task_id]
                run.tasks[task_id] = "running"
                task["status"] = "running"
                _schedule_save()
                _broadcast({"type": "task_status", "crewId": crew_id, "runId": run.id,
                            "taskId": task_id, "status": "running", "ts": _now_iso()})

                prompt = (task.get("prompt") or "").strip()
                if not prompt:
                    run.tasks[task_id] = "done"
                    task["status"] = "done"
                    _broadcast({"type": "task_status", "crewId": crew_id, "runId": run.id,
                                "taskId": task_id, "status": "done", "ts": _now_iso()})
                    continue

                assignee_id = task.get("assigneeId")
                if assignee_id:
                    targets = [member_by_id[assignee_id]] if assignee_id in member_by_id else []
                else:
                    targets = list(crew["members"])
                for member in targets:
                    member["status"] = "running"
                    try:
                        _spawn_worker(crew, member, prompt, task_id=task_id)
                    except RuntimeError as exc:
                        run.tasks[task_id] = "error"
                        task["status"] = "error"
                        _broadcast({"type": "task_status", "crewId": crew_id, "runId": run.id,
                                    "taskId": task_id, "status": "error", "detail": str(exc), "ts": _now_iso()})
                        break
                    futures.append(asyncio.ensure_future(_wait_worker(crew_id, member["id"], task_id)))

            if futures:
                await asyncio.gather(*futures, return_exceptions=True)

        run.status = "complete"
        run.finished_at = time.time()
        _broadcast({"type": "run_end", "crewId": crew_id, "runId": run.id,
                    "status": "complete", "ts": _now_iso()})
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("workflow run %s failed: %s", run.id, exc)
        run.status = "error"
        run.finished_at = time.time()
        _broadcast({"type": "run_end", "crewId": crew_id, "runId": run.id,
                    "status": "error", "ts": _now_iso()})


async def _wait_worker(crew_id: str, member_id: str, task_id: str) -> None:
    """Wait for a task's worker(s) to finish and fold the outcome into the run."""
    key = (crew_id, task_id)
    record = _workers.get(key)
    if record is None:
        return
    try:
        await asyncio.to_thread(record.proc.wait)
    except Exception:
        pass
    status = "done" if record.proc.returncode == 0 else "error"
    run = next((r for r in _runs.values() if r.crew_id == crew_id and r.status == "running"), None)
    if run is not None:
        run.tasks[task_id] = status
    task = next((t for t in (_workflow_store["workflows"].get(crew_id, {}).get("tasks") or [])
                 if t["id"] == task_id), None)
    if task is not None:
        task["status"] = status
    _schedule_save()
    _broadcast({"type": "task_status", "crewId": crew_id, "runId": run.id if run else None,
                "taskId": task_id, "status": status, "ts": _now_iso()})


@router.get("/crews/{crew_id}/runs")
def list_runs(crew_id: str) -> dict[str, Any]:
    runs = [r for r in _runs.values() if r.crew_id == crew_id]
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return {"ok": True, "runs": [
        {
            "id": r.id, "crewId": r.crew_id, "status": r.status,
            "tasks": r.tasks, "startedAt": r.started_at, "finishedAt": r.finished_at,
        }
        for r in runs
    ]}


@router.websocket("/events")
async def events_socket(ws: WebSocket) -> None:
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    _subscribers.add(ws)
    try:
        while True:
            await ws.receive_text()  # ping/pong keepalive; ignore payloads
    except WebSocketDisconnect:
        pass
    finally:
        _subscribers.discard(ws)


# Bootstrap on module import (guarded for reloads/tests).
_load_store()
