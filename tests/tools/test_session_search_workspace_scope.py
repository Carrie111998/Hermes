#!/usr/bin/env python3
"""Regression test: session_search respects workspace scoping.

When a session is opened inside a workspace ("New Session In <workspace>"),
memory recall must stay local to that workspace instead of mixing every
workspace's history. The recall tool accepts a ``workspace_target`` (the
session's cwd / git repo root) and filters discovery + browse to it.
"""
import secrets
import tempfile
import time
from pathlib import Path

import json

from hermes_state import SessionDB
from tools.session_search_tool import session_search

WS1 = "/tmp/ws_proj_a"
WS2 = "/tmp/ws_proj_b"


def _new_sid():
    return f"{int(time.time() * 1000)}_{secrets.token_hex(6)}"


def _seed(db):
    s1 = db.create_session(_new_sid(), source="cli", cwd=WS1, git_repo_root=WS1)
    db.append_message(s1, "user", "Deploy the auth refactor to production")
    db.append_message(s1, "assistant", "Done, auth refactor shipped.")
    s1b = db.create_session(_new_sid(), source="cli", cwd=WS1, git_repo_root=WS1)
    db.append_message(s1b, "user", "Refactor the login module")
    db.append_message(s1b, "assistant", "Login module refactored.")
    s2 = db.create_session(_new_sid(), source="cli", cwd=WS2, git_repo_root=WS2)
    db.append_message(s2, "user", "Deploy the auth refactor for the billing service")
    db.append_message(s2, "assistant", "Billing auth refactor deployed.")
    # Unbound session (no cwd) — only appears in global scope.
    s0 = db.create_session(_new_sid(), source="cli")
    db.append_message(s0, "user", "Deploy the auth refactor globally")
    db.append_message(s0, "assistant", "Global refactor done.")
    return s1, s1b, s2, s0


def _ids(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {r["session_id"] for r in payload.get("results", [])}


def test_workspace_scoping_isolates_discovery_and_browse():
    tmp = tempfile.mkdtemp(prefix="hermes_ws_test_")
    db = SessionDB(db_path=Path(tmp) / "state.db")
    s1, s1b, s2, s0 = _seed(db)

    g = session_search(query="auth refactor", db=db)
    w1 = session_search(query="auth refactor", db=db, workspace_target=WS1)
    w2 = session_search(query="auth refactor", db=db, workspace_target=WS2)
    b1 = session_search(db=db, workspace_target=WS1, limit=10)
    bg = session_search(db=db, limit=10)

    g_ids, w1_ids, w2_ids = _ids(g), _ids(w1), _ids(w2)
    b1_ids, bg_ids = _ids(b1), _ids(bg)

    # Global finds everything.
    assert len(g_ids) >= 3
    # WS1 scope must NOT include the WS2-only session, and vice versa.
    assert s2 not in w1_ids, f"WS1 leaked WS2: {w1_ids}"
    assert s1 not in w2_ids and s1b not in w2_ids, f"WS2 leaked WS1: {w2_ids}"
    assert s2 in w2_ids, f"WS2 scope missing its own session: {w2_ids}"
    # Browse is scoped too.
    assert s2 not in b1_ids, f"WS1 browse leaked WS2: {b1_ids}"
    assert b1_ids and b1_ids.issubset({s1, s1b, s0}) or b1_ids.issubset({s1, s1b})
    db.close()


def test_blank_workspace_target_is_global():
    """A blank workspace_target must preserve today's global behavior."""
    tmp = tempfile.mkdtemp(prefix="hermes_ws_test_")
    db = SessionDB(db_path=Path(tmp) / "state.db")
    _seed(db)
    global_q = session_search(query="auth refactor", db=db)
    blank_q = session_search(query="auth refactor", db=db, workspace_target="")
    assert _ids(global_q) == _ids(blank_q)
    db.close()
