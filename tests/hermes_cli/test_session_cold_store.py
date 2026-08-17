"""Contract tests for the first Store-only cold archive slice."""

import json
from datetime import UTC, datetime
import os
from pathlib import Path
import signal

import pytest

import hermes_cli.session_cold_store as cold_store
from hermes_cli.session_cold_store import store_archived_lineage
from hermes_state import SessionDB


def test_store_archived_compression_lineage_writes_one_terminal_id_snapshot(
    tmp_path: Path,
) -> None:
    """Store writes one immutable terminal-ID snapshot without deleting DB rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("root", source="cli")
        db.append_message("root", role="user", content="first")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.append_message("terminal", role="assistant", content="second")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", archive_root)

        assert result.terminal_id == "terminal"
        assert result.physical_ids == ("root", "terminal")
        terminal = db.get_session("terminal")
        assert terminal is not None
        started = datetime.fromtimestamp(float(terminal["started_at"]), UTC)
        expected_snapshot = (
            archive_root
            / "sessions"
            / "started"
            / f"{started:%Y}"
            / f"{started:%m}"
            / f"{started:%d}"
            / "terminal"
        )
        assert result.revision_dir == expected_snapshot
        assert {path.name for path in result.revision_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
        assert (result.revision_dir / "artifacts").is_dir()
        assert (result.revision_dir / "metadata.json").is_file()
        assert (result.revision_dir / "session.jsonl").is_file()
        assert not list(archive_root.rglob("revisions"))
        assert store_archived_lineage(db, "terminal", archive_root) == result
        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
    finally:
        db.close()


def test_store_rejects_a_compression_ancestor_when_a_tip_exists(tmp_path: Path) -> None:
    """An archive snapshot must be keyed to the complete chain terminal, never a prefix."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        with pytest.raises(ValueError, match="terminal"):
            store_archived_lineage(db, "root", tmp_path / "archive")
    finally:
        db.close()


def test_store_keeps_a_compression_child_with_an_inherited_old_delegate_marker(tmp_path: Path) -> None:
    """Inherited markers are not forks unless they name the direct parent."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session(
            "terminal",
            source="cli",
            parent_session_id="root",
            model_config={"_delegate_from": "older-delegate-parent"},
        )
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", tmp_path / "archive")

        assert result.physical_ids == ("root", "terminal")
    finally:
        db.close()


def test_store_reads_raw_rows_without_flushing_and_preserves_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store neither flushes pending accounting nor drops normalized prompt rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli", system_prompt="be helpful")
        db.append_message("terminal", role="user", content="hello")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        def unexpected_flush() -> None:
            raise AssertionError("Store must not flush accounting")

        monkeypatch.setattr(db, "flush_token_counts", unexpected_flush)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")

        records = [
            json.loads(line)
            for line in (result.revision_dir / "session.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert any(record["kind"] == "system-prompt" for record in records)
    finally:
        db.close()


def test_store_rejects_post_store_source_mutation_without_changing_db_or_snapshot(
    tmp_path: Path,
) -> None:
    """A marked lineage is an archival boundary, not a source of new revisions."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", archive_root)
        metadata_before = (result.revision_dir / "metadata.json").read_bytes()
        session_before = (result.revision_dir / "session.jsonl").read_bytes()

        db.append_message("terminal", role="assistant", content="changed after Store")
        changed_messages = db.get_messages("terminal")

        with pytest.raises(
            ValueError,
            match=r"archival boundary.*source changed after Store",
        ):
            store_archived_lineage(db, "terminal", archive_root)

        assert db.get_messages("terminal") == changed_messages
        assert (result.revision_dir / "metadata.json").read_bytes() == metadata_before
        assert (result.revision_dir / "session.jsonl").read_bytes() == session_before
        assert {path.name for path in result.revision_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
        assert not list(archive_root.rglob("revisions"))
    finally:
        db.close()


def test_store_uses_started_date_when_terminal_ended_at_changes(tmp_path: Path) -> None:
    """A reopened terminal keeps one snapshot identity even if it ends again later."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    first_ended_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    second_ended_at = datetime(2026, 3, 4, 5, 6, tzinfo=UTC).timestamp()
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="immutable")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (started_at, first_ended_at, "terminal"),
        )

        result = store_archived_lineage(db, "terminal", archive_root)
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (second_ended_at, "terminal"),
        )

        with pytest.raises(
            ValueError,
            match=r"archival boundary.*source changed after Store",
        ):
            store_archived_lineage(db, "terminal", archive_root)

        assert result.revision_dir == (
            archive_root / "sessions" / "started" / "2026" / "01" / "02" / "terminal"
        )
        assert list(archive_root.rglob("terminal")) == [result.revision_dir]
    finally:
        db.close()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(signal, "setitimer"),
    reason="requires POSIX FIFOs and interval timers",
)
@pytest.mark.parametrize("payload_name", ["metadata.json", "session.jsonl"])
def test_store_rejects_fifo_payload_without_blocking(
    tmp_path: Path,
    payload_name: str,
) -> None:
    """A damaged snapshot payload that is a FIFO is invalid, not a blocking read."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        payload = result.revision_dir / payload_name
        payload.unlink()
        os.mkfifo(payload)

        def fail_if_blocked(*_args: object) -> None:
            raise AssertionError("cold-store validation blocked while opening a FIFO")

        previous_handler = signal.signal(signal.SIGALRM, fail_if_blocked)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            with pytest.raises(ValueError, match="existing snapshot is invalid"):
                store_archived_lineage(db, "terminal", tmp_path / "archive")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)
    finally:
        db.close()


def test_store_rejects_snapshot_parent_symlink_swap_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated snapshot parent cannot be swapped to redirect transcript writes."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="sensitive transcript")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        real_mkdir = cold_store.os.mkdir
        swapped = False

        def racing_mkdir(
            path: str | Path,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if not swapped and Path(path).name.startswith(".staging-"):
                snapshot_parent = next(archive_root.glob("sessions/started/*/*/*"))
                snapshot_parent.rename(snapshot_parent.with_name("day-displaced"))
                snapshot_parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(cold_store.os, "mkdir", racing_mkdir)

        with pytest.raises(ValueError, match="unsafe archive parent path"):
            store_archived_lineage(db, "terminal", archive_root)

        assert swapped
        assert not any(outside.iterdir())
    finally:
        db.close()
