"""Tenant-scoped SSE bridge for the WebUI's compact Hermes chat."""
from __future__ import annotations

import json
import queue
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import json_dump, json_load, new_id, now


TERMINAL_EVENTS = {"done", "apperror", "cancel"}
STREAM_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 24 * 60 * 60
HISTORY_MESSAGES = 20
SAFE_CHAT_TOOLSETS = {"none", "search", "web"}


@dataclass
class ChatStream:
    stream_id: str
    session_id: str
    company_id: str
    principal_id: str
    events: queue.Queue[tuple[str, dict[str, Any]]] = field(default_factory=queue.Queue)
    created_at: float = field(default_factory=now)
    finished_at: float | None = None
    attached: bool = False
    done: bool = False
    abandoned: bool = False


def _default_agent_factory(**kwargs):
    # Importing run_agent initializes a large provider/tool surface. Keep that
    # cost behind chat_enabled and behind the first actual chat turn.
    from run_agent import AIAgent
    return AIAgent(**kwargs)


class ChatBridge:
    def __init__(self, db, settings, run_service, *, agent_factory: Callable | None = None,
                 workers: int = 4):
        self.db = db
        self.settings = settings
        self.run_service = run_service
        self.agent_factory = agent_factory or _default_agent_factory
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="interfaze-chat")
        self._streams: dict[str, ChatStream] = {}
        self._lock = threading.RLock()

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _gc(self) -> None:
        stamp = now()
        with self._lock:
            expired = [
                stream_id for stream_id, stream in self._streams.items()
                if (stream.abandoned and stream.finished_at is not None)
                or (stream.done and stream.finished_at is not None
                    and stamp - stream.finished_at > STREAM_TTL_SECONDS)
                or (not stream.attached and stamp - stream.created_at > STREAM_TTL_SECONDS)
            ]
            for stream_id in expired:
                self._streams.pop(stream_id, None)
        self.db.execute("DELETE FROM chat_sessions WHERE updated_at<?", (stamp - SESSION_TTL_SECONDS,))

    def create_session(self, company_id: str, principal_id: str, profile: str = "default") -> dict:
        self._gc()
        if not self.db.one("SELECT id FROM companies WHERE id=?", (company_id,)):
            raise LookupError("Company not found")
        session_id, stamp = new_id("chat"), now()
        self.db.execute(
            "INSERT INTO chat_sessions(id,company_id,user_id,profile,history,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (session_id, company_id, principal_id, profile, json_dump([]), stamp, stamp),
        )
        return self._session_view(session_id, profile)

    def get_session(self, session_id: str, company_id: str, principal_id: str) -> dict:
        self._gc()
        row = self.db.one(
            "SELECT * FROM chat_sessions WHERE id=? AND company_id=? AND user_id=?",
            (session_id, company_id, principal_id),
        )
        if not row:
            raise LookupError("Chat session not found")
        self.db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now(), session_id))
        return self._session_view(row["id"], row["profile"])

    def _session_view(self, session_id: str, profile: str) -> dict:
        return {
            "session_id": session_id,
            "profile": profile,
            "model": self.settings.chat_model,
            "workspace": "",
            "model_provider": "",
        }

    def _session_row(self, session_id: str, company_id: str, principal_id: str):
        row = self.db.one(
            "SELECT * FROM chat_sessions WHERE id=? AND company_id=? AND user_id=?",
            (session_id, company_id, principal_id),
        )
        if not row:
            raise LookupError("Chat session not found")
        return row

    def start(self, session_id: str, company_id: str, principal_id: str, message: str) -> str:
        self._gc()
        self._session_row(session_id, company_id, principal_id)
        self.db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now(), session_id))
        with self._lock:
            if any(
                stream.session_id == session_id and not stream.done and not stream.abandoned
                for stream in self._streams.values()
            ):
                raise RuntimeError("A chat response is already running for this session")
            stream_id = secrets.token_urlsafe(32)
            stream = ChatStream(stream_id, session_id, company_id, principal_id)
            self._streams[stream_id] = stream
        self.pool.submit(self._run_turn, stream, message)
        return stream_id

    def claim_stream(self, stream_id: str) -> ChatStream:
        self._gc()
        with self._lock:
            stream = self._streams.get(stream_id)
            if not stream or stream.attached or stream.abandoned:
                raise LookupError("Chat stream not found")
            if now() - stream.created_at > STREAM_TTL_SECONDS and not stream.done:
                self._streams.pop(stream_id, None)
                raise LookupError("Chat stream expired")
            stream.attached = True
            return stream

    def abandon(self, stream_id: str) -> None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream and not stream.done:
                stream.abandoned = True

    def _enabled_toolsets(self) -> list[str]:
        configured = str(self.settings.chat_toolset or "none").lower()
        if configured not in SAFE_CHAT_TOOLSETS or configured == "none":
            return []
        return [configured]

    def _system_prompt(self, company_id: str) -> str:
        context = self.run_service.company_context(company_id)
        return (
            "You are the authenticated company's read-only B2B sales assistant. "
            "Answer only from the supplied company context and the conversation. "
            "Never claim to have sent messages, changed records, or run actions. "
            "Keep answers concise and practical.\n\n"
            "Tenant company context:\n" + json.dumps(context, ensure_ascii=False, default=str)
        )

    @staticmethod
    def _usage(result: dict) -> dict[str, int]:
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return {
            "input_tokens": int(result.get("input_tokens") or result.get("prompt_tokens")
                                or usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output_tokens": int(result.get("output_tokens") or result.get("completion_tokens")
                                 or usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        }

    @staticmethod
    def _history(value) -> list[dict[str, str]]:
        clean: list[dict[str, str]] = []
        expected = "user"
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict) or item.get("role") != expected:
                continue
            clean.append({"role": expected, "content": str(item.get("content") or "")})
            expected = "assistant" if expected == "user" else "user"
        if clean and clean[-1]["role"] == "user":
            clean.pop()
        return clean[-HISTORY_MESSAGES:]

    def _save_turn(self, stream: ChatStream, prior_history: list, message: str, answer: str) -> None:
        history = self._history(prior_history)
        history.extend((
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ))
        history = history[-HISTORY_MESSAGES:]
        if history and history[0]["role"] == "assistant":
            history = history[1:]
        self.db.execute(
            "UPDATE chat_sessions SET history=?,updated_at=? "
            "WHERE id=? AND company_id=? AND user_id=?",
            (json_dump(history), now(), stream.session_id, stream.company_id, stream.principal_id),
        )

    def _finish(self, stream: ChatStream, event: str, payload: dict) -> None:
        stream.events.put((event, payload))
        with self._lock:
            stream.done = True
            stream.finished_at = now()

    def _run_turn(self, stream: ChatStream, message: str) -> None:
        try:
            row = self._session_row(stream.session_id, stream.company_id, stream.principal_id)
            history = self._history(json_load(row["history"], []) or [])

            def on_delta(delta) -> None:
                if isinstance(delta, str) and delta:
                    stream.events.put(("token", {"text": delta}))

            agent = self.agent_factory(
                model=self.settings.chat_model,
                max_iterations=15,
                enabled_toolsets=self._enabled_toolsets(),
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                load_soul_identity=False,
                platform="webui",
                user_id=stream.principal_id,
                ephemeral_system_prompt=self._system_prompt(stream.company_id),
                stream_delta_callback=on_delta,
            )
            result = agent.run_conversation(message, conversation_history=history)
            answer = str(result.get("final_response") or "")
            self._save_turn(stream, history, message, answer)
            self._finish(stream, "done", {
                "session": {"session_id": stream.session_id},
                "usage": self._usage(result),
                "answer": answer,
            })
        except Exception as exc:
            self._finish(stream, "apperror", {"message": str(exc)[:500] or "Agent error"})
