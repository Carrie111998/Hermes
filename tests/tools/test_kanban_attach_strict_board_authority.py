"""B1 — strict board authority for kanban attachment handlers.

Behavior contract (PR #90820 Round 3):

1. When ``HERMES_KANBAN_BOARD`` env var is set and a tool caller passes
   a matching ``board`` arg → handler proceeds.
2. When ``HERMES_KANBAN_BOARD`` env var is set and the ``board`` arg is
   OMITTED → handler uses the env-pinned authoritative board (NOT the
   current/symlink chain).
3. When ``HERMES_KANBAN_BOARD`` env var is set and the ``board`` arg
   conflicts → handler REJECTS with a tool-error and writes NOTHING to
   any board DB.
4. When ``HERMES_KANBAN_BOARD`` env var is NOT set (non-strict) →
   handler falls back to its legacy resolution chain and behavior is
   unchanged.
5. Both ``kanban_attach`` (byte attachment) and ``kanban_attach_url``
   (URL attachment) consume the authoritative board.

Tests exercise the REAL handler + REAL persistence path
(``hermes_cli.kanban_db.connect`` + ``store_attachment_bytes``) inside a
temp ``HERMES_HOME``, not a mocked unit test.

NOTE: this test relies on the autouse ``_isolate_env`` fixture from
tests/conftest.py to redirect HERMES_HOME into a per-test tempdir. It
uses ``monkeypatch.setenv`` (not raw ``os.environ``) so the redirect is
scoped to the test and reverted automatically.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

import pytest


def _setup_boards(home: Path) -> None:
    """Create two boards (``workers-board``, ``other-board``) and seed a task on each."""
    kanban_dir = home / "kanban"
    boards_dir = kanban_dir / "boards"
    boards_dir.mkdir(parents=True, exist_ok=True)

    for slug in ("workers-board", "other-board"):
        bd = boards_dir / slug
        bd.mkdir(exist_ok=True)
        (bd / "board.json").write_text(
            json.dumps({"slug": slug, "name": slug}), encoding="utf-8"
        )

    from hermes_cli import kanban_db

    for slug in ("workers-board", "other-board"):
        conn = kanban_db.connect(board=slug)
        existing = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", ("t_demo",)
        ).fetchone()
        if not existing:
            kanban_db.create_task(
                conn,
                title="demo",
                body="demo body",
                workspace_kind="scratch",
                initial_status="running",
            )
            conn.execute(
                "UPDATE tasks SET id = ? WHERE rowid = (SELECT MIN(rowid) FROM tasks)",
                ("t_demo",),
            )
            conn.commit()
        conn.close()


def _attachment_count(home: Path, slug: str) -> int:
    """Count attachments persisted on a given board's DB."""
    if slug == "default":
        db_path = home / "kanban.db"
    else:
        db_path = home / "kanban" / "boards" / slug / "kanban.db"
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        # Production schema names the table ``task_attachments``.
        row = conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?",
            ("t_demo",),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


@pytest.fixture
def strict_board_env(tmp_path, monkeypatch):
    """Provide an isolated HERMES_HOME with two kanban boards pre-seeded."""
    # Drop the memo so get_default_hermes_root() honors the new HERMES_HOME.
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _setup_boards(home)

    yield home


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------

def test_omitted_board_uses_env_pin(strict_board_env, monkeypatch):
    """When env pin is set and ``board=`` is omitted, the env pin wins."""
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workers-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    result = kanban_tools._handle_attach(
        {
            "task_id": "t_demo",
            "filename": "report.txt",
            "content_base64": base64.b64encode(b"hello world").decode("ascii"),
        }
    )
    parsed = json.loads(result)
    assert "error" not in parsed, f"expected success, got: {result}"
    assert parsed.get("task_id") == "t_demo"
    assert _attachment_count(strict_board_env, "workers-board") == 1
    assert _attachment_count(strict_board_env, "other-board") == 0


def test_matching_explicit_board_works(strict_board_env, monkeypatch):
    """When env pin matches an explicit ``board=`` arg, both align."""
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workers-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    result = kanban_tools._handle_attach(
        {
            "task_id": "t_demo",
            "filename": "matching.txt",
            "content_base64": base64.b64encode(b"matching").decode("ascii"),
            "board": "workers-board",
        }
    )
    parsed = json.loads(result)
    assert "error" not in parsed, f"matching explicit board should pass: {result}"
    assert _attachment_count(strict_board_env, "workers-board") == 1


def test_conflicting_explicit_board_rejected_no_db_write(strict_board_env, monkeypatch):
    """A conflicting ``board=`` arg must REJECT before any DB write."""
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workers-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")
    assert _attachment_count(strict_board_env, "workers-board") == 0
    assert _attachment_count(strict_board_env, "other-board") == 0

    result = kanban_tools._handle_attach(
        {
            "task_id": "t_demo",
            "filename": "should_not_persist.txt",
            "content_base64": base64.b64encode(b"never persisted").decode("ascii"),
            "board": "other-board",
        }
    )
    assert "conflict" in result.lower(), f"expected conflict rejection, got: {result}"
    assert "workers-board" in result
    assert "other-board" in result
    # NO attachment row landed on either board.
    assert _attachment_count(strict_board_env, "workers-board") == 0
    assert (
        _attachment_count(strict_board_env, "other-board") == 0
    ), "conflict path must NOT persist to either board"


def test_conflicting_board_rejected_for_url_attachment(strict_board_env, monkeypatch):
    """Same contract holds for ``kanban_attach_url`` (URL attachment)."""
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workers-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    # Replace the network fetcher with a stub so the test does not depend
    # on the real internet.
    def _fake_download(url, max_bytes):
        return b"fake url bytes", "text/plain"

    monkeypatch.setattr(kanban_tools, "_download_url_with_cap", _fake_download)

    result = kanban_tools._handle_attach_url(
        {
            "task_id": "t_demo",
            "url": "https://example.invalid/data.txt",
            "filename": "remote.txt",
            "board": "other-board",
        }
    )
    assert "conflict" in result.lower(), (
        f"URL attachment with conflicting board must be rejected, got: {result}"
    )
    assert _attachment_count(strict_board_env, "workers-board") == 0
    assert _attachment_count(strict_board_env, "other-board") == 0


def test_matching_explicit_board_works_for_url_attachment(strict_board_env, monkeypatch):
    """Matching explicit board works for URL attachment."""
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workers-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    def _fake_download(url, max_bytes):
        return b"fake url bytes", "text/plain"

    monkeypatch.setattr(kanban_tools, "_download_url_with_cap", _fake_download)

    result = kanban_tools._handle_attach_url(
        {
            "task_id": "t_demo",
            "url": "https://example.invalid/data.txt",
            "filename": "remote.txt",
            "board": "workers-board",
        }
    )
    parsed = json.loads(result)
    assert "error" not in parsed, f"matching URL attach should pass: {result}"
    assert _attachment_count(strict_board_env, "workers-board") == 1


def test_no_env_pin_uses_explicit_board(strict_board_env, monkeypatch):
    """No env pin → explicit ``board=`` wins (non-strict legacy behavior)."""
    from tools import kanban_tools

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    result = kanban_tools._handle_attach(
        {
            "task_id": "t_demo",
            "filename": "nonstrict.txt",
            "content_base64": base64.b64encode(b"non-strict").decode("ascii"),
            "board": "other-board",
        }
    )
    parsed = json.loads(result)
    assert "error" not in parsed, (
        f"non-strict (no env pin) must honor explicit board: {result}"
    )
    assert _attachment_count(strict_board_env, "other-board") == 1
    assert _attachment_count(strict_board_env, "workers-board") == 0


def test_no_env_pin_no_explicit_uses_current_board(strict_board_env, monkeypatch):
    """No env pin and no explicit ``board=`` → current board wins."""
    from tools import kanban_tools

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_demo")

    # Set the kanban/current symlink/file to workers-board.
    current = strict_board_env / "kanban" / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("workers-board\n", encoding="utf-8")

    result = kanban_tools._handle_attach(
        {
            "task_id": "t_demo",
            "filename": "via-current.txt",
            "content_base64": base64.b64encode(b"via current").decode("ascii"),
        }
    )
    parsed = json.loads(result)
    assert "error" not in parsed, (
        f"non-strict + no explicit must succeed via current: {result}"
    )
    assert _attachment_count(strict_board_env, "workers-board") == 1