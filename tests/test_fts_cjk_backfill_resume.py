"""Regression tests for #98743: interrupted CJK FTS backfill.

Covers the three layers from the issue:
  L1 — a writable open on a tokenizer-capable host auto-resumes a pending
        cjk backfill in the background (throttled), instead of freezing
        the markers forever with the index unservable.
  L2 — the pending state is VISIBLE: collect_state_db_stats exposes cjk
        backfill markers, doctor renders them, and the doctor advisory
        points at the resume path.
  L3 — an INCOMPLETE (mid-backfill, non-stale) index keeps its progress on
        a stale-reset check; only a genuinely stale index (breadcrumb set)
        is reset from scratch — and THAT reset logs how much progress is
        discarded.

Builds the loadable tokenizer from native/fts5_cjk/fts5_cjk.c on the fly;
skips when no C toolchain / extension loading is available (same pattern as
tests/test_fts_cjk_bigram.py).
"""

import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_state import FTS_CJK_STALE_KEY, SessionDB, collect_state_db_stats

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "native" / "fts5_cjk" / "fts5_cjk.c"
VENDOR = REPO / "native" / "fts5_cjk" / "vendor"

# Auto-resume must finish a small backfill well inside this budget; generous
# enough for a contended CI host, tight enough to fail if nothing advances.
RESUME_TIMEOUT_S = 30.0


@pytest.fixture(scope="session")
def cjk_so(tmp_path_factory):
    if shutil.which("gcc") is None or not SRC.exists():
        pytest.skip("no C toolchain / tokenizer source")
    out = tmp_path_factory.mktemp("fts5cjk98743") / "libfts5_cjk.so"
    try:
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", f"-I{VENDOR}", str(SRC),
             "-o", str(out)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(f"tokenizer build failed: {e.stderr[:200]}")
    probe = sqlite3.connect(":memory:")
    try:
        probe.enable_load_extension(True)
        probe.load_extension(str(out))
    except Exception as e:
        pytest.skip(f"extension loading unavailable: {e}")
    finally:
        probe.close()
    return out


def _populate(db, n=60, session_id="s1"):
    db.create_session(session_id=session_id, source="cli", model="m")
    for i in range(n):
        db.append_message(
            session_id, role="user", content=f"기존 메시지 {i} 일본 프로젝트"
        )


def _pending_db(cjk_so, tmp_path, monkeypatch, *, chunk_override=None):
    """A v23 DB with a PENDING (interrupted) cjk backfill: markers set,
    index absent. Mirrors the state an interrupted optimize leaves behind."""
    monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
    db_path = tmp_path / "state.db"

    # Phase 1: create the DB WITHOUT the tokenizer → v23 layout, no cjk table.
    monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(tmp_path / "absent.so"))
    d1 = SessionDB(db_path=db_path)
    _populate(d1)
    d1.close()

    # Phase 2: open WITH the tokenizer. _ensure_fts_cjk_schema creates the
    # table + markers (backfill pending, index not served)…
    monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(cjk_so))
    d2 = SessionDB(db_path=db_path)
    assert d2._fts_cjk_loaded
    assert d2.fts_cjk_rebuild_status() is not None
    # …and simulate the interruption by NOT running optimize, just closing.
    d2.close()
    return db_path


def _wait_for_resume(db, timeout_s=RESUME_TIMEOUT_S):
    """Block until the background auto-resume finishes the backfill."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if db.fts_cjk_rebuild_status() is None:
            return True
        time.sleep(0.05)
    return False


# ── L1: auto-resume on writable open ───────────────────────────────────


def test_writable_open_auto_resumes_pending_cjk_backfill(
    cjk_so, tmp_path, monkeypatch
):
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    d3 = SessionDB(db_path=db_path)
    try:
        assert d3._fts_cjk_loaded
        st = d3.fts_cjk_rebuild_status()
        assert st is not None and st["pending"], "backfill must start pending"

        # The writable open must resume the backfill without any manual
        # command — the frozen-forever state from the issue.
        assert _wait_for_resume(d3), (
            "background auto-resume did not finish the cjk backfill within "
            f"{RESUME_TIMEOUT_S}s"
        )
        assert d3._fts_cjk_available, "index must become servable after resume"
        # The whole corpus is searchable through the cjk index afterwards.
        assert d3._describe_search_path("일본") == "fts_cjk"
        rows = d3.search_messages("일본", limit=100)
        assert len(rows) == 60
    finally:
        d3.close()


def test_auto_resume_survives_reopen_without_duplicates(cjk_so, tmp_path, monkeypatch):
    """An auto-resumed-and-completed index must not re-backfill or double-
    index on a later open (markers gone, triggers cover new rows)."""
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    d3 = SessionDB(db_path=db_path)
    try:
        assert _wait_for_resume(d3)
    finally:
        d3.close()

    d4 = SessionDB(db_path=db_path)
    try:
        assert d4.fts_cjk_rebuild_status() is None
        assert d4._fts_cjk_available
        with d4._lock:
            idx = d4._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_cjk"
            ).fetchone()[0]
            non_tool = d4._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role <> 'tool'"
            ).fetchone()[0]
        assert idx == non_tool, "resume must index each row exactly once"
    finally:
        d4.close()


def test_read_only_open_does_not_spawn_resume(cjk_so, tmp_path, monkeypatch):
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    d3 = SessionDB(db_path=db_path, read_only=True)
    try:
        st = d3.fts_cjk_rebuild_status()
        assert st is not None, "read-only open must leave markers untouched"
        # Give any (forbidden) background worker a chance to misbehave.
        time.sleep(1.0)
        st2 = d3.fts_cjk_rebuild_status()
        assert st2 == st, "read-only open must not advance the backfill"
    finally:
        d3.close()


def test_stale_breadcrumb_blocks_auto_resume(cjk_so, tmp_path, monkeypatch):
    """A stale index (triggers dropped by a tokenizer-less process) must NOT
    be auto-resumed: the gap extent is unknown, so incremental INSERTs would
    corrupt the external-content index. It stays for optimize-storage."""
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)
    # Mark it stale the way a tokenizer-less process would.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'",
        (FTS_CJK_STALE_KEY,),
    )
    conn.commit()
    conn.close()

    d3 = SessionDB(db_path=db_path)
    try:
        time.sleep(1.5)
        st = d3.fts_cjk_rebuild_status()
        assert st is not None, "stale index must keep its markers (no resume)"
        assert not d3._fts_cjk_available
    finally:
        d3.close()


# ── L3: stale-reset keeps mid-backfill progress ────────────────────────


def test_incomplete_index_survives_stale_check_with_progress(cjk_so, tmp_path, monkeypatch):
    """_fts_cjk_reset_if_stale on a mid-backfill (non-stale) index must keep
    the completed progress — the '51% discarded' complaint from the issue."""
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    d3 = SessionDB(db_path=db_path)
    # Advance the backfill partway by hand (chunk-by-chunk, exactly like
    # the optimize loop does), then drop the connection mid-flight. The
    # chunk is shrunk so 60 rows span several steps (the class default of
    # 500 would finish in one).
    monkeypatch.setattr(d3, "_FTS_REBUILD_CHUNK_ROWS", 10)
    for _ in range(3):
        if not d3.fts_cjk_rebuild_step():
            break
    partial = d3.fts_cjk_rebuild_status()
    assert partial is not None and partial["indexed"] > 0
    d3.close()

    d4 = SessionDB(db_path=db_path)
    try:
        st = d4.fts_cjk_rebuild_status()
        assert st is not None, "progress markers must survive reopen"
        assert st["indexed"] >= partial["indexed"], (
            "reopen must not discard completed backfill progress"
        )
    finally:
        d4.close()


def test_stale_reset_discard_is_logged(cjk_so, tmp_path, monkeypatch, caplog):
    """When a genuinely stale reset DOES discard completed backfill work, the
    issue asks for at least a warning naming the discarded percentage."""
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    d3 = SessionDB(db_path=db_path)
    # Shrink the chunk so 60 rows span several steps (the class default of
    # 500 would finish in one).
    monkeypatch.setattr(d3, "_FTS_REBUILD_CHUNK_ROWS", 10)
    for _ in range(3):
        if not d3.fts_cjk_rebuild_step():
            break
    partial = d3.fts_cjk_rebuild_status()
    assert partial is not None and partial["indexed"] > 0
    d3.close()

    # Now make it genuinely stale.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'",
        (FTS_CJK_STALE_KEY,),
    )
    conn.commit()
    conn.close()

    d4 = SessionDB(db_path=db_path)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            d4._fts_cjk_reset_if_stale()
        assert any(
            "discarding" in r.message.lower() or "discard" in r.message.lower()
            for r in caplog.records
        ), "stale reset that throws away progress must warn"
    finally:
        d4.close()


# ── L2: visibility ─────────────────────────────────────────────────────


def test_collect_stats_exposes_cjk_backfill(cjk_so, tmp_path, monkeypatch):
    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)

    stats = collect_state_db_stats(db_path)
    assert stats["fts_cjk_rebuild_pending"] is True
    assert isinstance(stats["fts_cjk_rebuild_progress"], int)
    assert isinstance(stats["fts_cjk_rebuild_high_water"], int)
    assert (
        stats["fts_cjk_rebuild_progress"] < stats["fts_cjk_rebuild_high_water"]
    )

    # Fresh DB: nothing pending.
    fresh = tmp_path / "fresh.db"
    d = SessionDB(db_path=fresh)
    d.close()
    stats2 = collect_state_db_stats(fresh)
    assert stats2["fts_cjk_rebuild_pending"] in (False, None)


def test_doctor_renders_cjk_backfill_pending(cjk_so, tmp_path, monkeypatch):
    from hermes_cli.doctor import _render_state_db_stats

    db_path = _pending_db(cjk_so, tmp_path, monkeypatch)
    stats = collect_state_db_stats(db_path)

    lines = _render_state_db_stats(stats)
    text = " ".join(t for _, t, _ in lines)
    assert "cjk" in text.lower(), "doctor must surface a pending cjk backfill"
    assert any(kind == "warn" for kind, _, _ in lines), (
        "pending cjk backfill must render as a warning, not info-only"
    )
