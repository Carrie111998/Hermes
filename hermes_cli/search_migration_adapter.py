"""Inert, local-only derived-cache adapter for future search migration work.

This module is deliberately not imported by SessionDB or any runtime path.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Iterable, Mapping, Protocol

from hermes_state import SessionDB

SCHEMA_GENERATION = 10
MIN_SQLITE_VERSION = (3, 43, 0)  # contentless-delete FTS5 support
MAX_PAGE_SIZE = 500
MAX_OFFSET = 1_000_000
MAX_DECLARED_SHARDS = 4_096
MAX_REBUILD_ROWS = 2_000_000
MAX_REBUILD_INDEXED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_BATCH_ROWS = 10_000
_MAX_BATCH_DELETES = 10_000
_MAX_ROW_INDEXED_BYTES = 16 * 1024 * 1024
_MAX_BATCH_INDEXED_BYTES = 512 * 1024 * 1024
_MAX_PAGE_INDEXED_BYTES = 64 * 1024 * 1024
_UNAVAILABLE = "derived cache unavailable"


class DerivedCacheError(RuntimeError):
    code = "derived_cache_error"

    def __init__(self, detail: str = _UNAVAILABLE):
        super().__init__(detail)


class UnsupportedSQLiteError(DerivedCacheError):
    code = "unsupported_sqlite"


class CacheClosedError(DerivedCacheError):
    code = "cache_closed"


class CacheLockError(DerivedCacheError):
    code = "cache_locked"


class CacheCorruptionError(DerivedCacheError):
    code = "cache_corrupt"


@dataclass(frozen=True)
class CanonicalMessage:
    id: int
    session_id: str
    role: str
    content: str | None
    tool_name: str | None
    tool_calls: str | None
    timestamp: str
    source: str | None
    model: str | None
    session_started: str | None
    active: int | None
    compacted: int | None

    @property
    def indexed_text(self) -> str:
        return " ".join((self.content or "", self.tool_name or "", self.tool_calls or ""))


class AuthoritativeHydrator(Protocol):
    def hydrate(self, ids: Iterable[int]) -> Mapping[int, CanonicalMessage]: ...


class InMemoryHydrator:
    def __init__(self, rows: Iterable[CanonicalMessage]):
        self._rows = {row.id: row for row in rows}

    def hydrate(self, ids: Iterable[int]) -> Mapping[int, CanonicalMessage]:
        return {row_id: self._rows[row_id] for row_id in ids if row_id in self._rows}


@dataclass(frozen=True)
class ShardTarget:
    shard: str
    generation: int
    watermark: int
    state_digest: str
    previous_state_digest: str | None = None
    unicode_postings_digest: str = ""
    trigram_postings_digest: str = ""


@dataclass(frozen=True)
class SearchError:
    code: str
    detail: str


@dataclass(frozen=True)
class SearchResult:
    hits: list[dict]
    error: SearchError | None = None
    candidate_count: int = 0
    hydration_count: int = 0

    @property
    def available(self) -> bool:
        return self.error is None


class SearchMigrationAdapter:
    """Thread-safe, contentless-FTS cache. Public methods never expose SQLite text."""

    def __init__(
        self, path: str | Path, hydrator: AuthoritativeHydrator, *, declared_shards: set[str],
        capability_probe: Callable[[sqlite3.Connection], None] | None = None,
        max_rebuild_rows: int = MAX_REBUILD_ROWS,
        max_rebuild_indexed_bytes: int = MAX_REBUILD_INDEXED_BYTES,
        max_row_indexed_bytes: int = _MAX_ROW_INDEXED_BYTES,
        max_batch_indexed_bytes: int = _MAX_BATCH_INDEXED_BYTES,
        max_page_indexed_bytes: int = _MAX_PAGE_INDEXED_BYTES,
    ):
        if len(declared_shards) > MAX_DECLARED_SHARDS:
            raise ValueError("declared shard count exceeds bound")
        self.path, self.hydrator = Path(path), hydrator
        self.declared_shards = frozenset(declared_shards)
        self.max_rebuild_rows, self.max_rebuild_indexed_bytes = max_rebuild_rows, max_rebuild_indexed_bytes
        self.max_row_indexed_bytes = max_row_indexed_bytes
        self.max_batch_indexed_bytes = max_batch_indexed_bytes
        self.max_page_indexed_bytes = max_page_indexed_bytes
        self._lock, self._closed, self._failures = threading.RLock(), False, set()
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=0.1, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self.connection = connection
            (capability_probe or self._probe_capabilities)(connection)
            self._create_schema()
        except UnsupportedSQLiteError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            raise DerivedCacheError() from None

    @staticmethod
    def _probe_capabilities(connection: sqlite3.Connection) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise UnsupportedSQLiteError("unsupported sqlite capabilities")
        try:
            connection.execute("CREATE VIRTUAL TABLE temp.adapter_probe_u USING fts5(x, tokenize='unicode61')")
            connection.execute("CREATE VIRTUAL TABLE temp.adapter_probe_t USING fts5(x, tokenize='trigram')")
            connection.execute("CREATE VIRTUAL TABLE temp.adapter_probe_d USING fts5(x, content='', contentless_delete=1)")
            connection.execute("CREATE VIRTUAL TABLE temp.adapter_probe_v USING fts5vocab(adapter_probe_u, 'instance')")
            connection.execute("INSERT INTO adapter_probe_d(rowid, x) VALUES (1, 'x')")
            connection.execute("DELETE FROM adapter_probe_d WHERE rowid = 1")
        except sqlite3.DatabaseError:
            raise UnsupportedSQLiteError("unsupported sqlite capabilities") from None
        finally:
            for name in ("adapter_probe_v", "adapter_probe_u", "adapter_probe_t", "adapter_probe_d"):
                try:
                    connection.execute(f"DROP TABLE IF EXISTS temp.{name}")
                except sqlite3.DatabaseError:
                    # A failed cleanup must not mask the capability result.
                    pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise CacheClosedError()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def inject_failure(self, name: str) -> None:
        self._failures.add(name)

    @contextmanager
    def _write_transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                try:
                    self.connection.rollback()
                except sqlite3.DatabaseError:
                    pass
            raise

    def _create_schema(self) -> None:
        objects = self.connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        metadata_exists = any(row["name"] == "search_adapter_metadata" for row in objects)
        if objects and not metadata_exists:
            raise CacheCorruptionError()
        self.connection.execute("CREATE TABLE IF NOT EXISTS search_adapter_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        schema = self.connection.execute("SELECT value FROM search_adapter_metadata WHERE key='schema_generation'").fetchone()
        if metadata_exists and schema is None:
            raise CacheCorruptionError()
        if schema is not None and schema["value"] != str(SCHEMA_GENERATION):
            raise CacheCorruptionError()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS search_adapter_rows (
              id INTEGER PRIMARY KEY, shard TEXT NOT NULL, session_id TEXT NOT NULL, role TEXT NOT NULL,
              timestamp BLOB NOT NULL, timestamp_order REAL NOT NULL, source TEXT, model TEXT, session_started BLOB, active INTEGER,
              compacted INTEGER, row_digest TEXT NOT NULL, projection_digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS search_adapter_shards (
              shard TEXT PRIMARY KEY, generation INTEGER NOT NULL, watermark INTEGER NOT NULL,
              state_digest TEXT NOT NULL, unicode_postings_digest TEXT NOT NULL, trigram_postings_digest TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_adapter_unicode USING fts5(indexed, content='', contentless_delete=1, tokenize='unicode61');
            CREATE VIRTUAL TABLE IF NOT EXISTS search_adapter_trigram USING fts5(indexed, content='', contentless_delete=1, tokenize='trigram');
            CREATE VIRTUAL TABLE IF NOT EXISTS search_adapter_unicode_vocab USING fts5vocab(search_adapter_unicode, 'instance');
            CREATE VIRTUAL TABLE IF NOT EXISTS search_adapter_trigram_vocab USING fts5vocab(search_adapter_trigram, 'instance');
        """)
        self.connection.execute("INSERT OR IGNORE INTO search_adapter_metadata VALUES ('schema_generation', ?)", (str(SCHEMA_GENERATION),))
        self.connection.commit()

    @staticmethod
    def row_digest(row: CanonicalMessage) -> str:
        payload = {"canonical": asdict(row), "indexed_bytes": row.indexed_text.encode("utf-8").hex()}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _frame(digest, domain: bytes, *parts: object) -> None:
        digest.update(len(domain).to_bytes(4, "big")); digest.update(domain)
        for part in parts:
            value = str(part).encode("utf-8")
            digest.update(len(value).to_bytes(8, "big")); digest.update(value)

    @classmethod
    def projection_digest(cls, shard: str, row: CanonicalMessage, timestamp_order: float | None = None) -> str:
        order = cls._timestamp_order(row.timestamp) if timestamp_order is None else timestamp_order
        digest = hashlib.sha256()
        cls._frame(digest, b"search-projection-v1", row.id, shard, row.session_id, row.role, row.timestamp,
                   order.hex(), row.source, row.model, row.session_started, row.active, row.compacted)
        return digest.hexdigest()

    @classmethod
    def _state_hasher(cls):
        digest = hashlib.sha256(); cls._frame(digest, b"search-state-v1"); return digest

    @classmethod
    def _state_entry(cls, digest, row_id: int, row_digest: str, projection_digest: str) -> None:
        cls._frame(digest, b"search-state-entry-v1", row_id, row_digest, projection_digest)

    @classmethod
    def state_digest(cls, rows: Iterable[CanonicalMessage], shard: str = "a") -> str:
        return cls.streaming_state_digest(iter(sorted(rows, key=lambda row: row.id)), shard)

    @staticmethod
    def streaming_state_digest(rows: Iterable[CanonicalMessage], shard: str = "a") -> str:
        """Hash canonical rows already ordered by strictly increasing ID in O(1) memory."""
        digest = SearchMigrationAdapter._state_hasher()
        previous_id: int | None = None
        first = True
        for row in rows:
            if previous_id is not None and row.id <= previous_id:
                raise ValueError("streaming digest IDs must be strictly increasing")
            SearchMigrationAdapter._state_entry(digest, row.id, SearchMigrationAdapter.row_digest(row), SearchMigrationAdapter.projection_digest(shard, row))
            previous_id, first = row.id, False
        return digest.hexdigest()

    @staticmethod
    def _timestamp_order(timestamp: object) -> float:
        if isinstance(timestamp, bool):
            raise ValueError("timestamp is not orderable")
        if isinstance(timestamp, (int, float)):
            value = float(timestamp)
        elif isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                value = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
            except (TypeError, ValueError, OverflowError):
                raise ValueError("timestamp is not orderable") from None
        else:
            raise ValueError("timestamp is not orderable")
        if not math.isfinite(value):
            raise ValueError("timestamp is not orderable")
        return value

    def _stored_state_digest(self, shard: str) -> str:
        digest = self._state_hasher()
        rows = self.connection.execute("SELECT id, shard, session_id, role, timestamp, timestamp_order, source, model, session_started, active, compacted, row_digest FROM search_adapter_rows WHERE shard=? ORDER BY id", (shard,))
        for row in rows:
            projection = self._stored_projection_digest(row)
            self._state_entry(digest, row["id"], row["row_digest"], projection)
        return digest.hexdigest()

    @classmethod
    def _stored_projection_digest(cls, row: sqlite3.Row | Mapping[str, object]) -> str:
        order = row["timestamp_order"]
        try:
            expected_order = cls._timestamp_order(row["timestamp"])
        except (TypeError, ValueError, OverflowError):
            return "invalid-projection"
        if not isinstance(order, float) or not math.isfinite(order) or order != expected_order:
            return "invalid-projection"
        digest = hashlib.sha256()
        cls._frame(digest, b"search-projection-v1", row["id"], row["shard"], row["session_id"], row["role"], row["timestamp"],
                   order.hex(), row["source"], row["model"], row["session_started"], row["active"], row["compacted"])
        return digest.hexdigest()

    @classmethod
    def _postings_digest(cls, connection: sqlite3.Connection, table: str, *, shard: str | None = None) -> str:
        vocab = {
            "search_adapter_unicode": "search_adapter_unicode_vocab",
            "search_adapter_trigram": "search_adapter_trigram_vocab",
        }.get(table)
        if vocab is None:
            raise ValueError("unknown postings table")
        sql = f"SELECT v.term,v.doc,v.col,v.offset FROM {vocab} v"
        params: tuple[object, ...] = ()
        if shard is not None:
            sql += " JOIN search_adapter_rows r ON r.id=v.doc WHERE r.shard=?"; params = (shard,)
        digest = hashlib.sha256(); cls._frame(digest, b"search-postings-v1", table)
        for posting in connection.execute(sql + " ORDER BY v.term,v.doc,v.col,v.offset", params):
            cls._frame(digest, b"search-posting-v1", posting["term"], posting["doc"], posting["col"], posting["offset"])
        return digest.hexdigest()

    @staticmethod
    def _snippets(tokenizer: str, hydrated: Mapping[int, CanonicalMessage], match: str) -> dict[int, str]:
        """Render snippets in an isolated in-memory database after cache snapshot release."""
        page = sqlite3.connect(":memory:"); page.row_factory = sqlite3.Row
        try:
            page.execute(
                f"CREATE VIRTUAL TABLE adapter_page USING fts5("
                f"content, tool_name, tool_calls, tokenize='{tokenizer}')"
            )
            page.executemany(
                "INSERT INTO adapter_page(rowid,content,tool_name,tool_calls) "
                "VALUES (?,?,?,?)",
                [
                    (r.id, r.content or "", r.tool_name or "", r.tool_calls or "")
                    for r in hydrated.values()
                ],
            )
            return {
                r["rowid"]: r["snippet"]
                for r in page.execute(
                    "SELECT rowid,snippet(adapter_page,-1,'>>>','<<<','...',40) "
                    "snippet FROM adapter_page WHERE adapter_page MATCH ?",
                    (match,),
                )
            }
        finally:
            page.close()

    def _attest_complete_cache(self) -> bool:
        """Verify that per-shard metadata covers every cached row and posting.

        This is deliberately the single actual-cache gate for both reads and
        exact same-watermark replays. The shard joins used for the digest are
        safe only after ownership and rowid symmetry make them a complete
        partition of the cache universe.
        """
        local = {row["shard"]: row for row in self.connection.execute("SELECT * FROM search_adapter_shards")}
        if set(local) != self.declared_shards:
            return False
        if self.declared_shards:
            marks = ",".join("?" for _ in self.declared_shards)
            foreign = self.connection.execute(
                f"SELECT 1 FROM search_adapter_rows WHERE shard IS NULL OR shard NOT IN ({marks}) LIMIT 1",
                tuple(sorted(self.declared_shards)),
            ).fetchone()
        else:
            foreign = self.connection.execute("SELECT 1 FROM search_adapter_rows LIMIT 1").fetchone()
        if foreign:
            return False
        for table in ("search_adapter_unicode", "search_adapter_trigram"):
            mismatch = self.connection.execute(
                f"SELECT id FROM search_adapter_rows WHERE id NOT IN (SELECT rowid FROM {table}) "
                f"UNION SELECT rowid FROM {table} WHERE rowid NOT IN (SELECT id FROM search_adapter_rows)"
            ).fetchone()
            if mismatch:
                return False
        for shard, metadata in local.items():
            actual = self._stored_state_digest(shard)
            unicode = self._postings_digest(self.connection, "search_adapter_unicode", shard=shard)
            trigram = self._postings_digest(self.connection, "search_adapter_trigram", shard=shard)
            if (actual, unicode, trigram) != (
                metadata["state_digest"], metadata["unicode_postings_digest"], metadata["trigram_postings_digest"],
            ):
                return False
        return True

    @classmethod
    def target_from_rows(cls, shard: str, generation: int, watermark: int, rows: Iterable[CanonicalMessage], previous: str | None = None) -> ShardTarget:
        canonical = list(rows)
        for row in canonical:
            if len(row.indexed_text.encode("utf-8")) > _MAX_ROW_INDEXED_BYTES:
                raise ValueError("indexed bytes exceed row bound")
        connection = sqlite3.connect(":memory:"); connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE VIRTUAL TABLE search_adapter_unicode USING fts5(indexed, tokenize='unicode61')")
            connection.execute("CREATE VIRTUAL TABLE search_adapter_trigram USING fts5(indexed, tokenize='trigram')")
            connection.execute("CREATE VIRTUAL TABLE search_adapter_unicode_vocab USING fts5vocab(search_adapter_unicode, 'instance')")
            connection.execute("CREATE VIRTUAL TABLE search_adapter_trigram_vocab USING fts5vocab(search_adapter_trigram, 'instance')")
            connection.executemany("INSERT INTO search_adapter_unicode(rowid,indexed) VALUES (?,?)", ((row.id, row.indexed_text) for row in canonical))
            connection.executemany("INSERT INTO search_adapter_trigram(rowid,indexed) VALUES (?,?)", ((row.id, row.indexed_text) for row in canonical))
            return ShardTarget(shard, generation, watermark, cls.state_digest(canonical, shard), previous,
                               cls._postings_digest(connection, "search_adapter_unicode"), cls._postings_digest(connection, "search_adapter_trigram"))
        finally:
            connection.close()

    def _normalise(self, rows: Iterable[CanonicalMessage], deletes: Iterable[int]) -> tuple[list[CanonicalMessage], list[int]]:
        rows, deletes = list(rows), list(deletes)
        if len(rows) > _MAX_BATCH_ROWS or len(deletes) > _MAX_BATCH_DELETES:
            raise ValueError("batch exceeds bound")
        indexed_bytes = 0
        for row in rows:
            size = len(row.indexed_text.encode("utf-8")); indexed_bytes += size
            if size > self.max_row_indexed_bytes or indexed_bytes > self.max_batch_indexed_bytes:
                raise ValueError("indexed bytes exceed bound")
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)) or len(deletes) != len(set(deletes)) or set(ids) & set(deletes):
            raise ValueError("batch contains duplicate or conflicting IDs")
        return rows, deletes

    def _reject_foreign(self, shard: str, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        if self.connection.execute(f"SELECT 1 FROM search_adapter_rows WHERE id IN ({marks}) AND shard != ?", (*ids, shard)).fetchone():
            raise ValueError("message ID is owned by another shard")

    def _upsert(self, shard: str, row: CanonicalMessage) -> None:
        timestamp_order = self._timestamp_order(row.timestamp)
        for table in ("search_adapter_unicode", "search_adapter_trigram"):
            self.connection.execute(f"DELETE FROM {table} WHERE rowid=?", (row.id,))
            self.connection.execute(f"INSERT INTO {table}(rowid,indexed) VALUES (?,?)", (row.id, row.indexed_text))
        self.connection.execute("""INSERT OR REPLACE INTO search_adapter_rows
            (id, shard, session_id, role, timestamp, timestamp_order, source, model, session_started, active, compacted, row_digest, projection_digest)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.id, shard, row.session_id, row.role, row.timestamp, timestamp_order, row.source, row.model,
             row.session_started, row.active, row.compacted, self.row_digest(row), self.projection_digest(shard, row, timestamp_order)))

    def _delete(self, ids: Iterable[int]) -> None:
        for row_id in ids:
            for table in ("search_adapter_unicode", "search_adapter_trigram"):
                self.connection.execute(f"DELETE FROM {table} WHERE rowid=?", (row_id,))
            self.connection.execute("DELETE FROM search_adapter_rows WHERE id=?", (row_id,))

    def apply_target(self, target: ShardTarget, rows: Iterable[CanonicalMessage], *, delete_ids: Iterable[int] = ()) -> None:
        if target.shard not in self.declared_shards:
            raise ValueError("undeclared shard")
        rows, delete_ids = self._normalise(rows, delete_ids)
        try:
            with self._lock:
                self._ensure_open()
                with self._write_transaction():
                    local_shards = {row["shard"] for row in self.connection.execute("SELECT shard FROM search_adapter_shards")}
                    if not local_shards <= self.declared_shards:
                        raise CacheCorruptionError()
                    generations = {r["generation"] for r in self.connection.execute("SELECT DISTINCT generation FROM search_adapter_shards")}
                    if generations and generations != {target.generation}:
                        raise ValueError("incremental target must match global generation")
                    existing = self.connection.execute("SELECT generation,watermark,state_digest,unicode_postings_digest,trigram_postings_digest FROM search_adapter_shards WHERE shard=?", (target.shard,)).fetchone()
                    if existing:
                        if target.generation != existing["generation"]:
                            raise ValueError("generation changes require rebuild")
                        if target.watermark < existing["watermark"]:
                            raise ValueError("stale shard target")
                        if target.watermark == existing["watermark"]:
                            if (target.state_digest, target.unicode_postings_digest, target.trigram_postings_digest) != (
                                existing["state_digest"], existing["unicode_postings_digest"], existing["trigram_postings_digest"],
                            ):
                                raise CacheCorruptionError("conflicting shard replay")
                            if not self._attest_complete_cache():
                                raise CacheCorruptionError()
                            return
                        # An existing shard may advance only from a completely
                        # attested cache. Target-shard joins alone omit orphan
                        # postings and rows owned by undeclared shards.
                        if not self._attest_complete_cache():
                            raise CacheCorruptionError()
                        if target.previous_state_digest != existing["state_digest"]:
                            raise ValueError("previous state digest mismatch")
                    elif target.previous_state_digest is not None:
                        raise ValueError("initial target must not have previous state digest")
                    self._reject_foreign(target.shard, [r.id for r in rows] + delete_ids)
                    self._delete(delete_ids)
                    for row in rows:
                        self._upsert(target.shard, row)
                    actual = self._stored_state_digest(target.shard)
                    if actual != target.state_digest:
                        raise ValueError("target state digest mismatch")
                    unicode = self._postings_digest(self.connection, "search_adapter_unicode", shard=target.shard)
                    trigram = self._postings_digest(self.connection, "search_adapter_trigram", shard=target.shard)
                    if (unicode, trigram) != (target.unicode_postings_digest, target.trigram_postings_digest):
                        raise ValueError("target postings digest mismatch")
                    self.connection.execute("INSERT OR REPLACE INTO search_adapter_shards VALUES (?,?,?,?,?,?)", (target.shard, target.generation, target.watermark, actual, unicode, trigram))
                    if "post_apply_orphan" in self._failures:
                        self.connection.execute("INSERT INTO search_adapter_unicode(rowid,indexed) VALUES (99,'injected orphan')")
                    # A first target in a multi-shard cache cannot claim a
                    # complete universe yet. Once all declared metadata is
                    # present (always for an existing-shard advance), attest
                    # the post-mutation complete universe before commit.
                    local_shards = {row["shard"] for row in self.connection.execute("SELECT shard FROM search_adapter_shards")}
                    if self.declared_shards <= local_shards and not self._attest_complete_cache():
                        raise CacheCorruptionError()
        except DerivedCacheError:
            raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise CacheLockError() from None
            raise DerivedCacheError() from None
        except sqlite3.DatabaseError:
            raise DerivedCacheError() from None

    def rebuild(self, targets: Mapping[str, ShardTarget], rows_by_shard: Mapping[str, Iterable[CanonicalMessage]]) -> None:
        if set(targets) != self.declared_shards or set(rows_by_shard) != self.declared_shards:
            raise ValueError("full rebuild requires exact declared shards")
        if len({target.generation for target in targets.values()}) != 1:
            raise ValueError("full rebuild requires one generation")
        try:
            with self._lock:
                self._ensure_open()
                with self._write_transaction():
                    old = {row["shard"]: row for row in self.connection.execute("SELECT * FROM search_adapter_shards")}
                    generation = next(iter({t.generation for t in targets.values()}), 0)
                    if old and set(old) != self.declared_shards:
                        raise ValueError("full rebuild existing shard set is incomplete")
                    old_generations = {row["generation"] for row in old.values()}
                    if old and (len(old_generations) != 1 or generation < next(iter(old_generations))):
                        raise ValueError("full rebuild generation downgrade or inconsistency")
                    if old_generations == {generation}:
                        exact_replay = False
                        for shard in sorted(self.declared_shards):
                            target, existing = targets[shard], old[shard]
                            if target.watermark < existing["watermark"]:
                                raise ValueError("stale full rebuild target")
                            if target.watermark == existing["watermark"]:
                                exact_replay = True
                                expected = (target.state_digest, target.unicode_postings_digest, target.trigram_postings_digest)
                                attested = (existing["state_digest"], existing["unicode_postings_digest"], existing["trigram_postings_digest"])
                                if expected != attested:
                                    raise ValueError("conflicting full rebuild replay")
                        if exact_replay and not self._attest_complete_cache():
                            raise CacheCorruptionError()
                    self.connection.execute("DELETE FROM search_adapter_unicode"); self.connection.execute("DELETE FROM search_adapter_trigram")
                    self.connection.execute("DELETE FROM search_adapter_rows"); self.connection.execute("DELETE FROM search_adapter_shards")
                    total_rows, total_bytes = 0, 0
                    for shard in sorted(self.declared_shards):
                        target = targets[shard]
                        if target.shard != shard or target.previous_state_digest is not None:
                            raise ValueError("invalid rebuild target continuity")
                        for row in rows_by_shard[shard]:
                            size = len(row.indexed_text.encode("utf-8")); total_rows += 1; total_bytes += size
                            if size > self.max_row_indexed_bytes or total_rows > self.max_rebuild_rows or total_bytes > self.max_rebuild_indexed_bytes:
                                raise ValueError("rebuild exceeds bound")
                            if self.connection.execute("SELECT 1 FROM search_adapter_rows WHERE id=?", (row.id,)).fetchone():
                                raise ValueError("full rebuild contains duplicate message IDs")
                            self._upsert(shard, row)
                        actual = self._stored_state_digest(shard)
                        if actual != target.state_digest:
                            raise ValueError("target state digest mismatch")
                        unicode = self._postings_digest(self.connection, "search_adapter_unicode", shard=shard)
                        trigram = self._postings_digest(self.connection, "search_adapter_trigram", shard=shard)
                        if (unicode, trigram) != (target.unicode_postings_digest, target.trigram_postings_digest):
                            raise ValueError("target postings digest mismatch")
                        self.connection.execute("INSERT INTO search_adapter_shards VALUES (?,?,?,?,?,?)", (shard, target.generation, target.watermark, actual, unicode, trigram))
                    if "mid_rebuild" in self._failures:
                        raise sqlite3.DatabaseError()
        except DerivedCacheError:
            raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower(): raise CacheLockError() from None
            raise DerivedCacheError() from None
        except sqlite3.DatabaseError:
            raise DerivedCacheError() from None

    def optimize(self) -> None:
        try:
            with self._lock:
                self._ensure_open()
                with self._write_transaction():
                    for table in ("search_adapter_unicode", "search_adapter_trigram"):
                        self.connection.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
        except DerivedCacheError: raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower(): raise CacheLockError() from None
            raise DerivedCacheError() from None
        except sqlite3.DatabaseError: raise DerivedCacheError() from None

    def _availability(self, observed: Mapping[str, ShardTarget] | None) -> SearchError | None:
        if self._failures & {"cache_locked", "cache_corrupt"}:
            return SearchError("cache_locked" if "cache_locked" in self._failures else "cache_corrupt", _UNAVAILABLE)
        schema = self.connection.execute("SELECT value FROM search_adapter_metadata WHERE key='schema_generation'").fetchone()
        if not schema or schema["value"] != str(SCHEMA_GENERATION): return SearchError("cache_corrupt", _UNAVAILABLE)
        if observed is None or set(observed) != self.declared_shards: return SearchError("missing_shard_target", _UNAVAILABLE)
        local = {r["shard"]: r for r in self.connection.execute("SELECT * FROM search_adapter_shards")}
        if set(local) != self.declared_shards: return SearchError("watermark_lag", _UNAVAILABLE)
        if len({t.generation for t in observed.values()}) != 1 or len({r["generation"] for r in local.values()}) != 1: return SearchError("generation_mismatch", _UNAVAILABLE)
        for shard, target in observed.items():
            row = local[shard]
            if (row["generation"], row["watermark"], row["state_digest"], row["unicode_postings_digest"], row["trigram_postings_digest"]) != (target.generation, target.watermark, target.state_digest, target.unicode_postings_digest, target.trigram_postings_digest): return SearchError("watermark_mismatch", _UNAVAILABLE)
        if not self._attest_complete_cache(): return SearchError("cache_corrupt", _UNAVAILABLE)
        return None

    @staticmethod
    def _pagination(limit: object, offset: object) -> SearchError | None:
        if isinstance(limit, bool) or not isinstance(limit, int) or isinstance(offset, bool) or not isinstance(offset, int) or limit < 0 or offset < 0 or limit > MAX_PAGE_SIZE or offset > MAX_OFFSET:
            return SearchError("invalid_pagination", "invalid pagination")
        return None

    def search(self, query: str, *, source_filter=None, exclude_sources=None, role_filter=None, limit: int = 20, offset: int = 0, sort: str | None = None, include_inactive: bool = False, observed_targets: Mapping[str, ShardTarget] | None = None) -> SearchResult:
        invalid = self._pagination(limit, offset)
        if invalid: return SearchResult([], invalid)
        try:
            with self._lock:
                self._ensure_open()
                self.connection.execute("BEGIN")
                try:
                    unavailable = self._availability(observed_targets)
                    if unavailable: return SearchResult([], unavailable)
                    sanitized = SessionDB._sanitize_fts5_query(query or "")
                    if not sanitized or limit == 0: return SearchResult([])
                    raw = sanitized.strip('" ').strip(); tokens = [t for t in raw.split() if t.upper() not in {"AND", "OR", "NOT"}]
                    cjk = [t for t in tokens if SessionDB._contains_cjk(t)]
                    if cjk and (any(SessionDB._count_cjk(t) < 3 for t in cjk) or any(re.search(r"[A-Za-z0-9]", t) for t in cjk)):
                        return SearchResult([], SearchError("short_cjk_unsupported", "short or mixed CJK queries are unsupported"))
                    table = "search_adapter_trigram" if cjk else "search_adapter_unicode"
                    match = " ".join(f'"{t.replace(chr(34), chr(34)*2)}"' if t.upper() not in {"AND", "OR", "NOT"} else t for t in tokens) if cjk else sanitized
                    where, params = [f"{table} MATCH ?"], [match]
                    if not include_inactive: where.append("(m.active=1 OR m.compacted=1)")
                    for column, values, negate in (("source", source_filter, False), ("source", exclude_sources, True), ("role", role_filter, False)):
                        if values is not None and (values or column != "role"):
                            where.append(f"m.{column} {'NOT ' if negate else ''}IN ({','.join('?' for _ in values)})"); params.extend(values)
                    clause = f"FROM {table} JOIN search_adapter_rows m ON m.id={table}.rowid WHERE {' AND '.join(where)}"
                    order = "rank, m.id" if sort not in {"newest", "oldest"} else ("m.timestamp_order DESC, rank, m.id" if sort == "newest" else "m.timestamp_order ASC, rank, m.id")
                    count = self.connection.execute(f"SELECT COUNT(*) {clause}", params).fetchone()[0]
                    candidates = self.connection.execute(f"SELECT m.id,m.row_digest,rank {clause} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
                finally:
                    self.connection.rollback()  # Snapshot ends before hydrator I/O.
        except CacheClosedError: return SearchResult([], SearchError("cache_closed", _UNAVAILABLE))
        except sqlite3.DatabaseError: return SearchResult([], SearchError("cache_read_failure", "derived cache read failed"))
        ids = [r["id"] for r in candidates]
        try: hydrated = self.hydrator.hydrate(ids)
        except Exception: return SearchResult([], SearchError("hydration_failure", "authoritative hydration failed"), count)
        if not isinstance(hydrated, Mapping) or set(hydrated) != set(ids) or any(not isinstance(row, CanonicalMessage) or row.id != key for key, row in hydrated.items()):
            return SearchResult([], SearchError("hydration_mismatch", "authoritative hydration did not match candidates"), count, 0)
        if any(self.row_digest(hydrated[c["id"]]) != c["row_digest"] for c in candidates):
            return SearchResult([], SearchError("hydration_mismatch", "authoritative hydration did not match candidates"), count, 0)
        if sum(len(row.indexed_text.encode("utf-8")) for row in hydrated.values()) > self.max_page_indexed_bytes:
            return SearchResult([], SearchError("hydrated_page_too_large", "hydrated page exceeds indexed byte bound"), count, 0)
        try:
            with self._lock:
                self._ensure_open()
                tokenizer = "trigram" if cjk else "unicode61"
                snippets = self._snippets(tokenizer, hydrated, match)
        except (CacheClosedError, sqlite3.DatabaseError): return SearchResult([], SearchError("temp_fts_failure", "derived cache temporary FTS failed"), count, 0)
        return SearchResult([{"id": row.id, "session_id": row.session_id, "role": row.role, "snippet": snippets.get(row.id, ""), "timestamp": row.timestamp, "tool_name": row.tool_name, "source": row.source, "model": row.model, "session_started": row.session_started, "rank": candidate["rank"]} for candidate in candidates for row in [hydrated[candidate["id"]]]], candidate_count=count, hydration_count=len(ids))
