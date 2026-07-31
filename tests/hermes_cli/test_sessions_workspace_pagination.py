"""F10: workspace-filtered `hermes sessions list` paginates correctly.

The workspace key is derived (git_repo_root / cwd), not a DB column, so
the filter runs in Python. The old code applied the SQL OFFSET before the
filter — on page > 1 the page was sliced from unfiltered rows first, so a
workspace-filtered listing silently skipped matches (or repeated page 1).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# (sid, git_repo_root, started_at) — started_at ascending; canonical list
# order is started_at DESC, so most recent first.
_SEED = [
    ("w1_a", "/work/repo1", 1_000.0),
    ("w2_a", "/work/repo2", 1_500.0),
    ("w1_b", "/work/repo1", 2_000.0),
    ("w2_b", "/work/repo2", 2_500.0),
    ("w1_c", "/work/repo1", 3_000.0),
    ("w1_d", "/work/repo1", 4_000.0),
]


def _seed(home: Path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    conn = db._conn
    for sid, repo, started in _SEED:
        db.create_session(sid, "cli")
        db.set_session_title(sid, f"Session {sid}")
        db.append_message(sid, "user", f"opener {sid}", timestamp=started)
        conn.execute(
            "UPDATE sessions SET started_at = ?, git_repo_root = ? WHERE id = ?",
            (started, repo, sid),
        )
    conn.commit()
    db.close()


def _run(home: Path, *argv: str) -> str:
    env = {**os.environ, "HERMES_HOME": str(home), "TZ": "UTC"}
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sessions", "list", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def _home(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    _seed(home)
    return home


def test_workspace_page_two_skips_to_next_window(tmp_path):
    """repo1 window is w1_d, w1_c, w1_b, w1_a (most recent first).
    Page 2 (limit 2) must show w1_b, w1_a — not repeat w1_d, w1_c.
    """
    out = _run(_home(tmp_path), "--workspace", "repo1", "-l", "2", "2")
    assert "w1_b" in out and "w1_a" in out
    assert "w1_d" not in out and "w1_c" not in out
    assert "w2_a" not in out and "w2_b" not in out


def test_workspace_page_one_still_first_window(tmp_path):
    out = _run(_home(tmp_path), "--workspace", "repo1", "-l", "2", "1")
    assert "w1_d" in out and "w1_c" in out
    assert "w1_b" not in out and "w1_a" not in out


def test_unfiltered_pagination_unaffected(tmp_path):
    """Without a workspace filter the SQL offset still applies: page 2 of
    the full list is ranks 3-4 overall (w2_b, w1_b)."""
    out = _run(_home(tmp_path), "-l", "2", "2")
    assert "w2_b" in out and "w1_b" in out
    assert "w1_d" not in out and "w1_c" not in out


def test_workspace_basename_match(tmp_path):
    """--workspace matches the repo basename even when the needle is short."""
    out = _run(_home(tmp_path), "--workspace", "repo2", "-l", "10")
    assert "w2_a" in out and "w2_b" in out
    assert "w1_a" not in out
