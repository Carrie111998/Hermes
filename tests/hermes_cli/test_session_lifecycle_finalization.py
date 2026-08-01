import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import cli as cli_mod
from run_agent import AIAgent
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    recover_abandoned_session_rows,
    try_acquire_active_session,
)
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def reset_single_query_finalize_state(monkeypatch):
    monkeypatch.setattr(cli_mod, "_single_query_finalize_attempted_session_ids", set())
    monkeypatch.setattr(cli_mod, "_cleanup_done", False)


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def test_single_query_finalization_closes_owned_row_without_losing_transcript(
    session_db, monkeypatch
):
    session_id = "single-query"
    session_db.create_session(session_id="parent-session", source="cli")
    session_db.create_session(
        session_id=session_id,
        source="cli",
        parent_session_id="parent-session",
    )
    session_db.set_session_title(session_id, "Lifecycle probe")
    session_db.append_message(session_id, role="user", content="hello")
    session_db.append_message(session_id, role="assistant", content="done")

    released = []
    fake_cli = SimpleNamespace(
        _session_db=session_db,
        session_id=session_id,
        agent=SimpleNamespace(session_id=session_id, platform="cli"),
        conversation_history=[],
        _release_active_session=lambda: released.append(session_id),
    )
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kwargs: None)

    cli_mod._finalize_single_query(fake_cli)

    row = session_db.get_session(session_id)
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert row["title"] == "Lifecycle probe"
    assert row["parent_session_id"] == "parent-session"
    assert [m["content"] for m in session_db.get_messages(session_id)] == ["hello", "done"]
    assert released == [session_id]


def test_interactive_normal_exit_persists_then_ends_and_releases_owner(
    session_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    session_id = "interactive-normal"
    session_db.create_session(session_id=session_id, source="cli")
    session_db.set_session_title(session_id, "Interactive normal")
    lease, error = try_acquire_active_session(
        session_id=session_id, surface="cli", config={}
    )
    assert error is None and lease is not None

    def persist():
        session_db.append_message(session_id, role="user", content="interactive input")
        session_db.append_message(session_id, role="assistant", content="interactive output")

    fake_cli = SimpleNamespace(
        _session_db=session_db,
        session_id=session_id,
        agent=SimpleNamespace(session_id=session_id, platform="cli"),
        _persist_active_session_before_close=persist,
        _release_active_session=lease.release,
    )

    try:
        cli_mod._finalize_owned_cli_session_row(fake_cli)
    finally:
        fake_cli._release_active_session()

    row = session_db.get_session(session_id)
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert row["title"] == "Interactive normal"
    assert [message["content"] for message in session_db.get_messages(session_id)] == [
        "interactive input",
        "interactive output",
    ]
    assert active_session_registry_snapshot() == []


def test_one_shot_provider_failure_still_finalizes_durable_user_turn(
    session_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    session_id = "provider-failure"
    session_db.create_session(session_id=session_id, source="cli")
    lease, error = try_acquire_active_session(
        session_id=session_id, surface="cli", config={}
    )
    assert error is None and lease is not None

    fake_cli = SimpleNamespace(
        _session_db=session_db,
        session_id=session_id,
        agent=SimpleNamespace(session_id=session_id, platform="cli"),
        _persist_active_session_before_close=lambda: session_db.append_message(
            session_id, role="user", content="failed request"
        ),
        _release_active_session=lease.release,
    )
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="provider failed"):
        try:
            raise RuntimeError("provider failed")
        finally:
            cli_mod._finalize_single_query(fake_cli)

    row = session_db.get_session(session_id)
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert [message["content"] for message in session_db.get_messages(session_id)] == [
        "failed request"
    ]
    assert active_session_registry_snapshot() == []


def test_keyboard_interrupt_cleanup_finalizes_and_releases_owner(
    session_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    session_id = "keyboard-interrupt"
    session_db.create_session(session_id=session_id, source="cli")
    lease, error = try_acquire_active_session(
        session_id=session_id, surface="cli", config={}
    )
    assert error is None and lease is not None
    fake_cli = SimpleNamespace(
        _session_db=session_db,
        session_id=session_id,
        agent=SimpleNamespace(session_id=session_id, platform="cli"),
        _persist_active_session_before_close=lambda: session_db.append_message(
            session_id, role="user", content="interrupted input"
        ),
        _release_active_session=lease.release,
    )
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kwargs: None)

    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            cli_mod._finalize_single_query(fake_cli)

    row = session_db.get_session(session_id)
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert active_session_registry_snapshot() == []


@pytest.mark.parametrize(
    "failure_stage",
    ["snapshot", "system_prompt", "session_creation"],
)
def test_real_cli_pre_persist_failure_keeps_row_open_and_releases_lease(
    session_db, tmp_path, monkeypatch, failure_stage
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    session_id = f"pre-persist-{failure_stage}"
    session_db.create_session(session_id=session_id, source="cli")
    lease, error = try_acquire_active_session(
        session_id=session_id, surface="cli", config={}
    )
    assert error is None and lease is not None
    discard_calls = []

    if failure_stage == "snapshot":
        class SnapshotFailureAgent:
            platform = "cli"
            _pending_cli_user_message = None
            _cached_system_prompt = "system"
            _session_persist_lock = None

            def __init__(self, value):
                self.session_id = value

            @property
            def _session_messages(self):
                raise OSError("snapshot unavailable")

            def _ensure_db_session(self):
                raise AssertionError("snapshot failure must stop before session creation")

            def _persist_session(self, _messages, _history):
                raise AssertionError("snapshot failure must stop before persistence")

        agent = SnapshotFailureAgent(session_id)
    else:
        def ensure_session():
            if failure_stage == "session_creation":
                raise OSError("session creation failed")

        agent = SimpleNamespace(
            session_id=session_id,
            platform="cli",
            _session_messages=[{"role": "user", "content": "not durable"}],
            _pending_cli_user_message=None,
            _cached_system_prompt=None if failure_stage == "system_prompt" else "system",
            _session_persist_lock=None,
            _ensure_db_session=ensure_session,
            _persist_session=lambda _messages, _history: True,
        )
        if failure_stage == "system_prompt":
            monkeypatch.setattr(
                "agent.conversation_loop._restore_or_build_system_prompt",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("system prompt unavailable")
                ),
            )

    real_cli = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    real_cli.agent = agent
    real_cli.session_id = session_id
    real_cli._session_db = session_db
    real_cli.conversation_history = []

    def discard_empty(session_id):
        discard_calls.append(session_id)
        return False

    real_cli._discard_session_if_empty = discard_empty
    real_cli._release_active_session = lease.release
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kwargs: None)

    cli_mod._finalize_single_query(real_cli)

    row = session_db.get_session(session_id)
    assert row is not None
    assert row["ended_at"] is None
    assert row["end_reason"] is None
    assert discard_calls == []
    assert active_session_registry_snapshot() == []


def test_real_cli_empty_session_deletion_is_post_persistence_and_fail_closed(
    session_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for session_id in ("empty", "titled", "parent", "history"):
        session_db.create_session(session_id=session_id, source="cli")
    session_db.set_session_title("titled", "Keep titled")
    session_db.create_session(
        session_id="child", source="cli", parent_session_id="parent"
    )

    def finalize(session_id, history):
        agent = SimpleNamespace(
            session_id=session_id,
            platform="cli",
            _session_messages=[],
            _pending_cli_user_message=None,
            _cached_system_prompt="system",
            _session_persist_lock=None,
            _ensure_db_session=lambda: None,
            _persist_session=lambda _messages, _history: True,
        )
        real_cli = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
        real_cli.agent = agent
        real_cli.session_id = session_id
        real_cli._session_db = session_db
        real_cli.conversation_history = history
        cli_mod._finalize_owned_cli_session_row(real_cli)

    finalize("empty", [])
    finalize("titled", [])
    finalize("parent", [])
    finalize("history", [{"role": "user", "content": "not yet durable"}])

    assert session_db.get_session("empty") is None
    for session_id in ("titled", "parent", "history"):
        row = session_db.get_session(session_id)
        assert row is not None
        assert row["ended_at"] is not None
        assert row["end_reason"] == "cli_close"
    assert session_db.get_session("child") is not None


def test_owned_row_stays_open_when_final_transcript_persist_fails(
    session_db, monkeypatch, capsys
):
    session_id = "persist-failed"
    session_db.create_session(session_id=session_id, source="cli")
    discarded = []
    released = []

    def fail_persist():
        raise OSError("disk full")

    fake_cli = SimpleNamespace(
        _session_db=session_db,
        session_id=session_id,
        agent=SimpleNamespace(session_id=session_id, platform="cli"),
        conversation_history=[],
        _persist_active_session_before_close=fail_persist,
        _discard_session_if_empty=lambda value: discarded.append(value),
        _release_active_session=lambda: released.append(session_id),
    )
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kwargs: None)

    cli_mod._finalize_single_query(fake_cli)

    row = session_db.get_session(session_id)
    assert row is not None
    assert row["ended_at"] is None
    assert row["end_reason"] is None
    assert discarded == []
    assert released == [session_id]
    assert "left open" in capsys.readouterr().err


def test_real_cli_persistence_failure_leaves_owned_row_open(session_db, capsys):
    session_id = "real-persist-failure"
    session_db.create_session(session_id=session_id, source="cli")

    def fail_persist(_messages, _conversation_history):
        raise OSError("disk full")

    agent = SimpleNamespace(
        session_id=session_id,
        platform="cli",
        _session_messages=[{"role": "user", "content": "not durable"}],
        _pending_cli_user_message=None,
        _cached_system_prompt="system",
        _session_persist_lock=None,
        _ensure_db_session=lambda: None,
        _persist_session=fail_persist,
    )
    real_cli = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    real_cli.agent = agent
    real_cli.session_id = session_id
    real_cli._session_db = session_db
    real_cli.conversation_history = []

    cli_mod._finalize_owned_cli_session_row(real_cli)

    row = session_db.get_session(session_id)
    assert row["ended_at"] is None
    assert session_db.get_messages(session_id) == []
    assert "session row was left open" in capsys.readouterr().err


def test_real_cli_reported_db_failure_leaves_owned_row_open(session_db, capsys):
    session_id = "reported-db-failure"
    session_db.create_session(session_id=session_id, source="cli")
    agent = SimpleNamespace(
        session_id=session_id,
        platform="cli",
        _session_messages=[{"role": "user", "content": "not durable"}],
        _pending_cli_user_message=None,
        _cached_system_prompt="system",
        _session_persist_lock=None,
        _ensure_db_session=lambda: None,
        _persist_session=lambda _messages, _history: False,
    )
    real_cli = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    real_cli.agent = agent
    real_cli.session_id = session_id
    real_cli._session_db = session_db
    real_cli.conversation_history = []

    cli_mod._finalize_owned_cli_session_row(real_cli)

    assert session_db.get_session(session_id)["ended_at"] is None
    assert "session row was left open" in capsys.readouterr().err


def test_agent_persistence_reports_sqlite_flush_failure():
    fake_agent = AIAgent.__new__(AIAgent)
    fake_agent._session_persist_lock = None
    fake_agent._session_messages = None
    fake_agent._session_db = None
    fake_agent._drop_trailing_empty_response_scaffolding = lambda _messages: None
    fake_agent._save_session_log = lambda _messages: None
    fake_agent._flush_messages_to_session_db = lambda _messages, _history: False

    assert fake_agent._persist_session(
        [{"role": "user", "content": "not durable"}], []
    ) is False


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL lifecycle probe is POSIX-only")
def test_live_process_is_protected_then_killed_owner_is_recovered(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db_path = hermes_home / "state.db"
    ready_path = tmp_path / "ready"

    db = SessionDB(db_path=db_path)
    try:
        epoch = db.get_or_create_lifecycle_recovery_epoch(now=time.time() - 1.0)
    finally:
        db.close()

    child_code = """
import os
from pathlib import Path
import time
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB

db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "state.db")
db.create_session(session_id="killed-owner", source="cli")
db.append_message("killed-owner", role="user", content="preserve me")
lease, error = try_acquire_active_session(
    session_id="killed-owner", surface="cli", config={}
)
if error or lease is None:
    raise RuntimeError(error or "lease missing")
Path(os.environ["READY_PATH"]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["READY_PATH"] = str(ready_path)
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10.0
        while not ready_path.exists() and time.time() < deadline:
            if child.poll() is not None:
                stderr = child.stderr.read() if child.stderr else ""
                raise AssertionError(f"probe exited before ready: {stderr}")
            time.sleep(0.05)
        assert ready_path.exists(), "probe did not become ready"

        db = SessionDB(db_path=db_path)
        try:
            live = recover_abandoned_session_rows(
                db,
                apply=True,
                older_than_seconds=0,
                now=time.time() + 1.0,
            )
            assert live["excluded"] == {"killed-owner": ["active_lease"]}
            live_row = db.get_session("killed-owner")
            assert live_row is not None
            assert live_row["ended_at"] is None
        finally:
            db.close()

        os.kill(child.pid, 9)
        child.wait(timeout=10)

        db = SessionDB(db_path=db_path)
        try:
            recovered = recover_abandoned_session_rows(
                db,
                apply=True,
                older_than_seconds=0,
                now=time.time() + 2.0,
            )
            row = db.get_session("killed-owner")
            assert row is not None
            assert recovered["recovered_ids"] == ["killed-owner"]
            assert row["ended_at"] is not None
            assert row["end_reason"] == "orphan_recovered"
            assert row["started_at"] >= epoch
            assert [m["content"] for m in db.get_messages("killed-owner")] == [
                "preserve me"
            ]
        finally:
            db.close()
    finally:
        if child.poll() is None:
            os.kill(child.pid, 9)
            child.wait(timeout=10)
