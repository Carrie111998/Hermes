# Patching `hermes_state.py` — the 4 dual-write hooks

This is the only file of Hermes core you need to modify. The patch adds 4
small hooks that call into `tools/mysql_mirror.py` after each SQLite write.

> Apply once per Hermes installation (the source tree is shared by all
> profiles). Each profile only needs its own `.env` + gateway restart.
> Prerequisite: steps 1-3 of the README (database, tables, module in place).

## Target files

| File | Action | Purpose |
|---|---|---|
| `~/.hermes/hermes-agent/tools/mysql_mirror.py` | new file | mirror module (from this repo) |
| `~/.hermes/hermes-agent/hermes_state.py` | modify 4 spots | write hooks |

Dependency: `pymysql` inside the Hermes venv:

```bash
cd ~/.hermes/hermes-agent && venv/bin/pip install pymysql
```

## Step 1: place the module

Copy `mysql_mirror.py` from this repo to
`~/.hermes/hermes-agent/tools/mysql_mirror.py`.

## Step 2: add the 4 hooks

⚠️ Before patching, `grep` to confirm the anchors still exist — `hermes update`
changes line numbers and sometimes method names. Match anchors, not line
numbers. All hook blocks are tagged with a `[mysql-mirror patch]` comment so
you can find them later with `grep -c "mysql-mirror patch"` (expect 4).

### Hook 1: end of `_insert_session_row` (session row mirror)

Locate the final `self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)`
inside `_insert_session_row` (the one directly before the `create_session`
method definition). Append after it:

```python
        # [mysql-mirror patch] Dual-write the resulting session row to MySQL.
        # Read-back from SQLite (authoritative) — best-effort, never raises.
        try:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                from tools.mysql_mirror import mirror_session
                mirror_session(dict(row))
        except Exception:
            pass
```

### Hook 2: `append_message` return (single message mirror)

At the end of `append_message`, change:

```python
        return self._execute_write(
            _do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S
        )
```

to:

```python
        msg_id = self._execute_write(
            _do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S
        )
        # [mysql-mirror patch] Dual-write this message to MySQL (best-effort).
        try:
            from tools.mysql_mirror import mirror_message
            mirror_message(
                msg_id, session_id, role, content=stored_content,
                tool_call_id=tool_call_id, tool_calls=tool_calls_json,
                tool_name=tool_name, timestamp=message_timestamp,
                token_count=token_count, finish_reason=finish_reason,
                reasoning=reasoning, reasoning_content=reasoning_content,
                reasoning_details=reasoning_details,
                codex_reasoning_items=codex_reasoning_items,
                codex_message_items=codex_message_items,
                platform_message_id=platform_message_id,
                observed=observed, active=1,
                effect_disposition=effect_disposition,
                api_content=api_content if isinstance(api_content, str) else None,
                display_kind=display_kind if isinstance(display_kind, str) else None,
                display_metadata=json.loads(display_metadata_json)
                if isinstance(display_metadata_json, str) else display_metadata_json,
            )
        except Exception:
            pass
        return msg_id
```

Note: several methods end with the same `return self._execute_write(...)`
line — only patch the one inside `append_message`.

### Hook 3: `append_messages_batch` return (batch mirror)

Locate the non-recursive `_do` inside `append_messages_batch` (marked by the
comment `# Same criticality as append_message: this IS the turn's transcript.`).
Change:

```python
        return self._execute_write(
            _do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S
        )
```

to:

```python
        inserted = self._execute_write(
            _do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S
        )
        # [mysql-mirror patch] Dual-write the batch to MySQL (best-effort).
        # The input dicts carry no row ids (SQLite autoincrement assigns
        # them), so read the inserted rows back from the authoritative DB.
        try:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, inserted),
            ).fetchall()
            if rows:
                from tools.mysql_mirror import mirror_messages_batch
                mirror_messages_batch(session_id, [dict(r) for r in reversed(rows)])
        except Exception:
            pass
        return inserted
```

Key point: the input message dicts have **no `id` field** (assigned by SQLite
autoincrement). You must read the inserted rows back and mirror those, not
the inputs — otherwise MySQL gets `id = NULL`.

### Hook 4: end of `_record_model_usage` (usage mirror)

Locate the method that incrementally accumulates into `session_model_usage`
via `INSERT ... ON CONFLICT` (named `_record_model_usage` in Hermes 0.20.x;
older versions called it `_record_usage_delta` — match the behavior, not the
name). Append after the final `conn.execute(...)`:

```python
        # [mysql-mirror patch] UPSERT this usage row to MySQL (best-effort).
        # Mirrors the accumulated totals by reading the row back after the
        # SQLite upsert — keeps MySQL consistent without reimplementing the
        # ON CONFLICT delta logic.
        try:
            usage_row = conn.execute(
                "SELECT * FROM session_model_usage WHERE session_id = ? "
                "AND model = ? AND billing_provider = ? "
                "AND billing_base_url = ? AND billing_mode = ? AND task = ?",
                (session_id, eff_model, eff_provider, eff_base_url,
                 eff_billing_mode, task or ""),
            ).fetchone()
            if usage_row is not None:
                from tools.mysql_mirror import mirror_usage
                mirror_usage(session_id, eff_model, dict(usage_row))
        except Exception:
            pass
```

## Step 3: verify

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -m py_compile hermes_state.py tools/mysql_mirror.py   # syntax
venv/bin/python -m pytest tests/test_hermes_state.py -q -o addopts=   # regression
```

End-to-end test (temporary directory, never touches your real state.db):

```bash
cd ~/.hermes/hermes-agent && venv/bin/python << 'EOF'
import os, tempfile, pathlib
os.environ['MYSQL_MIRROR_HOST'] = 'your-host'
os.environ['MYSQL_MIRROR_PASSWORD'] = 'your-password'
os.environ['MYSQL_MIRROR_MACHINE'] = 'patch-test'
os.environ['HERMES_HOME'] = os.path.expanduser('~/.hermes')  # target profile home
import sys; sys.path.insert(0, '.')
tmp = tempfile.mkdtemp()
from hermes_state import SessionDB
db = SessionDB(db_path=pathlib.Path(tmp) / 'test.db')
db.create_session('e2e-check', 'test')
db.append_message('e2e-check', 'user', 'single test')
db.append_messages_batch('e2e-check', [
    {'role': 'assistant', 'content': 'b1'},
    {'role': 'tool', 'content': '{}', 'tool_name': 'x'},
])
db.close()
print("OK — now check MySQL, then DELETE the test rows")
EOF
```

Confirm in MySQL that session `e2e-check` and its 3 messages exist, then
delete the test rows.

Disabled-path check: clear all `MYSQL_MIRROR_*` env vars and repeat — no
errors, no new rows in MySQL.

## Step 4: restart

The patch only affects new processes:

```bash
hermes gateway restart            # current profile
hermes gateway restart -p other   # another profile
```

Conversations that happened before the restart are not backfilled
automatically — run `migrate.py` once to catch up.

## When to re-apply

`hermes update` overwrites `hermes_state.py`. Check:

```bash
grep -c 'mysql-mirror patch' ~/.hermes/hermes-agent/hermes_state.py
# must be 4; fewer means hooks were lost — re-apply this guide
ls ~/.hermes/hermes-agent/tools/mysql_mirror.py  # confirm module still exists
```

`tools/mysql_mirror.py` is a new file and normally survives updates;
the 4 hooks in `hermes_state.py` will NOT survive and must be re-applied.
