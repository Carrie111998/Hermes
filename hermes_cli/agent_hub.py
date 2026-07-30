"""Direct coding-harness sessions for the dashboard Agent Hub.

This module deliberately wraps the installed CLIs instead of adding another
model/tool implementation to Hermes core.  Conversations retain the native
harness session id, so every turn goes through Codex/Claude's own agent loop,
skills, repository instructions, and tool policy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write


_STORE_LOCK = threading.RLock()
_TURN_LOCKS_LOCK = threading.Lock()
_TURN_LOCKS: Dict[str, threading.Lock] = {}
_MAX_PROMPT_CHARS = 200_000
_MAX_HISTORY_MESSAGES = 200

HARNESSES: Dict[str, Dict[str, str]] = {
    "codex": {
        "name": "Codex",
        "description": "OpenAI's coding agent with repository-aware tools.",
        "command": "codex",
    },
    "claude": {
        "name": "Claude Code",
        "description": "Anthropic's coding agent with native skills and tools.",
        "command": "claude",
    },
    "antigravity": {
        "name": "Antigravity",
        "description": "Antigravity coding harness.",
        "command": "antigravity",
    },
}


def _hub_dir() -> Path:
    path = get_hermes_home() / "agent-hub"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _store_path() -> Path:
    return _hub_dir() / "conversations.json"


def _load_store() -> Dict[str, Dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_store(store: Dict[str, Dict[str, Any]]) -> None:
    atomic_json_write(_store_path(), store)


def harness_catalog() -> List[Dict[str, Any]]:
    rows = []
    for harness_id, definition in HARNESSES.items():
        executable = shutil.which(definition["command"])
        rows.append(
            {
                "id": harness_id,
                "name": definition["name"],
                "description": definition["description"],
                "available": bool(executable),
                "executable": executable,
            }
        )
    return rows


def list_conversations() -> List[Dict[str, Any]]:
    with _STORE_LOCK:
        values = list(_load_store().values())
    values.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return [_conversation_summary(row) for row in values]


def get_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    with _STORE_LOCK:
        row = _load_store().get(conversation_id)
    return dict(row) if isinstance(row, dict) else None


def delete_conversation(conversation_id: str) -> bool:
    with _STORE_LOCK:
        store = _load_store()
        removed = store.pop(conversation_id, None) is not None
        if removed:
            _save_store(store)
    return removed


def _conversation_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    messages = row.get("messages") or []
    last = messages[-1].get("content", "") if messages else ""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "New coding session",
        "harness": row.get("harness"),
        "cwd": row.get("cwd"),
        "skills": list(row.get("skills") or []),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "message_count": len(messages),
        "preview": str(last)[:160],
    }


def _resolve_skill_files(names: Iterable[str], profile_home: Optional[Path]) -> List[Path]:
    """Resolve selected skill names to SKILL.md paths without reading them."""
    from agent.skill_utils import get_external_skills_dirs, iter_skill_index_files

    wanted = {str(name).strip() for name in names if str(name).strip()}
    if not wanted:
        return []
    roots: List[Path] = []
    local = (profile_home or get_hermes_home()) / "skills"
    if local.is_dir():
        roots.append(local)
    roots.extend(Path(p) for p in get_external_skills_dirs())

    matches: Dict[str, Path] = {}
    for root in roots:
        for skill_md in iter_skill_index_files(root, "SKILL.md"):
            fallback = skill_md.parent.name
            name = fallback
            try:
                head = skill_md.read_text(encoding="utf-8")[:4000]
                if head.startswith("---"):
                    for line in head.splitlines()[1:]:
                        if line.strip() == "---":
                            break
                        if line.lower().startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip("\"'")
                            break
            except OSError:
                continue
            if name in wanted and name not in matches:
                matches[name] = skill_md.resolve()
    return [matches[name] for name in names if name in matches]


def _augmented_prompt(
    prompt: str,
    skill_files: Iterable[Path],
    attachments: Iterable[str],
) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Prompt is required")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError("Prompt is too long")

    prelude: List[str] = []
    paths = list(skill_files)
    if paths:
        prelude.append(
            "Use all of the following skills for this task. Read each SKILL.md "
            "completely before acting, follow its instructions, and combine the "
            "skills where useful:\n"
            + "\n".join(f"- {path}" for path in paths)
        )
    attached = [str(path) for path in attachments if str(path).strip()]
    if attached:
        prelude.append(
            "The user attached these files. Inspect them as needed:\n"
            + "\n".join(f"- {path}" for path in attached)
        )
    return "\n\n".join([*prelude, prompt])


def _extract_codex_output(stdout: str) -> tuple[str, Optional[str]]:
    final = ""
    session_id: Optional[str] = None
    plain: List[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            if line.strip():
                plain.append(line)
            continue
        event_type = str(event.get("type") or "")
        if event_type in {"thread.started", "session.started"}:
            session_id = str(
                event.get("thread_id") or event.get("session_id") or ""
            ) or session_id
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            final = str(item.get("text") or final)
        if event_type in {"turn.completed", "message.completed"}:
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                final = message
    return final or "\n".join(plain).strip(), session_id


def _extract_claude_output(stdout: str) -> tuple[str, Optional[str]]:
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout.strip(), None
    result = payload.get("result")
    if not isinstance(result, str):
        result = payload.get("message") if isinstance(payload.get("message"), str) else ""
    session_id = payload.get("session_id")
    return result.strip(), str(session_id) if session_id else None


def _build_command(
    harness: str,
    native_session_id: Optional[str],
    cwd: Path,
    model: Optional[str],
) -> List[str]:
    if harness == "codex":
        if native_session_id:
            command = ["codex", "exec", "resume", "--json", native_session_id]
        else:
            command = [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-C",
                str(cwd),
            ]
        if model:
            command.extend(["--model", model])
        return command
    if harness == "claude":
        command = [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ]
        if native_session_id:
            command.extend(["--resume", native_session_id])
        else:
            command.extend(["--session-id", str(uuid.uuid4())])
        if model:
            command.extend(["--model", model])
        return command
    if harness == "antigravity":
        # Antigravity distributions expose a Claude-compatible print mode.
        command = ["antigravity", "--print"]
        if native_session_id:
            command.extend(["--resume", native_session_id])
        if model:
            command.extend(["--model", model])
        command.append("-")
        return command
    raise ValueError(f"Unknown coding harness: {harness}")


def _run_turn_unlocked(
    *,
    harness: str,
    prompt: str,
    conversation_id: Optional[str] = None,
    cwd: Optional[str] = None,
    skills: Optional[List[str]] = None,
    attachments: Optional[List[str]] = None,
    model: Optional[str] = None,
    profile_home: Optional[Path] = None,
    timeout_seconds: int = 1800,
) -> Dict[str, Any]:
    definition = HARNESSES.get(harness)
    if not definition:
        raise ValueError(f"Unknown coding harness: {harness}")
    if not shutil.which(definition["command"]):
        raise RuntimeError(f"{definition['name']} is not installed or not on PATH")

    workdir = Path(cwd or os.getcwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError("Working directory does not exist")
    selected_skills = list(dict.fromkeys(skills or []))
    skill_files = _resolve_skill_files(selected_skills, profile_home)
    full_prompt = _augmented_prompt(prompt, skill_files, attachments or [])

    with _STORE_LOCK:
        store = _load_store()
        row = store.get(conversation_id or "")
        if row is not None and row.get("harness") != harness:
            raise ValueError("A conversation cannot switch coding harnesses")
        now = time.time()
        if row is None:
            conversation_id = conversation_id or str(uuid.uuid4())
            row = {
                "id": conversation_id,
                "title": prompt.strip().splitlines()[0][:72] or "New coding session",
                "harness": harness,
                "native_session_id": None,
                "cwd": str(workdir),
                "skills": selected_skills,
                "model": model or "",
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
        native_session_id = row.get("native_session_id")

    command = _build_command(harness, native_session_id, workdir, model)
    command_session_id = None
    if harness == "claude" and "--session-id" in command:
        command_session_id = command[command.index("--session-id") + 1]
    completed = subprocess.run(
        command,
        input=full_prompt,
        text=True,
        cwd=str(workdir),
        capture_output=True,
        timeout=max(30, min(int(timeout_seconds), 3600)),
        env=os.environ.copy(),
    )
    if harness == "codex":
        response, returned_session_id = _extract_codex_output(completed.stdout)
    elif harness == "claude":
        response, returned_session_id = _extract_claude_output(completed.stdout)
    else:
        response, returned_session_id = completed.stdout.strip(), None
    if completed.returncode != 0:
        detail = completed.stderr.strip() or response or "Coding harness failed"
        raise RuntimeError(detail[-4000:])
    if not response:
        raise RuntimeError("Coding harness returned an empty response")

    with _STORE_LOCK:
        store = _load_store()
        live = store.get(str(conversation_id), row)
        live["native_session_id"] = (
            returned_session_id or native_session_id or command_session_id
        )
        live["cwd"] = str(workdir)
        live["skills"] = selected_skills
        live["model"] = model or ""
        live["updated_at"] = time.time()
        messages = list(live.get("messages") or [])
        messages.extend(
            [
                {
                    "role": "user",
                    "content": prompt.strip(),
                    "attachments": list(attachments or []),
                    "created_at": now,
                },
                {
                    "role": "assistant",
                    "content": response,
                    "created_at": live["updated_at"],
                },
            ]
        )
        live["messages"] = messages[-_MAX_HISTORY_MESSAGES:]
        store[str(conversation_id)] = live
        _save_store(store)
    return live


def run_turn(**kwargs) -> Dict[str, Any]:
    """Run one native turn, serialized per conversation.

    Discord can deliver multiple messages almost simultaneously. Native
    harness session files are not safe to resume concurrently, so preserve
    turn order for one conversation while allowing unrelated channels/chats
    to run in parallel.
    """
    key = str(kwargs.get("conversation_id") or "new")
    with _TURN_LOCKS_LOCK:
        lock = _TURN_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _run_turn_unlocked(**kwargs)


def load_bindings() -> Dict[str, Dict[str, Any]]:
    path = _hub_dir() / "discord-bindings.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_binding(
    channel_id: str,
    harness: str,
    *,
    channel_name: str = "",
    skills: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    if harness not in HARNESSES:
        raise ValueError(f"Unknown coding harness: {harness}")
    channel_id = str(channel_id).strip()
    if not channel_id:
        raise ValueError("Discord channel is required")
    with _STORE_LOCK:
        bindings = load_bindings()
        binding = {
            "channel_id": channel_id,
            "channel_name": channel_name.strip(),
            "harness": harness,
            "skills": list(dict.fromkeys(skills or [])),
            "cwd": str(Path(cwd or os.getcwd()).expanduser().resolve()),
            "updated_at": time.time(),
        }
        bindings[channel_id] = binding
        atomic_json_write(_hub_dir() / "discord-bindings.json", bindings)
    return binding


def delete_binding(channel_id: str) -> bool:
    with _STORE_LOCK:
        bindings = load_bindings()
        removed = bindings.pop(str(channel_id), None) is not None
        if removed:
            atomic_json_write(_hub_dir() / "discord-bindings.json", bindings)
    return removed
