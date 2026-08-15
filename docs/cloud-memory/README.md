# Hermes MySQL Mirror

A dual-write mirror for [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s
SQLite `state.db` into MySQL / MariaDB.

Hermes only supports a local SQLite session database. If you run Hermes on
multiple machines, want an off-box backup of every conversation, or want to
run analytics / distillation pipelines over your full history with plain SQL,
you currently have to copy `state.db` around by hand. This project fixes that:
**every write to `state.db` is mirrored to MySQL in real time**, safely and
with zero impact when the database is unreachable.

## Background

I run Hermes on several machines — a desktop, a laptop, a NAS box. They share
the same persona (`SOUL.md`), the same skills, the same configuration. But
their *memories* live in isolated per-machine `state.db` files, so they don't
know what the others did.

Concretely: on machine A I worked with the agent on project A — decisions
were made, pitfalls were documented, a working approach was established. Days
later, on machine B, I ask for "the same approach as project A". The agent on
machine B has never heard of it. Same person, same persona, blank memory.

Hermes has no built-in answer for this today: SQLite only, local only.

This project is my answer: every machine dual-writes its full session
history into one shared MySQL/MariaDB database (each row keyed by `machine`).
Suddenly memory is cross-machine and cross-region:

- Any machine can query the complete, merged history with plain SQL.
- Backup and disaster recovery come for free (`restore.py`).
- Higher-level memory pipelines — e.g. a daily distillation job that reads
  MySQL and produces semantic long-term memory — get all machines' context
  in one place instead of N scattered SQLite files.

SQLite stays the source of truth and the runtime path; MySQL is the shared
long-term store. When MySQL is unreachable, everything degrades back to
stock single-machine behavior.

## Design goals

- **Non-invasive**: 4 small hooks in `hermes_state.py` + 1 new module
  (`tools/mysql_mirror.py`). No schema changes to SQLite, no new services.
- **Never breaks the primary path**: every mirror call is wrapped in
  `try/except`. If MySQL is down, unreachable, or unconfigured, Hermes keeps
  working exactly as before and the mirror silently disables itself.
- **Multi-machine safe**: all mirrored tables carry a `machine` column that is
  part of the primary key, so several machines can mirror into the same
  database without id collisions.
- **Idempotent**: sessions/usages are UPSERTed, messages are REPLACEd. You can
  re-run migration or catch-up scripts any number of times.
- **Restorable**: `restore.py` rebuilds a `state.db` (new machine, disaster)
  from the MySQL mirror, preserving original row ids so writes continue
  seamlessly.

## What gets mirrored

| SQLite table | MySQL table | Mode |
|---|---|---|
| sessions | sessions | UPSERT |
| messages | messages | REPLACE |
| session_model_usage | session_model_usage | UPSERT (accumulated totals) |

FTS index tables and runtime tables (async_delegations, gateway_routing, ...)
are not mirrored - they are derivable or machine-local.

## Repository layout

```
mysql_mirror.py   Mirror module (tools/mysql_mirror.py, already built into this fork)
schema.sql        MariaDB/MySQL schema (4 tables)
migrate.py        One-time backfill + catch-up: state.db -> MySQL
restore.py        Disaster recovery: MySQL -> fresh state.db
PATCH.md          Reference for the 4 hermes_state.py hooks (already applied in this fork)
```

## Quick start

Prerequisites: a MySQL/MariaDB server (a NAS box works great), Python 3.9+,
`pymysql` installed in the Hermes venv.

1. Create a database and tables:

   ```bash
   mysql -h <host> -u <user> -p -e "CREATE DATABASE hermes CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
   mysql -h <host> -u <user> -p hermes < schema.sql
   ```

   Use one database per Hermes profile. The database name is derived from the
   profile directory (`~/.hermes/profiles/foo` -> db `foo`; default home ->
   db `hermes`).

2. Add credentials to the profile's `.env` (this is the only secret store;
   unconfigured = mirror disabled, zero impact):

   ```
   MYSQL_MIRROR_HOST=10.0.0.5
   MYSQL_MIRROR_PORT=3306
   MYSQL_MIRROR_USER=hermes
   MYSQL_MIRROR_PASSWORD=change-me
   MYSQL_MIRROR_MACHINE=desktop
   ```

   `MYSQL_MIRROR_MACHINE` must be globally unique per machine (it keys all
   rows). Pick anything stable, e.g. the hostname.

3. Install the dependency:

   ```bash
   cd hermes-agent && venv/bin/pip install pymysql
   ```

4. The 4 `hermes_state.py` hooks and `tools/mysql_mirror.py` are already
   applied in this fork — see [PATCH.md](PATCH.md) if you prefer to apply
   them yourself onto a stock checkout.

5. Backfill existing history (also used as catch-up after downtime):

   ```bash
   venv/bin/python docs/cloud-memory/migrate.py \
       --db ~/.hermes/profiles/foo/state.db
   ```

6. Restart the gateway:

   ```bash
   hermes gateway restart
   ```

7. Verify: send a message in any channel, then

   ```sql
   SELECT machine, MAX(id) FROM messages GROUP BY machine;
   ```

## Daily operations

**Health check** - compare max ids between SQLite and MySQL:

```sql
SELECT MAX(id) FROM messages;
```

Equal = in sync. If MySQL is behind, the gateway was probably restarted after
a gap; re-run `migrate.py` (it only copies rows with id > current MySQL max,
so it doubles as catch-up).

**Disaster recovery / new machine** - same Quick start, but instead of step 5:

```bash
venv/bin/python docs/cloud-memory/restore.py \
    --db ~/.hermes/profiles/foo/state.db --machine desktop
```

`restore.py` re-inserts rows with their original ids and bumps
`sqlite_sequence`, so new messages continue from max(id)+1 instead of
colliding with mirrored history. Set `MYSQL_MIRROR_MACHINE` on the new
machine to the *restored* machine name so ids continue the same sequence.

**After `hermes update`** - updates overwrite `hermes_state.py`. Check and
re-apply the hooks:

```bash
grep -c "mysql-mirror patch" ~/.hermes/hermes-agent/hermes_state.py  # must be 4
```

Then restart the gateway.

## Design notes & pitfalls (learned the hard way)

- `append_messages_batch` input dicts carry no row ids (SQLite autoincrement
  assigns them) - the hook must read the inserted rows back from SQLite and
  mirror those, otherwise MySQL gets `id = NULL` rows.
- Timestamps are stored as `DOUBLE` Unix seconds, exactly like SQLite. No
  DATETIME conversion - keeps row-by-row reconciliation trivial.
- Always use parameterized SQL with `executemany`; naive string formatting
  breaks on `%` characters inside message content.
- Long-running pymysql connections can leave lock waits if killed mid
  transaction; the mirror uses `autocommit=True` and short timeouts.
- The mirror is a *mirror*, not the source of truth. If MySQL and SQLite ever
  disagree, SQLite wins.
- Env precedence: variables already present in the environment take priority
  over the profile `.env` (the scripts only fill in what is missing). If you
  ever exported `MYSQL_MIRROR_*` in your shell, stale values will silently
  override your `.env` — check with `env | grep MYSQL_MIRROR`.

## Scope

This mirrors the session transcript store (`state.db`). It pairs nicely with
higher-level memory pipelines (e.g. daily distillation jobs that read MySQL
instead of a local SQLite file), but it deliberately does not implement any
memory strategy itself.

## License

MIT
