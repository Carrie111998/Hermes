"""Contract tests for the first Store-only cold archive slice."""

import json
from datetime import UTC, datetime
import os
from pathlib import Path
import signal

import pytest

import hermes_cli.session_cold_store as cold_store
from hermes_cli.session_cold_store import (
    plan_archived_lineage,
    purge_archived_lineage,
    store_archived_lineage,
    validate_purge_archived_lineage,
)
from hermes_state import SessionDB


def test_store_archived_compression_lineage_writes_one_terminal_id_snapshot(
    tmp_path: Path,
) -> None:
    """Store writes one current terminal-ID snapshot without deleting DB rows."""
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
            / cold_store._safe_component("terminal")
        )
        assert result.snapshot_dir == expected_snapshot
        assert {path.name for path in result.snapshot_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
        assert (result.snapshot_dir / "artifacts").is_dir()
        assert (result.snapshot_dir / "metadata.json").is_file()
        assert (result.snapshot_dir / "session.jsonl").is_file()
        assert store_archived_lineage(db, "terminal", archive_root) == result
        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
    finally:
        db.close()


@pytest.mark.parametrize("snapshot_state", ["missing", "mismatched"])
def test_purge_rejects_missing_or_mismatched_snapshot_and_retains_source_rows(
    tmp_path: Path,
    snapshot_state: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        if snapshot_state == "missing":
            archive_root = tmp_path / "missing-archive"
        else:
            metadata_path = stored.snapshot_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source_fingerprint"] = "0" * 64
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(ValueError, match="snapshot"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
    finally:
        db.close()


def test_purge_rejects_source_mutation_after_store_and_retains_source_rows(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.append_message("terminal", role="assistant", content="changed after Store")

        with pytest.raises(ValueError, match="fingerprint"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert [message["content"] for message in db.get_messages("terminal")] == [
            "stored",
            "changed after Store",
        ]
    finally:
        db.close()


def test_purge_rechecks_pin_inside_delete_transaction(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db.set_session_pinned("terminal", True)

        with pytest.raises(ValueError, match="pinned"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "stored"
    finally:
        db.close()


def test_purge_rejects_uncovered_child_and_preserves_foreign_key_rows(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.create_session(
            "branch",
            source="cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )

        with pytest.raises(ValueError, match="uncovered child"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
        branch = db.get_session("branch")
        assert branch is not None
        assert branch["parent_session_id"] == "root"
    finally:
        db.close()


@pytest.mark.parametrize(
    "entry_json",
    ["{malformed", "[]", '{"session_id": 123}'],
)
def test_purge_rejects_unverifiable_routing_entries_and_retains_all_rows(
    tmp_path: Path,
    entry_json: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.save_gateway_routing_entry(
            "route-key", entry_json, scope="test-routing-scope"
        )

        with pytest.raises(ValueError, match="gateway_routing.*cannot be verified"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
        assert db.load_gateway_routing_entries(scope="test-routing-scope") == {
            "route-key": entry_json
        }
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["preflight", "purge"])
def test_purge_fails_closed_when_optional_delegation_reference_column_is_absent(
    tmp_path: Path,
    operation: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db._conn is not None
        db._conn.execute(
            "ALTER TABLE async_delegations DROP COLUMN origin_session_id"
        )
        db._conn.commit()
        before_meta = db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall()
        before_delegations = db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall()
        assert (
            db._conn.execute("SELECT COUNT(*) FROM gateway_routing").fetchone()[0]
            == 0
        )

        with pytest.raises(
            ValueError,
            match=r"schema is missing async_delegations\.origin_session_id",
        ):
            if operation == "preflight":
                validate_purge_archived_lineage(db, "terminal", archive_root)
            else:
                purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
        assert db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall() == before_meta
        assert db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall() == before_delegations
    finally:
        db.close()


def test_verify_rejects_live_source_change_after_store(tmp_path: Path) -> None:
    """Verify compares the current read-only Store plan with the snapshot."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        db.append_message("terminal", role="assistant", content="changed after Store")
        before_changes = db._conn.total_changes if db._conn is not None else -1

        with pytest.raises(ValueError, match="fingerprint"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert db._conn is not None
        assert db._conn.total_changes == before_changes
        assert stored.snapshot_dir.is_dir()
        assert "changed after Store" not in (
            stored.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        db.close()


def test_verify_missing_snapshot_does_not_create_archive_root(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "missing-archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        before_changes = db._conn.total_changes if db._conn is not None else -1

        with pytest.raises(ValueError, match="snapshot not found"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert not archive_root.exists()
        assert db._conn is not None
        assert db._conn.total_changes == before_changes
    finally:
        db.close()


def test_verify_rejects_corrupt_snapshot_without_replacing_it(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        metadata = stored.snapshot_dir / "metadata.json"
        metadata.write_text("{corrupt\n", encoding="utf-8")
        before_files = sorted(
            path.relative_to(archive_root) for path in archive_root.rglob("*")
        )

        with pytest.raises(ValueError, match="snapshot is corrupt"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert metadata.read_text(encoding="utf-8") == "{corrupt\n"
        assert sorted(
            path.relative_to(archive_root) for path in archive_root.rglob("*")
        ) == before_files
    finally:
        db.close()


def test_verify_rejects_symlinked_snapshot_payload(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    outside = tmp_path / "outside-metadata.json"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        metadata = stored.snapshot_dir / "metadata.json"
        original = metadata.read_text(encoding="utf-8")
        outside.write_text(original, encoding="utf-8")
        metadata.unlink()
        metadata.symlink_to(outside)

        with pytest.raises(ValueError, match="snapshot is corrupt"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert metadata.is_symlink()
        assert outside.read_text(encoding="utf-8") == original
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["store", "preflight"])
def test_store_rejects_blob_message_content_before_writing(
    tmp_path: Path,
    operation: str,
) -> None:
    """Archive v1 fails closed on SQLite BLOBs without changing its source."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        message_id = db.append_message("terminal", role="user", content="placeholder")
        assert db._conn is not None
        db._conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (b"future-blob", message_id),
        )
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        with pytest.raises(
            ValueError,
            match=r"cold store v1 does not support SQLite BLOB/bytes values.*message\.content",
        ):
            if operation == "store":
                store_archived_lineage(db, "terminal", archive_root)
            else:
                plan_archived_lineage(db, "terminal")

        assert not archive_root.exists()
        assert db._conn is not None
        session = db._conn.execute(
            "SELECT archived FROM sessions WHERE id = ?", ("terminal",)
        ).fetchone()
        message = db._conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", ("terminal",)
        ).fetchone()
        assert session is not None and session["archived"] == 1
        assert message is not None and message["content"] == b"future-blob"
    finally:
        db.close()


def test_store_reports_the_same_canonical_path_it_writes(tmp_path: Path) -> None:
    """A lexical ROOT alias cannot make the reported snapshot path misleading."""
    db = SessionDB(db_path=tmp_path / "state.db")
    canonical_root = tmp_path / "archive"
    root_spelling = canonical_root / "not-created" / ".."
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", root_spelling)

        assert result.snapshot_dir.is_dir()
        assert result.snapshot_dir.is_relative_to(canonical_root)
        assert "not-created" not in result.snapshot_dir.parts
    finally:
        db.close()


def test_store_keeps_distinct_ids_that_only_differ_by_trailing_space(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    try:
        for session_id, marker in (("foo", "plain"), ("foo ", "trailing-space")):
            db.create_session(session_id, source="cli")
            db.append_message(session_id, role="user", content=marker)
            db.end_session(session_id, "completed")
            assert db.set_session_archived(session_id, True)
            assert db._conn is not None
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at, session_id),
            )

        plain = store_archived_lineage(db, "foo", archive_root)
        trailing_space = store_archived_lineage(db, "foo ", archive_root)

        assert plain.snapshot_dir != trailing_space.snapshot_dir
        assert plain.snapshot_dir.is_dir()
        assert trailing_space.snapshot_dir.is_dir()
        assert '"content":"plain"' in (
            plain.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
        assert '"content":"trailing-space"' in (
            trailing_space.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        db.close()


def test_safe_component_reserves_the_hash_suffix_namespace() -> None:
    """A generated component cannot collide with an already-safe literal ID."""
    generated = cold_store._safe_component("foo ")

    assert cold_store._safe_component(generated) != generated


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
            for line in (result.snapshot_dir / "session.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert any(record["kind"] == "system-prompt" for record in records)
    finally:
        db.close()


def test_store_replaces_current_snapshot_when_archived_source_changes(
    tmp_path: Path,
) -> None:
    """A changed archived source replaces the snapshot and leaves source rows intact."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        first = store_archived_lineage(db, "terminal", archive_root)
        session_before = (first.snapshot_dir / "session.jsonl").read_bytes()

        db.append_message("terminal", role="assistant", content="changed after Store")
        changed_messages = db.get_messages("terminal")

        replacement = store_archived_lineage(db, "terminal", archive_root)

        assert db.get_messages("terminal") == changed_messages
        assert replacement.snapshot_dir == first.snapshot_dir
        assert replacement.source_fingerprint != first.source_fingerprint
        replacement_payload = (replacement.snapshot_dir / "session.jsonl").read_bytes()
        assert replacement_payload != session_before
        assert b"changed after Store" in replacement_payload
        assert {path.name for path in replacement.snapshot_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
    finally:
        db.close()


def test_store_same_fingerprint_is_idempotent_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="unchanged")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        first = store_archived_lineage(db, "terminal", archive_root)

        def unexpected_staging(_snapshot_parent_fd: int) -> tuple[str, int]:
            raise AssertionError("same fingerprint must not create a staged replacement")

        monkeypatch.setattr(cold_store, "_create_staging_directory", unexpected_staging)

        assert store_archived_lineage(db, "terminal", archive_root) == first
        assert first.snapshot_dir.is_dir()
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["store", "verify"])
def test_existing_snapshot_rejects_replaced_open_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A valid snapshot is not accepted after its opened parent is displaced."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        snapshot_parent = stored.snapshot_dir.parent
        displaced_parent = snapshot_parent.with_name("day-displaced")
        real_validate = cold_store._valid_existing_snapshot_at
        swapped = False

        def validate_then_replace_parent(*args, **kwargs):
            nonlocal swapped
            valid = real_validate(*args, **kwargs)
            if valid is True and not swapped:
                snapshot_parent.rename(displaced_parent)
                snapshot_parent.mkdir()
                swapped = True
            return valid

        monkeypatch.setattr(
            cold_store,
            "_valid_existing_snapshot_at",
            validate_then_replace_parent,
        )

        with pytest.raises(ValueError, match="unsafe archive parent path"):
            if operation == "store":
                store_archived_lineage(db, "terminal", archive_root)
            else:
                cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert swapped
        assert not stored.snapshot_dir.exists()
        assert (displaced_parent / stored.snapshot_dir.name).is_dir()
    finally:
        db.close()


def test_store_does_not_use_old_snapshot_after_replacement(tmp_path: Path) -> None:
    """Only the replacement payload remains at the logical session's current path."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    first_ended_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    second_ended_at = datetime(2026, 3, 4, 5, 6, tzinfo=UTC).timestamp()
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="old payload marker")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (started_at, first_ended_at, "terminal"),
        )

        first = store_archived_lineage(db, "terminal", archive_root)
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (second_ended_at, "terminal"),
        )
        db.append_message("terminal", role="assistant", content="new payload marker")

        replacement = store_archived_lineage(db, "terminal", archive_root)

        assert replacement.snapshot_dir == first.snapshot_dir == (
            archive_root
            / "sessions"
            / "started"
            / "2026"
            / "01"
            / "02"
            / cold_store._safe_component("terminal")
        )
        current_metadata = json.loads(
            (replacement.snapshot_dir / "metadata.json").read_text(encoding="utf-8")
        )
        assert current_metadata["source_fingerprint"] == replacement.source_fingerprint
        assert current_metadata["source_fingerprint"] != first.source_fingerprint
        assert list(archive_root.rglob(cold_store._safe_component("terminal"))) == [
            replacement.snapshot_dir
        ]
        payloads = list(archive_root.rglob("session.jsonl"))
        assert payloads == [replacement.snapshot_dir / "session.jsonl"]
        assert "new payload marker" in payloads[0].read_text(encoding="utf-8")
    finally:
        db.close()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(signal, "setitimer"),
    reason="requires POSIX FIFOs and interval timers",
)
@pytest.mark.parametrize("payload_name", ["metadata.json", "session.jsonl"])
def test_store_replaces_fifo_payload_without_blocking(
    tmp_path: Path,
    payload_name: str,
) -> None:
    """A damaged current payload is safely replaced without blocking on a FIFO."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        payload = result.snapshot_dir / payload_name
        payload.unlink()
        os.mkfifo(payload)

        def fail_if_blocked(*_args: object) -> None:
            raise AssertionError("cold-store validation blocked while opening a FIFO")

        previous_handler = signal.signal(signal.SIGALRM, fail_if_blocked)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)

        assert (replacement.snapshot_dir / payload_name).is_file()
    finally:
        db.close()


def test_store_replaces_non_object_snapshot_metadata(tmp_path: Path) -> None:
    """A malformed current metadata envelope is replaced by a valid snapshot."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        (result.snapshot_dir / "metadata.json").write_text("[]\n", encoding="utf-8")

        replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")

        metadata = json.loads(
            (replacement.snapshot_dir / "metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["source_fingerprint"] == replacement.source_fingerprint
    finally:
        db.close()


@pytest.mark.parametrize("payload_name", ["metadata.json", "session.jsonl"])
def test_store_replaces_snapshot_with_invalid_utf8(
    tmp_path: Path,
    payload_name: str,
) -> None:
    """Invalid UTF-8 is damaged snapshot state, just like malformed JSON."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        (result.snapshot_dir / payload_name).write_bytes(b"\xff")

        replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")

        assert replacement == result
        (replacement.snapshot_dir / payload_name).read_text(encoding="utf-8")
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
