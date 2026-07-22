# Obsidian Memory Provider — Phase A (Retrieval) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an external Hermes `MemoryProvider` that indexes the Obsidian vault with SQLite FTS5 and injects query-relevant vault notes per turn via `prefetch()`.

**Architecture:** Vault (`/srv/dj/obsidian`, markdown) is the source of truth. A disposable SQLite index (outside the vault) is keyed on `path + mtime + content_hash` and rebuilt incrementally from the `.md` files. Pure functions (chunker, sanitizer) are model-free and unit-tested. Retrieval is FTS5 `MATCH` + BM25. No embeddings in Phase A.

**Tech Stack:** Python 3.11 (stdlib only — `sqlite3` with FTS5, `hashlib`, `pathlib`), pytest via `uv run --extra dev python -m pytest`. Models on `agent/memory_provider.py::MemoryProvider`. Reference code: `plugins/memory/holographic/` (FTS5 schema + query sanitizer).

## Global Constraints

- Runtime is stdlib-only and sealed (`HERMES_DISABLE_LAZY_INSTALLS=1`) — NO new dependencies in Phase A. `sqlite3` (with FTS5) only.
- Any `open()` passes `encoding="utf-8"` (ruff PLW1514).
- Tests run with `uv run --extra dev python -m pytest`. Retrieval decisions verifiable WITHOUT model calls.
- The vault is the source of truth; the SQLite index is derived and disposable — never write index files inside the vault.
- Swedish for user-facing strings/docs; English for code identifiers.
- Vault path is configurable; default `/srv/dj/obsidian`. Index DB default `$HERMES_HOME/obsidian_index.db`.
- Provider registers via `register(ctx)` → `ctx.register_memory_provider(provider)`; manifest `plugin.yaml`. Exactly one external provider runs at a time.

## File Structure

- `plugins/memory/obsidian/__init__.py` — `ObsidianMemoryProvider(MemoryProvider)` + `register(ctx)`.
- `plugins/memory/obsidian/plugin.yaml` — manifest (name, version, description, hooks).
- `plugins/memory/obsidian/chunker.py` — pure: strip frontmatter, chunk by heading → chunks with heading trail.
- `plugins/memory/obsidian/sanitizer.py` — pure: FTS5 query sanitizer.
- `plugins/memory/obsidian/config.py` — parse provider config (vault_path, excludes, top_k, pinned).
- `plugins/memory/obsidian/index.py` — SQLite index: schema, upsert/delete note, incremental vault sync, search.
- Tests: `tests/obsidian_plugin/` (`__init__.py`, `test_chunker.py`, `test_sanitizer.py`, `test_index.py`, `test_sync.py`, `test_search.py`, `test_config.py`, `test_provider.py`).

---

### Task 1: Chunker (pure)

**Files:**
- Create: `plugins/memory/obsidian/chunker.py`
- Test: `tests/obsidian_plugin/__init__.py` (empty), `tests/obsidian_plugin/test_chunker.py`

**Interfaces:**
- Produces: `Chunk = namedtuple("Chunk", ["heading_trail", "content"])`; `chunk_markdown(text: str) -> list[Chunk]`. Strips YAML frontmatter; splits on ATX headings (`#`..`######`); each chunk's `heading_trail` is the heading text (empty string for pre-heading/preamble body); `content` is the section body incl. the heading line. A note with no headings → one chunk with `heading_trail=""`.

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/__init__.py` (empty file) and `tests/obsidian_plugin/test_chunker.py`:

```python
from plugins.memory.obsidian.chunker import chunk_markdown, Chunk


def test_strips_yaml_frontmatter():
    text = "---\ndate: 2026-07-22\ntags: [x]\n---\n# Titel\nbrödtext\n"
    chunks = chunk_markdown(text)
    joined = "\n".join(c.content for c in chunks)
    assert "date: 2026-07-22" not in joined
    assert "Titel" in joined


def test_no_headings_single_chunk():
    chunks = chunk_markdown("bara brödtext utan rubrik\nrad två")
    assert len(chunks) == 1
    assert chunks[0].heading_trail == ""
    assert "brödtext" in chunks[0].content


def test_splits_on_headings_with_trail():
    text = "# A\nalfa\n## B\nbeta\n## C\ngamma\n"
    chunks = chunk_markdown(text)
    trails = [c.heading_trail for c in chunks]
    assert trails == ["A", "B", "C"]
    assert "alfa" in chunks[0].content
    assert "beta" in chunks[1].content


def test_preamble_before_first_heading_is_its_own_chunk():
    text = "inledande text\n# Rubrik\nkropp\n"
    chunks = chunk_markdown(text)
    assert chunks[0].heading_trail == ""
    assert "inledande text" in chunks[0].content
    assert chunks[1].heading_trail == "Rubrik"


def test_empty_input_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("---\nonly: frontmatter\n---\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_chunker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'plugins.memory.obsidian'`.

- [ ] **Step 3: Write minimal implementation**

Create `plugins/memory/obsidian/__init__.py` as an empty file first (package marker — the provider class is added in Task 7). Then create `plugins/memory/obsidian/chunker.py`:

```python
"""Ren markdown-chunkning för Obsidian-noter (ingen modell, ingen I/O)."""

from __future__ import annotations

import re
from collections import namedtuple

Chunk = namedtuple("Chunk", ["heading_trail", "content"])

_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def chunk_markdown(text: str) -> "list[Chunk]":
    """Dela en not i chunks per rubrik. Frontmatter strippas."""
    body = _strip_frontmatter(text or "")
    chunks: list[Chunk] = []
    cur_trail = ""
    cur_lines: list[str] = []

    def _flush() -> None:
        content = "\n".join(cur_lines).strip()
        if content:
            chunks.append(Chunk(heading_trail=cur_trail, content=content))

    for line in body.split("\n"):
        m = _HEADING.match(line)
        if m:
            _flush()
            cur_trail = m.group(2).strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    _flush()
    return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_chunker.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/__init__.py plugins/memory/obsidian/chunker.py tests/obsidian_plugin/
git commit -m "feat(obsidian): pure markdown chunker (frontmatter strip + heading split)"
```

---

### Task 2: FTS5 query sanitizer (pure)

**Files:**
- Create: `plugins/memory/obsidian/sanitizer.py`
- Test: `tests/obsidian_plugin/test_sanitizer.py`

**Interfaces:**
- Produces: `sanitize_fts_query(query: str) -> str` — tokenizes, drops stopwords + <2-char tokens, strips FTS5 special chars, phrase-literals each token, OR-joins. Empty/pathological → returns the raw query (never a SQL error). Ported from `plugins/memory/holographic/retrieval.py:585-619`.

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_sanitizer.py`:

```python
from plugins.memory.obsidian.sanitizer import sanitize_fts_query


def test_or_joins_phrase_literal_tokens():
    out = sanitize_fts_query("bilförsäkring och hemförsäkring")
    assert '"bilförsäkring"' in out
    assert '"hemförsäkring"' in out
    assert " OR " in out


def test_drops_short_tokens():
    out = sanitize_fts_query("a bil")
    assert '"bil"' in out
    assert '"a"' not in out


def test_strips_fts_special_chars():
    out = sanitize_fts_query('spara: "något"* (viktigt)')
    # no raw FTS operator chars survive inside tokens
    for tok in out.split(" OR "):
        inner = tok.strip('"')
        assert not any(ch in inner for ch in '"()*^:')


def test_empty_returns_empty():
    assert sanitize_fts_query("") == ""


def test_all_stopwords_falls_back_to_raw():
    # if nothing survives, return raw (no crash, caller sees 0 results)
    out = sanitize_fts_query("och")
    assert out == "och"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_sanitizer.py -q`
Expected: FAIL — `ModuleNotFoundError` for `sanitizer`.

- [ ] **Step 3: Write minimal implementation**

Create `plugins/memory/obsidian/sanitizer.py`:

```python
"""FTS5-query-sanitizer (ren). Porterad från holographic-providern."""

from __future__ import annotations

# Svenska + engelska stoppord (håll litet; FTS5 OR-recall är målet).
_STOPWORDS = {
    "och", "att", "det", "som", "en", "ett", "på", "är", "för", "med", "av",
    "till", "den", "har", "jag", "om", "inte", "de", "vi", "the", "a", "an",
    "and", "or", "of", "to", "in", "is", "it", "for", "on", "with", "my",
}
_FTS_SPECIAL = '"()*^:-+'


def sanitize_fts_query(query: str) -> str:
    """Gör en naturlig fråga till ett FTS5-säkert OR-uttryck."""
    if not query:
        return ""
    tokens: list[str] = []
    for raw in query.lower().split():
        cleaned = raw.strip(".,;:!?\"'()[]{}#@<>").translate(
            str.maketrans("", "", _FTS_SPECIAL)
        )
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        tokens.append(f'"{cleaned}"')
    if not tokens:
        return query
    return " OR ".join(tokens)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_sanitizer.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/sanitizer.py tests/obsidian_plugin/test_sanitizer.py
git commit -m "feat(obsidian): FTS5 query sanitizer (OR-join, stopwords, escape)"
```

---

### Task 3: Index schema + upsert/delete note

**Files:**
- Create: `plugins/memory/obsidian/index.py`
- Test: `tests/obsidian_plugin/test_index.py`

**Interfaces:**
- Consumes: `chunk_markdown` (Task 1).
- Produces: `class ObsidianIndex` with `__init__(self, db_path: str)` (creates schema; `":memory:"` allowed for tests), `upsert_note(self, path: str, text: str, mtime: float) -> None` (chunks text, replaces all rows for path), `delete_note(self, path: str) -> None`, `indexed_paths(self) -> dict[str, tuple[float, str]]` (path → (mtime, content_hash)). Uses `hashlib.sha256` of raw text as `content_hash`.

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_index.py`:

```python
from plugins.memory.obsidian.index import ObsidianIndex


def _idx():
    return ObsidianIndex(":memory:")


def test_upsert_creates_chunk_rows():
    idx = _idx()
    idx.upsert_note("memory/daniel.md", "# Daniel\ngillar kaffe\n", 100.0)
    paths = idx.indexed_paths()
    assert "memory/daniel.md" in paths
    mtime, chash = paths["memory/daniel.md"]
    assert mtime == 100.0
    assert len(chash) == 64  # sha256 hex


def test_upsert_replaces_previous_rows_for_path():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nförsta\n", 1.0)
    idx.upsert_note("a.md", "# A\nandra\n", 2.0)
    # only the new content remains; mtime updated
    assert idx.indexed_paths()["a.md"][0] == 2.0
    # search proves old content is gone (Task 5 adds search; here check row count)
    assert idx._chunk_count_for("a.md") == 1


def test_delete_note_removes_rows():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nx\n", 1.0)
    idx.delete_note("a.md")
    assert "a.md" not in idx.indexed_paths()
    assert idx._chunk_count_for("a.md") == 0


def test_content_hash_changes_with_content():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nett\n", 1.0)
    h1 = idx.indexed_paths()["a.md"][1]
    idx.upsert_note("a.md", "# A\ntvå\n", 1.0)
    h2 = idx.indexed_paths()["a.md"][1]
    assert h1 != h2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_index.py -q`
Expected: FAIL — `ModuleNotFoundError` for `index`.

- [ ] **Step 3: Write minimal implementation**

Create `plugins/memory/obsidian/index.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_index.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/index.py tests/obsidian_plugin/test_index.py
git commit -m "feat(obsidian): SQLite FTS5 index schema + upsert/delete note"
```

---

### Task 4: Incremental vault sync

**Files:**
- Modify: `plugins/memory/obsidian/index.py`
- Test: `tests/obsidian_plugin/test_sync.py`

**Interfaces:**
- Consumes: `ObsidianIndex` (Task 3).
- Produces: `ObsidianIndex.sync_vault(self, vault_path: str, exclude_dirs: tuple[str, ...] = (".git", ".obsidian", ".trash")) -> dict` — walks `*.md` under vault (skipping excluded dirs), compares each file's `content_hash` against the index, re-indexes changed/new files, deletes rows for files no longer present. Returns a summary dict `{"added": n, "updated": n, "deleted": n, "unchanged": n}`.

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_sync.py`:

```python
from pathlib import Path

from plugins.memory.obsidian.index import ObsidianIndex


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_sync_indexes_new_files(tmp_path):
    _write(tmp_path / "memory" / "daniel.md", "# Daniel\nkaffe\n")
    _write(tmp_path / "projekt" / "saa.md", "# SAA\nbundle\n")
    idx = ObsidianIndex(":memory:")
    summary = idx.sync_vault(str(tmp_path))
    assert summary["added"] == 2
    assert set(idx.indexed_paths()) == {"memory/daniel.md", "projekt/saa.md"}


def test_sync_skips_unchanged_and_updates_changed(tmp_path):
    f = tmp_path / "a.md"
    _write(f, "# A\nett\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    # unchanged run
    s2 = idx.sync_vault(str(tmp_path))
    assert s2["unchanged"] == 1 and s2["updated"] == 0
    # change the file
    _write(f, "# A\ntvå\n")
    s3 = idx.sync_vault(str(tmp_path))
    assert s3["updated"] == 1


def test_sync_deletes_removed_files(tmp_path):
    f = tmp_path / "a.md"
    _write(f, "# A\nx\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    f.unlink()
    s = idx.sync_vault(str(tmp_path))
    assert s["deleted"] == 1
    assert "a.md" not in idx.indexed_paths()


def test_sync_excludes_git_and_obsidian_dirs(tmp_path):
    _write(tmp_path / ".git" / "x.md", "# git\n")
    _write(tmp_path / ".obsidian" / "y.md", "# cfg\n")
    _write(tmp_path / "real.md", "# real\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    assert set(idx.indexed_paths()) == {"real.md"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_sync.py -q`
Expected: FAIL — `AttributeError: 'ObsidianIndex' object has no attribute 'sync_vault'`.

- [ ] **Step 3: Write minimal implementation**

Add to `plugins/memory/obsidian/index.py` (import `os` and `hashlib` already present; add `os` import at top):

```python
import os


# ... inside class ObsidianIndex:

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_sync.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/index.py tests/obsidian_plugin/test_sync.py
git commit -m "feat(obsidian): incremental vault sync (hash-diff walk)"
```

---

### Task 5: Search (FTS5 MATCH + BM25 → top-k)

**Files:**
- Modify: `plugins/memory/obsidian/index.py`
- Test: `tests/obsidian_plugin/test_search.py`

**Interfaces:**
- Consumes: `ObsidianIndex` (Task 3), `sanitize_fts_query` (Task 2).
- Produces: `SearchHit = namedtuple("SearchHit", ["path", "heading", "content", "score"])`; `ObsidianIndex.search(self, query: str, top_k: int = 5) -> list[SearchHit]` — sanitizes query, runs FTS5 `MATCH` ordered by `bm25(chunks_fts)` ascending (lower = better), returns top_k hits. Empty query or no matches → `[]`.

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_search.py`:

```python
from plugins.memory.obsidian.index import ObsidianIndex, SearchHit


def _seeded():
    idx = ObsidianIndex(":memory:")
    idx.upsert_note("forsakringar/bil.md", "# Bilförsäkring\nfullständig hos Folksam", 1.0)
    idx.upsert_note("memory/daniel.md", "# Daniel\ndricker kaffe varje morgon", 1.0)
    idx.upsert_note("projekt/saa.md", "# SAA\nbundle-split pågår", 1.0)
    return idx


def test_search_finds_relevant_note():
    hits = _seeded().search("bilförsäkring folksam")
    assert hits
    assert hits[0].path == "forsakringar/bil.md"
    assert isinstance(hits[0], SearchHit)


def test_search_returns_empty_on_no_match():
    assert _seeded().search("kvantfysik") == []


def test_search_respects_top_k():
    idx = ObsidianIndex(":memory:")
    for i in range(5):
        idx.upsert_note(f"n{i}.md", f"# N{i}\nkaffe kaffe kaffe", 1.0)
    assert len(idx.search("kaffe", top_k=3)) == 3


def test_empty_query_returns_empty():
    assert _seeded().search("") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_search.py -q`
Expected: FAIL — `ImportError: cannot import name 'SearchHit'`.

- [ ] **Step 3: Write minimal implementation**

Add to `plugins/memory/obsidian/index.py` — add `from collections import namedtuple` and `from plugins.memory.obsidian.sanitizer import sanitize_fts_query` at top, and:

```python
SearchHit = namedtuple("SearchHit", ["path", "heading", "content", "score"])


# ... inside class ObsidianIndex:

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_search.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/index.py tests/obsidian_plugin/test_search.py
git commit -m "feat(obsidian): FTS5 BM25 search returning top-k hits"
```

---

### Task 6: Provider config (pure)

**Files:**
- Create: `plugins/memory/obsidian/config.py`
- Test: `tests/obsidian_plugin/test_config.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) ObsidianConfig(vault_path: str, top_k: int, exclude_dirs: tuple[str,...], pinned: tuple[str,...])`; `build_obsidian_config(cfg: Mapping | None) -> ObsidianConfig`. Defaults: `vault_path="/srv/dj/obsidian"`, `top_k=5`, `exclude_dirs=(".git",".obsidian",".trash")`, `pinned=()`. `pinned` lists vault-relative note paths for the always-on hot core (Phase B uses it; parse it now).

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_config.py`:

```python
from plugins.memory.obsidian.config import build_obsidian_config, ObsidianConfig


def test_defaults():
    c = build_obsidian_config({})
    assert isinstance(c, ObsidianConfig)
    assert c.vault_path == "/srv/dj/obsidian"
    assert c.top_k == 5
    assert ".git" in c.exclude_dirs
    assert c.pinned == ()


def test_overrides():
    c = build_obsidian_config(
        {"vault_path": "/x/vault", "top_k": 8,
         "exclude_dirs": [".git", "archive"],
         "pinned": ["memory/core.md", "memory/daniel.md"]}
    )
    assert c.vault_path == "/x/vault"
    assert c.top_k == 8
    assert c.exclude_dirs == (".git", "archive")
    assert c.pinned == ("memory/core.md", "memory/daniel.md")


def test_none_config_uses_defaults():
    assert build_obsidian_config(None).vault_path == "/srv/dj/obsidian"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError` for `config`.

- [ ] **Step 3: Write minimal implementation**

Create `plugins/memory/obsidian/config.py`:

```python
"""Parsning av obsidian-providerns config (ren)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_VAULT = "/srv/dj/obsidian"
DEFAULT_TOP_K = 5
DEFAULT_EXCLUDES = (".git", ".obsidian", ".trash")


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: str = DEFAULT_VAULT
    top_k: int = DEFAULT_TOP_K
    exclude_dirs: tuple = DEFAULT_EXCLUDES
    pinned: tuple = ()


def build_obsidian_config(cfg: "Mapping[str, Any] | None") -> ObsidianConfig:
    cfg = cfg or {}
    excludes = cfg.get("exclude_dirs")
    pinned = cfg.get("pinned")
    return ObsidianConfig(
        vault_path=str(cfg.get("vault_path", DEFAULT_VAULT)),
        top_k=int(cfg.get("top_k", DEFAULT_TOP_K)),
        exclude_dirs=tuple(excludes) if excludes else DEFAULT_EXCLUDES,
        pinned=tuple(str(p) for p in pinned) if pinned else (),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_config.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/config.py tests/obsidian_plugin/test_config.py
git commit -m "feat(obsidian): provider config parsing"
```

---

### Task 7: The provider class + registration

**Files:**
- Modify: `plugins/memory/obsidian/__init__.py`
- Create: `plugins/memory/obsidian/plugin.yaml`
- Test: `tests/obsidian_plugin/test_provider.py`

**Interfaces:**
- Consumes: `ObsidianIndex` (Tasks 3-5), `build_obsidian_config` (Task 6).
- Produces: `ObsidianMemoryProvider(MemoryProvider)` implementing `name`, `is_available`, `initialize`, `get_tool_schemas`, `prefetch`, `backup_paths`; plus `register(ctx)`. `initialize` builds the index at `$HERMES_HOME/obsidian_index.db` and runs `sync_vault` once. `prefetch(query)` returns formatted text of top-k hits (empty string if none).

- [ ] **Step 1: Write the failing test**

Create `tests/obsidian_plugin/test_provider.py`:

```python
from pathlib import Path

from plugins.memory.obsidian import ObsidianMemoryProvider


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _provider(tmp_path, vault):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault), "top_k": 3})
    p.initialize(session_id="s", hermes_home=str(tmp_path))
    return p


def test_name_and_available(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    p = _provider(tmp_path, vault)
    assert p.name == "obsidian"
    assert p.is_available() is True


def test_prefetch_returns_relevant_note(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "forsakringar" / "bil.md", "# Bilförsäkring\nFolksam helförsäkring")
    p = _provider(tmp_path, vault)
    out = p.prefetch("vad har jag för bilförsäkring?")
    assert "Bilförsäkring" in out or "Folksam" in out
    assert "forsakringar/bil.md" in out


def test_prefetch_empty_when_no_match(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "a.md", "# A\nkaffe")
    p = _provider(tmp_path, vault)
    assert p.prefetch("kvantkromodynamik") == ""


def test_index_db_created_outside_vault(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    _provider(tmp_path, vault)
    assert (tmp_path / "obsidian_index.db").exists()
    assert not (vault / "obsidian_index.db").exists()


def test_get_tool_schemas_empty_in_phase_a(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    assert _provider(tmp_path, vault).get_tool_schemas() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/test_provider.py -q`
Expected: FAIL — `ImportError: cannot import name 'ObsidianMemoryProvider'`.

- [ ] **Step 3: Write minimal implementation**

Replace `plugins/memory/obsidian/__init__.py` (was empty) with:

```python
"""Obsidian-minnesprovider — FTS5-retrieval över valvet (Fas A)."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from plugins.memory.obsidian.config import build_obsidian_config
from plugins.memory.obsidian.index import ObsidianIndex


class ObsidianMemoryProvider(MemoryProvider):
    def __init__(self, config: "Dict[str, Any] | None" = None) -> None:
        self._cfg = build_obsidian_config(config)
        self._index: "ObsidianIndex | None" = None
        self._db_path = ""

    @property
    def name(self) -> str:
        return "obsidian"

    def is_available(self) -> bool:
        # Local, stdlib-only. Available iff the vault dir exists (no network).
        return os.path.isdir(self._cfg.vault_path)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home") or os.path.expanduser("~/.hermes")
        self._db_path = os.path.join(hermes_home, "obsidian_index.db")
        self._index = ObsidianIndex(self._db_path)
        try:
            self._index.sync_vault(
                self._cfg.vault_path, exclude_dirs=self._cfg.exclude_dirs
            )
        except OSError:
            pass  # vault unreadable — provider degrades to empty recall

    def get_tool_schemas(self) -> List[Dict]:
        return []  # Phase A: context-only, no tools

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._index is None or not query:
            return ""
        try:
            hits = self._index.search(query, top_k=self._cfg.top_k)
        except Exception:
            return ""
        if not hits:
            return ""
        blocks = []
        for h in hits:
            anchor = f"{h.path}#{h.heading}" if h.heading else h.path
            blocks.append(f"[[{anchor}]]\n{h.content}")
        return "## Från Obsidian-valvet\n\n" + "\n\n".join(blocks)

    def backup_paths(self) -> "list[str]":
        return [self._db_path] if self._db_path else []


def register(ctx) -> None:
    """Registrera obsidian-providern med plugin-systemet."""
    ctx.register_memory_provider(ObsidianMemoryProvider())
```

Create `plugins/memory/obsidian/plugin.yaml`:

```yaml
name: obsidian
version: 0.1.0
description: "Obsidian vault memory — local FTS5 retrieval over markdown notes (vault = source of truth, disposable SQLite index)."
```

> Implementer note: confirm the exact `MemoryProvider.__init__` signature and whether providers take a `config=` kwarg (check `plugins/memory/holographic/__init__.py:415-419` — holographic's `register` passes `config=_load_plugin_config()`). If the base `__init__` requires calling `super().__init__(...)`, add it. If provider config is loaded from a plugin config file rather than passed to the constructor, mirror holographic's `_load_plugin_config()` pattern and have `register(ctx)` pass it — but keep the constructor accepting an explicit `config` dict so the tests above work unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/ -q`
Expected: PASS (all obsidian tests: chunker, sanitizer, index, sync, search, config, provider).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/obsidian/__init__.py plugins/memory/obsidian/plugin.yaml tests/obsidian_plugin/test_provider.py
git commit -m "feat(obsidian): MemoryProvider with prefetch + registration"
```

---

### Task 8: Discovery wiring check + config docs

**Files:**
- Modify: `cli-config.yaml.example`
- No new test file — verify provider is discoverable + document config.

- [ ] **Step 1: Verify plugin discovery finds the provider**

Run:
```bash
uv run --extra dev python -c "
from plugins.memory import load_memory_provider
p = load_memory_provider('obsidian')
print('loaded:', p.name if p else None, '| available:', p.is_available() if p else None)
"
```
Expected: `loaded: obsidian | available: True/False`. If `None`, read `plugins/memory/__init__.py` discovery (`_iter_provider_dirs`, `_is_memory_provider_dir`, `load_memory_provider`) and fix the provider dir/`register` so it's detected (the dir must contain `register_memory_provider` or a `MemoryProvider` subclass — it does). Report findings if discovery needs a change.

- [ ] **Step 2: Document config**

Add to `cli-config.yaml.example` under the memory section (near `memory:`):

```yaml
# External memory provider (one at a time). Obsidian = local FTS5 retrieval
# over a markdown vault (the vault is the source of truth; the SQLite index is
# derived and disposable, stored outside the vault under HERMES_HOME).
memory:
  provider: obsidian   # enable the Obsidian provider
obsidian:
  vault_path: "/srv/dj/obsidian"
  top_k: 5
  # exclude_dirs: [".git", ".obsidian", ".trash"]
  # pinned: ["memory/core.md", "memory/daniel.md"]   # Phase B: always-on core
```

- [ ] **Step 3: Full obsidian suite green**

Run: `uv run --extra dev python -m pytest tests/obsidian_plugin/ -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add cli-config.yaml.example
git commit -m "docs(obsidian): document memory.provider=obsidian config"
```

---

## Operational (done by controller, NOT a subagent task)

After Phase A merges, activate on ONE profile and verify live recall:
- Set `memory.provider: obsidian` + `obsidian.vault_path` in one profile config (chown hermes), restart that gateway.
- Confirm the index DB is created under that profile's HERMES_HOME, and that a message referencing vault content pulls the right note (check logs / a oneshot).
- Separately restore the migration-stranded `daily-note` + `kunskapsgraf.py` automation to hermes and set `OBSIDIAN_VAULT_PATH` in hermes `.env` (tracked in the design spec, not this code plan).

## Self-Review

**Spec coverage (Phase A scope):**
- Purpose-built provider, not holographic → all tasks build `plugins/memory/obsidian/`. ✓
- FTS5 base, no new deps → stdlib `sqlite3` only (Tasks 3-5). ✓
- Vault = source of truth, disposable index keyed on path+mtime+hash → Tasks 3-4. ✓
- Chunk by heading, strip frontmatter → Task 1. ✓
- Incremental sync (re-index changed, delete removed) → Task 4. ✓
- Query-relevant prefetch → Tasks 5, 7. ✓
- Index outside vault, backup_paths → Task 7. ✓
- Decisions testable without model calls → all tests use fixtures/`:memory:`, no model. ✓
- Semantic embeddings / write-back / hot-core → explicitly Phase B/C, `pinned` parsed but unused (Task 6). Correctly out of Phase A scope.

**Placeholder scan:** One "Implementer note" in Task 7 (confirm `MemoryProvider.__init__`/config-passing against holographic) — a real verification step against the codebase, not a placeholder; tests pin the behavior.

**Type consistency:** `Chunk(heading_trail, content)`, `SearchHit(path, heading, content, score)`, `ObsidianConfig(vault_path, top_k, exclude_dirs, pinned)`, `ObsidianIndex.search(query, top_k)` used consistently across tasks. `prefetch(query, *, session_id="")` matches `MemoryProvider` ABC.
