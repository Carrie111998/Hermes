# State DB Maintenance

This runbook covers destructive or large `state.db` maintenance such as
physical trigram FTS reclamation and `VACUUM`. Ordinary Hermes startup must not
perform these operations.

## Session Trigram FTS Reclaim

Use this only during an offline maintenance window for a shared default
database.

1. Verify the candidate runtime first.

   ```bash
   hermes doctor
   python - <<'PY'
   import sqlite3
   print(sqlite3.sqlite_version)
   PY
   ```

   Continue only on SQLite `3.50.7`, `3.51.3`, or newer. Older WAL-reset
   vulnerable runtimes must be upgraded before opening the live database for
   maintenance.

2. Stop every default-database owner.

   Stop the gateway, dashboard, Desktop backend, and orphaned isolated
   `hermes serve` process before maintenance. Confirm no process owns
   `state.db`, `state.db-wal`, or `state.db-shm`.

   ```bash
   lsof ~/.hermes/state.db ~/.hermes/state.db-wal ~/.hermes/state.db-shm
   ```

   The command must return no owners. If it lists any process, stop here.

3. Capture and verify a rollback copy.

   Keep the backup on a disk with enough free space for `state.db`, sidecars,
   and the temporary `VACUUM` rewrite.

   ```bash
   cp -a ~/.hermes/state.db ~/.hermes/state.db-wal ~/.hermes/state.db-shm /path/to/backup-dir/
   sha256sum /path/to/backup-dir/state.db*
   sqlite3 "file:/path/to/backup-dir/state.db?mode=ro" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
   ```

   The backup readback must show `ok` for `integrity_check` and no
   `foreign_key_check` rows.

4. Persist the shared logical policy.

   Remove the legacy `HERMES_DISABLE_FTS_TRIGRAM` service override, then write
   the shared config value:

   ```bash
   hermes config set sessions.trigram_fts false
   ```

   Open the database once with the candidate runtime so the missing
   `state_meta.trigram_fts_policy` marker is seeded from shared config.

5. Run the explicit offline reclaim.

   ```bash
   hermes sessions optimize-storage --reclaim-disabled-trigram --yes
   ```

   This is the only supported path for physically dropping the disabled
   trigram FTS table and running `VACUUM`.

6. Re-run integrity checks and reopen.

   ```bash
   sqlite3 "file:$HOME/.hermes/state.db?mode=ro" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
   hermes sessions list --limit 1
   ```

   Restart all default-database surfaces only after the checks pass and the
   reopened database reports the same persisted trigram policy.

7. Observe for seven days.

   Alert on either signal:

   - `SessionDB write lock wait` over 10 seconds on the default database.
   - `session_persistence_failed:locked` in normal gateway, Desktop, cron, or
     CLI traffic.

## Stop Conditions

Stop before mutating the live database if any of these is true:

- The active runtime is below SQLite `3.50.7` or `3.51.3`.
- The backup cannot be read back or fails integrity checks.
- Any process still owns `state.db`, `state.db-wal`, or `state.db-shm`.
- The shared policy cannot be written to config or persisted in `state_meta`.
- `integrity_check` or `foreign_key_check` fails before or after reclaim.

