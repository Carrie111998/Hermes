"""CS-02b concurrency proofs for the side-effect ledger."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from hermes_cli.side_effects import api, schema


def test_concurrent_reserve_same_key_only_one_wins(tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    schema.migrate(db_path)
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        return api.reserve(
            task_id="race-task",
            lane="platform",
            action_type="test.action",
            payload={"message": "only once"},
            db_path=db_path,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: attempt(), range(8)))

    winners = [result for result in results if result.reserved_id is not None]
    duplicates = [
        result
        for result in results
        if result.already_in_flight is not None
        or result.already_done is not None
    ]
    assert len(winners) == 1
    assert len(duplicates) == 7
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM side_effects").fetchone()[0] == 1


def test_concurrent_gc_and_insert_no_lock_error(tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    schema.migrate(db_path)
    old = (api._now() - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db_path) as conn:
        for number in range(20):
            conn.execute(
                """
                INSERT INTO side_effects (
                    ts, updated_at, task_id, lane, action_type, payload_hash,
                    idempotency_key, status, attempt_number, vendor
                ) VALUES (?, ?, ?, 'platform', 'test.action', ?, ?, 'done', 1, 'test')
                """,
                (
                    old,
                    old,
                    f"gc-seed-{number}",
                    f"seed-hash-{number}",
                    f"seed-key-{number}",
                ),
            )

    errors: list[BaseException] = []
    gc_deleted = 0
    insertion_done = threading.Event()

    def insert_rows() -> None:
        try:
            for number in range(100):
                result = api.reserve(
                    task_id=f"insert-{number}",
                    lane="platform",
                    action_type="test.action",
                    payload={"number": number},
                    db_path=db_path,
                )
                assert result.reserved_id is not None
                time.sleep(0.02)
        except BaseException as exc:
            errors.append(exc)
        finally:
            insertion_done.set()

    def collect_rows() -> None:
        nonlocal gc_deleted
        try:
            while not insertion_done.is_set():
                result = api.gc(older_than_days=90, db_path=db_path)
                gc_deleted += result["deleted"]
                time.sleep(0.2)
            result = api.gc(older_than_days=90, db_path=db_path)
            gc_deleted += result["deleted"]
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        insert_future = executor.submit(insert_rows)
        gc_future = executor.submit(collect_rows)
        insert_future.result()
        gc_future.result()

    assert errors == []
    assert gc_deleted == 20
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM side_effects").fetchone()[0]
    assert remaining == 120 - gc_deleted
