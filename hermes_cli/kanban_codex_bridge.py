"""Durable Kanban execution state for Codex app-server workers.

Hermes owns card lifecycle and authority. Codex owns one implementation
thread for the current claimed task run. This module proves that ownership,
persists the Codex thread before a turn starts, and forwards later card
comments without replaying an ambiguously accepted ``turn/steer`` request.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_executor_sessions (
    task_id          TEXT NOT NULL,
    run_id           INTEGER NOT NULL,
    executor         TEXT NOT NULL,
    thread_id        TEXT,
    active_turn_id   TEXT,
    last_comment_id  INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL,
    last_error       TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (task_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_executor_sessions_task
    ON task_executor_sessions(task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_executor_sessions_thread
    ON task_executor_sessions(thread_id)
    WHERE thread_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_executor_comment_deliveries (
    task_id           TEXT NOT NULL,
    comment_id        INTEGER NOT NULL,
    first_run_id      INTEGER NOT NULL,
    last_run_id       INTEGER NOT NULL,
    client_message_id TEXT NOT NULL,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (task_id, comment_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_comment_client_id
    ON task_executor_comment_deliveries(client_message_id);

CREATE TABLE IF NOT EXISTS task_executor_host_commands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    tool           TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    state          TEXT NOT NULL,
    result_json    TEXT,
    error          TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executor_host_commands_pending
    ON task_executor_host_commands(task_id, run_id, state, id);
"""

_MAX_STEER_COMMENT_CHARS = 8_000
_POLL_INTERVAL_SECONDS = 0.75
_MAX_CONSECUTIVE_FAILURES = 3
_NON_RESUMABLE_OUTCOMES = {"completed"}
_HOST_COMMAND_POLL_SECONDS = 0.1


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


@dataclass(frozen=True)
class PreparedExecution:
    task_id: str
    run_id: int
    resume_thread_id: Optional[str]
    last_comment_id: int


@dataclass(frozen=True)
class CommentDelivery:
    client_message_id: str
    state: str
    created: bool


def request_host_broker_command(
    *,
    db_path: Path,
    task_id: str,
    run_id: int,
    tool: str,
    arguments: list[str],
    timeout: float = 170.0,
) -> dict[str, Any]:
    """Queue one broker command for the unsandboxed claimed worker host."""
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    with kb.connect_closing(db_path=Path(db_path)) as conn:
        with kb.write_txn(conn):
            _require_current_run(conn, task_id=task_id, run_id=int(run_id))
            cur = conn.execute(
                """
                INSERT INTO task_executor_host_commands (
                    task_id, run_id, tool, arguments_json, state,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    int(run_id),
                    tool,
                    json.dumps(arguments, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            command_id = int(cur.lastrowid)

    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        with kb.connect_closing(db_path=Path(db_path)) as conn:
            row = conn.execute(
                "SELECT state, result_json, error "
                "FROM task_executor_host_commands WHERE id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"host broker command {command_id} disappeared")
        state = str(row["state"])
        if state == "completed":
            return json.loads(str(row["result_json"] or "{}"))
        if state == "failed":
            raise RuntimeError(
                str(row["error"] or "host broker command failed")
            )
        time.sleep(_HOST_COMMAND_POLL_SECONDS)
    raise RuntimeError(f"host broker command {command_id} timed out")


class CodexHostCommandForwarder:
    """Execute task-scoped broker requests in the claimed worker process."""

    def __init__(
        self,
        *,
        db_path: Path,
        task_id: str,
        run_id: int,
        executor: Optional[Any] = None,
        poll_interval: float = _HOST_COMMAND_POLL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._task_id = task_id
        self._run_id = int(run_id)
        self._executor = executor
        self._poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failure: Optional[BaseException] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Codex host command bridge already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"codex-host-commands-{self._task_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 12.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError(
                    f"Codex host command bridge for {self._task_id} did not stop"
                )
        if self._failure is not None:
            raise RuntimeError(
                f"Codex host command bridge failed for {self._task_id}: "
                f"{self._failure}"
            ) from self._failure

    def poll_once(self) -> int:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing(db_path=self._db_path) as conn:
            with kb.write_txn(conn):
                _require_current_run(
                    conn,
                    task_id=self._task_id,
                    run_id=self._run_id,
                )
                row = conn.execute(
                    """
                    SELECT id, tool, arguments_json
                      FROM task_executor_host_commands
                     WHERE task_id = ? AND run_id = ? AND state = 'pending'
                     ORDER BY id ASC LIMIT 1
                    """,
                    (self._task_id, self._run_id),
                ).fetchone()
                if row is None:
                    return 0
                command_id = int(row["id"])
                conn.execute(
                    "UPDATE task_executor_host_commands "
                    "SET state = 'running', updated_at = ? "
                    "WHERE id = ? AND state = 'pending'",
                    (int(time.time()), command_id),
                )

        try:
            arguments = json.loads(str(row["arguments_json"]))
            if not isinstance(arguments, list):
                raise ValueError("host broker arguments are not an array")
            if self._executor is not None:
                result = self._executor(str(row["tool"]), arguments)
            else:
                from hermes_cli.kwilo_github_broker import (
                    resolve_current_task_broker,
                    run_broker_command,
                )

                context = resolve_current_task_broker()
                if context is None:
                    raise RuntimeError(
                        "current task has no GitHub App broker authority"
                    )
                result = run_broker_command(
                    context,
                    str(row["tool"]),
                    arguments,
                )
            state = "completed"
            result_json = json.dumps(result, ensure_ascii=False)
            error = None
        except Exception as exc:
            state = "failed"
            result_json = None
            error = str(exc)[:2_000]

        with kb.connect_closing(db_path=self._db_path) as conn:
            with kb.write_txn(conn):
                conn.execute(
                    """
                    UPDATE task_executor_host_commands
                       SET state = ?, result_json = ?, error = ?, updated_at = ?
                     WHERE id = ? AND task_id = ? AND run_id = ?
                    """,
                    (
                        state,
                        result_json,
                        error,
                        int(time.time()),
                        command_id,
                        self._task_id,
                        self._run_id,
                    ),
                )
        return 1

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.poll_once():
                    self._stop.wait(self._poll_interval)
        except RuntimeError as exc:
            # The task completing or blocking releases this run normally.
            if "not the current live claimed run" not in str(exc):
                self._failure = exc
        except Exception as exc:
            self._failure = exc


def _require_current_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT t.status AS task_status,
               t.current_run_id,
               r.status AS run_status,
               r.outcome,
               r.ended_at
          FROM tasks t
          JOIN task_runs r
            ON r.id = t.current_run_id
           AND r.task_id = t.id
         WHERE t.id = ?
           AND t.status = 'running'
           AND t.current_run_id = ?
           AND r.status = 'running'
           AND r.ended_at IS NULL
        """,
        (task_id, int(run_id)),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"task {task_id} run {run_id} is not the current live claimed run"
        )
    return row


def _guarded_session_update(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
    *,
    task_id: str,
    run_id: int,
) -> None:
    cur = conn.execute(
        sql
        + """
           AND EXISTS (
               SELECT 1
                 FROM tasks t
                 JOIN task_runs r
                   ON r.id = t.current_run_id
                  AND r.task_id = t.id
                WHERE t.id = task_executor_sessions.task_id
                  AND t.status = 'running'
                  AND t.current_run_id = task_executor_sessions.run_id
                  AND r.status = 'running'
                  AND r.ended_at IS NULL
           )
        """,
        params,
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"task {task_id} run {run_id} lost its Codex executor lease"
        )


def prepare_execution(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    initial_comment_id: int,
) -> PreparedExecution:
    """Create or resume the executor row for the current claimed run.

    ``initial_comment_id`` is the exact final comment included in the atomic
    worker-context snapshot. Newer comments are exclusively bridge input.
    """
    _require_current_run(conn, task_id=task_id, run_id=run_id)
    latest_comment_id = int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        or 0
    )
    initial_comment_id = int(initial_comment_id)
    if initial_comment_id < 0 or initial_comment_id > latest_comment_id:
        raise ValueError(
            f"invalid comment watermark {initial_comment_id}; "
            f"latest card comment is {latest_comment_id}"
        )

    existing = conn.execute(
        """
        SELECT thread_id, last_comment_id
          FROM task_executor_sessions
         WHERE task_id = ? AND run_id = ? AND executor = 'codex'
        """,
        (task_id, int(run_id)),
    ).fetchone()
    if existing is not None:
        return PreparedExecution(
            task_id=task_id,
            run_id=int(run_id),
            resume_thread_id=(
                str(existing["thread_id"]) if existing["thread_id"] else None
            ),
            last_comment_id=int(existing["last_comment_id"]),
        )

    prior = conn.execute(
        """
        SELECT s.run_id, s.thread_id, s.last_comment_id, s.state,
               r.status AS run_status, r.outcome, r.ended_at
          FROM task_executor_sessions s
          JOIN task_runs r ON r.id = s.run_id AND r.task_id = s.task_id
         WHERE s.task_id = ?
           AND s.executor = 'codex'
           AND s.run_id < ?
         ORDER BY s.run_id DESC
         LIMIT 1
        """,
        (task_id, int(run_id)),
    ).fetchone()
    resumable = bool(
        prior
        and prior["thread_id"]
        and (
            prior["state"] != "closed"
            or str(prior["outcome"] or "") == "blocked"
        )
        and str(prior["outcome"] or "") not in _NON_RESUMABLE_OUTCOMES
    )
    resume_thread_id = str(prior["thread_id"]) if resumable else None
    # Every claimed run receives a fresh atomic worker-context prompt, even
    # when that prompt is sent to a durable resumed thread. Its watermark is
    # therefore authoritative for this run. Reusing the prior cursor would
    # deliver comments that are already in the new prompt a second time via
    # turn/steer, and can race a short resumed turn to completion.
    last_comment_id = initial_comment_id
    now = int(time.time())

    # A late old worker cannot regain ownership because every later write is
    # current-run guarded. Marking old rows makes the lifecycle explicit and
    # removes mutable updated_at from retry selection.
    conn.execute(
        """
        UPDATE task_executor_sessions
           SET state = CASE WHEN state = 'closed' THEN state ELSE 'superseded' END,
               active_turn_id = NULL,
               updated_at = ?
         WHERE task_id = ? AND run_id < ?
        """,
        (now, task_id, int(run_id)),
    )
    conn.execute(
        """
        INSERT INTO task_executor_sessions (
            task_id, run_id, executor, thread_id, active_turn_id,
            last_comment_id, state, last_error, created_at, updated_at
        ) VALUES (?, ?, 'codex', ?, NULL, ?, 'starting', NULL, ?, ?)
        """,
        (
            task_id,
            int(run_id),
            resume_thread_id,
            last_comment_id,
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE task_executor_comment_deliveries
           SET state = CASE
                   WHEN state IN ('accepted', 'ignored') THEN state
                   ELSE 'included'
               END,
               last_run_id = ?,
               last_error = NULL,
               updated_at = ?
         WHERE task_id = ? AND comment_id <= ?
        """,
        (int(run_id), now, task_id, initial_comment_id),
    )
    return PreparedExecution(
        task_id=task_id,
        run_id=int(run_id),
        resume_thread_id=resume_thread_id,
        last_comment_id=last_comment_id,
    )


def record_active_runtime(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    thread_id: str,
    turn_id: Optional[str],
) -> None:
    now = int(time.time())
    _guarded_session_update(
        conn,
        """
        UPDATE task_executor_sessions
           SET thread_id = ?,
               active_turn_id = ?,
               state = CASE WHEN ? IS NULL THEN 'starting' ELSE 'active' END,
               updated_at = ?
         WHERE task_id = ? AND run_id = ?
        """,
        (thread_id, turn_id, turn_id, now, task_id, int(run_id)),
        task_id=task_id,
        run_id=run_id,
    )


def pending_comments(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    after_id: int,
    limit: int = 20,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, author, body, created_at
          FROM task_comments
         WHERE task_id = ? AND id > ?
         ORDER BY id ASC
         LIMIT ?
        """,
        (task_id, int(after_id), max(1, min(int(limit), 100))),
    ).fetchall()


def begin_comment_delivery(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    comment_id: int,
) -> CommentDelivery:
    _require_current_run(conn, task_id=task_id, run_id=run_id)
    client_message_id = f"hermes-kanban-{task_id}-comment-{int(comment_id)}"
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO task_executor_comment_deliveries (
            task_id, comment_id, first_run_id, last_run_id, client_message_id,
            state, attempts, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
        """,
        (
            task_id,
            int(comment_id),
            int(run_id),
            int(run_id),
            client_message_id,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT client_message_id, state
          FROM task_executor_comment_deliveries
         WHERE task_id = ? AND comment_id = ?
        """,
        (task_id, int(comment_id)),
    ).fetchone()
    return CommentDelivery(
        client_message_id=str(row["client_message_id"]),
        state=str(row["state"]),
        created=cur.rowcount == 1,
    )


def finish_comment_delivery(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    comment_id: int,
    state: str,
    error: Optional[str] = None,
) -> None:
    if state not in {"accepted", "ignored", "included"}:
        raise ValueError(f"invalid comment delivery state {state!r}")
    _require_current_run(conn, task_id=task_id, run_id=run_id)
    now = int(time.time())
    cur = conn.execute(
        """
        UPDATE task_executor_comment_deliveries
           SET state = ?,
               last_run_id = ?,
               attempts = attempts + CASE WHEN ? = 'accepted' THEN 1 ELSE 0 END,
               last_error = ?,
               updated_at = ?
         WHERE task_id = ? AND comment_id = ?
        """,
        (
            state,
            int(run_id),
            state,
            str(error)[:2_000] if error else None,
            now,
            task_id,
            int(comment_id),
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"comment {comment_id} has no durable delivery record for {task_id}"
        )
    _guarded_session_update(
        conn,
        """
        UPDATE task_executor_sessions
           SET last_comment_id = MAX(last_comment_id, ?),
               updated_at = ?
         WHERE task_id = ? AND run_id = ?
        """,
        (int(comment_id), now, task_id, int(run_id)),
        task_id=task_id,
        run_id=run_id,
    )


def finish_execution(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    error: Optional[str] = None,
) -> None:
    row = conn.execute(
        """
        SELECT t.status, t.current_run_id, s.last_comment_id,
               (SELECT COALESCE(MAX(id), 0)
                  FROM task_comments
                 WHERE task_id = t.id) AS latest_comment_id
          FROM tasks t
          JOIN task_executor_sessions s
            ON s.task_id = t.id AND s.run_id = ?
         WHERE t.id = ?
        """,
        (int(run_id), task_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing Codex executor state for {task_id} run {run_id}")
    is_current = row["current_run_id"] == int(run_id)
    status = str(row["status"])
    has_late_comments = int(row["latest_comment_id"]) > int(row["last_comment_id"])
    if status in {"blocked", "review"}:
        # A capability/review stop is a resumable handoff, not the end of the
        # implementation identity. Keep the thread available for an explicit
        # operator resume of this same card.
        state = "attention"
    elif status in {"done", "archived"}:
        state = "attention" if has_late_comments else "closed"
    elif not is_current:
        state = "superseded"
    elif error:
        state = "failed"
    else:
        state = "idle"
    cur = conn.execute(
        """
        UPDATE task_executor_sessions
           SET active_turn_id = NULL,
               state = ?,
               last_error = ?,
               updated_at = ?
         WHERE task_id = ? AND run_id = ?
        """,
        (
            state,
            str(error)[:2_000] if error else None,
            int(time.time()),
            task_id,
            int(run_id),
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"could not finalize Codex executor {task_id} run {run_id}")


class CodexCommentForwarder:
    """Poll one card and steer new, non-self comments into its active turn."""

    def __init__(
        self,
        *,
        db_path: Path,
        task_id: str,
        run_id: int,
        session: Any,
        ignored_authors: Optional[set[str]] = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._task_id = task_id
        self._run_id = int(run_id)
        self._session = session
        self._ignored_authors = {
            author.strip().lower()
            for author in (ignored_authors or set())
            if author and author.strip()
        }
        self._poll_interval = max(float(poll_interval), 0.05)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failure: Optional[BaseException] = None
        self._failure_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"codex-card-comments-{self._task_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 12.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError(
                    f"Codex comment bridge for {self._task_id} did not stop"
                )
        self.check_health()

    def check_health(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError(
                f"Codex comment bridge failed for {self._task_id}: {failure}"
            ) from failure

    def poll_once(self) -> int:
        """Forward currently pending comments; return the number delivered."""
        thread_id = self._session.thread_id
        turn_id = self._session.active_turn_id
        if not thread_id:
            return 0
        from hermes_cli import kanban_db as kb

        with kb.connect_closing(db_path=self._db_path) as conn:
            task_row = conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id = ?",
                (self._task_id,),
            ).fetchone()
            if task_row is None:
                raise RuntimeError(f"task {self._task_id} no longer exists")
            # kanban_complete/kanban_block legitimately releases the current
            # run before the Codex turn emits its final message. A dependency
            # block routes to ``todo`` (and the loop breaker may route to
            # ``triage``), so an allowlist of terminal-looking columns is not
            # sufficient. Once this run is no longer the task's current
            # running lease, comment steering for it is finished cleanly.
            if (
                str(task_row["status"]) != "running"
                or task_row["current_run_id"] != self._run_id
            ):
                self._stop.set()
                return 0
            with kb.write_txn(conn):
                record_active_runtime(
                    conn,
                    task_id=self._task_id,
                    run_id=self._run_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
            if not turn_id:
                return 0
            row = conn.execute(
                """
                SELECT last_comment_id
                  FROM task_executor_sessions
                 WHERE task_id = ? AND run_id = ?
                """,
                (self._task_id, self._run_id),
            ).fetchone()
            cursor = int(row["last_comment_id"]) if row else 0
            comments = pending_comments(conn, task_id=self._task_id, after_id=cursor)

        delivered = 0
        for comment in comments:
            comment_id = int(comment["id"])
            author = str(comment["author"] or "").strip()
            with kb.connect_closing(db_path=self._db_path) as conn:
                with kb.write_txn(conn):
                    delivery = begin_comment_delivery(
                        conn,
                        task_id=self._task_id,
                        run_id=self._run_id,
                        comment_id=comment_id,
                    )
                    if author.lower() in self._ignored_authors:
                        finish_comment_delivery(
                            conn,
                            task_id=self._task_id,
                            run_id=self._run_id,
                            comment_id=comment_id,
                            state="ignored",
                        )
                        continue
                    if delivery.state in {"accepted", "ignored", "included"}:
                        finish_comment_delivery(
                            conn,
                            task_id=self._task_id,
                            run_id=self._run_id,
                            comment_id=comment_id,
                            state=delivery.state,
                        )
                        continue

            # A pending record may represent a response timeout after Codex
            # accepted the steer. Reconcile the durable thread before resend.
            if not delivery.created and self._session.has_user_message(
                delivery.client_message_id
            ):
                with kb.connect_closing(db_path=self._db_path) as conn:
                    with kb.write_txn(conn):
                        finish_comment_delivery(
                            conn,
                            task_id=self._task_id,
                            run_id=self._run_id,
                            comment_id=comment_id,
                            state="accepted",
                        )
                delivered += 1
                continue

            body = str(comment["body"] or "").strip()
            prompt = (
                f"Untrusted Hermes card comment from {author} "
                f"(comment {comment_id}):\n\n"
                f"{body[:_MAX_STEER_COMMENT_CHARS]}"
            )
            self._session.steer_turn(
                prompt,
                expected_turn_id=turn_id,
                client_user_message_id=delivery.client_message_id,
            )
            with kb.connect_closing(db_path=self._db_path) as conn:
                with kb.write_txn(conn):
                    finish_comment_delivery(
                        conn,
                        task_id=self._task_id,
                        run_id=self._run_id,
                        comment_id=comment_id,
                        state="accepted",
                    )
            delivered += 1
        return delivered

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                self.poll_once()
                consecutive_failures = 0
            except RuntimeError as exc:
                # The turn can complete between reading active_turn_id and
                # turn/steer. Keep the cursor pending for a resumed turn.
                if "no active turn to steer" in str(exc):
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    logger.exception(
                        "Codex comment bridge failed for task %s", self._task_id
                    )
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Codex comment bridge failed for task %s", self._task_id
                )
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                failure = RuntimeError(
                    f"{consecutive_failures} consecutive polling/delivery failures"
                )
                with self._failure_lock:
                    self._failure = failure
                try:
                    self._session.request_interrupt()
                except Exception:
                    pass
                self._stop.set()
                break
            self._stop.wait(self._poll_interval)
