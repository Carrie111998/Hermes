# Hermes state.db — Safe Offline Recovery Guide

Applies to the Hermes session store (`state.db`) after the corruption class
from incident 2026-08-03 (SQLite B-tree corruption in the `messages` table +
malformed FTS indexes draining the kanban worker fleet).

> **Golden rule: the live DB is never the repair surface.** Every repair
> step below runs against a fresh timestamped copy. If anything goes wrong,
> the untouched original (or its backup) is still there.

Reference: `docs/design/state-db-corruption-worker-drain.md` (incident
trace + corruption classes + detector/repair machinery).

## 0. Where the DBs live

| Profile | state.db path |
|---|---|
| default | `~/.hermes/state.db` |
| named profile | `~/.hermes/profiles/<name>/state.db` |

Kanban dispatch now quarantines any profile whose store fails the
pre-dispatch probe (`hermes_cli.kanban_db.pre_dispatch_state_db_probe` →
`hermes_state._db_opens_cleanly`), so a corrupt store shows up as
`profile <name> store unhealthy: ...; worker blocked` on the board instead
of a silent worker drain. Fix the store, then unblock the tasks.

## 1. Detect — run the probe first

```bash
# Fast, read-only: prints a reason when the store is unhealthy, else nothing.
python3 - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, "/home/solo/.hermes/hermes-agent")
from hermes_state import _db_opens_cleanly
for p in ["/home/solo/.hermes/state.db",
          "/home/solo/.hermes/profiles/quill/state.db",
          "/home/solo/.hermes/profiles/orion/state.db"]:
    d = Path(p)
    print(p, "->", _db_opens_cleanly(d) if d.exists() else "missing")
PY
```

Also useful: `hermes doctor` and `hermes sessions` already run the same
probe machinery (`_db_opens_cleanly` / `repair_state_db_schema`).

## 2. Back up — always, before anything

```bash
TS=$(date +%Y%m%d_%H%M%S)
DB=/home/solo/.hermes/state.db
cp -a "$DB" "$DB.pre-recovery-$TS"                    # byte copy (crude but exact)
# Preferred: consistent online backup via SQLite's backup API
sqlite3 "$DB" ".backup '$DB.backup-$TS'" 2>/dev/null || \
python3 -c "import sqlite3,sys; src=sqlite3.connect('$DB'); dst=sqlite3.connect('$DB.backup-$TS'); src.backup(dst); dst.close(); src.close()"
```

Back up every store you might touch (default + each affected profile). Do
**not** rely on the auto-backups taken by `repair_state_db_schema` — take
your own before running any tool.

## 3. Verify the backup, never the live DB

Run these against the **backup copy**:

```bash
DBBK="$DB.backup-$TS"
sqlite3 "$DBBK" "PRAGMA integrity_check;"
sqlite3 "$DBBK" "PRAGMA foreign_key_check;"
```

- `integrity_check` returning only `ok` means the b-tree is coherent.
- `foreign_key_check` returning no rows means FK constraints hold.
- Any other output names the damaged objects (e.g. `wrong # of entries in
  index`, `database disk image is malformed`).

## 4. Repair classes — on a copy, not the live DB

The repair ladder (`hermes_state.repair_state_db_schema(db_path,
backup=True)`) already does the safe sequence with its own timestamped
backup: in-place FTS rebuild → `REINDEX` → `sqlite_master` dedup → drop FTS
+ VACUUM. Run it on a **copy** so the original stays byte-identical:

```bash
WORK=/tmp/state-repair-$$
mkdir -p "$WORK"
cp -a "$DB" "$WORK/state.db"
python3 - <<PY
import sys
sys.path.insert(0, "/home/solo/.hermes/hermes-agent")
from pathlib import Path
from hermes_state import _db_opens_cleanly, repair_state_db_schema
p = Path("$WORK/state.db")
print("before:", _db_opens_cleanly(p))
ok, note = repair_state_db_schema(p, backup=False)   # backup=False: we already copied
print("repaired:", ok, note)
print("after:  ", _db_opens_cleanly(p))
PY
```

### FTS-index-only corruption (Quill/Orion class from the incident)

If `integrity_check` passes but search / writes fail on the FTS tables,
rebuild the FTS indexes on the copy:

```bash
# Rebuild whichever FTS tables exist (trigram/cjk are conditional — a
# missing table is normal, not an error).
python3 - "$WORK/state.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'messages_fts%'")]
for t in tables:
    if t in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
        c.execute(f"INSERT INTO {t}({t}) VALUES('rebuild')")
        print("rebuilt", t)
c.close()
PY
```

Alternatively `REINDEX` all indexes on the copy:

```sql
sqlite3 /tmp/state-repair-XXXX/state.db "REINDEX;"
```

### `messages` table b-tree corruption (root/default incident class)

This class is **not** self-healing — every ladder pass fails with
`database disk image is malformed`. On the copy:

1. `PRAGMA integrity_check` to confirm the class.
2. Try `PRAGMA writable_schema=ON;`-free salvage **first** via the ladder
   above. If it reports `repaired: False`, do not fight the live file.
3. Move to row-level salvage **on the copy**:

```sql
-- On the COPY: export every readable row to a fresh DB.
sqlite3 /tmp/state-repair-XXXX/state.db \
  ".output /tmp/state-repair-XXXX/sessions.csv" "SELECT * FROM sessions;"
-- Then rebuild schema + re-import; keep unreadable rows aside (see §5).
```

## 5. Preserve unreadable rows — never silently drop

A salvage/restore may hit rows SQLite cannot read (the incident left 13
unreadable message rows). They must never vanish without a trace:

1. Before dropping anything, dump the **original backup** to a durable
   archive and record exactly which rows failed to read:
   ```bash
   python3 - <<PY
   import sqlite3
   conn = sqlite3.connect("$DB.backup-$TS")
   try:
       bad = conn.execute("SELECT rowid FROM messages").fetchall()
   except Exception as e:
       print("unreadable messages table:", e)
   PY
   ```
2. Keep the failing rows in a **separate backup** file (do not edit the
   live DB, do not delete the rows from the archive):
   ```bash
   cp -a "$DB.backup-$TS" "$DB.unreadable-rows-$TS"
   ```
3. Note the rowids / affected tables in a README next to that backup so a
   future human knows what is missing and why.
4. Only after the unreadable rows are archived may the repaired copy drop
   them. Never "clean up" by deleting the backup.

## 6. Restore — when repair is not possible

1. Stop Hermes processes that hold the store open (gateway / CLI / kanban
   dispatcher), or work on the copy and swap at the end.
2. Copy the repaired (or original backup) file into place:
   ```bash
   cp -a "$WORK/state.db" "$DB"
   ```
   Preserve permissions (`chown`/`chmod` to match the original; the DB is
   typically `0600` owned by the Hermes user).
3. Re-run the probe (§1) against the live path — it must return `None`.
4. Run `PRAGMA integrity_check;` and `PRAGMA foreign_key_check;` on the
   restored live DB.
5. Unblock any tasks the quarantine gate blocked:
   ```bash
   hermes kanban unblock <task-id>     # per task, or
   hermes kanban list --status blocked # find them
   ```
   The next dispatch tick re-probes the store; healthy → normal dispatch,
   and the queue-drain alert clears.

## 7. Verification checklist

- [ ] Fresh timestamped backup exists before any repair
- [ ] `integrity_check` + `foreign_key_check` run on the backup
- [ ] FTS rebuild / REINDEX done on a copy, never the live DB
- [ ] Unreadable rows preserved in a separate backup, with a note
- [ ] Live restore only after probe returns `None`
- [ ] Kanban tasks unblocked; dispatch resumes; queue-drain alert clears

## Notes / local-only status

- This guide is local documentation for the SoLo Hermes install
  (`/home/solo/.hermes`). The probe + quarantine hardening is committed on
  task branches in the local hermes-agent checkout; no PR to official
  Hermes was opened (no GitHub credential on this host; local-only per
  SoLo policy unless he explicitly approves a PR).
