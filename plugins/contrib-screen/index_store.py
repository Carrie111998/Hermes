from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home

DDL = """
CREATE TABLE IF NOT EXISTS repo (
    full_name   TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    html_url    TEXT,
    stars       INTEGER,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS issue (
    repo       TEXT NOT NULL,
    number     TEXT NOT NULL,
    is_pr      INTEGER NOT NULL DEFAULT 0,
    state      TEXT,
    title      TEXT,
    body       TEXT,
    author     TEXT,
    assignees  TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at  TEXT,
    url        TEXT,
    PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS issue_repo_state ON issue(repo, state);

CREATE TABLE IF NOT EXISTS issue_comment (
    repo       TEXT NOT NULL,
    number     TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    author     TEXT,
    body       TEXT,
    created_at TEXT,
    url        TEXT,
    PRIMARY KEY (repo, number, comment_id)
);
CREATE INDEX IF NOT EXISTS comment_issue ON issue_comment(repo, number);
"""


def default_db_dir() -> Path:
    return get_hermes_home() / "contrib-screen" / "org-index"


def default_db_path(org: str) -> Path:
    return default_db_dir() / f"{org.lower()}.db"


class IndexStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(DDL)
        self._setup_search()
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _setup_search(self) -> None:
        # FTS5 is a compile time option, not guaranteed present. Absence must
        # be survivable: search() falls back to LIKE, correct but slower.
        self.fts = False
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS issue_fts USING fts5("
                " repo UNINDEXED, number UNINDEXED, part UNINDEXED, text,"
                " tokenize='porter unicode61')"
            )
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def _fts_put(self, repo: str, number: str, part: str, text: str) -> None:
        if not self.fts:
            return
        self.db.execute(
            "DELETE FROM issue_fts WHERE repo=? AND number=? AND part=?", (repo, number, part)
        )
        if text:
            self.db.execute(
                "INSERT INTO issue_fts(repo, number, part, text) VALUES(?,?,?,?)",
                (repo, number, part, text),
            )

    def upsert_repo(self, full_name: str, **fields) -> None:
        owner, _, name = full_name.partition("/")
        row = {"full_name": full_name, "owner": owner, "name": name, **fields}
        cols = ("full_name", "owner", "name", "description", "html_url", "stars", "synced_at")
        row = {k: v for k, v in row.items() if k in cols}
        placeholders = ",".join("?" * len(row))
        updates = ",".join(f"{k}=excluded.{k}" for k in row if k != "full_name")
        self.db.execute(
            f"INSERT INTO repo({','.join(row)}) VALUES({placeholders})"
            f" ON CONFLICT(full_name) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def upsert_issue(self, repo: str, number: str, **fields) -> None:
        cols = ("is_pr", "state", "title", "body", "author", "assignees",
                "created_at", "updated_at", "closed_at", "url")
        row = {k: _pack(v) for k, v in fields.items() if k in cols}
        row.update({"repo": repo, "number": str(number)})
        placeholders = ",".join("?" * len(row))
        updates = ",".join(f"{k}=excluded.{k}" for k in row if k not in ("repo", "number"))
        self.db.execute(
            f"INSERT INTO issue({','.join(row)}) VALUES({placeholders})"
            f" ON CONFLICT(repo, number) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        self._fts_put(repo, str(number), "issue",
                      f"{fields.get('title') or ''} {fields.get('body') or ''}".strip())

    def get_issue(self, repo: str, number: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM issue WHERE repo=? AND number=?", (repo, str(number))
        ).fetchone()
        return dict(r) if r else None

    def add_comments(self, rows: list[dict]) -> int:
        # Upsert without deleting first: comments arrive as one flat list
        # spanning every issue in the repo, so there is no natural "replace
        # this issue's comments" boundary at sync time.
        n = 0
        for c in rows:
            self.db.execute(
                "INSERT OR REPLACE INTO issue_comment"
                "(repo,number,comment_id,author,body,created_at,url)"
                " VALUES(?,?,?,?,?,?,?)",
                (c["repo"], str(c["number"]), str(c["comment_id"]), c.get("author"),
                 c.get("body"), c.get("created_at"), c.get("url")),
            )
            self._fts_put(c["repo"], str(c["number"]), f"c{c['comment_id']}", c.get("body") or "")
            n += 1
        return n

    def comments(self, repo: str, number: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM issue_comment WHERE repo=? AND number=? ORDER BY created_at",
            (repo, str(number)),
        )]

    def search(self, query: str, limit: int = 40) -> list[dict]:
        q = query.strip()
        if not q:
            return []
        if self.fts:
            try:
                keys = self.db.execute(
                    "SELECT repo, number FROM issue_fts WHERE issue_fts MATCH ?"
                    " ORDER BY rank LIMIT ?", (_fts_query(q), max(limit * 8, 200)),
                )
                out, seen = [], set()
                for k in keys:
                    ident = (k["repo"], k["number"])
                    if ident in seen:
                        continue
                    seen.add(ident)
                    row = self.get_issue(*ident)
                    if row:
                        out.append(row)
                    if len(out) >= limit:
                        break
                return out
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        return [dict(r) for r in self.db.execute(
            "SELECT DISTINCT i.* FROM issue i"
            " LEFT JOIN issue_comment c ON c.repo=i.repo AND c.number=i.number"
            " WHERE i.title LIKE ? OR i.body LIKE ? OR c.body LIKE ?"
            " ORDER BY COALESCE(i.updated_at, i.created_at) DESC LIMIT ?",
            (like, like, like, limit),
        )]

    def merged_prs(self, limit: int = 10) -> list[dict]:
        """Real merged PR title/body pairs, most recent first — the raw
        material for maintainer-voice calibration (see
        internal-docs/harness/org-awareness-and-voice-design.md)."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM issue WHERE is_pr=1 AND state='closed'"
            " ORDER BY COALESCE(closed_at, updated_at) DESC LIMIT ?",
            (limit,),
        )]

    def counts(self) -> dict[str, int]:
        return {
            t: int(self.db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
            for t in ("repo", "issue", "issue_comment")
        }

    def commit(self) -> None:
        self.db.commit()


def _fts_query(text: str) -> str:
    words = [w.replace('"', "") for w in re.findall(r"\S+", text)]
    return " AND ".join(f'"{w}"' for w in words if w)


def _pack(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v
