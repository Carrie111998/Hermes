"""Regression gate for #86516: four SessionDB readers bypassed ``_read_ctx``.

``get_compression_lock_holder``, ``clear_session_activity_labels``'s
no-op fast path, ``get_handoff_state`` and ``list_pending_handoffs`` ran
their SELECTs as bare ``self._conn.execute(...)`` — on the *shared writer
connection*, outside both ``self._lock`` and the WAL reader pool. That is
worse than the locked-reader shape ``test_no_locked_readers_gate.py``
covers: with a concurrent thread inside ``_execute_write``'s
``BEGIN IMMEDIATE``, the SELECT joins that thread's *uncommitted*
transaction (dirty/phantom read), and under DELETE journal mode it can
collide outright — which the two handoff sites swallowed into ``None``
and ``[]``.

Two layers here:

* a source gate proving the four methods reach the database only through
  ``_read_ctx()`` (so a future edit cannot silently reintroduce it), and
* a behavioural invariant: while a writer holds an open transaction,
  these readers observe committed-only state and return promptly.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from hermes_state import SessionDB

_STATE_PY = Path(__file__).resolve().parents[2] / "hermes_state.py"

# The four methods #86516 moved onto _read_ctx. Every read they perform must
# go through the pooled reader (or its locked-writer fallback) — never the
# bare writer connection.
_READ_CTX_ONLY_METHODS = (
    "get_compression_lock_holder",
    "clear_session_activity_labels",
    "get_handoff_state",
    "list_pending_handoffs",
)

# How long a reader may take while a writer holds an open transaction. The
# readers are pooled and lock-free, so this is orders of magnitude of slack;
# it exists only so a regression fails instead of hanging the suite.
_READER_TIMEOUT_S = 10.0


def _session_db_class(state_py: Path) -> ast.ClassDef:
    tree = ast.parse(state_py.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SessionDB":
            return node
    raise AssertionError("SessionDB class not found in %s" % state_py)


def _is_writer_conn_execute(call: ast.Call) -> bool:
    """True for ``self._conn.execute*(...)`` — the shared writer connection."""
    f = call.func
    if not (
        isinstance(f, ast.Attribute)
        and f.attr in ("execute", "executemany", "executescript")
    ):
        return False
    target = f.value
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "_conn"
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    )


def _opens_read_ctx(method: ast.AST) -> bool:
    for node in ast.walk(method):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            ctx = item.context_expr
            if (
                isinstance(ctx, ast.Call)
                and isinstance(ctx.func, ast.Attribute)
                and ctx.func.attr == "_read_ctx"
                and isinstance(ctx.func.value, ast.Name)
                and ctx.func.value.id == "self"
            ):
                return True
    return False


def _scan_writer_conn_readers(state_py: Optional[Path] = None) -> List[str]:
    """Report which of the four methods still touch ``self._conn`` directly.

    Writes issued through ``_execute_write``'s ``fn(conn)`` callback are not
    ``self._conn`` calls, so a method that both reads and writes still scans
    clean once its read moved to ``_read_ctx``.
    """
    target = state_py if state_py is not None else _STATE_PY
    session_db = _session_db_class(target)
    seen: set[str] = set()
    violations: List[str] = []

    for method in session_db.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name not in _READ_CTX_ONLY_METHODS:
            continue
        seen.add(method.name)
        offenders = [
            node.lineno
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and _is_writer_conn_execute(node)
        ]
        if offenders:
            violations.append(
                f"{method.name} (line {offenders[0]}): executes on the shared "
                f"writer connection `self._conn` — route the read through "
                f"`with self._read_ctx() as conn:` (#86516)"
            )
        elif not _opens_read_ctx(method):
            violations.append(
                f"{method.name} (line {method.lineno}): reads the database "
                f"without opening `self._read_ctx()` (#86516)"
            )

    missing = set(_READ_CTX_ONLY_METHODS) - seen
    assert not missing, f"methods disappeared from SessionDB: {sorted(missing)}"
    return violations


class TestReadersUseReadCtx:
    def test_four_readers_never_touch_the_writer_connection(self) -> None:
        violations = _scan_writer_conn_readers()
        assert violations == [], (
            "SessionDB read paths still running on the shared writer "
            "connection (they join a concurrent BEGIN IMMEDIATE and read "
            "uncommitted rows):\n  " + "\n  ".join(violations)
        )

    def test_gate_detects_a_bare_writer_select(self, tmp_path: Path) -> None:
        """Sabotage self-check: the pre-fix shape must be flagged."""
        sabotage = (
            "class SessionDB:\n"
            "    def get_compression_lock_holder(self, session_id):\n"
            "        return self._conn.execute('SELECT holder FROM t').fetchone()\n"
            "    def clear_session_activity_labels(self, session_id):\n"
            "        with self._read_ctx() as conn:\n"
            "            conn.execute('SELECT 1')\n"
            "    def get_handoff_state(self, session_id):\n"
            "        return None\n"
            "    def list_pending_handoffs(self):\n"
            "        with self._read_ctx() as conn:\n"
            "            return conn.execute('SELECT 2').fetchall()\n"
        )
        p = tmp_path / "fake_state.py"
        p.write_text(sabotage, encoding="utf-8")
        flagged = {v.split(" ")[0] for v in _scan_writer_conn_readers(p)}
        # get_compression_lock_holder: bare writer SELECT.
        # get_handoff_state: reads nothing through _read_ctx at all.
        assert flagged == {"get_compression_lock_holder", "get_handoff_state"}


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _insert_session(conn: Any, session_id: str, **cols: Any) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        (session_id, "cli", time.time()),
    )
    for name, value in cols.items():
        conn.execute(f"UPDATE sessions SET {name} = ? WHERE id = ?", (value, session_id))


def _run_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Dict[str, Any]:
    """Run *fn* on a thread; return ``{"done", "value", "error"}``.

    A reader that convoys behind the in-flight writer never returns, so the
    assertion has to be "finished within the budget", not a plain call.
    """
    box: Dict[str, Any] = {"done": False, "value": None, "error": None}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # surfaced by the assertions below
            box["error"] = exc
        finally:
            box["done"] = True

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_s)
    return box


class TestReadersIgnoreInFlightWrites:
    """While another thread holds ``BEGIN IMMEDIATE``, readers see only
    committed rows and do not convoy behind it."""

    def test_readers_see_committed_state_only(self, db: SessionDB) -> None:
        if not db._wal_active:
            pytest.skip("WAL inactive: _read_ctx degrades to the writer lock")

        from agent.session_activity import ActivityProvenance

        # Committed baseline: one pending handoff, one session whose activity
        # labels are already clear, and no compression lock.
        def _seed(conn: Any) -> None:
            _insert_session(conn, "sess-committed", handoff_state="pending")
            _insert_session(
                conn,
                "sess-labels",
                last_activity_description="",
                last_activity_provenance=ActivityProvenance.UNKNOWN.value,
            )

        db._execute_write(_seed)

        inside = threading.Event()
        release = threading.Event()
        writer_error: List[BaseException] = []

        def _in_flight(conn: Any) -> None:
            # Every row this stages is invisible to a correct reader.
            _insert_session(conn, "sess-inflight", handoff_state="pending")
            conn.execute(
                "UPDATE sessions SET last_activity_description = ?, "
                "last_activity_provenance = ? WHERE id = ?",
                ("compressing", ActivityProvenance.AGENT_COMPRESSION.value, "sess-labels"),
            )
            now = time.time()
            conn.execute(
                "INSERT OR IGNORE INTO compression_locks "
                "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                ("sess-committed", "ghost-holder", now, now + 300),
            )
            inside.set()
            release.wait(_READER_TIMEOUT_S)

        def _writer() -> None:
            try:
                db._execute_write(_in_flight)
            except BaseException as exc:
                writer_error.append(exc)

        writer = threading.Thread(target=_writer, daemon=True)
        writer.start()
        try:
            assert inside.wait(_READER_TIMEOUT_S), "writer never entered its transaction"

            pending = _run_with_timeout(db.list_pending_handoffs, _READER_TIMEOUT_S)
            assert pending["done"], "list_pending_handoffs convoyed behind the writer"
            assert pending["error"] is None, pending["error"]
            assert [r["id"] for r in pending["value"]] == ["sess-committed"], (
                "list_pending_handoffs read an uncommitted row (or swallowed a "
                "lock error into [])"
            )

            ghost = _run_with_timeout(
                lambda: db.get_handoff_state("sess-inflight"), _READER_TIMEOUT_S
            )
            assert ghost["done"], "get_handoff_state convoyed behind the writer"
            assert ghost["error"] is None, ghost["error"]
            assert ghost["value"] is None, "get_handoff_state read an uncommitted row"

            committed = _run_with_timeout(
                lambda: db.get_handoff_state("sess-committed"), _READER_TIMEOUT_S
            )
            assert committed["done"], "get_handoff_state convoyed behind the writer"
            assert committed["error"] is None, committed["error"]
            assert committed["value"] is not None, (
                "get_handoff_state swallowed a lock error into None for a "
                "committed row"
            )
            assert committed["value"]["state"] == "pending"

            holder = _run_with_timeout(
                lambda: db.get_compression_lock_holder("sess-committed"),
                _READER_TIMEOUT_S,
            )
            assert holder["done"], "get_compression_lock_holder convoyed behind the writer"
            assert holder["error"] is None, holder["error"]
            assert holder["value"] is None, (
                "get_compression_lock_holder read an uncommitted lock row"
            )

            # Committed labels are already clear, so the fast path must
            # short-circuit. A dirty read of the in-flight "compressing"
            # labels sends it into _execute_write, which blocks on the
            # writer lock the in-flight transaction still holds.
            cleared = _run_with_timeout(
                lambda: db.clear_session_activity_labels("sess-labels"),
                _READER_TIMEOUT_S,
            )
            assert cleared["done"], (
                "clear_session_activity_labels took the write path — its fast "
                "path read uncommitted activity labels"
            )
            assert cleared["error"] is None, cleared["error"]
        finally:
            release.set()
            writer.join(_READER_TIMEOUT_S)

        assert not writer_error, writer_error
        assert writer.is_alive() is False

        # And once committed, the same readers observe the new state.
        assert db.get_handoff_state("sess-inflight") == {
            "state": "pending",
            "platform": None,
            "error": None,
        }
        assert db.get_compression_lock_holder("sess-committed") == "ghost-holder"
        assert sorted(r["id"] for r in db.list_pending_handoffs()) == [
            "sess-committed",
            "sess-inflight",
        ]
