"""Contract tests for the first Store-only cold archive slice."""

import json
from pathlib import Path

import pytest

from hermes_cli.session_cold_store import store_archived_lineage
from hermes_state import SessionDB


def test_store_archived_compression_lineage_writes_terminal_id_revision(tmp_path: Path) -> None:
    """Store writes a verified local revision without deleting temp-DB rows."""
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
        assert result.revision_dir.name == result.source_fingerprint
        assert result.revision_dir.parent.name == "revisions"
        assert result.revision_dir.parent.parent.name == "terminal"
        assert (result.revision_dir / "metadata.json").is_file()
        assert (result.revision_dir / "session.jsonl").is_file()
        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
    finally:
        db.close()


def test_store_rejects_a_compression_ancestor_when_a_tip_exists(tmp_path: Path) -> None:
    """An archive revision must be keyed to the complete chain terminal, never a prefix."""
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
            for line in (result.revision_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert any(record["kind"] == "system-prompt" for record in records)
    finally:
        db.close()
