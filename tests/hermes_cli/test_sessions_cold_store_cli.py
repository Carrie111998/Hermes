"""Public CLI contract for cold-archiving one marked session lineage."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable

import pytest

import hermes_cli.session_cold_store as cold_store
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def session_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import hermes_state

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    return home


def _seed_archived_lineage(*, unrelated: bool = False) -> None:
    db = SessionDB()
    try:
        db.create_session(
            "lineage-root", source="cli", system_prompt="cold archive root prompt"
        )
        db.append_message("lineage-root", role="user", content="sensitive first turn")
        db.end_session("lineage-root", "compression")
        db.create_session(
            "lineage-terminal",
            source="cli",
            parent_session_id="lineage-root",
            system_prompt="cold archive shared prompt",
        )
        db.append_message(
            "lineage-terminal", role="assistant", content="sensitive final turn"
        )
        db.end_session("lineage-terminal", "completed")
        assert db.set_session_archived("lineage-terminal", True)
        if unrelated:
            db.create_session(
                "unrelated", source="cli", system_prompt="cold archive shared prompt"
            )
            db.append_message("unrelated", role="user", content="keep this row")
    finally:
        db.close()


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    try:
        main_mod.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _session_rows() -> list[tuple[str, int]]:
    db = SessionDB()
    try:
        assert db._conn is not None
        return [
            (str(row["id"]), int(row["archived"]))
            for row in db._conn.execute(
                "SELECT id, archived FROM sessions ORDER BY id"
            ).fetchall()
        ]
    finally:
        db.close()


def _message_rows() -> list[tuple[str, str]]:
    db = SessionDB()
    try:
        assert db._conn is not None
        return [
            (str(row["session_id"]), str(row["content"]))
            for row in db._conn.execute(
                "SELECT session_id, content FROM messages ORDER BY id"
            ).fetchall()
        ]
    finally:
        db.close()


def _snapshot_files(archive_root: Path) -> dict[Path, bytes]:
    if not archive_root.exists():
        return {}
    return {
        path.relative_to(archive_root): path.read_bytes()
        for path in archive_root.rglob("*")
        if path.is_file()
    }


def test_sessions_help_exposes_only_converged_public_cold_archive_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run_cli(monkeypatch, capsys, "sessions", "--help")

    assert code == 0
    assert err == ""
    assert re.search(r"\barchive\b", out)
    assert re.search(r"\bcold-archive\b", out)
    assert "cold-store" not in out
    assert "cold-verify" not in out
    assert "cold-purge" not in out


@pytest.mark.parametrize("hidden_action", ["cold-store", "cold-verify", "cold-purge"])
def test_hidden_stage_commands_are_rejected_by_argparse(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hidden_action: str,
) -> None:
    code, _out, err = _run_cli(monkeypatch, capsys, "sessions", hidden_action)

    assert code == 2
    assert "invalid choice" in err
    assert hidden_action in err


@pytest.mark.parametrize(
    ("arguments", "required_argument"),
    [
        (("sessions", "cold-archive"), "ROOT"),
        (("sessions", "cold-archive", "archive-root"), "--session-id"),
    ],
)
def test_cold_archive_requires_root_and_named_session_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    required_argument: str,
) -> None:
    code, _out, err = _run_cli(monkeypatch, capsys, *arguments)

    assert code == 2
    assert required_argument in err
    assert "required" in err


def test_cold_archive_dry_run_is_read_only_and_does_not_require_snapshot(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "new-cold-root"
    before_sessions = _session_rows()
    before_messages = _message_rows()
    db_path = session_home / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_compression_locks_expires")
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    opens: list[bool] = []
    original_session_db = SessionDB

    def tracked_open(*args, **kwargs):
        opens.append(bool(kwargs.get("read_only")))
        return original_session_db(*args, **kwargs)

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("dry-run must not prompt")

    monkeypatch.setattr("hermes_state.SessionDB", tracked_open)
    monkeypatch.setattr("builtins.input", unexpected_prompt)

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-archive",
        str(archive_root),
        "--session-id",
        "lineage-term",
        "--dry-run",
    )

    assert code == 0
    assert err == ""
    assert "resolved terminal ID: lineage-terminal" in out
    assert "Store → Verify → Purge" in out
    assert "nothing was written or deleted" in out
    assert opens == [True]
    assert not archive_root.exists()
    with sqlite3.connect(db_path) as conn:
        assert int(conn.execute("PRAGMA schema_version").fetchone()[0]) == schema_version
        assert conn.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'index' AND name = 'idx_compression_locks_expires'"
        ).fetchone() is None
    assert _session_rows() == before_sessions
    assert _message_rows() == before_messages


def test_cold_archive_yes_runs_store_verify_purge_serially_and_keeps_snapshot(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage(unrelated=True)
    archive_root = session_home.parent / "cold-root"
    calls: list[str] = []
    original_store = cold_store.store_archived_lineage
    original_verify = cold_store.verify_archived_lineage
    original_purge = cold_store.purge_archived_lineage

    def record_store(*args, **kwargs):
        calls.append("Store")
        return original_store(*args, **kwargs)

    def record_verify(*args, **kwargs):
        calls.append("Verify")
        return original_verify(*args, **kwargs)

    def record_purge(*args, **kwargs):
        calls.append("Purge")
        return original_purge(*args, **kwargs)

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(cold_store, "store_archived_lineage", record_store)
    monkeypatch.setattr(cold_store, "verify_archived_lineage", record_verify)
    monkeypatch.setattr(cold_store, "purge_archived_lineage", record_purge)
    monkeypatch.setattr("builtins.input", unexpected_prompt)

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-archive",
        str(archive_root),
        "--session-id",
        "lineage-terminal",
        "--yes",
    )

    assert code == 0
    assert err == ""
    assert calls == ["Store", "Verify", "Purge"]
    assert "Cold-archived session lineage:" in out
    assert "terminal ID: lineage-terminal" in out
    assert "physical IDs: lineage-root, lineage-terminal" in out
    assert re.search(r"fingerprint: [0-9a-f]{64}\b", out)
    snapshot_match = re.search(r"local snapshot retained: (.+)$", out, re.MULTILINE)
    assert snapshot_match is not None
    snapshot = Path(snapshot_match.group(1))
    assert snapshot.is_relative_to(archive_root)
    assert snapshot.is_dir()
    assert {path.name for path in snapshot.iterdir()} == {
        "artifacts",
        "metadata.json",
        "session.jsonl",
    }
    assert _session_rows() == [("unrelated", 0)]
    assert _message_rows() == [("unrelated", "keep this row")]


@pytest.mark.parametrize(
    ("failed_stage", "expected_calls", "snapshot_retained"),
    [
        pytest.param("Store", ["Store"], False, id="store"),
        pytest.param("Verify", ["Store", "Verify"], True, id="verify"),
        pytest.param("Purge", ["Store", "Verify", "Purge"], True, id="purge"),
    ],
)
def test_cold_archive_stage_failure_stops_and_retains_source_rows_and_snapshot(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_stage: str,
    expected_calls: list[str],
    snapshot_retained: bool,
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"
    before_sessions = _session_rows()
    before_messages = _message_rows()
    calls: list[str] = []
    original_store = cold_store.store_archived_lineage
    original_verify = cold_store.verify_archived_lineage

    def stage(
        name: str, implementation: Callable[..., object] | None
    ) -> Callable[..., object]:
        def run(*args, **kwargs):
            calls.append(name)
            if name == failed_stage:
                raise OSError(f"forced {name} failure")
            assert implementation is not None
            return implementation(*args, **kwargs)

        return run

    monkeypatch.setattr(
        cold_store, "store_archived_lineage", stage("Store", original_store)
    )
    monkeypatch.setattr(
        cold_store, "verify_archived_lineage", stage("Verify", original_verify)
    )
    monkeypatch.setattr(cold_store, "purge_archived_lineage", stage("Purge", None))

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-archive",
        str(archive_root),
        "--session-id",
        "lineage-terminal",
        "--yes",
    )

    assert code == 1
    assert err == ""
    assert calls == expected_calls
    assert f"Error: cold archive {failed_stage} failed: forced {failed_stage} failure" in out
    assert "source rows were retained" in out.lower()
    assert _session_rows() == before_sessions
    assert _message_rows() == before_messages
    files = _snapshot_files(archive_root)
    assert bool(files) is snapshot_retained
    if snapshot_retained:
        assert any(path.name == "metadata.json" for path in files)
        assert "local snapshot was retained" in out.lower()
    else:
        assert "no verified local snapshot was produced" in out.lower()


def test_cold_archive_without_confirmation_refuses_without_prompt_or_mutation(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"
    before_sessions = _session_rows()
    before_messages = _message_rows()

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("cold-archive confirmation is flag-based")

    monkeypatch.setattr("builtins.input", unexpected_prompt)
    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-archive",
        str(archive_root),
        "--session-id",
        "lineage-terminal",
    )

    assert code == 1
    assert err == ""
    assert "requires either --dry-run or --yes" in out
    assert "nothing was written or deleted" in out
    assert not archive_root.exists()
    assert _session_rows() == before_sessions
    assert _message_rows() == before_messages


def test_cold_archive_rejects_conflicting_confirmation_flags(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"

    code, _out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-archive",
        str(archive_root),
        "--session-id",
        "lineage-terminal",
        "--dry-run",
        "--yes",
    )

    assert code == 2
    assert "not allowed with argument" in err
    assert not archive_root.exists()
    assert _session_rows() == [
        ("lineage-root", 1),
        ("lineage-terminal", 1),
    ]
