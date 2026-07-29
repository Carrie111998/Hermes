"""``sessions.index_exclude_sources``: keep machine transcripts out of FTS.

The two full-text indexes cost several times the bytes of the text they
cover. On an automation-driven install the machine transcripts (webhook
routes, cron jobs, delegated subagents) are the large majority of stored
text and the least likely to be keyword-searched, so this setting lets an
operator keep them fully STORED while leaving them OUT of the index.

Every test here drives the real ``SessionDB`` against a temp database — the
guard lives in trigger DDL, so a mock would prove nothing about whether a
row actually lands in the index.
"""

import os
import sqlite3
from unittest import mock

import pytest

from hermes_state import (
    SessionDB,
    decode_fts_exclude_sources,
    encode_fts_exclude_sources,
    fts_exclude_sources_from_env,
)
from hermes_cli.config import join_index_exclude_sources


def _open(tmp_path, excluded=None, name="state.db"):
    """Open a SessionDB with ``excluded`` carried on the env bridge."""
    env = {}
    if excluded is not None:
        env["HERMES_FTS_EXCLUDE_SOURCES"] = ",".join(excluded)
    with mock.patch.dict(os.environ, env, clear=False):
        if excluded is None:
            os.environ.pop("HERMES_FTS_EXCLUDE_SOURCES", None)
        return SessionDB(db_path=tmp_path / name)


def _write(db, session_id, source, text):
    db.create_session(session_id, source)
    return db.append_message(session_id, "user", text)


def _indexed(db, table, message_id):
    """Is ``message_id`` present in ``table``'s index?"""
    row = db._conn.execute(
        f"SELECT 1 FROM {table}_docsize WHERE id = ?", (message_id,)
    ).fetchone()
    return row is not None


# ── encoding helpers ─────────────────────────────────────────────────────────

class TestEncoding:
    def test_empty_encodes_to_none_so_the_marker_key_is_absent(self):
        assert encode_fts_exclude_sources([]) is None
        # Callers translate None into DELETING the marker key.
        assert encode_fts_exclude_sources(()) is None

    def test_value_is_comma_wrapped_for_prefix_safe_membership(self):
        # The trigger tests membership with instr(value, ',' || source || ',').
        # Without the wrapping commas "cron" would match "cronish".
        assert encode_fts_exclude_sources(["webhook", "cron"]) == ",webhook,cron,"

    def test_prefix_of_an_excluded_source_is_not_itself_excluded(self):
        value = encode_fts_exclude_sources(["cronish"])
        assert value is not None and ",cron," not in value

    def test_round_trip(self):
        sources = ["webhook", "cron", "subagent"]
        assert decode_fts_exclude_sources(
            encode_fts_exclude_sources(sources)
        ) == sources

    def test_duplicates_collapse_and_blanks_drop(self):
        assert decode_fts_exclude_sources(
            encode_fts_exclude_sources(["cron", "cron", " ", "webhook"])
        ) == ["cron", "webhook"]

    def test_source_containing_a_comma_is_dropped_not_split(self):
        # The carrier is comma-delimited; splitting one name into two bogus
        # ones would silently exclude sources nobody configured.
        assert encode_fts_exclude_sources(["we,bhook"]) is None

    def test_config_list_renders_for_the_env_carrier(self):
        assert join_index_exclude_sources(["webhook", "cron"]) == "webhook,cron"

    def test_config_accepts_a_hand_edited_comma_string(self):
        assert join_index_exclude_sources("webhook, cron") == "webhook,cron"

    def test_config_non_sequence_renders_empty(self):
        assert join_index_exclude_sources(None) == ""
        assert join_index_exclude_sources(7) == ""

    def test_env_bridge_reads_comma_separated(self):
        with mock.patch.dict(
            os.environ, {"HERMES_FTS_EXCLUDE_SOURCES": "webhook, cron,"}
        ):
            assert fts_exclude_sources_from_env() == ["webhook", "cron"]

    def test_env_bridge_unset_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_FTS_EXCLUDE_SOURCES", None)
            assert fts_exclude_sources_from_env() == []


# ── indexing behavior ────────────────────────────────────────────────────────

class TestIndexingHonorsExclusion:
    def test_default_indexes_every_source(self, tmp_path):
        db = _open(tmp_path)
        try:
            assert db.fts_excluded_sources() == []
            mid = _write(db, "s-hook", "webhook", "needle alpha")
            assert _indexed(db, "messages_fts", mid)
        finally:
            db.close()

    def test_excluded_source_is_not_indexed(self, tmp_path):
        db = _open(tmp_path, ["webhook", "cron"])
        try:
            assert db.fts_excluded_sources() == ["webhook", "cron"]
            hook = _write(db, "s-hook", "webhook", "needle alpha")
            cron = _write(db, "s-cron", "cron", "needle alpha")
            kept = _write(db, "s-cli", "cli", "needle alpha")
            assert not _indexed(db, "messages_fts", hook)
            assert not _indexed(db, "messages_fts", cron)
            assert _indexed(db, "messages_fts", kept)
        finally:
            db.close()

    def test_excluded_source_is_left_out_of_the_trigram_index_too(
        self, tmp_path
    ):
        db = _open(tmp_path, ["webhook"])
        try:
            if not db._fts_table_exists("messages_fts_trigram"):
                pytest.skip("trigram tokenizer unavailable in this sqlite")
            hook = _write(db, "s-hook", "webhook", "needle alpha")
            kept = _write(db, "s-cli", "cli", "needle alpha")
            assert not _indexed(db, "messages_fts_trigram", hook)
            assert _indexed(db, "messages_fts_trigram", kept)
        finally:
            db.close()

    def test_a_source_whose_name_starts_with_an_excluded_one_is_indexed(
        self, tmp_path
    ):
        db = _open(tmp_path, ["cron"])
        try:
            mid = _write(db, "s-x", "cronish", "needle alpha")
            assert _indexed(db, "messages_fts", mid)
        finally:
            db.close()

    def test_transcript_stays_stored_and_readable_when_unindexed(
        self, tmp_path
    ):
        db = _open(tmp_path, ["webhook"])
        try:
            _write(db, "s-hook", "webhook", "needle alpha")
            msgs = db.get_messages("s-hook")
            assert [m["content"] for m in msgs] == ["needle alpha"]
        finally:
            db.close()

    def test_unindexed_row_is_absent_from_keyword_search_results(
        self, tmp_path
    ):
        db = _open(tmp_path, ["webhook"])
        try:
            _write(db, "s-hook", "webhook", "zzquux")
            _write(db, "s-cli", "cli", "zzquux")
            found = {r["session_id"] for r in db.search_messages("zzquux")}
            assert "s-cli" in found
            assert "s-hook" not in found
        finally:
            db.close()

    def test_updating_an_excluded_row_does_not_index_it(self, tmp_path):
        db = _open(tmp_path, ["webhook"])
        try:
            mid = _write(db, "s-hook", "webhook", "needle alpha")
            db._conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                ("needle beta", mid),
            )
            db._conn.commit()
            assert not _indexed(db, "messages_fts", mid)
        finally:
            db.close()

    def test_deleting_an_unindexed_row_does_not_raise(self, tmp_path):
        # The delete trigger fires for every row, indexed or not. Issuing a
        # 'delete' op for a row FTS5 never saw corrupts the index, which is
        # why the trigger carries a docsize existence check.
        db = _open(tmp_path, ["webhook"])
        try:
            mid = _write(db, "s-hook", "webhook", "needle alpha")
            db._conn.execute("DELETE FROM messages WHERE id = ?", (mid,))
            db._conn.commit()
            assert db._conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"
            ) is not None
        finally:
            db.close()


class TestConfigIsAuthoritative:
    def test_reopening_with_a_new_setting_applies_to_new_writes(
        self, tmp_path
    ):
        db = _open(tmp_path)
        try:
            before = _write(db, "s-a", "webhook", "needle alpha")
            assert _indexed(db, "messages_fts", before)
        finally:
            db.close()

        db = _open(tmp_path, ["webhook"])
        try:
            assert db.fts_excluded_sources() == ["webhook"]
            after = _write(db, "s-b", "webhook", "needle alpha")
            assert not _indexed(db, "messages_fts", after)
            # Rows indexed under the old setting are untouched until an
            # explicit prune — that is what `hermes sessions prune-index` is
            # for, and asserting it here documents the boundary.
            assert _indexed(db, "messages_fts", before)
        finally:
            db.close()

    def test_clearing_the_setting_resumes_indexing(self, tmp_path):
        db = _open(tmp_path, ["webhook"])
        try:
            assert not _indexed(
                db, "messages_fts", _write(db, "s-a", "webhook", "alpha")
            )
        finally:
            db.close()

        db = _open(tmp_path, [])
        try:
            assert db.fts_excluded_sources() == []
            assert _indexed(
                db, "messages_fts", _write(db, "s-b", "webhook", "alpha")
            )
        finally:
            db.close()

    def test_old_trigger_bodies_are_replaced_not_kept(self, tmp_path):
        # CREATE TRIGGER IF NOT EXISTS keeps whatever body is already stored,
        # so a database carrying a pre-change trigger would keep indexing
        # every source forever unless the stale trigger is dropped first.
        db = _open(tmp_path)
        try:
            pass
        finally:
            db.close()

        raw = sqlite3.connect(str(tmp_path / "state.db"))
        raw.execute("DROP TRIGGER messages_fts_insert")
        raw.execute(
            """
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages
            BEGIN
                INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
                VALUES (new.id, new.content, new.tool_name, new.tool_calls);
            END;
            """
        )
        raw.commit()
        raw.close()

        db = _open(tmp_path, ["webhook"])
        try:
            body = db._conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name = 'messages_fts_insert'"
            ).fetchone()[0]
            assert "fts_exclude_sources" in body
            assert not _indexed(
                db, "messages_fts", _write(db, "s-b", "webhook", "alpha")
            )
        finally:
            db.close()


class TestPruneIndex:
    def test_prune_unindexes_existing_rows_and_keeps_transcripts(
        self, tmp_path
    ):
        db = _open(tmp_path)
        try:
            hook = _write(db, "s-hook", "webhook", "needle alpha")
            kept = _write(db, "s-cli", "cli", "needle alpha")
        finally:
            db.close()

        db = _open(tmp_path, ["webhook"])
        try:
            result = db.prune_fts_for_excluded_sources(chunk_rows=1)
            assert result["ok"] is True
            assert result["removed"]["messages_fts"] >= 1
            assert not _indexed(db, "messages_fts", hook)
            assert _indexed(db, "messages_fts", kept)
            # Index-only: the message rows themselves survive.
            assert [m["content"] for m in db.get_messages("s-hook")] == [
                "needle alpha"
            ]
        finally:
            db.close()

    def test_prune_is_idempotent(self, tmp_path):
        db = _open(tmp_path)
        try:
            _write(db, "s-hook", "webhook", "needle alpha")
        finally:
            db.close()

        db = _open(tmp_path, ["webhook"])
        try:
            first = db.prune_fts_for_excluded_sources()
            second = db.prune_fts_for_excluded_sources()
            assert first["removed"]["messages_fts"] >= 1
            assert second["removed"]["messages_fts"] == 0
        finally:
            db.close()

    def test_prune_declines_when_nothing_is_excluded(self, tmp_path):
        db = _open(tmp_path)
        try:
            result = db.prune_fts_for_excluded_sources()
            assert result["ok"] is False
            assert result["reason"] == "no_excluded_sources"
        finally:
            db.close()

    def test_pruned_index_still_passes_its_own_integrity_check(
        self, tmp_path
    ):
        db = _open(tmp_path)
        try:
            _write(db, "s-hook", "webhook", "needle alpha")
            _write(db, "s-cli", "cli", "needle alpha")
        finally:
            db.close()

        db = _open(tmp_path, ["webhook"])
        try:
            db.prune_fts_for_excluded_sources()
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts) "
                "VALUES('integrity-check')"
            )
            found = {r["session_id"] for r in db.search_messages("needle")}
            assert found == {"s-cli"}
        finally:
            db.close()


class TestRebuildReappliesExclusion:
    def test_a_full_rebuild_does_not_re_add_excluded_rows(self, tmp_path):
        # FTS5's 'rebuild' repopulates from the whole content source and
        # cannot filter, so every corruption recovery would silently
        # re-inflate the index unless the exclusion is re-applied.
        db = _open(tmp_path, ["webhook"])
        try:
            hook = _write(db, "s-hook", "webhook", "needle alpha")
            kept = _write(db, "s-cli", "cli", "needle alpha")
            SessionDB._rebuild_fts_indexes(
                db._conn.cursor(),
                include_trigram=db._fts_table_exists("messages_fts_trigram"),
            )
            db._conn.commit()
            assert not _indexed(db, "messages_fts", hook)
            assert _indexed(db, "messages_fts", kept)
        finally:
            db.close()
