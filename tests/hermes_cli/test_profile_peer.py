import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from hermes_cli.profile_peer import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_TIMEOUT,
    ProfilePeerDispatcher,
    safe_context_slug,
)


def _state_db(home):
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, title TEXT)"
    )
    con.commit()
    con.close()
    return db


def _fake_hermes(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls.jsonl"
    hermes = fakebin / "hermes"
    hermes.write_text(
        """#!/usr/bin/env python3
import json, os, sqlite3, sys, time
with open(os.environ['FAKE_HERMES_CALLS'], 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'peer': os.environ.get('HERMES_A2A_PEER')}) + '\\n')
if '--resume' not in sys.argv:
    con = sqlite3.connect(os.path.join(os.environ['HERMES_HOME'], 'state.db'))
    con.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', ('sess-1', sys.argv[sys.argv.index('--source') + 1], time.time(), None))
    con.commit(); con.close()
print('fake reply')
"""
    )
    hermes.chmod(0o755)
    return fakebin, calls


def test_safe_context_slug():
    assert safe_context_slug("ctx/unsafe value") == "ctx-unsafe-value"
    assert safe_context_slug("!!!") == "ctx"
    assert len(safe_context_slug("x" * 200)) == 96


def test_first_contact_creates_titles_then_resumes(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    db = _state_db(home)
    fakebin, calls = _fake_hermes(tmp_path)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_HERMES_CALLS", str(calls))
    monkeypatch.setattr("hermes_cli.profile_peer._profile_home", lambda profile: str(home))

    dispatcher = ProfilePeerDispatcher()
    first = dispatcher.call(
        profile="dev", message="hello", context_id="ctx/unsafe value",
        title_prefix="a2a-dev", env_extra={"HERMES_A2A_PEER": "peer"}, timeout=5,
    )
    second = dispatcher.call(
        profile="dev", message="again", context_id="ctx/unsafe value",
        title_prefix="a2a-dev", env_extra={"HERMES_A2A_PEER": "peer"}, timeout=5,
    )

    assert first.state == second.state == STATE_COMPLETED
    assert first.text == second.text == "fake reply"
    assert first.session_id == second.session_id == "sess-1"
    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    assert "--resume" not in rows[0]["argv"]
    assert rows[1]["argv"][rows[1]["argv"].index("--resume") + 1] == "sess-1"
    assert rows[0]["peer"] == "peer"
    digest = hashlib.sha256(b"ctx/unsafe value").hexdigest()[:12]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT title FROM sessions").fetchone()[0] == (
            f"a2a-dev-ctx-unsafe-value-{digest}"
        )


def test_colliding_display_slugs_use_separate_sessions(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    db = _state_db(home)
    monkeypatch.setattr("hermes_cli.profile_peer._profile_home", lambda profile: str(home))
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if "--resume" not in argv:
            session_id = f"sess-{len(calls)}"
            with sqlite3.connect(db) as con:
                con.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                    (session_id, "a2a", time.time(), None),
                )
                con.commit()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("hermes_cli.profile_peer.subprocess.run", run)
    dispatcher = ProfilePeerDispatcher()
    first = dispatcher.call(profile="dev", message="one", context_id="foo/a")
    second = dispatcher.call(profile="dev", message="two", context_id="foo a")
    again = dispatcher.call(profile="dev", message="three", context_id="foo/a")

    assert first.session_id == "sess-1"
    assert second.session_id == "sess-2"
    assert again.session_id == "sess-1"
    assert "--resume" not in calls[0]
    assert "--resume" not in calls[1]
    assert calls[2][calls[2].index("--resume") + 1] == "sess-1"
    with sqlite3.connect(db) as con:
        titles = [row[0] for row in con.execute("SELECT title FROM sessions ORDER BY id")]
    assert len(set(titles)) == 2
    assert all(title.startswith("a2a-peer-foo-a-") for title in titles)


def test_stale_cached_session_is_evicted_and_recreated(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    db = _state_db(home)
    monkeypatch.setattr("hermes_cli.profile_peer._profile_home", lambda profile: str(home))
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if "--resume" not in argv:
            session_id = f"sess-{len(calls)}"
            with sqlite3.connect(db) as con:
                con.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                    (session_id, "a2a", time.time(), None),
                )
                con.commit()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("hermes_cli.profile_peer.subprocess.run", run)
    dispatcher = ProfilePeerDispatcher()
    first = dispatcher.call(profile="dev", message="one", context_id="ctx")
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM sessions WHERE id = ?", (first.session_id,))
        con.commit()
    second = dispatcher.call(profile="dev", message="two", context_id="ctx")

    assert first.session_id == "sess-1"
    assert second.session_id == "sess-2"
    assert "--resume" not in calls[1]


def test_structured_timeout_and_failure(monkeypatch):
    dispatcher = ProfilePeerDispatcher()

    def timeout(*args, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("hermes_cli.profile_peer.subprocess.run", timeout)
    result = dispatcher.call(profile="dev", message="x", context_id="c", timeout=1)
    assert result.state == STATE_TIMEOUT
    assert result.error == "profile did not reply in time"

    monkeypatch.setattr(
        "hermes_cli.profile_peer.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=7, stdout="", stderr="bad"),
    )
    result = dispatcher.call(profile="dev", message="x", context_id="other", timeout=1)
    assert result.state == STATE_FAILED
    assert result.error == "bad"


def test_same_context_calls_are_serialized(monkeypatch):
    dispatcher = ProfilePeerDispatcher()
    active = 0
    maximum = 0
    guard = threading.Lock()

    def run(*args, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("hermes_cli.profile_peer.subprocess.run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(dispatcher.call, profile="dev", message="x", context_id="ctx")
            for _ in range(2)
        ]
        assert [f.result().state for f in futures] == [STATE_COMPLETED, STATE_COMPLETED]
    assert maximum == 1
