import json
import re
import shlex
import threading
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.process_registry import process_registry


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _default_registry_path() -> Path:
    return get_hermes_home() / "miniapp_agents.json"


class MiniAppAgentRegistry:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else _default_registry_path()
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self._path)

    def _require_agent(self, name: str) -> dict[str, Any]:
        data = self._load()
        agent = data.get(name)
        if not agent:
            raise KeyError(f"Unknown agent: {name}")
        return agent

    @staticmethod
    def _status_from_poll(result: dict[str, Any], fallback: str) -> str:
        status = result.get("status")
        if status == "running":
            return "running"
        if status == "exited":
            exit_code = result.get("exit_code")
            return "done" if exit_code in (0, None) else "dead"
        if status == "not_found":
            return "dead"
        return fallback

    @staticmethod
    def _shell_join(parts: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in parts if part)

    @staticmethod
    def _spawn_command(prompt: str, *, mode: str, worktree: bool) -> tuple[str, bool]:
        base = ["hermes", "chat", "--quiet"]
        if worktree:
            base.append("--worktree")

        if mode == "interactive":
            return MiniAppAgentRegistry._shell_join(base), True
        if mode == "oneshot":
            return MiniAppAgentRegistry._shell_join([*base, "--query", prompt]), False
        raise ValueError("Mode must be 'interactive' or 'oneshot'")

    @staticmethod
    def _normalize_name(name: str | None, session_id: str) -> tuple[str, str]:
        if name:
            candidate = name.strip()
            if not candidate or not _NAME_RE.fullmatch(candidate):
                raise ValueError("Agent name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
            return candidate, candidate

        generated = f"agent-{session_id[-6:]}"
        return generated, generated

    def spawn(self, *, prompt: str, name: str | None, mode: str, worktree: bool) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Prompt is required")

        command, needs_initial_submit = self._spawn_command(prompt, mode=mode, worktree=worktree)
        session = process_registry.spawn_local(command=command, use_pty=True)
        if needs_initial_submit:
            send_result = process_registry.submit_stdin(session.id, prompt)
            if send_result.get("status") != "ok":
                process_registry.kill_process(session.id)
                raise RuntimeError(send_result.get("error", "Failed to submit initial prompt"))

        stored_name, display_name = self._normalize_name(name, session.id)

        with self._lock:
            data = self._load()
            if stored_name in data:
                process_registry.kill_process(session.id)
                raise ValueError(f"Agent already exists: {stored_name}")

            agent = {
                "name": stored_name,
                "display_name": display_name,
                "session_id": session.id,
                "pid": session.pid,
                "status": "running",
                "mode": mode,
                "worktree": bool(worktree),
                "model": "hermes-agent",
                "started_at": int(session.started_at),
                "command": command,
            }
            data[stored_name] = agent
            self._save(data)
        return agent

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
            changed = False
            rows: list[dict[str, Any]] = []
            for name, stored in sorted(data.items(), key=lambda item: item[1].get("started_at", 0), reverse=True):
                poll_result = process_registry.poll(stored["session_id"])
                row = dict(stored)
                row["status"] = self._status_from_poll(poll_result, stored.get("status", "running"))
                row["uptime"] = int(poll_result.get("uptime_seconds", 0) or 0)
                if poll_result.get("pid"):
                    row["pid"] = poll_result["pid"]
                if row["status"] != stored.get("status"):
                    data[name]["status"] = row["status"]
                    changed = True
                rows.append(row)
            if changed:
                self._save(data)
        return rows

    def get_agent(self, name: str) -> dict[str, Any]:
        stored = self._require_agent(name)
        poll_result = process_registry.poll(stored["session_id"])
        read_result = process_registry.read_log(stored["session_id"])
        row = dict(stored)
        row["status"] = self._status_from_poll(poll_result, stored.get("status", "running"))
        row["uptime"] = int(poll_result.get("uptime_seconds", 0) or 0)
        row["output"] = read_result.get("output", "")
        if poll_result.get("pid"):
            row["pid"] = poll_result["pid"]
        return row

    def send_message(self, name: str, message: str) -> dict[str, Any]:
        stored = self._require_agent(name)
        message = (message or "").strip()
        if not message:
            raise ValueError("Message is required")
        if stored.get("mode") != "interactive":
            raise ValueError("Only interactive agents accept follow-up messages")

        result = process_registry.submit_stdin(stored["session_id"], message)
        if result.get("status") != "ok":
            raise RuntimeError(result.get("error", "Failed to send message"))
        return {"ok": True}

    def delete(self, name: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            stored = data.get(name)
            if not stored:
                raise KeyError(f"Unknown agent: {name}")

            result = process_registry.kill_process(stored["session_id"])
            if result.get("status") not in {"killed", "already_exited"}:
                raise RuntimeError(result.get("error", "Failed to kill agent"))

            del data[name]
            self._save(data)
        return {"ok": True}
