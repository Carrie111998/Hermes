"""Provider-contract tests for zero-write inventory and archive CAS."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_portfolio as portfolio


def _router():
    plugin = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "kanban_portfolio_contract_test", plugin
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    value = tmp_path / ".hermes"
    value.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(value))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return value


@pytest.fixture
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int, int, str | None]]:
    result: dict[str, tuple[int, int, int, str | None]] = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            digest,
        )
    return result


def _logical_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _blocked_task() -> str:
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="COD-32 residue",
            body=(
                "Automated Linear delegation via Buck/Hermes.\n"
                "Project ID: project-cod\n"
                "Identifier: COD-32\n"
                "Kanban board/tenant: default\n"
            ),
            assignee="coder",
            tenant="default",
        )
        assert kb.block_task(conn, task_id, reason="terminal residue")
        return task_id
    finally:
        conn.close()


def _expected(client: TestClient, task_id: str) -> tuple[str, int]:
    task = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}", params={"board": "default"}
    )
    board = client.get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "default", "include_archived": "true"},
    )
    assert task.status_code == board.status_code == 200
    return task.json()["task"]["updated_at"], board.json()["latest_event_id"]


def _payload(task_id: str, revision: str, watermark: int, *, key: str = "cod32:1"):
    return {
        "contract": kb.PORTFOLIO_KANBAN_CONDITIONAL_ARCHIVE_CONTRACT,
        "board": "default",
        "card_id": task_id,
        "expected_status": "blocked",
        "expected_revision": revision,
        "expected_event_watermark": watermark,
        "operation_key": key,
        "reason": "terminal ownership residue",
    }


def test_inventory_and_operation_gets_are_byte_and_metadata_zero_write(
    client: TestClient, home: Path
) -> None:
    task_id = _blocked_task()
    # Deliberately create and hold a WAL snapshot even when Hermes' runtime
    # policy falls back to DELETE mode for a vulnerable linked SQLite build.
    db_path = kb.kanban_db_path()
    writer = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'operator', 'evidence', 1)",
            (task_id,),
        )
        writer.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'commented', 1)",
            (task_id,),
        )
        writer.execute("COMMIT")
        assert db_path.with_name(db_path.name + "-wal").exists()
        # Opening the live database for the logical baseline may legitimately
        # update SQLite's shared-memory read marks. Capture filesystem bytes
        # only after that baseline connection has closed so the endpoint reads
        # are the sole operations under test.
        before_dump = _logical_dump(db_path)
        before_tree = _tree_fingerprint(home)

        def read_once(_: int) -> tuple[int, int, int]:
            board = client.get(
                "/api/plugins/kanban/portfolio/board",
                params={"board": "default", "include_archived": "true"},
            )
            task = client.get(
                f"/api/plugins/kanban/portfolio/tasks/{task_id}",
                params={"board": "default"},
            )
            operation = client.get(
                f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
                params={"board": "default", "operation_key": "missing"},
            )
            assert (
                board.json()["contract"] == kb.PORTFOLIO_KANBAN_ZERO_WRITE_GET_CONTRACT
            )
            assert (
                task.json()["contract"] == kb.PORTFOLIO_KANBAN_ZERO_WRITE_GET_CONTRACT
            )
            assert board.json()["exhaustive"] is task.json()["exhaustive"] is True
            assert operation.json()["operation"] is None
            return board.status_code, task.status_code, operation.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(read_once, range(16))) == [(200, 200, 200)] * 16
        assert _tree_fingerprint(home) == before_tree
        assert _logical_dump(db_path) == before_dump
    finally:
        writer.close()


def test_absent_board_get_creates_nothing(client: TestClient, home: Path) -> None:
    before = _tree_fingerprint(home)
    response = client.get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "does-not-exist", "include_archived": "true"},
    )
    assert response.status_code == 404
    assert _tree_fingerprint(home) == before
    assert not (home / "kanban" / "boards" / "does-not-exist").exists()


def test_unmigrated_board_get_fails_closed_without_schema_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "legacy"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    path = home / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    conn.close()
    before = _tree_fingerprint(home)
    app = FastAPI()
    app.include_router(_router(), prefix="/api/plugins/kanban")
    response = TestClient(app).get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "default", "include_archived": "true"},
    )
    assert response.status_code == 503
    assert _tree_fingerprint(home) == before


def test_conditional_archive_atomic_success_replay_and_readback(
    client: TestClient,
) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)
    payload = _payload(task_id, revision, watermark)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=payload,
    )
    assert response.status_code == 200, response.text
    proof = response.json()
    assert proof["contract"] == kb.PORTFOLIO_KANBAN_CONDITIONAL_ARCHIVE_CONTRACT
    assert proof["prior_status"] == "blocked"
    assert proof["prior_revision"] == revision
    assert proof["prior_event_watermark"] == watermark
    assert proof["status"] == "archived"
    assert proof["event_id"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "archived"
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conditional_archive_operations"
            ).fetchone()[0]
            == 1
        )
        assert {
            row["name"]: row["type"]
            for row in conn.execute("PRAGMA table_info(conditional_archive_operations)")
        }["event_id"] == "INTEGER"
        other = kb.create_task(conn, title="unrelated global-watermark advance")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'probe', 1)",
            (other,),
        )
    finally:
        conn.close()

    replay = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json() == proof
    conn = kb.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            == event_count
        )
    finally:
        conn.close()
    readback = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default", "operation_key": payload["operation_key"]},
    )
    assert readback.status_code == 200
    assert readback.json()["operation"] == proof


def test_stale_or_rebound_cas_conflicts_and_preserves_card(client: TestClient) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)
    stale = _payload(task_id, revision, watermark - 1)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=stale,
    )
    assert response.status_code == 409
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()

    good = _payload(task_id, revision, watermark, key="same-key")
    assert (
        client.post(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
            params={"board": "default"},
            json=good,
        ).status_code
        == 200
    )
    changed = dict(good, reason="changed binding")
    assert (
        client.post(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
            params={"board": "default"},
            json=changed,
        ).status_code
        == 409
    )


def test_live_claim_is_preserved_even_with_exact_snapshot(client: TestClient) -> None:
    task_id = _blocked_task()
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_lock = 'live-claim', claim_expires = ?, "
                "worker_pid = 1234 WHERE id = ?",
                (2_000_000_000, task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, created_at) "
                "VALUES (?, 'external_reactivated', 1)",
                (task_id,),
            )
    finally:
        conn.close()
    revision, watermark = _expected(client, task_id)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=_payload(task_id, revision, watermark),
    )
    assert response.status_code == 409
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.claim_lock == "live-claim"
    finally:
        conn.close()


def test_concurrent_stale_posts_have_one_winner(client: TestClient) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)

    def submit(index: int) -> int:
        return client.post(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
            params={"board": "default"},
            json=_payload(task_id, revision, watermark, key=f"race:{index}"),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, range(2)))
    assert statuses == [200, 409]
    conn = kb.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conditional_archive_operations"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id = ? AND kind = 'conditional_archived'",
                (task_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_database_is_rejected(client: TestClient, home: Path) -> None:
    link = home / "kanban-copy.db"
    os.link(home / "kanban.db", link)
    try:
        response = client.get(
            "/api/plugins/kanban/portfolio/board",
            params={"board": "default", "include_archived": "true"},
        )
        assert response.status_code == 503
    finally:
        link.unlink()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlinked_named_board_directory_is_rejected(
    client: TestClient, home: Path, tmp_path: Path
) -> None:
    external = tmp_path / "external-board"
    external.mkdir(mode=0o700)
    kb.init_db(db_path=external / "kanban.db")
    boards = home / "kanban" / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    (boards / "redirected").symlink_to(external, target_is_directory=True)
    response = client.get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "redirected", "include_archived": "true"},
    )
    assert response.status_code == 503


def test_snapshot_requires_two_complete_byte_equal_db_wal_captures(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "kanban.db"
    calls = 0

    def forged_capture(
        _path: Path, _wal: Path
    ) -> tuple[tuple[int, int, int, int], None, bytes, bytes | None]:
        nonlocal calls
        calls += 1
        return (
            (1, 2, 11, 100),
            None,
            b"same-size-A" if calls % 2 else b"same-size-B",
            b"wal",
        )

    monkeypatch.setattr(portfolio, "_capture_files", forged_capture)
    with pytest.raises(portfolio.PortfolioSnapshotUnavailable):
        with portfolio.zero_write_snapshot("default"):
            pass
    assert calls == 2 * portfolio._COPY_ATTEMPTS
    assert path.exists()


def test_snapshot_rejects_same_bytes_when_wal_identity_changes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def replaced_wal(
        _path: Path, _wal: Path
    ) -> tuple[
        tuple[int, int, int, int],
        tuple[int, int, int, int],
        bytes,
        bytes,
    ]:
        nonlocal calls
        calls += 1
        return (1, 2, 10, 100), (1, calls, 32, 100), b"same-db", b"same-wal"

    monkeypatch.setattr(portfolio, "_capture_files", replaced_wal)
    with pytest.raises(portfolio.PortfolioSnapshotUnavailable):
        with portfolio.zero_write_snapshot("default"):
            pass
    assert calls == 2 * portfolio._COPY_ATTEMPTS


def test_exact_schema_digest_rejects_forged_trigger(home: Path) -> None:
    conn = sqlite3.connect(home / "kanban.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("DROP TRIGGER trg_tasks_revision_after_update")
        conn.executescript(
            """
            CREATE TRIGGER trg_tasks_revision_after_update
            AFTER UPDATE ON tasks
            BEGIN
                SELECT CASE WHEN OLD.revision + 1 > 0 THEN 1 END;
            END;
            """
        )
        with pytest.raises(kb.PortfolioContractSchemaUnavailable, match="artifact"):
            portfolio.validate_portfolio_schema(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "artifact_sql",
    (
        """CREATE TRIGGER trg_unreviewed_revision_reset AFTER UPDATE ON tasks
            BEGIN UPDATE tasks SET revision = OLD.revision WHERE id = NEW.id; END""",
        "CREATE UNIQUE INDEX idx_unreviewed_unique_task_status ON tasks(id, status)",
        "CREATE INDEX idx_unreviewed_partial_events ON task_events(kind) WHERE payload IS NOT NULL",
        "CREATE TRIGGER trg_unreviewed_run_mutation AFTER INSERT ON task_runs BEGIN SELECT 1; END",
    ),
)
def test_unexpected_behavior_artifacts_fail_get_and_post(
    client: TestClient, home: Path, artifact_sql: str
) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)
    conn = sqlite3.connect(home / "kanban.db", isolation_level=None)
    try:
        conn.execute(artifact_sql)
    finally:
        conn.close()
    board = client.get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "default", "include_archived": "true"},
    )
    assert board.status_code == 503
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=_payload(task_id, revision, watermark),
    )
    assert response.status_code == 503


def test_existing_marker_digest_must_be_exact(home: Path) -> None:
    conn = sqlite3.connect(home / "kanban.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "UPDATE kanban_schema_revisions SET schema_digest = 'forged' "
            "WHERE revision = ?",
            (kb.PORTFOLIO_KANBAN_SCHEMA_REVISION,),
        )
        with pytest.raises(kb.PortfolioContractSchemaUnavailable, match="marker"):
            kb._install_portfolio_contract_schema(conn)
        assert (
            conn.execute(
                "SELECT schema_digest FROM kanban_schema_revisions WHERE revision = ?",
                (kb.PORTFOLIO_KANBAN_SCHEMA_REVISION,),
            ).fetchone()["schema_digest"]
            == "forged"
        )
    finally:
        conn.close()


def test_migration_rejects_partial_artifacts_before_marker_is_blessed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial.db"
    kb.init_db(db_path=path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        for kind, name in (
            ("TRIGGER", "trg_tasks_revision_after_update"),
            ("INDEX", "idx_conditional_archive_card"),
            ("TABLE", "conditional_archive_operations"),
            ("TABLE", "kanban_schema_revisions"),
        ):
            conn.execute(f"DROP {kind} {name}")
        conn.execute("CREATE TABLE conditional_archive_operations (operation_key TEXT)")
    finally:
        conn.close()
    kb._INITIALIZED_PATHS.clear()
    with pytest.raises(kb.PortfolioContractSchemaUnavailable, match="partial"):
        kb.init_db(db_path=path)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'kanban_schema_revisions'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def _remove_portfolio_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER trg_tasks_revision_after_update")
    conn.execute("DROP INDEX idx_conditional_archive_card")
    conn.execute("DROP TABLE conditional_archive_operations")
    conn.execute("DROP TABLE kanban_schema_revisions")


def _schema_fingerprint(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]


@pytest.mark.parametrize(
    ("action", "target"),
    (
        (sqlite3.SQLITE_CREATE_TABLE, "kanban_schema_revisions"),
        (sqlite3.SQLITE_CREATE_TABLE, "conditional_archive_operations"),
        (sqlite3.SQLITE_CREATE_INDEX, "idx_conditional_archive_card"),
        (sqlite3.SQLITE_CREATE_TRIGGER, "trg_tasks_revision_after_update"),
        (sqlite3.SQLITE_INSERT, "kanban_schema_revisions"),
    ),
)
def test_portfolio_migration_failpoints_roll_back_every_artifact(
    home: Path, action: int, target: str
) -> None:
    conn = sqlite3.connect(home / "kanban.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _remove_portfolio_schema(conn)
        before = _schema_fingerprint(conn)

        def deny(
            selected: int,
            first: str | None,
            _second: str | None,
            _db: str | None,
            _source: str | None,
        ) -> int:
            if selected == action and first == target:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny)
        with pytest.raises(sqlite3.DatabaseError):
            kb._install_portfolio_contract_schema(conn)
        conn.set_authorizer(None)
        assert _schema_fingerprint(conn) == before
        assert not conn.in_transaction
    finally:
        conn.set_authorizer(None)
        conn.close()


def test_portfolio_migration_validation_failure_rolls_back_and_reopen_is_idempotent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "kanban.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _remove_portfolio_schema(conn)
        before = _schema_fingerprint(conn)
        original = kb.validate_portfolio_contract_schema

        def injected_failure(_conn: sqlite3.Connection) -> None:
            raise kb.PortfolioContractSchemaUnavailable("injected validation failure")

        monkeypatch.setattr(kb, "validate_portfolio_contract_schema", injected_failure)
        with pytest.raises(kb.PortfolioContractSchemaUnavailable, match="injected"):
            kb._install_portfolio_contract_schema(conn)
        assert _schema_fingerprint(conn) == before
        monkeypatch.setattr(kb, "validate_portfolio_contract_schema", original)
        kb._install_portfolio_contract_schema(conn)
        installed = _schema_fingerprint(conn)
        kb._install_portfolio_contract_schema(conn)
        assert _schema_fingerprint(conn) == installed
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.clear()
    reopened = kb.connect(db_path=path)
    try:
        kb.validate_portfolio_contract_schema(reopened)
        assert _schema_fingerprint(reopened) == installed
    finally:
        reopened.close()


def test_archive_uses_dedicated_existing_board_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)

    def forbidden_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("auto-initializing connect must not be used")

    monkeypatch.setattr(kb, "connect", forbidden_connect)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=_payload(task_id, revision, watermark),
    )
    assert response.status_code == 200, response.text


def test_nullable_ordinary_card_body_is_controller_compatible(
    client: TestClient,
) -> None:
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ordinary title-only card")
    finally:
        conn.close()
    response = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}", params={"board": "default"}
    )
    assert response.status_code == 200
    task = response.json()["task"]
    # ProductionOwnershipEvidenceAdapter checks this type before marker parsing.
    assert task["body"] == ""
    assert isinstance(task["body"], str)


def test_board_contract_rejects_filters_and_requires_archive_inclusion(
    client: TestClient,
) -> None:
    _blocked_task()
    endpoint = "/api/plugins/kanban/portfolio/board"
    assert client.get(endpoint, params={"board": "default"}).status_code == 422
    assert (
        client.get(
            endpoint,
            params={"board": "default", "include_archived": "false"},
        ).status_code
        == 400
    )
    for extra in ("tenant", "workflow_template_id", "current_step_key", "status"):
        response = client.get(
            endpoint,
            params={
                "board": "default",
                "include_archived": "true",
                extra: "filtered",
            },
        )
        assert response.status_code == 400, (extra, response.text)


def test_task_byte_bound_is_scoped_before_materialization(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = kb.connect()
    try:
        selected = kb.create_task(conn, title="selected")
        unrelated = kb.create_task(conn, title="unrelated")
        conn.executemany(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'a', ?, 1)",
            [(unrelated, "x" * 100)] * 6,
        )
    finally:
        conn.close()
    monkeypatch.setattr(portfolio, "MAX_PREMATERIALIZATION_SOURCE_BYTES", 500)
    scoped = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{selected}",
        params={"board": "default"},
    )
    assert scoped.status_code == 200, scoped.text
    overflow = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{unrelated}",
        params={"board": "default"},
    )
    assert overflow.status_code == 413


def _insert_oversized_collection(
    conn: sqlite3.Connection,
    task_id: str,
    collection: str,
    pieces: list[str],
) -> None:
    if collection == "links":
        conn.executemany(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            [(task_id, f"{piece}-{index}") for index, piece in enumerate(pieces)],
        )
    elif collection == "comments":
        conn.executemany(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'author', ?, 1)",
            [(task_id, piece) for piece in pieces],
        )
    elif collection == "events":
        conn.executemany(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'evidence', ?, 1)",
            [(task_id, piece) for piece in pieces],
        )
    elif collection == "runs":
        conn.executemany(
            "INSERT INTO task_runs "
            "(task_id, status, started_at, ended_at, metadata) "
            "VALUES (?, 'completed', 1, 2, ?)",
            [(task_id, piece) for piece in pieces],
        )
    elif collection == "attachments":
        conn.executemany(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, size, created_at) "
            "VALUES (?, 'evidence', ?, 0, 1)",
            [(task_id, piece) for piece in pieces],
        )
    elif collection in {"child_results", "child_summaries"}:
        for index, piece in enumerate(pieces):
            child = kb.create_task(conn, title=f"child-{index}")
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (task_id, child),
            )
            if collection == "child_results":
                conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (piece, child))
            else:
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, status, started_at, ended_at, summary) "
                    "VALUES (?, 'completed', 1, 2, ?)",
                    (child, piece),
                )
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(collection)


@pytest.mark.parametrize(
    "collection",
    (
        "links",
        "comments",
        "events",
        "runs",
        "attachments",
        "child_results",
        "child_summaries",
    ),
)
@pytest.mark.parametrize(
    "pieces", (["x" * 1_200], ["x" * 450] * 3), ids=("single-row", "aggregate")
)
def test_each_evidence_collection_is_byte_bounded_before_materialization(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    collection: str,
    pieces: list[str],
) -> None:
    task_id = _blocked_task()
    conn = kb.connect()
    try:
        _insert_oversized_collection(conn, task_id, collection, pieces)
    finally:
        conn.close()
    monkeypatch.setattr(portfolio, "MAX_PREMATERIALIZATION_SOURCE_BYTES", 1_000)
    response = client.get(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}",
        params={"board": "default"},
    )
    assert response.status_code == 413, (collection, response.text)


@pytest.mark.parametrize(
    "bodies", (["x" * 1_200], ["x" * 450] * 3), ids=("single-row", "aggregate")
)
def test_board_task_scalars_are_byte_bounded_before_materialization(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bodies: list[str]
) -> None:
    conn = kb.connect()
    try:
        for index, body in enumerate(bodies):
            kb.create_task(conn, title=f"bounded-{index}", body=body)
    finally:
        conn.close()
    monkeypatch.setattr(portfolio, "MAX_PREMATERIALIZATION_SOURCE_BYTES", 1_000)
    response = client.get(
        "/api/plugins/kanban/portfolio/board",
        params={"board": "default", "include_archived": "true"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("run_status", sorted(kb.TERMINAL_RUN_STATUSES))
def test_terminal_current_run_is_cleared_with_archive_in_one_revision(
    client: TestClient, run_status: str
) -> None:
    task_id = _blocked_task()
    conn = kb.connect()
    try:
        run = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
            "VALUES (?, ?, 1, 2, ?)",
            (task_id, run_status, run_status),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run.lastrowid, task_id)
        )
    finally:
        conn.close()
    revision, watermark = _expected(client, task_id)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=_payload(task_id, revision, watermark),
    )
    assert response.status_code == 200, response.text
    assert int(response.json()["post_revision"]) == int(revision) + 1
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, revision, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("archived", int(revision) + 1, None)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("run_status", "ended_at"),
    (
        ("running", 2),
        ("queued", 2),
        ("active", 2),
        ("in_progress", 2),
        ("done", None),
        ("unknown", 2),
        ("", 2),
    ),
)
def test_current_run_status_end_contradictions_conflict_without_change(
    client: TestClient, run_status: str, ended_at: int | None
) -> None:
    task_id = _blocked_task()
    conn = kb.connect()
    try:
        run = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at) VALUES (?, ?, 1, ?)",
            (task_id, run_status, ended_at),
        )
        run_id = int(run.lastrowid or 0)
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id)
        )
    finally:
        conn.close()
    revision, watermark = _expected(client, task_id)
    response = client.post(
        f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
        params={"board": "default"},
        json=_payload(task_id, revision, watermark),
    )
    assert response.status_code == 409
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, revision, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("blocked", int(revision), run_id)
    finally:
        conn.close()


def test_contract_routes_require_explicit_nonempty_board(client: TestClient) -> None:
    task_id = _blocked_task()
    revision, watermark = _expected(client, task_id)
    assert client.get("/api/plugins/kanban/portfolio/board").status_code == 422
    assert (
        client.get(f"/api/plugins/kanban/portfolio/tasks/{task_id}").status_code == 422
    )
    assert (
        client.get(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
            params={"operation_key": "missing"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}/conditional-archive",
            json=_payload(task_id, revision, watermark),
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/plugins/kanban/portfolio/board", params={"board": ""}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/plugins/kanban/portfolio/board",
            params={
                "board": "../default",
                "include_archived": "true",
            },
        ).status_code
        == 400
    )


def test_contract_responses_are_deterministic_and_run_filters_are_rejected(
    client: TestClient,
) -> None:
    task_id = _blocked_task()
    params = {"board": "default", "include_archived": "true"}
    assert (
        client.get("/api/plugins/kanban/portfolio/board", params=params).content
        == client.get("/api/plugins/kanban/portfolio/board", params=params).content
    )
    task_params = {"board": "default"}
    assert (
        client.get(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}", params=task_params
        ).content
        == client.get(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}", params=task_params
        ).content
    )
    assert (
        client.get(
            f"/api/plugins/kanban/portfolio/tasks/{task_id}",
            params={
                "board": "default",
                "run_state_type": "status",
                "run_state_name": "running",
            },
        ).status_code
        == 400
    )
