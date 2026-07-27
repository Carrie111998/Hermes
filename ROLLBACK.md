# Rollback procedure

Every command below was exercised by `scripts/rehearse_rollback.py`, which
restores each artifact into a scratch tree and verifies it. Last rehearsal:
**20/20 checks passed** against safety point
`C:/Users/Waxilliam/backups/hermes-audit-safety-20260727T070301Z`.

Re-verify before relying on it:

```bash
python scripts/rehearse_rollback.py
```

**Stop the gateway first for anything under §1–§3.** It is the only writer, and
restoring a database underneath a live writer is how you get the corruption you
were rolling back to avoid.

```bash
schtasks //End //TN "Hermes_Gateway_aletheon"
```

`hermes gateway stop` reports success while leaving PID alive (Session 0,
"Access is denied") — verify by watching the heartbeat go stale, not by
trusting the message:

```bash
python -c "import json,datetime;h=json.load(open(r'C:/Users/Waxilliam/AppData/Local/hermes/profiles/aletheon/state/gateway.heartbeat'));print((datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(h['updated_at'])).total_seconds())"
```

Over ~90 s and climbing means it is really down (the beat interval is ~30 s).

---

## 1. A database

Backups are **online-backup-API snapshots**, not file copies, so they are
transactionally consistent and restore by plain copy.

```bash
BK="C:/Users/Waxilliam/backups/hermes-audit-safety-20260727T070301Z/databases"
P="C:/Users/Waxilliam/AppData/Local/hermes/profiles/aletheon"

cp "$P/state.db" "$P/state.db.pre-rollback"      # keep what you are replacing
cp "$BK/state.db" "$P/state.db"
rm -f "$P/state.db-wal" "$P/state.db-shm"        # stale sidecars for the OLD file
python -c "import sqlite3;print(sqlite3.connect(r'$P/state.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

Name mapping is flattened with `__` for path separators:
`cron__executions.db` → `cron/executions.db`,
`workers__bridge.db` → `workers/bridge.db`,
`memories__mneme-notes__mneme.db` → `memories/mneme-notes/mneme.db`.

Deleting the `-wal`/`-shm` sidecars matters: they belong to the file you just
replaced and SQLite will otherwise try to replay them over the restored one.

## 2. Git history

Both bundles are complete packs — verified and cloned during rehearsal.

```bash
BK="C:/Users/Waxilliam/backups/hermes-audit-safety-20260727T070301Z"
git bundle verify "$BK/inner-repo.bundle"

# inspect without touching the live repo
git clone "$BK/inner-repo.bundle" /tmp/hermes-rollback-inspect

# or recover specific refs into the live repo
git fetch "$BK/inner-repo.bundle" 'refs/heads/*:refs/heads/recovered/*'
```

Bundle HEADs at snapshot time: inner `8364110ab`, outer `1614b6e`.

## 3. Configuration

```bash
BK="C:/Users/Waxilliam/backups/hermes-audit-safety-20260727T070301Z"
tar -xzf "$BK/outer-uncommitted.tar.gz" -C /tmp profiles/aletheon/config.yaml
python -c "import yaml;print(len(yaml.safe_load(open('/tmp/profiles/aletheon/config.yaml',encoding='utf-8'))))"
# then copy into place, and:
hermes -p aletheon doctor
```

The archive's config carries `delegation.max_concurrent_children: 5` and no
`max_async_children` — i.e. the v30→v33 migration is already in the backup, so
restoring it does **not** un-migrate anything.

## 4. Runtime (Python / SQLite)

| venv | Python | SQLite | Safe? |
|---|---|---|---|
| `hermes-agent/venv313` | 3.13.14 | 3.53.1 | yes — current |
| `hermes-agent/venv` | 3.11.15 | 3.50.4 | **WAL-reset vulnerable** |

Rolling back to `venv` is possible but is a **decision, not a default**: it is
inside `requires-python`, so the Python guard passes it, while ~10 of 11
databases are already in WAL. The guard refuses it unless you accept the risk
explicitly:

```bash
HERMES_ALLOW_VULNERABLE_SQLITE=1 hermes-agent/venv/Scripts/hermes.exe -p aletheon doctor
```

`HERMES_SUPPRESS_SQLITE_WARNING` will **not** do this — it silences a message
and cannot clear a real vulnerability. That separation is deliberate.

Before running on `venv`, take a fresh DB backup: you are choosing to operate a
vulnerable library against live WAL files.

## 5. A bad upstream merge

`scripts/safe_update.py` prints the pre-merge HEAD and the exact command. From
a clean tree:

```bash
git reset --hard <pre-merge-HEAD>
python scripts/verify_protected_behavior.py     # must report 15/15 caught
python -m pytest tests/contract/ -q             # must be green
```

If a merge already landed and a protection was lost, do **not** re-resolve
file-wise — that is how `6ab037f1e` ("restore upstream changes lost by
file-level conflict resolution") happened. Reset, then redo the merge by hunk.

## What is NOT recoverable

The pre-migration execution-receipt store. It was deleted rather than merged
between 01:50 and 07:03 UTC on 2026-07-27 — before this safety point was taken
— and no copy exists on disk. Recorded here so nobody spends time looking.
