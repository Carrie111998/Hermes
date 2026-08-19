"""C2: optional CJK session-metadata FTS capability (#128 / fork #26).

Same external-content raw (title, id, display_name) document keyed by named
``sessions.row_id`` as the Unicode lane, but tokenized with the loadable
``cjk_unicode61`` bigram tokenizer and gated by its OWN marker pair
(``fts_session_cjk_*``) and stale key. Capable-host tests build the loadable
extension from ``native/fts5_cjk/fts5_cjk.c`` (skipped without a C
toolchain); degraded tests that need no tokenizer always run.
"""

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_state import SCHEMA_SQL, SessionDB

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "native" / "fts5_cjk" / "fts5_cjk.c"
VENDOR = REPO / "native" / "fts5_cjk" / "vendor"

CJK_HW = "fts_session_cjk_rebuild_high_water"
CJK_PROG = "fts_session_cjk_rebuild_progress"
UNI_HW = "fts_session_rebuild_high_water"


def _loadable_extension(path: Path) -> bool:
    probe = sqlite3.connect(":memory:")
    try:
        probe.enable_load_extension(True)
        probe.load_extension(str(path))
        return True
    except Exception:
        return False
    finally:
        probe.close()


def _session_markers(db, prefix="fts_session_cjk_"):
    return {
        r["key"]: r["value"]
        for r in db._conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?",
            (prefix + "%",),
        ).fetchall()
    }


def _build_populated_sessions_db(db_path, n=12):
    """Modern sessions (named row_id) with ``n`` rows and no FTS surfaces,
    so opening stages a full H/P claim over an empty index."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA_SQL)
    t0 = time.time()
    conn.executemany(
        "INSERT INTO sessions (row_id, id, source, started_at, title, display_name) "
        "VALUES (?, ?, 'cli', ?, ?, ?)",
        [
            (i, f"s{i}", t0 + i, f"標題 {i}", f"頻道 #{i}")
            for i in range(1, n + 1)
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    """Fresh SessionDB with no CJK tokenizer (plain FTS5) — used by the
    tokenizer-independent fallback tests."""
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture(scope="session")
def cjk_so(tmp_path_factory):
    """Provide a loadable cjk_unicode61 tokenizer extension.

    Honors a prebuilt artifact via ``HERMES_FTS5_CJK_SO``; otherwise builds
    from ``native/fts5_cjk/fts5_cjk.c`` with gcc. Skips when neither is
    available.
    """
    prebuilt = os.environ.get("HERMES_FTS5_CJK_SO")
    if prebuilt:
        p = Path(prebuilt)
        if p.is_file() and _loadable_extension(p):
            return p
        pytest.skip(f"HERMES_FTS5_CJK_SO set but not loadable: {p}")
    if shutil.which("gcc") is None or not SRC.exists():
        pytest.skip("no C toolchain / tokenizer source")
    ext = "dll" if os.name == "nt" else "so"
    out = tmp_path_factory.mktemp("fts5cjk") / f"libfts5_cjk.{ext}"
    try:
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", f"-I{VENDOR}", str(SRC), "-o", str(out)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(f"tokenizer build failed: {e.stderr[:200]}")
    if not _loadable_extension(out):
        pytest.skip("built tokenizer not loadable in this build")
    return out


@pytest.fixture()
def cjk_db(cjk_so, tmp_path, monkeypatch):
    """Fresh SessionDB on a tokenizer-capable host (empty DB)."""
    monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


class TestCjkExternalContentShape:
    def test_ddl_is_external_content_raw_metadata_cjk(self, cjk_db):
        sql = cjk_db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sessions_fts_cjk'"
        ).fetchone()[0]
        assert "content='sessions'" in sql
        assert "content_rowid='row_id'" in sql
        assert "tokenize='cjk_unicode61'" in sql

    def test_cjk_search_covers_title_id_display_name(self, cjk_db):
        def seed(conn):
            for sid, title, dn in [
                ("s1", "財務季度預算", None),
                ("s2", "Budget Review", "財務頻道"),
            ]:
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, title, display_name) "
                    "VALUES (?,?,?,?,?)",
                    (sid, "cli", 1.0, title, dn),
                )

        cjk_db._execute_write(seed)

        def hits(q):
            return [
                r[0]
                for r in cjk_db._conn.execute(
                    "SELECT rowid FROM sessions_fts_cjk "
                    "WHERE sessions_fts_cjk MATCH ?",
                    (q,),
                ).fetchall()
            ]

        # cjk_unicode61 emits overlapping bigrams, so a 2-char CJK query hits.
        # "財務" appears in s1's title AND s2's display_name; "預算" only in s1.
        assert hits("財務") == [1, 2]
        assert hits("預算") == [1]  # title-only hit

    def test_empty_db_complete_no_markers(self, cjk_db):
        assert _session_markers(cjk_db) == {}
        assert cjk_db._sessions_cjk_available is True


class TestCjkRebuildMarkers:
    def test_populated_db_stages_cjk_session_markers(self, cjk_so, tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
        db = SessionDB(db_path=db_path)
        try:
            m = _session_markers(db)
            assert m.get(CJK_HW) == "12"
            assert m.get(CJK_PROG) == "0"
            # Search-serving stays off while a backfill is pending.
            assert db._sessions_cjk_available is False
        finally:
            db.close()

    def test_cjk_markers_independent_of_unicode_lane(self, cjk_so, tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
        db = SessionDB(db_path=db_path)
        try:
            m = _session_markers(db)
            assert UNI_HW in _session_markers(db, prefix="fts_session_rebuild_")
            assert UNI_HW not in m  # unicode H is not the CJK H
        finally:
            db.close()

    def test_rebuild_backfills_then_serves(self, cjk_so, tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
        db = SessionDB(db_path=db_path)
        try:
            while db.fts_session_cjk_rebuild_step():
                pass
            assert _session_markers(db) == {}
            assert db._sessions_cjk_available is True
            n = db._conn.execute(
                "SELECT COUNT(*) FROM sessions_fts_cjk"
            ).fetchone()[0]
            assert n == 12
        finally:
            db.close()


class TestCjkDegradation:
    def test_incapable_host_no_cjk_index_stays_healthy(self, db):
        # No tokenizer on this host: no sessions_fts_cjk table, availability
        # off, unicode lane untouched, writes still work.
        assert db._sessions_cjk_available is False
        assert _session_markers(db) == {}
        tbl = db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sessions_fts_cjk'"
        ).fetchone()
        assert tbl is None
        assert db._sessions_fts_available is True

    def test_incapable_host_writes_still_work(self, db):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO sessions (id, source, started_at, title) "
                "VALUES ('s1','cli',1.0,'你好世界')"
            )
        )
        n = db._conn.execute("SELECT COUNT(*) FROM sessions WHERE id='s1'").fetchone()[0]
        assert n == 1
