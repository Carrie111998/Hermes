"""Durable questions that plugins deliver after a gateway session is idle."""

from __future__ import annotations

import json
import asyncio
import contextvars
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


QuestionState = Literal["queued", "awaiting", "handling", "resolved"]


@dataclass(frozen=True)
class DeferredQuestion:
    id: str
    plugin_id: str
    platform: str
    session_key: str
    chat_id: str
    question: str
    handler_name: str
    context: dict[str, object]
    dedupe_key: str
    state: QuestionState
    response: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class DeferredQuestionResult:
    resolved: bool
    reply: str
    question: str | None = None

    @classmethod
    def done(cls, reply: str) -> "DeferredQuestionResult":
        return cls(resolved=True, reply=reply)

    @classmethod
    def clarify(cls, question: str) -> "DeferredQuestionResult":
        return cls(resolved=False, reply="", question=question)


DeferredQuestionHandler = Callable[
    [DeferredQuestion, str], Awaitable[DeferredQuestionResult]
]


class DeferredQuestionService:
    """Persist deferred questions and dispatch captured replies to plugins."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._handlers: dict[tuple[str, str], DeferredQuestionHandler] = {}
        self._adapters: dict[str, tuple[Any, asyncio.AbstractEventLoop]] = {}
        self._busy_callbacks: set[str] = set()
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self.delivery_retry_seconds = 5.0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deferred_questions (
                    id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    handler_name TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'awaiting', 'handling', 'resolved')
                    ),
                    response TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_deferred_questions_unresolved_dedupe
                ON deferred_questions(plugin_id, dedupe_key)
                WHERE state != 'resolved';
                CREATE INDEX IF NOT EXISTS
                    ix_deferred_questions_session_state
                ON deferred_questions(session_key, state, created_at);
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> DeferredQuestion | None:
        if row is None:
            return None
        context = json.loads(row["context_json"])
        if not isinstance(context, dict):
            raise ValueError("deferred question context must be a JSON object")
        return DeferredQuestion(
            id=row["id"],
            plugin_id=row["plugin_id"],
            platform=row["platform"],
            session_key=row["session_key"],
            chat_id=row["chat_id"],
            question=row["question"],
            handler_name=row["handler_name"],
            context=context,
            dedupe_key=row["dedupe_key"],
            state=row["state"],
            response=row["response"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, question_id: str) -> DeferredQuestion:
        with self._lock, self._connect() as conn:
            record = self._from_row(
                conn.execute(
                    "SELECT * FROM deferred_questions WHERE id = ?", (question_id,)
                ).fetchone()
            )
        if record is None:
            raise KeyError(question_id)
        return record

    def enqueue(
        self,
        *,
        plugin_id: str,
        platform: str,
        session_key: str,
        chat_id: str,
        question: str,
        handler_name: str,
        context: dict[str, object],
        dedupe_key: str,
    ) -> DeferredQuestion:
        context_json = json.dumps(context, sort_keys=True, separators=(",", ":"))
        if not all(
            value.strip()
            for value in (
                plugin_id,
                platform,
                session_key,
                chat_id,
                question,
                handler_name,
                dedupe_key,
            )
        ):
            raise ValueError("deferred question fields must not be blank")
        now = time.time()
        question_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO deferred_questions (
                        id, plugin_id, platform, session_key, chat_id, question,
                        handler_name, context_json, dedupe_key, state, response,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)
                    """,
                    (
                        question_id,
                        plugin_id,
                        platform,
                        session_key,
                        chat_id,
                        question,
                        handler_name,
                        context_json,
                        dedupe_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT * FROM deferred_questions
                    WHERE plugin_id = ? AND dedupe_key = ? AND state != 'resolved'
                    """,
                    (plugin_id, dedupe_key),
                ).fetchone()
                existing = self._from_row(row)
                if existing is None:
                    raise
                record = existing
            else:
                record = self._from_row(
                    conn.execute(
                        "SELECT * FROM deferred_questions WHERE id = ?",
                        (question_id,),
                    ).fetchone()
                )
                if record is None:
                    raise RuntimeError("inserted deferred question disappeared")
        self._wake_platform(platform)
        return record

    def pending_for_session(self, session_key: str) -> DeferredQuestion | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM deferred_questions
                WHERE session_key = ? AND state != 'resolved'
                ORDER BY created_at ASC LIMIT 1
                """,
                (session_key,),
            ).fetchone()
        return self._from_row(row)

    def claim_for_delivery(self, question_id: str) -> DeferredQuestion | None:
        now = time.time()
        with self._lock, self._connect() as conn:
            changed = conn.execute(
                """
                UPDATE deferred_questions
                SET state = 'awaiting', updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, question_id),
            ).rowcount
        return self.get(question_id) if changed else None

    def requeue(self, question_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE deferred_questions
                SET state = 'queued', response = NULL, updated_at = ?
                WHERE id = ? AND state = 'awaiting'
                """,
                (time.time(), question_id),
            )

    def register_handler(
        self,
        plugin_id: str,
        handler_name: str,
        handler: DeferredQuestionHandler,
    ) -> None:
        if not callable(handler):
            raise TypeError("deferred question handler must be callable")
        self._handlers[(plugin_id, handler_name)] = handler

    def bind_adapter(self, platform: str, adapter: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self._adapters[platform] = (adapter, loop)
        self._wake_platform(platform)

        def retry_captured() -> None:
            asyncio.create_task(self.retry_handling())

        if loop.is_running():
            loop.call_soon_threadsafe(
                retry_captured, context=contextvars.Context()
            )

    def _wake_platform(self, platform: str) -> None:
        binding = self._adapters.get(platform)
        if binding is None:
            return
        _adapter, loop = binding

        def schedule() -> None:
            asyncio.create_task(self.deliver_ready(platform))

        if loop.is_running():
            loop.call_soon_threadsafe(schedule, context=contextvars.Context())

    def _schedule_delivery_retry(self, record: DeferredQuestion) -> None:
        if record.id in self._retry_tasks:
            return
        binding = self._adapters.get(record.platform)
        if binding is None:
            return
        _adapter, loop = binding

        async def retry() -> None:
            try:
                await asyncio.sleep(self.delivery_retry_seconds)
                self._retry_tasks.pop(record.id, None)
                await self.deliver_ready(record.platform, record.session_key)
            finally:
                self._retry_tasks.pop(record.id, None)

        def schedule() -> None:
            task = asyncio.create_task(retry())
            self._retry_tasks[record.id] = task

        if loop.is_running():
            loop.call_soon_threadsafe(schedule, context=contextvars.Context())

    def _queued(
        self, platform: str, session_key: str | None = None
    ) -> list[DeferredQuestion]:
        sql = """
            SELECT * FROM deferred_questions
            WHERE platform = ? AND state = 'queued'
        """
        params: tuple[object, ...] = (platform,)
        if session_key is not None:
            sql += " AND session_key = ?"
            params += (session_key,)
        sql += " ORDER BY created_at ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [record for row in rows if (record := self._from_row(row)) is not None]

    async def deliver_ready(
        self, platform: str, session_key: str | None = None
    ) -> None:
        binding = self._adapters.get(platform)
        if binding is None:
            return
        adapter, _loop = binding
        for record in self._queued(platform, session_key):
            if adapter.is_session_active(record.session_key):
                if record.id in self._busy_callbacks:
                    continue
                self._busy_callbacks.add(record.id)

                async def after_delivery(
                    *,
                    question_id: str = record.id,
                    key: str = record.session_key,
                ) -> None:
                    self._busy_callbacks.discard(question_id)
                    await self.deliver_ready(platform, key)

                adapter.register_post_delivery_callback(
                    record.session_key, after_delivery
                )
                continue
            claimed = self.claim_for_delivery(record.id)
            if claimed is None:
                continue
            result = await adapter.send(claimed.chat_id, claimed.question)
            if not getattr(result, "success", False):
                self.requeue(claimed.id)
                self._schedule_delivery_retry(claimed)

    async def handle_response(
        self, session_key: str, response: str
    ) -> DeferredQuestionResult | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM deferred_questions
                WHERE session_key = ? AND state = 'awaiting'
                ORDER BY created_at ASC LIMIT 1
                """,
                (session_key,),
            ).fetchone()
            record = self._from_row(row)
            if record is None:
                return None
            changed = conn.execute(
                """
                UPDATE deferred_questions
                SET state = 'handling', response = ?, updated_at = ?
                WHERE id = ? AND state = 'awaiting'
                """,
                (response, time.time(), record.id),
            ).rowcount
            if not changed:
                return None
        return await self._run_handler(self.get(record.id))

    async def _run_handler(self, record: DeferredQuestion) -> DeferredQuestionResult:
        handler = self._handlers.get((record.plugin_id, record.handler_name))
        if handler is None:
            raise LookupError(
                f"no deferred question handler registered for "
                f"{record.plugin_id}.{record.handler_name}"
            )
        if record.response is None:
            raise ValueError("handling question has no captured response")
        result = await handler(record, record.response)
        if not isinstance(result, DeferredQuestionResult):
            raise TypeError("deferred question handler returned an invalid result")
        now = time.time()
        with self._lock, self._connect() as conn:
            if result.resolved:
                conn.execute(
                    """
                    UPDATE deferred_questions
                    SET state = 'resolved', updated_at = ?
                    WHERE id = ? AND state = 'handling'
                    """,
                    (now, record.id),
                )
            else:
                if not result.question or not result.question.strip():
                    raise ValueError("clarification result requires a question")
                conn.execute(
                    """
                    UPDATE deferred_questions
                    SET state = 'awaiting', question = ?, response = NULL,
                        updated_at = ?
                    WHERE id = ? AND state = 'handling'
                    """,
                    (result.question, now, record.id),
                )
        return result

    async def retry_handling(
        self,
    ) -> list[tuple[str, DeferredQuestionResult]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM deferred_questions
                WHERE state = 'handling' ORDER BY created_at ASC
                """
            ).fetchall()
        results = []
        for row in rows:
            record = self._from_row(row)
            if record is None:
                continue
            if (record.plugin_id, record.handler_name) not in self._handlers:
                continue
            results.append((record.id, await self._run_handler(record)))
        return results

    def resolve_without_handler(self, question_id: str, response: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE deferred_questions
                SET state = 'resolved', response = ?, updated_at = ?
                WHERE id = ? AND state != 'resolved'
                """,
                (response, time.time(), question_id),
            )


class DeferredQuestionClient:
    """Plugin-scoped facade over the host deferred-question service."""

    def __init__(self, service: DeferredQuestionService, plugin_id: str) -> None:
        self._service = service
        self._plugin_id = plugin_id

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def register_handler(
        self, handler_name: str, handler: DeferredQuestionHandler
    ) -> None:
        self._service.register_handler(self._plugin_id, handler_name, handler)

    def enqueue(
        self,
        *,
        platform: str,
        session_key: str,
        chat_id: str,
        question: str,
        context: dict[str, object],
        dedupe_key: str,
        handler_name: str,
    ) -> DeferredQuestion:
        return self._service.enqueue(
            plugin_id=self._plugin_id,
            platform=platform,
            session_key=session_key,
            chat_id=chat_id,
            question=question,
            handler_name=handler_name,
            context=context,
            dedupe_key=dedupe_key,
        )


_singleton_lock = threading.Lock()
_singletons: dict[Path, DeferredQuestionService] = {}


def get_deferred_question_service() -> DeferredQuestionService:
    """Return the profile-scoped host service shared by plugins and adapters."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home().expanduser().resolve(strict=False)
    with _singleton_lock:
        service = _singletons.get(home)
        if service is None:
            service = DeferredQuestionService(
                home / "deferred_questions.sqlite3"
            )
            _singletons[home] = service
        return service
