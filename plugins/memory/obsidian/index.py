"""SQLite FTS5-index över Obsidian-valvet (derivat, raderbart)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import namedtuple

from plugins.memory.obsidian.chunker import chunk_markdown
from plugins.memory.obsidian.sanitizer import sanitize_fts_query

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    heading      TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    mtime        REAL NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(content, heading, content=chunks, content_rowid=chunk_id);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content, heading)
        VALUES (new.chunk_id, new.content, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, heading)
        VALUES ('delete', old.chunk_id, old.content, old.heading);
END;
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SearchHit = namedtuple("SearchHit", ["path", "heading", "content", "score"])


class ObsidianIndex:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def upsert_note(self, path: str, text: str, mtime: float) -> None:
        chash = _hash(text)
        self.delete_note(path)
        rows = [
            (path, c.heading_trail, c.content, mtime, chash)
            for c in chunk_markdown(text)
        ]
        if rows:
            self.conn.executemany(
                "INSERT INTO chunks(path, heading, content, mtime, content_hash)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        else:
            # Note with no indexable body: record a marker row so the path is
            # tracked (mtime/hash) and won't be re-read every sync. Empty
            # content is not inserted into FTS (nothing to match).
            self.conn.execute(
                "INSERT INTO chunks(path, heading, content, mtime, content_hash)"
                " VALUES (?, '', '', ?, ?)",
                (path, mtime, chash),
            )
        self.conn.commit()

    def delete_note(self, path: str) -> None:
        self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self.conn.commit()

    def search(self, query: str, top_k: int = 5) -> "list[SearchHit]":
        fts = sanitize_fts_query(query)
        if not fts:
            return []
        try:
            cur = self.conn.execute(
                "SELECT c.path, c.heading, c.content, bm25(chunks_fts) AS score "
                "FROM chunks_fts "
                "JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? AND c.content != '' "
                "ORDER BY score ASC LIMIT ?",
                (fts, top_k),
            )
            return [SearchHit(*row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    def indexed_paths(self) -> "dict[str, tuple[float, str]]":
        cur = self.conn.execute(
            "SELECT path, MAX(mtime), content_hash FROM chunks GROUP BY path"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    def _chunk_count_for(self, path: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path = ? AND content != ''",
            (path,),
        )
        return cur.fetchone()[0]

    def sync_vault(
        self,
        vault_path: str,
        exclude_dirs: "tuple[str, ...]" = (".git", ".obsidian", ".trash"),
    ) -> dict:
        """Inkrementell sync: walk valvet, diffa content_hash, re-indexera ändrat."""
        existing = self.indexed_paths()
        seen: set[str] = set()
        summary = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}

        for root, dirs, files in os.walk(vault_path):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                abspath = os.path.join(root, fn)
                rel = os.path.relpath(abspath, vault_path)
                seen.add(rel)
                try:
                    with open(abspath, encoding="utf-8") as fh:
                        text = fh.read()
                    mtime = os.path.getmtime(abspath)
                except OSError:
                    continue
                chash = _hash(text)
                if rel not in existing:
                    self.upsert_note(rel, text, mtime)
                    summary["added"] += 1
                elif existing[rel][1] != chash:
                    self.upsert_note(rel, text, mtime)
                    summary["updated"] += 1
                else:
                    summary["unchanged"] += 1

        for rel in existing:
            if rel not in seen:
                self.delete_note(rel)
                summary["deleted"] += 1
        return summary
