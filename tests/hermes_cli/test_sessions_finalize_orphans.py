import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import active_sessions
from hermes_cli.sessions_cmd import cmd_sessions
import hermes_state


class FakeDB:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _args(**overrides):
    values = {
        "sessions_action": "finalize-orphans",
        "apply": False,
        "yes": False,
        "min_age_hours": 24.0,
        "limit": 100,
        "json": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_finalize_orphans_defaults_to_read_only_report(monkeypatch, capsys):
    db = FakeDB()
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **_kwargs: db)
    calls = []

    def fake_recover(session_db, **kwargs):
        calls.append((session_db, kwargs))
        return {
            "candidate_ids": ["candidate"],
            "recovered_ids": [],
            "excluded": {"live": ["active_lease"]},
        }

    monkeypatch.setattr(active_sessions, "recover_abandoned_session_rows", fake_recover)

    assert cmd_sessions(_args()) == 0

    output = capsys.readouterr().out
    assert "Dry run — no session rows were changed." in output
    assert "proven candidates: 1" in output
    assert calls == [
        (
            db,
            {"apply": False, "older_than_seconds": 86400.0, "limit": 100},
        )
    ]


def test_finalize_orphans_apply_reclassifies_after_explicit_approval(
    monkeypatch, capsys
):
    db = FakeDB()
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **_kwargs: db)
    calls = []

    def fake_recover(session_db, **kwargs):
        calls.append((session_db, kwargs))
        if kwargs["apply"]:
            return {
                "candidate_ids": ["candidate"],
                "recovered_ids": ["candidate"],
                "excluded": {},
            }
        return {
            "candidate_ids": ["candidate"],
            "recovered_ids": [],
            "excluded": {},
        }

    monkeypatch.setattr(active_sessions, "recover_abandoned_session_rows", fake_recover)

    assert cmd_sessions(_args(apply=True, yes=True)) == 0

    output = capsys.readouterr().out
    assert "Finalized 1 proven abandoned session row(s)" in output
    assert [kwargs["apply"] for _, kwargs in calls] == [False, True]


def test_finalize_orphans_apply_requires_yes_flag(monkeypatch, capsys):
    db = FakeDB()
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **_kwargs: db)
    calls = []

    def fake_recover(session_db, **kwargs):
        calls.append(kwargs)
        return {
            "candidate_ids": ["candidate"],
            "recovered_ids": [],
            "excluded": {},
        }

    monkeypatch.setattr(active_sessions, "recover_abandoned_session_rows", fake_recover)
    assert cmd_sessions(_args(apply=True, yes=False)) == 2

    assert "requires --yes" in capsys.readouterr().out
    assert [call["apply"] for call in calls] == [False]


def test_finalize_orphans_reports_classifier_failure_without_traceback(
    monkeypatch, capsys
):
    db = FakeDB()
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **_kwargs: db)
    monkeypatch.setattr(
        active_sessions,
        "recover_abandoned_session_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("invalid active session registry")
        ),
    )

    assert cmd_sessions(_args(json=True)) == 1
    assert "invalid active session registry" in capsys.readouterr().out
    assert db.closed


def test_finalize_orphans_cli_is_dry_by_default_and_apply_requires_yes(tmp_path):
    env = os.environ.copy()
    hermes_home = tmp_path / ".hermes"
    env["HERMES_HOME"] = str(hermes_home)
    repo_root = Path(__file__).resolve().parents[2]

    seed = hermes_state.SessionDB(db_path=hermes_home / "state.db")
    seed.get_or_create_lifecycle_recovery_epoch(now=100.0)
    seed.close()

    def snapshot_lifecycle_state():
        paths = [
            hermes_home / "state.db",
            hermes_home / "state.db-wal",
            hermes_home / "state.db-shm",
            hermes_home / "runtime" / "active_sessions.json",
            hermes_home / "runtime" / "active_sessions.lock",
        ]
        return {
            str(path.relative_to(hermes_home)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        }

    before = snapshot_lifecycle_state()

    dry = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sessions", "finalize-orphans", "--json"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert json.loads(dry.stdout)["mode"] == "dry_run"
    assert snapshot_lifecycle_state() == before
    db = hermes_state.SessionDB(db_path=hermes_home / "state.db")
    try:
        assert db.get_lifecycle_recovery_epoch() == 100.0
    finally:
        db.close()

    rejected = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sessions", "finalize-orphans", "--apply"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rejected.returncode == 2
    assert "requires --yes" in rejected.stdout


def test_finalize_orphans_dry_refuses_active_wal_without_mutating(tmp_path):
    env = os.environ.copy()
    hermes_home = tmp_path / ".hermes"
    env["HERMES_HOME"] = str(hermes_home)
    repo_root = Path(__file__).resolve().parents[2]
    live = hermes_state.SessionDB(db_path=hermes_home / "state.db")
    live.get_or_create_lifecycle_recovery_epoch(now=100.0)
    wal_path = hermes_home / "state.db-wal"
    assert wal_path.stat().st_size > 0

    paths = [
        hermes_home / "state.db",
        wal_path,
        hermes_home / "state.db-shm",
    ]
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "sessions",
                "finalize-orphans",
                "--json",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        }
        assert result.returncode == 1
        assert "active WAL" in result.stdout
        assert after == before
    finally:
        live.close()


def test_immutable_audit_refuses_nonempty_rollback_journal(tmp_path):
    db_path = tmp_path / "state.db"
    db = hermes_state.SessionDB(db_path=db_path)
    db.close()
    journal_path = Path(f"{db_path}-journal")
    journal_path.write_bytes(b"uncheckpointed")

    with pytest.raises(RuntimeError, match="active rollback journal"):
        hermes_state.SessionDB(db_path=db_path, read_only=True, immutable=True)


def test_immutable_audit_refuses_wal_created_during_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = hermes_state.SessionDB(db_path=db_path)
    db.close()

    def copy_then_create_wal(source, destination):
        shutil.copyfile(source, destination)
        Path(f"{source}-wal").write_bytes(b"concurrent writer")

    monkeypatch.setattr(
        hermes_state,
        "_copy_file_for_immutable_audit",
        copy_then_create_wal,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="changed during immutable audit snapshot"):
        hermes_state.SessionDB(db_path=db_path, read_only=True, immutable=True)


def test_immutable_audit_refuses_checkpoint_during_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = hermes_state.SessionDB(db_path=db_path)
    db.create_session("before-checkpoint", source="cli")
    db.close()

    def copy_then_checkpoint(source, destination):
        shutil.copyfile(source, destination)
        writer = sqlite3.connect(source, isolation_level=None)
        try:
            writer.execute(
                "UPDATE sessions SET title = 'changed' WHERE id = 'before-checkpoint'"
            )
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            writer.close()

    monkeypatch.setattr(
        hermes_state,
        "_copy_file_for_immutable_audit",
        copy_then_checkpoint,
    )

    with pytest.raises(RuntimeError, match="changed during immutable audit snapshot"):
        hermes_state.SessionDB(db_path=db_path, read_only=True, immutable=True)


def test_immutable_audit_reads_stable_copy_and_removes_it_on_close(tmp_path):
    db_path = tmp_path / "state.db"
    live = hermes_state.SessionDB(db_path=db_path)
    live.create_session("before", source="cli")
    live.close()

    audit = hermes_state.SessionDB(db_path=db_path, read_only=True, immutable=True)
    snapshot_path = audit._immutable_snapshot_path
    writer = hermes_state.SessionDB(db_path=db_path)
    try:
        writer.create_session("after", source="cli")
        rows = audit._conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["before"]
        assert snapshot_path is not None and snapshot_path.exists()
    finally:
        writer.close()
        audit.close()

    assert snapshot_path is not None and not snapshot_path.exists()
