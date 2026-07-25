#!/usr/bin/env python3
"""Todoist -> Hermes Kanban bridge.

Todoist remains the system of record. This script classifies Todoist tasks,
creates idempotent Kanban handoff cards for agent-suitable work, and posts
Kanban completion evidence back as Todoist comments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - standalone skill checkout fallback
    def get_hermes_home() -> str:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


API_BASE = "https://api.todoist.com/rest/v2"
LEDGER_VERSION = 1
AUTHOR = "todoist-kanban"


@dataclass
class BridgeConfig:
    board: Optional[str] = None
    tenant: str = "todoist"
    default_assignee: Optional[str] = None
    created_by: str = AUTHOR
    workspace: str = "scratch"
    priority_offset: int = 0
    include_labels: set[str] = field(default_factory=lambda: {"hermes", "agent"})
    agent_labels: set[str] = field(default_factory=lambda: {"hermes", "agent", "codex", "automatable"})
    human_labels: set[str] = field(default_factory=lambda: {"human", "manual", "errand", "call", "waiting"})
    exclude_labels: set[str] = field(default_factory=lambda: {"human", "manual", "waiting"})
    include_projects: set[str] = field(default_factory=set)
    exclude_projects: set[str] = field(default_factory=set)
    comment_prefix: str = "Hermes Kanban"
    max_tasks: int = 100
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    max_runtime: Optional[int] = None
    skills: list[str] = field(default_factory=list)


def hermes_home() -> Path:
    return Path(get_hermes_home()).expanduser()


def _normal_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    out: set[str] = set()
    for value in values if isinstance(values, Iterable) else []:
        text = str(value).strip().casefold()
        if text:
            out.add(text)
    return out


def _normal_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, Iterable) else []:
        text = str(value).strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _read_dotenv() -> None:
    path = hermes_home() / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def load_config() -> BridgeConfig:
    data: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config as load_hermes_config

        raw = load_hermes_config()
        if isinstance(raw.get("todoist_kanban"), dict):
            data = raw["todoist_kanban"]
    except Exception:
        data = {}

    cfg = BridgeConfig()
    if "board" in data:
        cfg.board = str(data.get("board") or "").strip() or None
    if "tenant" in data:
        cfg.tenant = str(data.get("tenant") or "todoist").strip() or "todoist"
    if "default_assignee" in data:
        cfg.default_assignee = str(data.get("default_assignee") or "").strip() or None
    if "created_by" in data:
        cfg.created_by = str(data.get("created_by") or AUTHOR).strip() or AUTHOR
    if "workspace" in data:
        cfg.workspace = str(data.get("workspace") or "scratch").strip() or "scratch"
    if "priority_offset" in data:
        cfg.priority_offset = int(data.get("priority_offset") or 0)
    if "include_labels" in data:
        cfg.include_labels = _normal_set(data.get("include_labels"))
    if "agent_labels" in data:
        cfg.agent_labels = _normal_set(data.get("agent_labels"))
    if "human_labels" in data:
        cfg.human_labels = _normal_set(data.get("human_labels"))
    if "exclude_labels" in data:
        cfg.exclude_labels = _normal_set(data.get("exclude_labels"))
    if "include_projects" in data:
        cfg.include_projects = _normal_set(data.get("include_projects"))
    if "exclude_projects" in data:
        cfg.exclude_projects = _normal_set(data.get("exclude_projects"))
    if "comment_prefix" in data:
        cfg.comment_prefix = str(data.get("comment_prefix") or "Hermes Kanban").strip() or "Hermes Kanban"
    if "max_tasks" in data:
        cfg.max_tasks = max(1, int(data.get("max_tasks") or 100))
    if "goal_mode" in data:
        cfg.goal_mode = bool(data.get("goal_mode"))
    if "goal_max_turns" in data and data.get("goal_max_turns") is not None:
        cfg.goal_max_turns = max(1, int(data["goal_max_turns"]))
    if "max_runtime_seconds" in data and data.get("max_runtime_seconds") is not None:
        cfg.max_runtime = max(1, int(data["max_runtime_seconds"]))
    if "skills" in data:
        cfg.skills = _normal_list(data.get("skills"))
    return cfg


def ledger_path() -> Path:
    return hermes_home() / "todoist-kanban" / "ledger.json"


def load_ledger() -> dict[str, Any]:
    path = ledger_path()
    if not path.exists():
        return {"version": LEDGER_VERSION, "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
            data.setdefault("version", LEDGER_VERSION)
            return data
    except Exception:
        pass
    return {"version": LEDGER_VERSION, "tasks": {}}


def save_ledger(data: dict[str, Any]) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def todoist_token() -> str:
    _read_dotenv()
    token = os.environ.get("TODOIST_API_TOKEN") or os.environ.get("HERMES_TODOIST_API_TOKEN")
    if not token:
        raise RuntimeError(
            "missing Todoist token: set TODOIST_API_TOKEN in ${HERMES_HOME:-~/.hermes}/.env"
        )
    return token


class TodoistClient:
    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token or todoist_token()

    def request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Todoist API {method} {path} failed: {exc.code} {detail}") from exc
        if not raw:
            return None
        return json.loads(raw)

    def active_tasks(self, *, project_id: Optional[str] = None, label: Optional[str] = None) -> list[dict[str, Any]]:
        query: dict[str, str] = {}
        if project_id:
            query["project_id"] = project_id
        if label:
            query["label"] = label
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        data = self.request("GET", "/tasks" + suffix)
        return list(data or [])

    def add_comment(self, task_id: str, content: str) -> Any:
        return self.request("POST", "/comments", {"task_id": str(task_id), "content": content})


def task_id(task: dict[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or "").strip()


def task_labels(task: dict[str, Any]) -> set[str]:
    labels = task.get("labels") or task.get("label_names") or []
    out: set[str] = set()
    for item in labels:
        if isinstance(item, dict):
            name = item.get("name") or item.get("id")
        else:
            name = item
        text = str(name or "").strip().casefold()
        if text:
            out.add(text)
    return out


def task_project_id(task: dict[str, Any]) -> str:
    return str(task.get("project_id") or "").strip().casefold()


def classify_task(task: dict[str, Any], cfg: BridgeConfig) -> dict[str, Any]:
    tid = task_id(task)
    labels = task_labels(task)
    content = str(task.get("content") or "").strip()
    description = str(task.get("description") or "").strip()
    lower_text = f"{content}\n{description}".casefold()

    if not tid:
        return {"task_id": "", "class": "ignore", "reason": "missing Todoist task id"}
    if task.get("is_completed") or task.get("checked") or task.get("completed_at"):
        return {"task_id": tid, "class": "ignore", "reason": "already completed in Todoist"}
    project_id = task_project_id(task)
    if cfg.include_projects and project_id not in cfg.include_projects:
        return {"task_id": tid, "class": "ignore", "reason": "project is not included"}
    if cfg.exclude_projects and project_id in cfg.exclude_projects:
        return {"task_id": tid, "class": "ignore", "reason": "project is excluded"}
    if labels & cfg.exclude_labels:
        return {"task_id": tid, "class": "human_commitment", "reason": "human/excluded label present"}
    if labels & cfg.human_labels:
        return {"task_id": tid, "class": "human_commitment", "reason": "human label present"}
    if labels & cfg.agent_labels:
        return {"task_id": tid, "class": "agent_capable", "reason": "agent label present"}
    if cfg.include_labels and not (labels & cfg.include_labels):
        return {"task_id": tid, "class": "human_commitment", "reason": "no configured agent label"}
    if lower_text.startswith("agent:") or "\nagent:" in lower_text or "[agent]" in lower_text:
        return {"task_id": tid, "class": "agent_capable", "reason": "agent marker present"}
    return {"task_id": tid, "class": "human_commitment", "reason": "default human-authoritative task"}


def idempotency_key_for(task: dict[str, Any]) -> str:
    tid = task_id(task)
    if not tid:
        digest = hashlib.sha256(json.dumps(task, sort_keys=True).encode("utf-8")).hexdigest()[:20]
        tid = f"unknown:{digest}"
    return f"todoist:{tid}"


def kanban_priority(task: dict[str, Any], cfg: BridgeConfig) -> int:
    try:
        todoist_priority = int(task.get("priority") or 1)
    except Exception:
        todoist_priority = 1
    return max(0, todoist_priority - 1 + cfg.priority_offset)


def kanban_body(task: dict[str, Any], classification: dict[str, Any]) -> str:
    due = task.get("due") if isinstance(task.get("due"), dict) else {}
    lines = [
        "Origin: Todoist",
        f"Todoist task id: {task_id(task)}",
    ]
    url = task.get("url")
    if url:
        lines.append(f"Todoist URL: {url}")
    if task.get("project_id"):
        lines.append(f"Todoist project id: {task.get('project_id')}")
    labels = sorted(task_labels(task))
    if labels:
        lines.append("Todoist labels: " + ", ".join(labels))
    if due:
        due_text = due.get("datetime") or due.get("date") or due.get("string")
        if due_text:
            lines.append(f"Todoist due: {due_text}")
    lines.extend(
        [
            f"Classification: {classification['class']} ({classification['reason']})",
            "",
            "Task:",
            str(task.get("content") or "").strip(),
        ]
    )
    description = str(task.get("description") or "").strip()
    if description:
        lines.extend(["", "Todoist description:", description])
    lines.extend(
        [
            "",
            "Completion contract:",
            "- Do agent-suitable work only; do not change Neil's Todoist due dates or commitments.",
            "- Complete this Kanban card with concise evidence and next steps.",
            "- The bridge will post the Kanban result back to Todoist as a comment.",
        ]
    )
    return "\n".join(lines).strip()


def create_handoff(task: dict[str, Any], cfg: BridgeConfig) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    classification = classify_task(task, cfg)
    if classification["class"] != "agent_capable":
        return {
            "todoist_task_id": task_id(task),
            "classification": classification,
            "created": False,
            "reason": "not agent-capable",
        }
    workspace_kind = cfg.workspace
    workspace_path = None
    if ":" in workspace_kind:
        workspace_kind, workspace_path = workspace_kind.split(":", 1)
    with kb.connect_closing(board=cfg.board) as conn:
        kid = kb.create_task(
            conn,
            title=f"Todoist: {str(task.get('content') or '').strip()}",
            body=kanban_body(task, classification),
            assignee=cfg.default_assignee,
            created_by=cfg.created_by,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path or None,
            tenant=cfg.tenant,
            priority=kanban_priority(task, cfg),
            idempotency_key=idempotency_key_for(task),
            max_runtime_seconds=cfg.max_runtime,
            skills=cfg.skills or None,
            goal_mode=cfg.goal_mode,
            goal_max_turns=cfg.goal_max_turns,
        )
        ktask = kb.get_task(conn, kid)

    ledger = load_ledger()
    record = ledger.setdefault("tasks", {}).setdefault(task_id(task), {})
    record.update(
        {
            "todoist_task_id": task_id(task),
            "kanban_task_id": kid,
            "idempotency_key": idempotency_key_for(task),
            "classification": classification,
            "last_seen_at": int(time.time()),
            "todoist_url": task.get("url"),
            "content": task.get("content"),
        }
    )
    save_ledger(ledger)
    return {
        "todoist_task_id": task_id(task),
        "kanban_task_id": kid,
        "kanban_status": ktask.status if ktask else None,
        "classification": classification,
        "created": True,
        "idempotency_key": idempotency_key_for(task),
    }


def load_tasks_from_file(path: str) -> list[dict[str, Any]]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return list(data["tasks"])
    if isinstance(data, dict) and data.get("event_name") and isinstance(data.get("event_data"), dict):
        return [data["event_data"]]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return list(data)
    raise ValueError("task input must be a Todoist task object, webhook payload, or list")


def fetch_todoist_tasks(client: TodoistClient, cfg: BridgeConfig) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if cfg.include_projects:
        for project_id in sorted(cfg.include_projects):
            tasks.extend(client.active_tasks(project_id=project_id))
    elif cfg.include_labels:
        seen: set[str] = set()
        for label in sorted(cfg.include_labels):
            for task in client.active_tasks(label=label):
                tid = task_id(task)
                if tid and tid not in seen:
                    tasks.append(task)
                    seen.add(tid)
    else:
        tasks = client.active_tasks()
    return tasks[: cfg.max_tasks]


def sync(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    tasks = load_tasks_from_file(args.source) if args.source else fetch_todoist_tasks(TodoistClient(), cfg)
    results = []
    for task in tasks[: args.limit or cfg.max_tasks]:
        classification = classify_task(task, cfg)
        if args.dry_run:
            results.append(
                {
                    "todoist_task_id": task_id(task),
                    "classification": classification,
                    "would_create": classification["class"] == "agent_capable",
                    "idempotency_key": idempotency_key_for(task),
                }
            )
        else:
            results.append(create_handoff(task, cfg))
    print(json.dumps({"results": results}, indent=2, sort_keys=True))
    return 0


def webhook_filter(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    task = payload.get("event_data") if isinstance(payload, dict) else payload
    if not isinstance(task, dict):
        return 0
    classification = classify_task(task, cfg)
    if classification["class"] != "agent_capable":
        return 0
    out = {
        "todoist_task": task,
        "classification": classification,
        "idempotency_key": idempotency_key_for(task),
        "prompt": (
            f"Create or update the Hermes Kanban handoff for Todoist task "
            f"{task_id(task)} using the todoist-kanban skill."
        ),
    }
    print(json.dumps(out, sort_keys=True))
    return 0


def _kanban_done_records(cfg: BridgeConfig) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing(board=cfg.board) as conn:
        rows = conn.execute(
            """
            SELECT id, title, result, completed_at, idempotency_key
              FROM tasks
             WHERE status = 'done'
               AND idempotency_key LIKE 'todoist:%'
             ORDER BY completed_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _todoist_id_from_key(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def _postback_content(row: dict[str, Any], cfg: BridgeConfig) -> str:
    completed = row.get("completed_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(completed))) if completed else ""
    body = str(row.get("result") or "").strip() or "Kanban card completed without a result summary."
    return (
        f"{cfg.comment_prefix}: completed Kanban card {row['id']}"
        + (f" at {when}" if when else "")
        + "\n\n"
        + body
    )


def postback(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    ledger = load_ledger()
    records = _kanban_done_records(cfg)
    client = None if args.dry_run else TodoistClient()
    posted = []
    for row in records:
        tid = _todoist_id_from_key(str(row.get("idempotency_key") or ""))
        task_record = ledger.setdefault("tasks", {}).setdefault(tid, {"todoist_task_id": tid})
        already = set(task_record.get("posted_kanban_results") or [])
        if row["id"] in already:
            continue
        content = _postback_content(row, cfg)
        if client is not None:
            client.add_comment(tid, content)
        already.add(row["id"])
        task_record["posted_kanban_results"] = sorted(already)
        task_record["last_postback_at"] = int(time.time())
        task_record["kanban_task_id"] = row["id"]
        posted.append({"todoist_task_id": tid, "kanban_task_id": row["id"], "dry_run": bool(args.dry_run)})
    if posted and not args.dry_run:
        save_ledger(ledger)
    elif args.dry_run:
        pass
    else:
        save_ledger(ledger)
    print(json.dumps({"posted": posted}, indent=2, sort_keys=True))
    return 0


def review(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    ledger = load_ledger()
    records = list((ledger.get("tasks") or {}).values())
    now = int(time.time())
    window = 86400 if args.period == "daily" else 7 * 86400
    recent = [r for r in records if int(r.get("last_seen_at") or 0) >= now - window]
    created = [r for r in recent if r.get("kanban_task_id")]
    posted = [r for r in records if r.get("last_postback_at") and int(r["last_postback_at"]) >= now - window]
    summary = {
        "period": args.period,
        "recent_todoist_agent_tasks": len(recent),
        "kanban_handoffs": len(created),
        "todoist_postbacks": len(posted),
        "handoffs": [
            {
                "todoist_task_id": r.get("todoist_task_id"),
                "kanban_task_id": r.get("kanban_task_id"),
                "content": r.get("content"),
                "classification": (r.get("classification") or {}).get("class"),
            }
            for r in created
        ],
    }
    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"# Todoist Kanban {args.period.title()} Review")
        print()
        print(f"- Recent Todoist agent tasks: {summary['recent_todoist_agent_tasks']}")
        print(f"- Kanban handoffs: {summary['kanban_handoffs']}")
        print(f"- Todoist postbacks: {summary['todoist_postbacks']}")
        if summary["handoffs"]:
            print()
            print("## Handoffs")
            for item in summary["handoffs"]:
                print(f"- Todoist {item['todoist_task_id']} -> Kanban {item['kanban_task_id']}: {item.get('content') or ''}")
    return 0


def classify_cmd(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    tasks = load_tasks_from_file(args.source)
    out = [
        {
            "todoist_task_id": task_id(task),
            "classification": classify_task(task, cfg),
            "idempotency_key": idempotency_key_for(task),
        }
        for task in tasks
    ]
    print(json.dumps(out[0] if len(out) == 1 else out, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Todoist system-of-record bridge for Hermes Kanban")
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="Classify Todoist task JSON")
    p_classify.add_argument("--source", default="-", help="JSON file path or '-' for stdin")

    p_sync = sub.add_parser("sync", help="Create idempotent Kanban handoffs for agent-capable Todoist tasks")
    p_sync.add_argument("--source", default=None, help="Optional JSON fixture/file instead of live Todoist")
    p_sync.add_argument("--limit", type=int, default=None)
    p_sync.add_argument("--dry-run", action="store_true")

    sub.add_parser("webhook-filter", help="Read one Todoist webhook payload from stdin and emit only agent-capable payloads")

    p_postback = sub.add_parser("postback", help="Post completed Kanban evidence back to Todoist comments")
    p_postback.add_argument("--dry-run", action="store_true")

    p_review = sub.add_parser("review", help="Summarize daily or weekly bridge activity")
    p_review.add_argument("period", choices=("daily", "weekly"))
    p_review.add_argument("--format", choices=("markdown", "json"), default="markdown")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        if args.command == "classify":
            return classify_cmd(args, cfg)
        if args.command == "sync":
            return sync(args, cfg)
        if args.command == "webhook-filter":
            return webhook_filter(args, cfg)
        if args.command == "postback":
            return postback(args, cfg)
        if args.command == "review":
            return review(args, cfg)
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"todoist-kanban: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
