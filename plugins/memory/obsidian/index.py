"""SQLite FTS5-index över Obsidian-valvet (derivat, raderbart)."""

from __future__ import annotations

import hashlib
import sqlite3

from plugins.memory.obsidian.chunker import chunk_markdown

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
