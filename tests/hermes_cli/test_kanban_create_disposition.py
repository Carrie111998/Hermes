"""`kanban create` must report whether it inserted a row or adopted one.

``create_task`` already knows, atomically, whether it returned a
same-idempotency-key row or inserted a new one — but it only ever handed
back a bare id, so ``hermes kanban create --json`` could not tell an
automation caller which happened. Timestamp / title / status heuristics
cannot recover it (a same-second adoption is indistinguishable from a
fresh insert), so the answer has to come from the DB layer's own branch.

Covered here: the structured DB result, the ``disposition`` JSON field on
the CLI, the archived-predecessor carve-out, legacy-board compatibility,
and the concurrency invariant (one key never yields two ``created``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


ROOT = Path(__file__).parents[2]

# The gateway parses this exact shape out of the CLI's human-readable create
# output to auto-subscribe the originating chat to the new task's events
# (``gateway/slash_commands.py::_handle_kanban_command``). Adding the JSON
# field must not disturb it.
_GATEWAY_CREATE_RE = re.compile(r"Created\s+(t_[0-9a-f]+)\b")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer — create_task_result
# ---------------------------------------------------------------------------


def test_fresh_create_reports_created(kanban_home):
    with kb.connect_closing() as conn:
        result = kb.create_task_result(
            conn, title="fresh", idempotency_key="key-fresh"
        )

    assert result.created is True
    assert result.disposition == "created"
    assert result.task_id.startswith("t_")


def test_create_without_idempotency_key_reports_created(kanban_home):
    """No key means no dedup — every call is a genuine insert."""
    with kb.connect_closing() as conn:
        first = kb.create_task_result(conn, title="unkeyed")
        second = kb.create_task_result(conn, title="unkeyed")

    assert first.task_id != second.task_id
    assert (first.disposition, second.disposition) == ("created", "created")


def test_same_key_ready_task_reports_existing(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task_result(
            conn, title="nightly", idempotency_key="key-ready"
        )
        second = kb.create_task_result(
            conn, title="nightly", idempotency_key="key-ready"
        )
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = 'key-ready'"
        ).fetchall()

    assert second.task_id == first.task_id
    assert second.created is False
    assert second.disposition == "existing"
    assert len(rows) == 1


def test_same_key_done_task_reports_existing(kanban_home):
    """Dedup is retired by ``archived`` only — a finished task still dedups."""
    with kb.connect_closing() as conn:
        first = kb.create_task_result(
            conn, title="done work", idempotency_key="key-done"
        )
        assert kb.complete_task(conn, first.task_id, result="ok") is True
        finished = kb.get_task(conn, first.task_id)
        assert finished is not None and finished.status == "done"

        second = kb.create_task_result(
            conn, title="done work", idempotency_key="key-done"
        )

    assert second.task_id == first.task_id
    assert second.disposition == "existing"


def test_archived_predecessor_causes_a_new_created(kanban_home):
    """``archived`` rows are excluded from dedup, so the next call inserts."""
    with kb.connect_closing() as conn:
        first = kb.create_task_result(
            conn, title="retired", idempotency_key="key-archived"
        )
        assert kb.archive_task(conn, first.task_id) is True

        second = kb.create_task_result(
            conn, title="retired again", idempotency_key="key-archived"
        )
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = 'key-archived' "
            "ORDER BY created_at"
        ).fetchall()

    assert second.task_id != first.task_id
    assert second.created is True
    assert second.disposition == "created"
    assert {r["status"] for r in rows} == {"archived", "ready"}


def test_create_task_wrapper_still_returns_a_plain_id(kanban_home):
    """Existing callers keep a bare ``str`` — no return-type migration."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy caller", idempotency_key="key-w")
        again = kb.create_task(conn, title="legacy caller", idempotency_key="key-w")

    assert type(task_id) is str
    assert again == task_id
    # Round-trips through the same wire encodings callers already use.
    assert json.loads(json.dumps({"id": task_id}))["id"] == task_id


def test_disposition_is_derived_from_created_only(kanban_home):
    """The frozen result is the single source of truth, and is immutable."""
    created = kb.TaskCreation(task_id="t_deadbeef", created=True)
    existing = kb.TaskCreation(task_id="t_deadbeef", created=False)

    assert created.disposition == "created"
    assert existing.disposition == "existing"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(created, "task_id", "t_other")


def test_legacy_board_without_idempotency_column_still_reports_disposition(tmp_path):
    """A pre-``idempotency_key`` board migrates and then dedups normally."""
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    migrated = kb.connect(db_path)
    try:
        first = kb.create_task_result(
            migrated, title="post-migration", idempotency_key="key-legacy"
        )
        second = kb.create_task_result(
            migrated, title="post-migration", idempotency_key="key-legacy"
        )
    finally:
        migrated.close()

    assert first.disposition == "created"
    assert second.disposition == "existing"
    assert second.task_id == first.task_id


def test_pre_existing_key_row_written_without_the_helper_reports_existing(kanban_home):
    """Rows a legacy writer inserted directly still satisfy the dedup branch."""
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, "
            "workspace_kind, idempotency_key) "
            "VALUES ('t_00000001', 'hand written', 'ready', 0, 1, 'scratch', 'key-raw')"
        )
        result = kb.create_task_result(
            conn, title="hand written", idempotency_key="key-raw"
        )

    assert result.task_id == "t_00000001"
    assert result.disposition == "existing"


def test_concurrent_same_key_creates_yield_exactly_one_created(kanban_home):
    """One idempotency key must never produce two ``created`` outcomes.

    This is the test that pins the re-check under ``BEGIN IMMEDIATE``:
    delete it and all eight racers report ``created`` and insert eight
    rows. A connection is SQLite's unit of locking, so eight
    barrier-released connections are the worst case — every one of them
    clears the lock-free dedup lookup before any of them commits, which is
    precisely the window the fast path alone cannot close.
    """
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[kb.TaskCreation | None] = [None] * workers
    failures: list[str | None] = [None] * workers

    def _worker(index: int) -> None:
        try:
            conn = kb.connect()
            try:
                barrier.wait(timeout=30)
                outcomes[index] = kb.create_task_result(
                    conn, title=f"racer {index}", idempotency_key="key-race"
                )
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            failures[index] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == [None] * workers
    dispositions = [o.disposition for o in outcomes if o is not None]
    assert len(dispositions) == workers
    assert dispositions.count("created") == 1
    assert dispositions.count("existing") == workers - 1

    ids = {o.task_id for o in outcomes if o is not None}
    assert len(ids) == 1

    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = 'key-race'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == ids.pop()


# ---------------------------------------------------------------------------
# CLI layer — hermes kanban create --json
# ---------------------------------------------------------------------------


def test_cli_json_reports_created_then_existing(kanban_home):
    first = json.loads(kc.run_slash('create "cli task" --idempotency-key cli-1 --json'))
    second = json.loads(kc.run_slash('create "cli task" --idempotency-key cli-1 --json'))

    assert first["disposition"] == "created"
    assert second["disposition"] == "existing"
    assert second["id"] == first["id"]


def test_cli_json_adds_exactly_one_key_and_preserves_the_task_shape(kanban_home):
    payload = json.loads(kc.run_slash('create "shape check" --json'))

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task is not None
    expected = kc._task_to_dict(task)

    assert set(payload) == set(expected) | {"disposition"}
    assert {k: v for k, v in payload.items() if k != "disposition"} == expected


def test_cli_json_archived_predecessor_reports_created(kanban_home):
    first = json.loads(kc.run_slash('create "recycled" --idempotency-key cli-arc --json'))
    kc.run_slash(f"archive {first['id']}")
    second = json.loads(kc.run_slash('create "recycled" --idempotency-key cli-arc --json'))

    assert first["disposition"] == "created"
    assert second["disposition"] == "created"
    assert second["id"] != first["id"]


def test_cli_text_output_stays_parseable_by_the_gateway(kanban_home):
    """The human line is a load-bearing contract, not just cosmetics."""
    first = kc.run_slash('create "text task" --idempotency-key cli-txt')
    second = kc.run_slash('create "text task" --idempotency-key cli-txt')

    first_match = _GATEWAY_CREATE_RE.search(first)
    second_match = _GATEWAY_CREATE_RE.search(second)
    assert first_match is not None, first
    assert second_match is not None, second
    assert second_match.group(1) == first_match.group(1)


# ---------------------------------------------------------------------------
# Real CLI subprocess — the actual `hermes kanban create --json` boundary
# ---------------------------------------------------------------------------


def _hermes_env(home: Path) -> dict[str, str]:
    """Env that pins a child `hermes` process to a throwaway board."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=_hermes_env(home),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_subprocess_create_json_reports_created_then_existing(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    first = _run_hermes(
        home, "kanban", "create", "e2e task", "--idempotency-key", "e2e-1", "--json"
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["disposition"] == "created"

    second = _run_hermes(
        home, "kanban", "create", "e2e task", "--idempotency-key", "e2e-1", "--json"
    )
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["disposition"] == "existing"
    assert second_payload["id"] == first_payload["id"]

    archived = _run_hermes(home, "kanban", "archive", first_payload["id"])
    assert archived.returncode == 0, archived.stderr

    third = _run_hermes(
        home, "kanban", "create", "e2e task", "--idempotency-key", "e2e-1", "--json"
    )
    assert third.returncode == 0, third.stderr
    third_payload = json.loads(third.stdout)
    assert third_payload["disposition"] == "created"
    assert third_payload["id"] != first_payload["id"]


def test_subprocess_concurrent_creates_report_one_created(tmp_path, monkeypatch):
    """Four real `hermes` processes on one board still agree on one insert.

    Contention is forced rather than hoped for: this process holds
    ``BEGIN IMMEDIATE`` on the board while the four children start, so all
    four are parked on the same write lock (their 120s busy timeout
    absorbs the wait) before any of them can proceed.

    What this does *not* do is pin the ``BEGIN IMMEDIATE`` re-check —
    measured, it stays green with that re-check deleted. A `hermes`
    process takes the write lock on its way through connect/migration,
    *before* it runs the lock-free dedup lookup, so the CLI path is
    already totally serialized by SQLite and every racer but the first
    sees the committed row on the fast path. The re-check is pinned by
    ``test_concurrent_same_key_creates_yield_exactly_one_created``, where
    connections clear the fast path together. This test guards the
    end-to-end contract instead: whatever the interleaving, four
    concurrent CLI creates emit one ``created``, one id, and one row.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # Initialize the board once so the racers overlap on the create, not on
    # schema creation.
    assert _run_hermes(home, "kanban", "init").returncode == 0

    env = _hermes_env(home)
    gate = kb.connect()
    procs: list[subprocess.Popen[str]] = []
    try:
        gate.execute("BEGIN IMMEDIATE")
        procs = [
            subprocess.Popen(
                [
                    sys.executable, "-m", "hermes_cli.main", "kanban", "create",
                    f"racer {i}", "--idempotency-key", "e2e-race", "--json",
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(4)
        ]
        # Loose lower bound, deliberately not a tight one: it only has to
        # exceed interpreter startup for the children to be queued on the
        # lock when it drops. If a straggler is still importing, it simply
        # dedups on the fast path — the assertions below hold either way,
        # so a slow machine weakens the race without failing the test.
        time.sleep(3.0)
    finally:
        gate.execute("ROLLBACK")
        gate.close()

    payloads = []
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err
        payloads.append(json.loads(out))

    assert [p["disposition"] for p in payloads].count("created") == 1
    assert len({p["id"] for p in payloads}) == 1

    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = 'e2e-race'"
        ).fetchall()
    assert len(rows) == 1
