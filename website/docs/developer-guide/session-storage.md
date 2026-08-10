# Session Storage

Hermes Agent uses a SQLite database (`~/.hermes/state.db`) to persist session
metadata, full message history, and model configuration across CLI and gateway
sessions. This replaces the earlier per-session JSONL file approach.

Source file: `hermes_state.py`


## Architecture Overview

```
~/.hermes/state.db (SQLite, WAL mode)
├── sessions              — Session metadata, token counts, billing
├── messages              — Full message history per session
├── session_model_usage   — Per-model/per-task usage attribution rows
├── messages_fts          — FTS5 virtual table (content + tool_name + tool_calls)
├── messages_fts_trigram  — FTS5 virtual table with trigram tokenizer (CJK / substring search)
├── messages_fts_cjk      — FTS5 virtual table with cjk_unicode61 tokenizer
├── state_meta            — Key/value metadata table
├── gateway_routing       — Gateway routing metadata
├── compression_locks     — Cross-process compression locking
├── async_delegations     — Async delegation bookkeeping
└── schema_version        — Single-row table tracking migration state
```

Key design decisions:
- **WAL mode** for concurrent readers + one writer (gateway multi-platform)
- **FTS5 virtual table** for fast text search across all session messages
- **Session lineage** via `parent_session_id` chains (compression-triggered splits)
- **Source tagging** (`cli`, `telegram`, `discord`, etc.) for platform filtering
- Batch runner and RL trajectories are NOT stored here (separate systems)


## SQLite Schema

### Sessions Table

Abridged — see `SCHEMA_SQL` in `hermes_state.py` for the full current column list
(which also includes gateway routing metadata such as `session_key`, `chat_id`,
`chat_type`, `thread_id`, `display_name`, `origin_json`, `expiry_finalized`,
workspace fields `cwd` / `git_branch` / `git_repo_root`, handoff and
compression-failure fields, `profile_name`, `rewind_count`, `archived`, and
`pinned`):

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    -- ... additional gateway/workspace/handoff/compression columns ...
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique
    ON sessions(title) WHERE title IS NOT NULL;
```

### Messages Table

Abridged — the full schema also includes `effect_disposition`,
`platform_message_id`, `observed`, `active`, `compacted`, `api_content`,
`display_kind`, and `display_metadata`:

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
    -- ... additional display/compaction columns ...
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);
```

Notes:
- `tool_calls` is stored as a JSON string (serialized list of tool call objects)
- `reasoning_details`, `codex_reasoning_items`, and `codex_message_items` are stored as JSON strings
- `reasoning` stores the raw reasoning text for providers that expose it
- `api_content` is a byte-fidelity sidecar: the exact content string sent to the API for this message when it differs from `content` (ephemeral memory/plugin injections, persist overrides). It preserves the wire bytes for prompt-cache-stable replay — stored as sent, except lone surrogates, which sqlite3 cannot bind and which the conversation loop scrubs from every outgoing payload anyway. `NULL` means `content` was sent verbatim.
- Timestamps are Unix epoch floats (`time.time()`)

### FTS5 Full-Text Search

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);
```

The FTS5 table is kept in sync via three triggers that fire on INSERT, UPDATE,
and DELETE of the `messages` table. The current triggers are gated on the
`fts_rebuild_high_water` / `fts_rebuild_progress` markers in `state_meta` (so a
background FTS rebuild can proceed without double-indexing) and cover all three
indexed columns — see `SCHEMA_SQL` in `hermes_state.py` for the exact SQL.


## Schema Version and Migrations

Current schema version: **23**

The `schema_version` table stores a single integer. Simple column additions are handled declaratively by `_reconcile_columns()` (which diffs live columns against `SCHEMA_SQL` and ADDs any missing ones). The version-gated chain is reserved for data migrations and index/FTS changes that can't be expressed declaratively:

| Version | Change |
|---------|--------|
| 1 | Initial schema (sessions, messages, FTS5) |
| 2 | Add `finish_reason` column to messages |
| 3 | Add `title` column to sessions |
| 4 | Add unique index on `title` (NULLs allowed, non-NULL must be unique) |
| 5 | Add billing columns: `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `billing_provider`, `billing_base_url`, `billing_mode`, `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`, `pricing_version` |
| 6 | Add reasoning columns to messages: `reasoning`, `reasoning_details`, `codex_reasoning_items` |
| 7 | Add `reasoning_content` column to messages |
| 8 | Add `api_call_count` column to sessions |
| 9 | Add `codex_message_items` column to messages for Codex Responses message id/phase replay |
| 10 | Add `messages_fts_trigram` virtual table (trigram tokenizer for CJK / substring search) and backfill existing rows |
| 11 | Re-index `messages_fts` and `messages_fts_trigram` to cover `tool_name` + `tool_calls` and switch from external-content to inline mode; drop old triggers and backfill every message row |
| 16 | Tag delegate subagent rows in `model_config` (`$._delegate_from`) so session pickers stay clean after parent deletes orphan them |
| 18 | Gateway metadata consolidation — backfill `display_name` / `origin_json` / `expiry_finalized` from `sessions.json` |
| 20 | Per-model usage attribution — seed `session_model_usage` rows from historical per-session aggregate totals |
| 22 | Task-dimension usage attribution — rebuild `session_model_usage` so the `task` column participates in the PRIMARY KEY |
| 23 | FTS storage redesign — external-content FTS tables replacing the v11 inline-mode copies (opt-in transition for existing DBs) |

Versions not listed above were declarative column additions handled by `_reconcile_columns()` (version bump only, no data migration).

Declarative column adds use `ALTER TABLE ADD COLUMN` wrapped in try/except to handle the column-already-exists case (idempotent). The version number is bumped after each successful migration block.


## Write Contention Handling

Multiple hermes processes (gateway + CLI sessions + worktree agents) share one
`state.db`. The `SessionDB` class handles write contention with:

- **Short SQLite timeout** (1 second) instead of the default 30s
- **Application-level retry** with random jitter (20-150ms, up to 15 retries)
- **BEGIN IMMEDIATE** transactions to surface lock contention at transaction start
- **Periodic WAL checkpoints** every 50 successful writes (PASSIVE mode)

This avoids the "convoy effect" where SQLite's deterministic internal backoff
causes all competing writers to retry at the same intervals.

```
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
_CHECKPOINT_EVERY_N_WRITES = 50
```


## Connection Ownership and Failure Cleanup

SQLite connections are resources, not ordinary Python values. A connection can
keep descriptors for `state.db`, `state.db-wal`, and `state.db-shm` open. The
descriptor lifetime must therefore be tied to an explicit owner and an
explicit shutdown path. Python garbage collection is not a correctness
mechanism here: a failed constructor may never return an object whose
destructor could clean up, and `SessionDB` intentionally keeps strong tracking
references for lock-safety.

### Ownership model

`SessionDB` owns every SQLite connection it opens. The caller owns the
`SessionDB` instance and must call `close()` when that instance is no longer
needed.

| Resource | Created by | Owner after creation | Cleanup |
| --- | --- | --- | --- |
| Writable connection (`_conn`) | `SessionDB.__init__` | `SessionDB` instance | Constructor `finally` on failed init; `SessionDB.close()` after successful init |
| Read-only connection (`_conn` for `read_only=True`) | `SessionDB.__init__` | `SessionDB` instance | Same as writable connection; no WAL checkpoint on close |
| Per-thread WAL reader | `_get_read_conn()` | `SessionDB` instance, held in `_read_conns` | Setup `finally` before registration; `SessionDB.close()` after registration |
| Background token writer | `SessionDB` instance | `SessionDB` instance | `_stop_token_writer()` during `close()` before writer connection closes |
| `SessionDB` supplied by a caller | Caller | Original caller | The callee must not close it |

The last row is the important boundary for reusable helpers. A helper that
receives a database handle did not create that handle and must not close it.
A helper that opens its own handle must close it, including when the query or
formatter raises.

The safe default shape is:

```python
db = SessionDB(db_path=path, read_only=True)
try:
    rows = db.list_sessions_rich(limit=20)
finally:
    db.close()
```

For an optional injected handle, keep ownership explicit:

```python
def load_rows(*, db=None, db_path=None):
    owns_db = db is None
    db = db or SessionDB(db_path=db_path, read_only=True)
    try:
        return db.list_sessions_rich(limit=20)
    finally:
        if owns_db:
            db.close()
```

Do not use a blanket `finally: db.close()` when `db` can be supplied by a
caller. That fixes one leak by closing a resource still in use by another
owner.

### Constructor failure safety

Opening a connection is only the first step of initialization. The writable
path can still fail while enabling WAL, applying pragmas, loading the optional
FTS tokenizer, reconciling schema, repairing malformed schema, or retrying a
contended open. The read-only path can fail while applying read pragmas or
probing FTS tables.

The lifecycle is deliberately two-phase:

```text
allocate SessionDB state
        |
        v
connect and register SQLite resource
        |
        v
WAL / pragmas / FTS / schema / repair setup
        |
   success? -------------------- no
        |                         |
        v                         v
mark initialization complete   detach connection, close it, re-raise
        |
        v
return owned SessionDB to caller
```

`__init__` starts with `initialization_complete = False`. Its outer
`finally` detaches and closes `_conn` unless initialization reaches the
success point. Detaching first (`self._conn = None`) prevents later cleanup
from treating a failed, partially initialized object as usable. The close
helper is best-effort so a cleanup error never hides the original database
error. The `finally` also runs for `BaseException` paths such as interruption;
cleanup must not depend on an `except Exception` branch being entered.

Lock retries follow the same rule. When one initialization attempt opens a
connection and then receives a retryable `locked`/`busy` error, that attempt's
connection is closed before the next attempt sleeps. Otherwise a 20-second
patience window can create one leaked descriptor per retry.

The read-only setup has an additional local guard. If a reader is opened but
fails during pragma or FTS probing, it is removed from the instance and closed
immediately. If setup succeeds far enough to register it, `close()` owns the
remaining cleanup.

### WAL readers and worker threads

WAL reads use a separate read-only connection per reader thread so search and
browse operations do not queue behind the shared writer lock. This creates a
second lifecycle that a writer-only cleanup cannot see:

1. `_get_read_conn()` opens the reader with `check_same_thread=False`.
2. The connection is added to the instance-wide `_read_conns` set under
   `_read_conns_lock`.
3. The thread-local reference is used for later reads.
4. `SessionDB.close()` sets `_read_conns_closed`, snapshots and clears the set,
   closes every registered reader, and clears the current thread's local
   reference.

The strong set is intentional. Thread-local storage alone would allow a short
lived worker thread to disappear while the tracked SQLite connection remains
open. The registration lock closes the race where `close()` drains the set
while another thread is still finishing connection setup: a late opener sees
`_read_conns_closed`, does not register, and closes itself in its local
`finally` block.

`check_same_thread=False` is used for this managed reader lifecycle so the
owner can drain a reader created on another worker. It does not make cursors or
arbitrary concurrent operations safe. The normal contract remains: one
`SessionDB` owner controls shutdown, and query code uses the connection only
for its read operation.

When WAL is unavailable and SQLite falls back to DELETE journal mode, the
read path uses the legacy locked writer connection instead of opening a
per-thread reader. That fallback has fewer descriptors, but it does not remove
the requirement to close the writer.

### Timeout and future ownership

Cancellation of a wait is not cancellation of a Python thread. If a worker
constructing `SessionDB()` outlives the caller's timeout, the worker can still
return a live database after the caller has already degraded or moved on.

The owner of the future must handle both outcomes:

```python
future = executor.submit(SessionDB, db_path)
try:
    db = future.result(timeout=timeout)
except TimeoutError:
    future.add_done_callback(_close_late_session_db)
    db = None
```

The callback closes a successful late result and ignores a future that failed
before returning a database. Dropping the future, calling `cancel()`, or
closing only the database received before the timeout is insufficient.

### Caller ownership matrix

The following paths are representative and define the expected pattern for
new code:

| Caller | Handle type | Required behavior |
| --- | --- | --- |
| `tools/session_search_tool.py` | Lazy local handle | Close in the wrapper `finally` after discovery, scroll, or browse |
| `tools/session_search_tool.py` | Caller-injected shared handle | Use it; leave it open for the caller |
| Cross-profile session search | Dedicated read-only handle | Close after the single operation |
| `tools/react_to_message_tool.py` | Per-reaction local handle | Close even when the reaction write raises |
| `agent/trace_upload.py` | Loader-owned handle | Close after messages and metadata are read, before upload continues |
| `mcp_serve.py` | Per-request read or poll handle | Close at the request boundary; do not retain it in an event result |
| CLI and gateway insights | Command-local handle | Close when report generation or formatting raises |
| `hermes_cli/sessions_cmd.py` | Repair/statistics probe | Close after the probe, including error paths |
| `cron/scheduler.py` | Future-created handle | Close normal results and late results after timeout |

This matrix is an ownership rule, not a list of optional cleanup suggestions.
Every new `SessionDB()` call should answer two questions in code review:
"Who owns this instance?" and "Which `finally` closes it if the next line
raises?"

### What `close()` does

`SessionDB.close()` performs shutdown in dependency order:

1. Stop the background token writer and drain work that still needs `_conn`.
2. Unregister the atexit drain hook so a closed instance is not retained until
   interpreter exit.
3. Mark reader registration closed, drain registered WAL readers, and close
   them outside the reader-set lock.
4. For writable stores, attempt a best-effort `PRAGMA wal_checkpoint(TRUNCATE)`
   to reduce sidecar size. Read-only stores never request a checkpoint.
5. Close and clear the writer connection.

Checkpoint failure is logged and does not prevent descriptor cleanup. Shutdown
must remain idempotent enough for error paths: a caller may close an instance
after a partial operation or while another subsystem is already degrading.

### Diagnosing `EMFILE`

`EMFILE` (`Too many open files`) is usually the final symptom, not the first
failure. Inspect the sequence that precedes it:

| Observation | Likely ownership bug |
| --- | --- |
| Live connection count rises after repeated failed `SessionDB()` opens | Constructor failure path did not close a partial writer or read-only probe |
| `state.db-wal` and `state.db-shm` descriptors grow during search/browse | Per-thread WAL readers were not closed or were not retained for shutdown |
| Leak appears only after worker timeout | Late future result was returned without a completion callback |
| Shared session search starts failing after a helper returns | Helper closed an injected handle it did not own |
| `close()` logs cross-thread SQLite errors | Reader connection was thread-bound or shutdown raced registration |

Do not delete `state.db-wal` or `state.db-shm` while Hermes or another process
may still have the database open. Stop the owning process, allow its
`SessionDB.close()` path to run, then inspect remaining descriptors with the
OS-appropriate process/file-handle tool. Removing sidecars while a live WAL
writer exists can destroy uncheckpointed transactions.

### Regression contract

Connection cleanup is part of the storage contract. Changes to initialization,
WAL readers, async token accounting, or helpers that open `SessionDB` should
preserve tests for:

- failed writable initialization;
- failed WAL reader setup;
- failed read-only initialization;
- a reader created on a worker thread and closed by its owner;
- lazy, cross-profile, CLI, gateway, MCP, reaction, and trace-upload cleanup;
- a late `SessionDB` result after a timeout.

The focused tests live in `tests/test_hermes_state.py` and the caller-specific
tests under `tests/tools/`, `tests/agent/`, `tests/cli/`, `tests/cron/`, and
`tests/test_mcp_serve.py`. A new connection path without a matching failure
test is incomplete, even if the happy-path query passes.

Do not solve this class of bug with `__del__`, a process-wide connection cache,
or a broad `except` that hides the original SQLite error. Keep ownership local,
close in `finally`, preserve the original exception, and test the failure
point immediately after the connection is opened.


## Common Operations

### Initialize

```python
from hermes_state import SessionDB

db = SessionDB()                           # Default: ~/.hermes/state.db
db = SessionDB(db_path=Path("/tmp/test.db"))  # Custom path
```

### Create and Manage Sessions

```python
# Create a new session
db.create_session(
    session_id="sess_abc123",
    source="cli",
    model="anthropic/claude-sonnet-4.6",
    user_id="user_1",
    parent_session_id=None,  # or previous session ID for lineage
)

# End a session
db.end_session("sess_abc123", end_reason="user_exit")

# Reopen a session (clear ended_at/end_reason)
db.reopen_session("sess_abc123")
```

### Store Messages

```python
msg_id = db.append_message(
    session_id="sess_abc123",
    role="assistant",
    content="Here's the answer...",
    tool_calls=[{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}],
    token_count=150,
    finish_reason="stop",
    reasoning="Let me think about this...",
)
```

### Retrieve Messages

```python
# Raw messages with all metadata
messages = db.get_messages("sess_abc123")

# OpenAI conversation format (for API replay)
conversation = db.get_messages_as_conversation("sess_abc123")
# Returns: [{"role": "user", "content": "..."}, {"role": "assistant", ...}]
```

### Session Titles

```python
# Set a title (must be unique among non-NULL titles)
db.set_session_title("sess_abc123", "Fix Docker Build")

# Resolve by title (returns most recent in lineage)
session_id = db.resolve_session_by_title("Fix Docker Build")

# Auto-generate next title in lineage
next_title = db.get_next_title_in_lineage("Fix Docker Build")
# Returns: "Fix Docker Build #2"
```


## Full-Text Search

The `search_messages()` method supports FTS5 query syntax with automatic
sanitization of user input.

### Basic Search

```python
results = db.search_messages("docker deployment")
```

### FTS5 Query Syntax

| Syntax | Example | Meaning |
|--------|---------|---------|
| Keywords | `docker deployment` | Both terms (implicit AND) |
| Quoted phrase | `"exact phrase"` | Exact phrase match |
| Boolean OR | `docker OR kubernetes` | Either term |
| Boolean NOT | `python NOT java` | Exclude term |
| Prefix | `deploy*` | Prefix match |

### Filtered Search

```python
# Search only CLI sessions
results = db.search_messages("error", source_filter=["cli"])

# Exclude gateway sessions
results = db.search_messages("bug", exclude_sources=["telegram", "discord"])

# Search only user messages
results = db.search_messages("help", role_filter=["user"])
```

### Search Results Format

Each result includes:
- `id`, `session_id`, `role`, `timestamp`
- `snippet` — FTS5-generated snippet with `>>>match<<<` markers
- `context` — 1 message before and after the match (content truncated to 200 chars)
- `source`, `model`, `session_started` — from the parent session

The `_sanitize_fts5_query()` method handles edge cases:
- Strips unmatched quotes and special characters
- Wraps hyphenated terms in quotes (`chat-send` → `"chat-send"`)
- Removes dangling boolean operators (`hello AND` → `hello`)


## Session Lineage

Sessions can form chains via `parent_session_id`. This happens when context
compression triggers a session split in the gateway.

### Query: Find Session Lineage

```sql
-- Find all ancestors of a session
WITH RECURSIVE lineage AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN lineage l ON s.id = l.parent_session_id
)
SELECT id, title, started_at, parent_session_id FROM lineage;

-- Find all descendants of a session
WITH RECURSIVE descendants AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN descendants d ON s.parent_session_id = d.id
)
SELECT id, title, started_at FROM descendants;
```

### Query: Recent Sessions with Preview

```sql
SELECT s.*,
    COALESCE(
        (SELECT SUBSTR(m.content, 1, 63)
         FROM messages m
         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
         ORDER BY m.timestamp, m.id LIMIT 1),
        ''
    ) AS preview,
    COALESCE(
        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
        s.started_at
    ) AS last_active
FROM sessions s
ORDER BY s.started_at DESC
LIMIT 20;
```

### Query: Token Usage Statistics

```sql
-- Total tokens by model
SELECT model,
       COUNT(*) as session_count,
       SUM(input_tokens) as total_input,
       SUM(output_tokens) as total_output,
       SUM(estimated_cost_usd) as total_cost
FROM sessions
WHERE model IS NOT NULL
GROUP BY model
ORDER BY total_cost DESC;

-- Sessions with highest token usage
SELECT id, title, model, input_tokens + output_tokens AS total_tokens,
       estimated_cost_usd
FROM sessions
ORDER BY total_tokens DESC
LIMIT 10;
```


## Export and Cleanup

```python
# Export a single session with messages
data = db.export_session("sess_abc123")

# Export all sessions (with messages) as list of dicts
all_data = db.export_all(source="cli")

# Delete old sessions (only ended sessions)
deleted_count = db.prune_sessions(older_than_days=90)
deleted_count = db.prune_sessions(older_than_days=30, source="telegram")

# Clear messages but keep the session record
db.clear_messages("sess_abc123")

# Delete session and all messages
db.delete_session("sess_abc123")
```


## Database Location

Default path: `~/.hermes/state.db`

This is derived from `hermes_constants.get_hermes_home()` which resolves to
`~/.hermes/` by default, or the value of `HERMES_HOME` environment variable.

The database file, WAL file (`state.db-wal`), and shared-memory file
(`state.db-shm`) are all created in the same directory.
