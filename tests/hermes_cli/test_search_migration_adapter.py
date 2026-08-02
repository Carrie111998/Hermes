"""Security/correctness gates for the inert search migration adapter.

No test wires this adapter into SessionDB or a serving path.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import threading

import pytest

from hermes_cli.search_migration_adapter import (
    CacheClosedError, CacheCorruptionError, CacheLockError, CanonicalMessage, DerivedCacheError,
    InMemoryHydrator, MAX_PAGE_SIZE, SearchMigrationAdapter, ShardTarget,
    UnsupportedSQLiteError,
)
from hermes_state import SessionDB


def row(i: int, content: str, *, timestamp="2026-07-31T00:00:00+00:00", source="cli"):
    return CanonicalMessage(i, "s", "user", content, None, None, timestamp, source, "model", "start", 1, 0)


def target(shard: str, generation: int, watermark: int, rows, previous=None):
    return SearchMigrationAdapter.target_from_rows(shard, generation, watermark, rows, previous)


def adapter(tmp_path: Path, rows, shards={"a"}):
    a = SearchMigrationAdapter(tmp_path / "cache.db", InMemoryHydrator(rows), declared_shards=shards)
    grouped = {shard: [] for shard in shards}; grouped["a"] = list(rows)
    if len(shards) == 1:
        targets = {"a": target("a", 1, 1, rows)}
        a.apply_target(targets["a"], rows)
    else:
        targets = {s: target(s, 1, 1, grouped[s]) for s in shards}
        a.rebuild(targets, grouped)
    a._authoritative_targets = targets
    return a


def observed(a):
    return {r["shard"]: ShardTarget(r["shard"], r["generation"], r["watermark"], r["state_digest"], None,
                                      r["unicode_postings_digest"], r["trigram_postings_digest"])
            for r in a.connection.execute("SELECT * FROM search_adapter_shards")}


def search(a, query, **kwargs):
    return a.search(query, observed_targets=kwargs.pop("observed_targets", observed(a)), **kwargs)


def test_contentless_indexes_and_exact_hydration_after_slice(tmp_path):
    rows = [row(i, f"needle {i}") for i in range(20)]
    a = adapter(tmp_path, rows)
    result = search(a, "needle", limit=3, offset=4)
    assert result.available and result.candidate_count == 20 and result.hydration_count == 3
    assert [h["id"] for h in result.hits] == [4, 5, 6]
    assert a.connection.execute("SELECT indexed FROM search_adapter_unicode").fetchone()[0] is None


@pytest.mark.parametrize("limit,offset", [(-1, 0), (True, 0), ("1", 0), (1, -1), (1, True), (MAX_PAGE_SIZE + 1, 0), (1, 1_000_001)])
def test_pagination_is_bounded_and_fail_closed(tmp_path, limit, offset):
    result = search(adapter(tmp_path, [row(1, "needle")]), "needle", limit=limit, offset=offset)
    assert (result.error.code, result.hits, result.hydration_count) == ("invalid_pagination", [], 0)


def test_digest_continuity_allows_watermark_jump_only_with_previous_state(tmp_path):
    old, new = row(1, "old"), row(1, "newer")
    a = adapter(tmp_path, [old])
    previous = SearchMigrationAdapter.state_digest([old])
    with pytest.raises(ValueError, match="previous"):
        a.apply_target(target("a", 1, 99, [new]), [new])
    a.apply_target(target("a", 1, 99, [new], previous), [new])
    a.hydrator = InMemoryHydrator([new])
    assert [x["id"] for x in search(a, "newer").hits] == [1]
    before = tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone())
    a.apply_target(target("a", 1, 99, [new]), [new])
    assert tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone()) == before
    assert [x["id"] for x in search(a, "newer").hits] == [1]


def test_target_and_hydration_content_attestation_fail_closed(tmp_path):
    indexed, changed = row(1, "needle"), row(1, "changed")
    a = adapter(tmp_path, [indexed])
    a.hydrator = InMemoryHydrator([changed])
    result = search(a, "needle")
    assert (result.error.code, result.hydration_count) == ("hydration_mismatch", 0)
    bad = {"a": ShardTarget("a", 1, 1, "0" * 64)}
    assert search(a, "needle", observed_targets=bad).error.code == "watermark_mismatch"


@pytest.mark.parametrize("mutation", [
    lambda a: a.connection.execute("DELETE FROM search_adapter_metadata"),
    lambda a: a.connection.execute("DELETE FROM search_adapter_unicode WHERE rowid=1"),
    lambda a: a.connection.execute("INSERT INTO search_adapter_trigram(rowid,indexed) VALUES (99,'orphan')"),
    lambda a: a.connection.execute("UPDATE search_adapter_rows SET row_digest='bad'"),
])
def test_metadata_row_and_both_fts_integrity_are_required(tmp_path, mutation):
    a = adapter(tmp_path, [row(1, "needle")]); mutation(a); a.connection.commit()
    assert search(a, "needle").error.code == "cache_corrupt"


@pytest.mark.parametrize("column,value", [
    ("source", "telegram"), ("role", "assistant"), ("active", 0), ("compacted", 1),
    ("timestamp", "2026-08-01T00:00:00+00:00"), ("timestamp_order", 999.0),
    ("session_id", "other"), ("model", "other-model"), ("session_started", "other-start"),
    ("shard", "other"),
])
def test_stored_projection_mutation_with_unchanged_digests_fails_closed(tmp_path, column, value):
    a = adapter(tmp_path, [row(1, "needle")])
    a.connection.execute(f"UPDATE search_adapter_rows SET {column}=? WHERE id=1", (value,))
    a.connection.commit()
    result = search(a, "needle", observed_targets={"a": target("a", 1, 1, [row(1, "needle")])})
    assert (result.error.code, result.candidate_count, result.hydration_count) == ("cache_corrupt", 0, 0)


@pytest.mark.parametrize("table", ["search_adapter_unicode", "search_adapter_trigram"])
def test_same_rowid_fts_posting_replacement_fails_closed(tmp_path, table):
    a = adapter(tmp_path, [row(1, "needle")])
    a.connection.execute(f"DELETE FROM {table} WHERE rowid=1")
    a.connection.execute(f"INSERT INTO {table}(rowid,indexed) VALUES (1,'different')")
    a.connection.commit()
    assert a.connection.execute(f"INSERT INTO {table}({table}) VALUES ('integrity-check')")
    result = search(a, "needle", observed_targets={"a": target("a", 1, 1, [row(1, "needle")])})
    assert (result.error.code, result.candidate_count, result.hydration_count) == ("cache_corrupt", 0, 0)


def test_commit_lock_rolls_back_mutation_and_allows_retry(tmp_path):
    old, new = row(1, "old"), row(1, "new")
    a = adapter(tmp_path, [old]); path = tmp_path / "cache.db"
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN"); reader.execute("SELECT * FROM search_adapter_rows").fetchall()
    advanced = target("a", 1, 2, [new], SearchMigrationAdapter.state_digest([old], "a"))
    try:
        with pytest.raises(CacheLockError):
            a.apply_target(advanced, [new])
        assert not a.connection.in_transaction
        assert reader.execute("SELECT state_digest FROM search_adapter_shards").fetchone()[0] == target("a", 1, 1, [old]).state_digest
    finally:
        reader.rollback(); reader.close()
    a.apply_target(advanced, [new]); a._authoritative_targets = {"a": advanced}; a.hydrator = InMemoryHydrator([new])
    assert [hit["id"] for hit in search(a, "new").hits] == [1]


def test_incremental_and_hydrated_indexed_byte_bounds_reject_without_mutation(tmp_path):
    old, huge = row(1, "old"), row(2, "x" * 32)
    a = SearchMigrationAdapter(tmp_path / "cache.db", InMemoryHydrator([old, huge]), declared_shards={"a"}, max_batch_indexed_bytes=16, max_row_indexed_bytes=16)
    a.apply_target(target("a", 1, 1, [old]), [old]); a._authoritative_targets = {"a": target("a", 1, 1, [old])}
    with pytest.raises(ValueError, match="indexed bytes"):
        a.apply_target(target("a", 1, 2, [old, huge], SearchMigrationAdapter.state_digest([old], "a")), [huge])
    assert [hit["id"] for hit in search(a, "old").hits] == [1]
    a.max_page_indexed_bytes = 1
    result = search(a, "old")
    assert (result.error.code, result.hydration_count) == ("hydrated_page_too_large", 0)


def test_stored_state_digest_streams_cursor_without_materializing(tmp_path):
    a = adapter(tmp_path, [row(1, "needle")])
    class OneShot:
        def __iter__(self): return self
        def __next__(self):
            if getattr(self, "used", False): raise StopIteration
            self.used = True
            return {"id": 1, "row_digest": SearchMigrationAdapter.row_digest(row(1, "needle")), "shard": "a", "session_id": "s", "role": "user", "timestamp": "2026-07-31T00:00:00+00:00", "timestamp_order": SearchMigrationAdapter._timestamp_order("2026-07-31T00:00:00+00:00"), "source": "cli", "model": "model", "session_started": "start", "active": 1, "compacted": 0}
    original = a.connection.execute
    class ConnectionProxy:
        def execute(self, sql, params=()):
            if "SELECT id, shard" in sql: return OneShot()
            return original(sql, params)
    a.connection = ConnectionProxy()
    assert a._stored_state_digest("a") == target("a", 1, 1, [row(1, "needle")]).state_digest


def test_existing_db_missing_metadata_is_corruption_but_empty_db_initializes(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path); c.execute("CREATE TABLE arbitrary(x)"); c.commit(); c.close()
    with pytest.raises(CacheCorruptionError): SearchMigrationAdapter(path, InMemoryHydrator([]), declared_shards=set())
    assert SearchMigrationAdapter(tmp_path / "new.db", InMemoryHydrator([]), declared_shards=set())


def test_prior_schema_generation_fails_closed_on_open(tmp_path):
    path = tmp_path / "prior-schema.db"
    current = SearchMigrationAdapter(path, InMemoryHydrator([]), declared_shards=set())
    current.connection.execute("UPDATE search_adapter_metadata SET value='9' WHERE key='schema_generation'")
    current.connection.commit()
    current.close()
    with pytest.raises(CacheCorruptionError):
        SearchMigrationAdapter(path, InMemoryHydrator([]), declared_shards=set())


def test_snapshot_is_coherent_when_second_connection_advances_between_validation_and_selection(tmp_path):
    path = tmp_path / "race.db"; old, new = row(1, "needle"), row(1, "other")
    first = SearchMigrationAdapter(path, InMemoryHydrator([old]), declared_shards={"a"})
    first.apply_target(target("a", 1, 1, [old]), [old]); second = SearchMigrationAdapter(path, InMemoryHydrator([new]), declared_shards={"a"})
    original = first._availability; advanced = []
    def race(targets):
        result = original(targets)
        if not advanced:
            advanced.append(True)
            with pytest.raises(DerivedCacheError):
                second.apply_target(target("a", 1, 2, [new], SearchMigrationAdapter.state_digest([old])), [new])
        return result
    first._availability = race
    result = first.search("needle", observed_targets=observed(first))
    assert result.available and [h["id"] for h in result.hits] == [1] and result.candidate_count == 1


def test_ordinary_search_attestation_is_main_database_read_only(tmp_path):
    a = adapter(tmp_path, [row(1, "needle")])
    before = (a.connection.execute("PRAGMA schema_version").fetchone()[0], a.connection.execute("PRAGMA data_version").fetchone()[0])
    writes = {
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
    }
    actions = []
    a.connection.set_authorizer(lambda action, *_: actions.append(action) or sqlite3.SQLITE_OK)
    try:
        assert search(a, "needle").available
    finally:
        a.connection.set_authorizer(None)
    after = (a.connection.execute("PRAGMA schema_version").fetchone()[0], a.connection.execute("PRAGMA data_version").fetchone()[0])
    vocab = [r["name"] for r in a.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5vocab%'")]
    assert not (set(actions) & writes)
    assert after == before
    assert vocab == ["search_adapter_unicode_vocab", "search_adapter_trigram_vocab"]


def test_two_adapter_ordinary_searches_share_read_snapshot_without_vocab_ddl_contention(tmp_path):
    path, rows = tmp_path / "cache.db", [row(1, "needle")]
    first = adapter(tmp_path, rows)
    second = SearchMigrationAdapter(path, InMemoryHydrator(rows), declared_shards={"a"})
    targets = observed(first)
    entered, release, results = threading.Event(), threading.Event(), []
    original = first._postings_digest

    def pause_after_first_digest(connection, table, *, shard=None):
        digest = original(connection, table, shard=shard)
        if table == "search_adapter_unicode" and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return digest

    first._postings_digest = pause_after_first_digest
    worker = threading.Thread(target=lambda: results.append(first.search("needle", observed_targets=targets)))
    worker.start()
    assert entered.wait(5)
    concurrent = second.search("needle", observed_targets=targets)
    release.set(); worker.join(5)
    assert concurrent.available and results[0].available
    vocab = [r["name"] for r in first.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5vocab%'")]
    assert vocab == ["search_adapter_unicode_vocab", "search_adapter_trigram_vocab"]


def test_search_fails_closed_while_another_connection_holds_exclusive_writer_reservation(tmp_path):
    path, rows = tmp_path / "cache.db", [row(1, "needle")]
    first = adapter(tmp_path, rows)
    second = SearchMigrationAdapter(path, InMemoryHydrator(rows), declared_shards={"a"})
    targets = observed(first)
    first.connection.execute("BEGIN EXCLUSIVE")
    try:
        result = second.search("needle", observed_targets=targets)
        assert (result.error.code, result.hits, result.hydration_count) == ("cache_read_failure", [], 0)
    finally:
        first.connection.rollback()
    assert second.search("needle", observed_targets=targets).available


def test_order_has_unique_id_tie_breaker_across_reopen_and_optimize(tmp_path):
    rows = [row(i, "needle", timestamp="2026-07-31T00:00:00+00:00") for i in range(10, 0, -1)]
    a = adapter(tmp_path, rows); before = [h["id"] for h in search(a, "needle", limit=5).hits]
    a.optimize(); a.close()
    reopened = SearchMigrationAdapter(tmp_path / "cache.db", InMemoryHydrator(rows), declared_shards={"a"})
    assert before == [1, 2, 3, 4, 5] == [h["id"] for h in search(reopened, "needle", limit=5).hits]
    assert set(before).isdisjoint({h["id"] for h in search(reopened, "needle", limit=5, offset=5).hits})


@pytest.mark.parametrize("query", ["中文", "abc中文文", "中文abc", "大别山abc"])
def test_cjk_bearing_and_mixed_tokens_are_fail_closed(tmp_path, query):
    result = search(adapter(tmp_path, [row(1, "大别山 needle")]), query)
    assert result.error.code == "short_cjk_unsupported"


def test_rebuild_streams_and_rolls_back_on_failure(tmp_path):
    old = row(1, "old"); a = adapter(tmp_path, [old])
    new = row(2, "new"); a.inject_failure("mid_rebuild")
    with pytest.raises(DerivedCacheError) as err:
        a.rebuild({"a": target("a", 2, 1, [new])}, {"a": iter([new])})
    assert str(err.value) == "derived cache unavailable"
    assert [h["id"] for h in search(a, "old").hits] == [1]


def test_rebuild_rejects_stale_same_generation_watermark_and_preserves_cache(tmp_path):
    old, stale = row(1, "old needle"), row(1, "stale needle")
    a = adapter(tmp_path, [old])
    committed = target("a", 1, 100, [old])
    a.rebuild({"a": committed}, {"a": [old]})
    before = tuple(a.connection.execute("SELECT generation,watermark,state_digest FROM search_adapter_shards").fetchone())
    with pytest.raises(ValueError, match="stale"):
        a.rebuild({"a": target("a", 1, 1, [stale])}, {"a": [stale]})
    assert tuple(a.connection.execute("SELECT generation,watermark,state_digest FROM search_adapter_shards").fetchone()) == before
    assert [hit["id"] for hit in search(a, "old", observed_targets={"a": committed}).hits] == [1]


def test_rebuild_same_watermark_requires_exact_attested_replay(tmp_path):
    old, conflicting = row(1, "old needle"), row(1, "conflicting needle")
    a = adapter(tmp_path, [old])
    committed = target("a", 1, 100, [old])
    a.rebuild({"a": committed}, {"a": [old]})
    before = tuple(a.connection.execute("SELECT generation,watermark,state_digest FROM search_adapter_shards").fetchone())
    with pytest.raises(ValueError, match="conflicting"):
        a.rebuild({"a": target("a", 1, 100, [conflicting])}, {"a": [conflicting]})
    assert tuple(a.connection.execute("SELECT generation,watermark,state_digest FROM search_adapter_shards").fetchone()) == before
    a.rebuild({"a": committed}, {"a": [old]})
    assert [hit["id"] for hit in search(a, "old", observed_targets={"a": committed}).hits] == [1]


def test_undeclared_complete_row_is_unattested_for_search_and_exact_replay(tmp_path):
    canonical, extra = row(1, "declared needle"), row(99, "undeclared needle")
    a = adapter(tmp_path, [canonical])
    committed = target("a", 1, 100, [canonical])
    a.rebuild({"a": committed}, {"a": [canonical]})
    # The authoritative input has a target only for declared shard a. The
    # complete extra row and both postings exist only in the local cache.
    a._upsert("undeclared", extra)
    a.connection.commit()
    a.hydrator = InMemoryHydrator([canonical, extra])
    result = search(a, "undeclared", observed_targets={"a": committed})
    assert (result.error.code, result.hits, result.candidate_count, result.hydration_count) == ("cache_corrupt", [], 0, 0)
    with pytest.raises(CacheCorruptionError):
        a.rebuild({"a": committed}, {"a": [canonical]})
    assert a.connection.execute("SELECT shard FROM search_adapter_rows WHERE id=99").fetchone()[0] == "undeclared"
    for table in ("search_adapter_unicode", "search_adapter_trigram"):
        assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=99").fetchone()[0] == 99


@pytest.mark.parametrize("table", ["search_adapter_unicode", "search_adapter_trigram"])
def test_incremental_exact_replay_rejects_actual_orphan_posting_without_mutation(tmp_path, table):
    canonical = row(1, "needle")
    a = adapter(tmp_path, [canonical])
    committed = target("a", 1, 100, [canonical], SearchMigrationAdapter.state_digest([canonical]))
    a.apply_target(committed, [canonical])
    a.connection.execute(f"INSERT INTO {table}(rowid,indexed) VALUES (99,'orphan needle')")
    a.connection.commit()
    with pytest.raises(CacheCorruptionError):
        a.apply_target(committed, [canonical])
    assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=99").fetchone()[0] == 99
    assert a.connection.execute("SELECT watermark FROM search_adapter_shards WHERE shard='a'").fetchone()[0] == 100


def test_incremental_exact_replay_rejects_actual_undeclared_complete_row_without_mutation(tmp_path):
    canonical, extra = row(1, "declared needle"), row(99, "undeclared needle")
    a = adapter(tmp_path, [canonical])
    committed = target("a", 1, 100, [canonical], SearchMigrationAdapter.state_digest([canonical]))
    a.apply_target(committed, [canonical])
    a._upsert("undeclared", extra)
    a.connection.commit()
    with pytest.raises(CacheCorruptionError):
        a.apply_target(committed, [canonical])
    assert a.connection.execute("SELECT shard FROM search_adapter_rows WHERE id=99").fetchone()[0] == "undeclared"
    for table in ("search_adapter_unicode", "search_adapter_trigram"):
        assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=99").fetchone()[0] == 99


def _assert_higher_watermark_rejection_preserves_cache(a, advanced, evidence):
    before = tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone())
    with pytest.raises(CacheCorruptionError):
        a.apply_target(advanced, [row(2, "canonical new needle")])
    assert tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone()) == before
    assert tuple(a.connection.execute("SELECT COUNT(*) FROM search_adapter_rows WHERE id=99").fetchone()) + tuple(
        a.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE rowid=99").fetchone()[0]
        for table in ("search_adapter_unicode", "search_adapter_trigram")
    ) == evidence
    assert a.connection.execute("SELECT id FROM search_adapter_rows WHERE id=2").fetchone() is None
    for table in ("search_adapter_unicode", "search_adapter_trigram"):
        assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=2").fetchone() is None


def test_higher_watermark_rejects_actual_orphan_unicode_posting_without_mutation(tmp_path):
    old, new = row(1, "old needle"), row(2, "canonical new needle")
    a = adapter(tmp_path, [old])
    a.connection.execute("INSERT INTO search_adapter_unicode(rowid,indexed) VALUES (99,'orphan needle')")
    a.connection.commit()
    _assert_higher_watermark_rejection_preserves_cache(
        a, target("a", 1, 2, [old, new], SearchMigrationAdapter.state_digest([old])), (0, 1, 0),
    )


def test_higher_watermark_rejects_actual_orphan_trigram_posting_without_mutation(tmp_path):
    old, new = row(1, "old needle"), row(2, "canonical new needle")
    a = adapter(tmp_path, [old])
    a.connection.execute("INSERT INTO search_adapter_trigram(rowid,indexed) VALUES (99,'orphan needle')")
    a.connection.commit()
    _assert_higher_watermark_rejection_preserves_cache(
        a, target("a", 1, 2, [old, new], SearchMigrationAdapter.state_digest([old])), (0, 0, 1),
    )


def test_higher_watermark_rejects_actual_undeclared_complete_row_without_mutation(tmp_path):
    old, new, extra = row(1, "old needle"), row(2, "canonical new needle"), row(99, "undeclared needle")
    a = adapter(tmp_path, [old])
    a._upsert("undeclared", extra)
    a.connection.commit()
    _assert_higher_watermark_rejection_preserves_cache(
        a, target("a", 1, 2, [old, new], SearchMigrationAdapter.state_digest([old])), (1, 1, 1),
    )


def test_higher_watermark_post_apply_attestation_rolls_back_injected_orphan(tmp_path):
    old, new = row(1, "old needle"), row(2, "canonical new needle")
    a = adapter(tmp_path, [old])
    before = tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone())
    a.inject_failure("post_apply_orphan")
    with pytest.raises(CacheCorruptionError):
        a.apply_target(target("a", 1, 2, [old, new], SearchMigrationAdapter.state_digest([old])), [new])
    assert tuple(a.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard='a'").fetchone()) == before
    assert [record["id"] for record in a.connection.execute("SELECT id FROM search_adapter_rows ORDER BY id")] == [1]
    assert a.connection.execute("SELECT rowid FROM search_adapter_unicode WHERE rowid=99").fetchone() is None
    assert a.connection.execute("SELECT rowid FROM search_adapter_trigram WHERE rowid=99").fetchone() is None


def test_higher_watermark_clean_apply_attests_and_remains_searchable(tmp_path):
    old, new = row(1, "old needle"), row(2, "canonical new needle")
    a = adapter(tmp_path, [old])
    advanced = target("a", 1, 2, [old, new], SearchMigrationAdapter.state_digest([old]))
    a.apply_target(advanced, [new])
    a.hydrator = InMemoryHydrator([old, new])
    assert a.connection.execute("SELECT watermark FROM search_adapter_shards WHERE shard='a'").fetchone()[0] == 2
    assert [hit["id"] for hit in search(a, "canonical").hits] == [2]


def test_incremental_multi_shard_initialization_attests_once_metadata_is_complete(tmp_path):
    left, right = row(1, "left needle"), row(2, "right needle")
    a = SearchMigrationAdapter(tmp_path / "multi.db", InMemoryHydrator([left, right]), declared_shards={"a", "b"})
    left_target, right_target = target("a", 1, 1, [left]), target("b", 1, 1, [right])
    a.apply_target(left_target, [left])
    assert [record["shard"] for record in a.connection.execute("SELECT shard FROM search_adapter_shards")] == ["a"]
    a.apply_target(right_target, [right])
    assert [hit["id"] for hit in search(a, "right", observed_targets={"a": left_target, "b": right_target}).hits] == [2]


@pytest.mark.parametrize("extra_shard", ["x", "", None])
def test_incremental_multi_shard_initialization_rejects_undeclared_metadata_before_mutation(tmp_path, extra_shard):
    left, right = row(1, "left needle"), row(2, "right needle")
    a = SearchMigrationAdapter(tmp_path / "multi-corrupt.db", InMemoryHydrator([left, right]), declared_shards={"a", "b"})
    left_target, right_target = target("a", 10, 1, [left]), target("b", 10, 1, [right])
    a.apply_target(left_target, [left])
    a.connection.execute(
        "INSERT INTO search_adapter_shards VALUES (?,?,?,?,?,?)",
        (extra_shard, 10, 1, "extra-state", "extra-unicode", "extra-trigram"),
    )
    a.connection.commit()
    before_metadata = [tuple(record) for record in a.connection.execute("SELECT * FROM search_adapter_shards ORDER BY shard")]
    before_rows = [tuple(record) for record in a.connection.execute("SELECT * FROM search_adapter_rows ORDER BY id")]
    before_evidence = tuple(
        a.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("search_adapter_unicode", "search_adapter_trigram")
    )
    with pytest.raises(CacheCorruptionError):
        a.apply_target(right_target, [right])
    assert [tuple(record) for record in a.connection.execute("SELECT * FROM search_adapter_shards ORDER BY shard")] == before_metadata
    assert [tuple(record) for record in a.connection.execute("SELECT * FROM search_adapter_rows ORDER BY id")] == before_rows
    assert tuple(
        a.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("search_adapter_unicode", "search_adapter_trigram")
    ) == before_evidence
    assert a.connection.execute("SELECT id FROM search_adapter_rows WHERE id=?", (right.id,)).fetchone() is None
    for table in ("search_adapter_unicode", "search_adapter_trigram"):
        assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=?", (right.id,)).fetchone() is None


@pytest.mark.parametrize("field", ["unicode_postings_digest", "trigram_postings_digest"])
def test_incremental_exact_replay_rejects_conflicting_target_postings_digest_without_mutation(tmp_path, field):
    canonical = row(1, "needle")
    a = adapter(tmp_path, [canonical])
    committed = target("a", 1, 100, [canonical], SearchMigrationAdapter.state_digest([canonical]))
    a.apply_target(committed, [canonical])
    conflicting = replace(committed, **{field: "0" * 64})
    with pytest.raises(CacheCorruptionError, match="conflicting shard replay"):
        a.apply_target(conflicting, [canonical])
    stored = a.connection.execute("SELECT * FROM search_adapter_shards WHERE shard='a'").fetchone()
    assert (stored["state_digest"], stored["unicode_postings_digest"], stored["trigram_postings_digest"]) == (
        committed.state_digest, committed.unicode_postings_digest, committed.trigram_postings_digest,
    )


@pytest.mark.parametrize("table", ["search_adapter_unicode", "search_adapter_trigram"])
def test_orphan_fts_rowid_is_unattested_for_search_and_exact_replay(tmp_path, table):
    canonical = row(1, "needle")
    a = adapter(tmp_path, [canonical])
    committed = target("a", 1, 100, [canonical])
    a.rebuild({"a": committed}, {"a": [canonical]})
    a.connection.execute(f"INSERT INTO {table}(rowid,indexed) VALUES (99,'orphan needle')")
    a.connection.commit()
    result = search(a, "needle", observed_targets={"a": committed})
    assert (result.error.code, result.hits, result.candidate_count, result.hydration_count) == ("cache_corrupt", [], 0, 0)
    with pytest.raises(CacheCorruptionError):
        a.rebuild({"a": committed}, {"a": [canonical]})
    assert a.connection.execute(f"SELECT rowid FROM {table} WHERE rowid=99").fetchone()[0] == 99


def test_rebuild_accepts_higher_watermark_and_higher_generation(tmp_path):
    old, advanced, reset = row(1, "old"), row(1, "advanced"), row(1, "reset")
    a = adapter(tmp_path, [old])
    at_100 = target("a", 1, 100, [old])
    a.rebuild({"a": at_100}, {"a": [old]})
    at_101 = target("a", 1, 101, [advanced])
    a.rebuild({"a": at_101}, {"a": [advanced]})
    reset_generation = target("a", 2, 1, [reset])
    a.rebuild({"a": reset_generation}, {"a": [reset]})
    a.hydrator = InMemoryHydrator([reset])
    assert [hit["id"] for hit in search(a, "reset", observed_targets={"a": reset_generation}).hits] == [1]


def test_rebuild_rejects_partial_existing_shard_set_without_mutation(tmp_path):
    left, right = row(1, "left"), row(2, "right")
    a = SearchMigrationAdapter(tmp_path / "partial.db", InMemoryHydrator([left, right]), declared_shards={"a", "b"})
    a.apply_target(target("a", 1, 1, [left]), [left])
    targets = {"a": target("a", 1, 2, [left]), "b": target("b", 1, 2, [right])}
    with pytest.raises(ValueError, match="shard set"):
        a.rebuild(targets, {"a": [left], "b": [right]})
    assert [record["shard"] for record in a.connection.execute("SELECT shard FROM search_adapter_shards")] == ["a"]
    assert [record["id"] for record in a.connection.execute("SELECT id FROM search_adapter_rows")] == [1]


@pytest.mark.parametrize("timestamp", ["not-a-time", sqlite3.Binary(b"not-a-time")])
def test_malformed_stored_timestamp_returns_cache_corrupt_without_hydration(tmp_path, timestamp):
    canonical = row(1, "needle")
    a = adapter(tmp_path, [canonical])
    a.connection.execute("UPDATE search_adapter_rows SET timestamp=? WHERE id=1", (timestamp,))
    a.connection.commit()
    result = search(a, "needle", observed_targets={"a": target("a", 1, 1, [canonical])})
    assert (result.error.code, result.hits, result.candidate_count, result.hydration_count) == ("cache_corrupt", [], 0, 0)


def test_rebuild_bounds_and_cross_shard_duplicate_rejection(tmp_path):
    rows = [row(1, "a"), row(2, "b")]
    a = SearchMigrationAdapter(tmp_path / "cache.db", InMemoryHydrator(rows), declared_shards={"a", "b"}, max_rebuild_rows=1)
    with pytest.raises(ValueError, match="bound"):
        a.rebuild({"a": target("a", 1, 1, [rows[0]]), "b": target("b", 1, 1, [rows[1]])}, {"a": iter([rows[0]]), "b": iter([rows[1]])})
    a = SearchMigrationAdapter(tmp_path / "two.db", InMemoryHydrator(rows), declared_shards={"a", "b"})
    with pytest.raises(ValueError, match="duplicate"):
        a.rebuild({"a": target("a", 1, 1, [rows[0]]), "b": target("b", 1, 1, [rows[0]])}, {"a": [rows[0]], "b": [rows[0]]})


def test_cross_thread_search_write_close_and_stable_closed_failure(tmp_path):
    old, new = row(1, "needle"), row(1, "new")
    a = adapter(tmp_path, [old]); results = []
    def worker():
        results.append(search(a, "needle").available)
        a.apply_target(target("a", 1, 2, [new], SearchMigrationAdapter.state_digest([old])), [new])
    t = threading.Thread(target=worker); t.start(); t.join(); assert results == [True]
    closer = threading.Thread(target=a.close); closer.start(); closer.join()
    assert a.search("new", observed_targets={"a": ShardTarget("a", 1, 2, SearchMigrationAdapter.state_digest([new]))}).error.code == "cache_closed"
    with pytest.raises(CacheClosedError): a.optimize()


def test_capability_and_public_database_errors_are_stable(tmp_path):
    def unsupported(_): raise UnsupportedSQLiteError("unsupported sqlite capabilities")
    with pytest.raises(UnsupportedSQLiteError) as err:
        SearchMigrationAdapter(tmp_path / "bad.db", InMemoryHydrator([]), declared_shards=set(), capability_probe=unsupported)
    assert (err.value.code, str(err.value)) == ("unsupported_sqlite", "unsupported sqlite capabilities")
    a = adapter(tmp_path, [row(1, "needle")]); a.connection.close(); a._closed = True
    assert a.search("needle", observed_targets={"a": ShardTarget("a", 1, 1, SearchMigrationAdapter.state_digest([row(1, "needle")]))}).error.code == "cache_closed"


def test_hydration_metrics_never_count_unaccepted_data(tmp_path):
    class Bad:
        def hydrate(self, ids): return {1: row(1, "needle"), 2: row(2, "needle")}
    a = SearchMigrationAdapter(tmp_path / "cache.db", Bad(), declared_shards={"a"}); r = row(1, "needle")
    a.apply_target(target("a", 1, 1, [r]), [r])
    result = search(a, "needle")
    assert (result.error.code, result.hydration_count) == ("hydration_mismatch", 0)


def test_apply_lock_and_malformed_index_are_stable(tmp_path):
    a = adapter(tmp_path, [row(1, "needle")]); path = tmp_path / "cache.db"; b = SearchMigrationAdapter(path, InMemoryHydrator([]), declared_shards={"a"})
    a.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DerivedCacheError) as err: b.apply_target(target("a", 1, 2, [], SearchMigrationAdapter.state_digest([row(1, "needle")])), [])
        assert str(err.value) == "derived cache unavailable"
    finally: a.connection.rollback()
    a.connection.execute("DROP TABLE search_adapter_unicode"); a.connection.commit()
    assert search(a, "needle").error.code == "cache_read_failure"


# R4 characterization coverage ported to the digest-attested target protocol.
def _oracle(tmp_path):
    db = SessionDB(tmp_path / "oracle.db")
    for session_id, source in (("s-cli", "cli"), ("s-chat", "telegram")):
        db.create_session(session_id, source=source, model="test-model")
    for session_id, role, content, tool_name, timestamp in (
        ("s-cli", "user", "needle alpha deploy", None, 10), ("s-cli", "assistant", "needle beta deploy", "tool-needle", 20),
        ("s-chat", "tool", "other body", "needle-tool", 20), ("s-chat", "user", "chat-send P2.2 file_name", None, 30),
    ):
        db.append_message(session_id, role=role, content=content, tool_name=tool_name, timestamp=timestamp)
    rows = [CanonicalMessage(r["id"], r["session_id"], r["role"], r["content"], r["tool_name"], r["tool_calls"], r["timestamp"], r["source"], r["model"], r["session_started"], r["active"], r["compacted"])
            for r in db._conn.execute("SELECT m.*,s.source,s.model,s.started_at session_started FROM messages m JOIN sessions s ON s.id=m.session_id")]
    return db, rows


@pytest.mark.parametrize("query", ["needle", "needle AND deploy", "needle OR absent", "needle NOT absent", '"needle alpha"', "need*", "chat-send", "P2.2", "file_name"])
@pytest.mark.parametrize("limit,offset,sort", [(0, 0, None), (1, 0, None), (20, 0, "newest"), (100, 1, "oldest"), (300, 0, "bogus"), (500, 0, None)])
@pytest.mark.parametrize("kwargs", [{}, {"source_filter": ["cli"]}, {"exclude_sources": ["cli"]}, {"role_filter": ["assistant", "tool"]}])
def test_r4_sessiondb_oracle_cartesian_digest_attested(tmp_path, query, limit, offset, sort, kwargs):
    db, rows = _oracle(tmp_path)
    try:
        a = adapter(tmp_path, rows)
        expected = db.search_messages(query, limit=limit, offset=offset, sort=sort, **kwargs)
        actual = search(a, query, limit=limit, offset=offset, sort=sort, **kwargs)
        assert actual.available
        assert [{k: v for k, v in hit.items() if k != "rank"} for hit in actual.hits] == [{k: v for k, v in hit.items() if k != "context"} for hit in expected]
    finally:
        db.close()


@pytest.mark.parametrize("kwargs", [{"source_filter": []}, {"exclude_sources": []}, {"role_filter": []}, {"sort": "not-a-sort"}, {"limit": 0}, {"limit": -1}, {"include_inactive": False}])
def test_r4_edge_filter_and_inactive_contract(tmp_path, kwargs):
    db, rows = _oracle(tmp_path)
    db._conn.execute("UPDATE messages SET active=0, compacted=0 WHERE id=?", (rows[0].id,)); db._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE id=?", (rows[1].id,)); db._conn.commit()
    rows[0] = replace(rows[0], active=0, compacted=0); rows[1] = replace(rows[1], active=0, compacted=1)
    try:
        actual = search(adapter(tmp_path, rows), "needle", **kwargs)
        expected = db.search_messages("needle", **kwargs)
        if kwargs.get("limit") == -1: assert actual.error.code == "invalid_pagination"
        else: assert [{k: v for k, v in h.items() if k != "rank"} for h in actual.hits] == [{k: v for k, v in h.items() if k != "context"} for h in expected]
    finally:
        db.close()


@pytest.mark.parametrize("query", ["needle", "needle OR absent", '"needle alpha"', "need*", "tool-needle"])
def test_r4_ready_query_rank_and_snippet_shape(tmp_path, query):
    db, rows = _oracle(tmp_path)
    try: assert search(adapter(tmp_path, rows), query).available
    finally: db.close()


@pytest.mark.parametrize("query", ["中文", "x中文", "中文x"])
def test_r4_short_and_mixed_cjk_contract(tmp_path, query):
    result = search(adapter(tmp_path, [row(1, "中文 needle")]), query)
    assert (result.error.code, result.error.detail) == ("short_cjk_unsupported", "short or mixed CJK queries are unsupported")


def test_numeric_timestamp_sort_matches_sessiondb_and_preserves_canonical_timestamp(tmp_path):
    db = SessionDB(tmp_path / "timestamps.db"); db.create_session("s", source="cli", model="m")
    db.append_message("s", role="user", content="needle", timestamp=9); db.append_message("s", role="user", content="needle", timestamp=10)
    rows = [CanonicalMessage(r["id"], r["session_id"], r["role"], r["content"], r["tool_name"], r["tool_calls"], r["timestamp"], r["source"], r["model"], r["session_started"], r["active"], r["compacted"])
            for r in db._conn.execute("SELECT m.*,s.source,s.model,s.started_at session_started FROM messages m JOIN sessions s ON s.id=m.session_id")]
    try:
        a = adapter(tmp_path, rows)
        for sort in ("newest", "oldest"):
            assert [h["id"] for h in search(a, "needle", sort=sort).hits] == [h["id"] for h in db.search_messages("needle", sort=sort)]
        assert [h["timestamp"] for h in search(a, "needle", sort="oldest").hits] == [9.0, 10.0]
    finally: db.close()


def test_invalid_timestamp_rolls_back_before_mutation(tmp_path):
    old, bad = row(1, "old"), row(2, "bad", timestamp="not-a-time"); a = adapter(tmp_path, [old])
    with pytest.raises(ValueError, match="orderable"):
        a.apply_target(target("a", 1, 2, [old, bad], SearchMigrationAdapter.state_digest([old])), [old, bad])
    assert [h["id"] for h in search(a, "old").hits] == [1]


def test_streaming_digest_matches_convenience_and_rejects_bad_id_order():
    rows = [row(1, "a"), row(2, "b"), row(3, "c")]
    assert SearchMigrationAdapter.streaming_state_digest(iter(rows)) == SearchMigrationAdapter.state_digest(rows)
    for bad in ([rows[1], rows[0]], [rows[0], rows[0]]):
        with pytest.raises(ValueError, match="strictly"): SearchMigrationAdapter.streaming_state_digest(iter(bad))


def test_r4_count_query_has_no_order_by(tmp_path):
    a = adapter(tmp_path, [row(1, "needle"), row(2, "needle")]); statements = []; a.connection.set_trace_callback(statements.append)
    result = search(a, "needle", sort="newest", limit=1)
    assert "ORDER BY" not in next(s for s in statements if "COUNT(*)" in s).upper()
    assert (result.candidate_count, len(result.hits)) == (2, 1)


def test_capability_cleanup_error_cannot_mask_unsupported(tmp_path):
    class CleanupBrokenConnection:
        def execute(self, sql):
            if "DROP TABLE" in sql: raise sqlite3.DatabaseError("cleanup")
            raise sqlite3.DatabaseError("probe")
    with pytest.raises(UnsupportedSQLiteError): SearchMigrationAdapter._probe_capabilities(CleanupBrokenConnection())


def test_r4_replay_update_delete_reinsert_and_rebuild_idempotence(tmp_path):
    original, updated = row(4, "first needle"), row(4, "second needle", timestamp="2026-07-31T01:00:00+00:00")
    a = adapter(tmp_path, [original]); old = SearchMigrationAdapter.state_digest([original])
    a.apply_target(target("a", 1, 2, [updated], old), [updated]); a.apply_target(target("a", 1, 2, [updated]), [updated])
    a.apply_target(target("a", 1, 3, [], SearchMigrationAdapter.state_digest([updated])), [], delete_ids=[4])
    a.apply_target(target("a", 1, 4, [updated], SearchMigrationAdapter.state_digest([])), [updated])
    a.rebuild({"a": target("a", 2, 1, [updated])}, {"a": [updated]}); a.rebuild({"a": target("a", 2, 1, [updated])}, {"a": [updated]})
    a.hydrator = InMemoryHydrator([updated])
    assert [h["id"] for h in search(a, "second").hits] == [4]


def test_r4_local_ahead_and_generation_change_fail_closed(tmp_path):
    original, replacement = row(1, "one"), row(1, "two")
    a = adapter(tmp_path, [original])
    assert search(a, "one", observed_targets={"a": target("a", 1, 0, [original])}).error.code == "watermark_mismatch"
    with pytest.raises(ValueError, match="generation"):
        a.apply_target(target("a", 2, 2, [replacement], SearchMigrationAdapter.state_digest([original])), [replacement])
    assert [h["id"] for h in search(a, "one").hits] == [1]


def test_r4_cross_shard_ownership_rejects_upsert_and_delete(tmp_path):
    owner, conflict = row(1, "owned"), row(1, "conflict")
    a = SearchMigrationAdapter(tmp_path / "owners.db", InMemoryHydrator([owner, conflict]), declared_shards={"a", "b"})
    a.apply_target(target("a", 1, 1, [owner]), [owner])
    for rows, deletes in (([conflict], []), ([], [1])):
        with pytest.raises(ValueError, match="owned"):
            a.apply_target(target("b", 1, 1, rows), rows, delete_ids=deletes)
    assert a.connection.execute("SELECT shard FROM search_adapter_rows WHERE id=1").fetchone()[0] == "a"


def test_r4_hydrates_only_page_and_counts_all_candidates(tmp_path):
    class Recording(InMemoryHydrator):
        def __init__(self, rows): super().__init__(rows); self.requests = []
        def hydrate(self, ids): self.requests.append(list(ids)); return super().hydrate(self.requests[-1])
    rows = [row(i, f"needle {i}") for i in range(300)]; hydrator = Recording(rows)
    a = SearchMigrationAdapter(tmp_path / "page.db", hydrator, declared_shards={"a"}); a.apply_target(target("a", 1, 1, rows), rows); hydrator.requests.clear()
    result = search(a, "needle", limit=7, offset=200)
    assert (result.candidate_count, result.hydration_count, len(hydrator.requests[0])) == (300, 7, 7)


def test_r4_three_character_cjk_remains_sessiondb_compatible(tmp_path):
    db = SessionDB(tmp_path / "cjk.db"); db.create_session("cjk", source="cli", model="m"); db.append_message("cjk", role="user", content="大别山项目")
    record = db._conn.execute("SELECT m.*,s.source,s.model,s.started_at session_started FROM messages m JOIN sessions s ON s.id=m.session_id").fetchone()
    canonical = CanonicalMessage(record["id"], record["session_id"], record["role"], record["content"], record["tool_name"], record["tool_calls"], record["timestamp"], record["source"], record["model"], record["session_started"], record["active"], record["compacted"])
    try:
        actual, expected = search(adapter(tmp_path, [canonical]), "大别山"), db.search_messages("大别山")
        assert [{k: v for k, v in h.items() if k != "rank"} for h in actual.hits] == [{k: v for k, v in h.items() if k != "context"} for h in expected]
    finally: db.close()
