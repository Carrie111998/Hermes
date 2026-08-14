# Fix: `hermes backup` no longer errors on transient chrome-debug files

**Status:** fixed and live-verified.
**Follows:** `docs/rca-chrome-debug-transient-files-backup.md` (root cause).

## What changed

Two complementary changes in `hermes_cli/backup.py`, matching the RCA's two
recommendations, plus one additional fix the RCA flagged as a same-class risk
in the secondary (automatic backup) code path.

### 1. Exclude known-transient chrome-debug subpaths from the backup walk

Added `_CHROME_DEBUG_ROOT_DIR`, `_CHROME_DEBUG_TRANSIENT_DIRS`,
`_CHROME_DEBUG_TRANSIENT_FILES`, and `_is_chrome_debug_transient_dir()`.
Wired into:

- `_should_exclude()` — file-level exclusion used by both backup paths via
  `_should_skip_backup_file()`.
- Both `os.walk()` scan loops (`_run_backup_locked` and
  `_write_full_zip_backup_locked`) — directory-level pruning so `os.walk`
  never descends into `chrome-debug/*/Sessions/`,
  `chrome-debug/*/shared_proto_db/`, or `chrome-debug/BrowserMetrics/` at
  all, not just filtering their contents after the fact.

Scoped to fire only when rooted under the top-level `chrome-debug/`
directory, so a same-named `Sessions`/`shared_proto_db` directory elsewhere
under `HERMES_HOME` (e.g. inside a skill) is never affected — covered by
`test_chrome_debug_exclusion_is_scoped_to_top_level_dir`.

This eliminates the scan-then-archive race in the common case: the files
that were racing the backup are no longer scanned or archived at all, so
there's nothing to vanish out from under `zf.write()`.

### 2. Reclassify vanished-mid-backup skips as "transient", not "errors"

Defense in depth for any file that isn't covered by the exclusion above (a
different live process racing the backup, or a transient chrome-debug file
type not yet catalogued):

- **`_run_backup_locked`** (the primary `hermes backup` / `hermes import`
  path): the archive loop now catches `FileNotFoundError` *before* the
  broader `(PermissionError, OSError, ValueError)` clause and appends to a
  new `transient_skipped` list instead of `errors`. The finalize/summary
  block only flips to `"Backup incomplete"` and suppresses the restore hint
  when `errors` is non-empty — `transient_skipped` entries print as a
  separate, clearly-labeled "Note (N file(s) changed during backup,
  harmless -- nothing to restore there)" section instead.
- The `.db` safe-copy branch (`_safe_copy_db()` returning `False`) now
  checks `abs_path.exists()` post-hoc: if the source vanished (the
  chrome-debug `*.db` race described in the RCA), it's `transient_skipped`;
  if the source is still there but genuinely failed to copy (locked,
  corrupt, permissions), it's still a hard `errors` entry and still flips
  the summary. `sqlite3.connect()` raises a generic `OperationalError` for a
  missing file rather than `FileNotFoundError`, so this existence check is
  the only reliable way to distinguish the two cases from inside
  `_safe_copy_db`'s boolean return.
- **`_write_full_zip_backup_locked`** (the shared helper behind `hermes
  update`'s pre-update backup and `hermes claw migrate`'s pre-migration
  backup): this path had a *more severe* version of the same bug that the
  RCA flagged as "not yet observed live but same code path" — a vanished
  `.db` file raised `_SQLiteSnapshotError`, which the outer `except`
  caught and turned into a full `return None`, discarding the *entire*
  archive (all files scanned before AND after the vanished one), not just
  skipping that one file. Now a vanished `.db` (source doesn't exist after
  `_safe_copy_db` returns `False`) or a `FileNotFoundError` from a plain
  `zf.write()` is logged and skipped via `continue`, and the archive
  completes with everything else intact. A `.db` file that's still present
  but genuinely fails to copy still raises `_SQLiteSnapshotError` and still
  aborts+preserves the previous archive — unchanged, and still covered by
  the existing `test_automatic_backup_still_aborts_on_genuine_db_failure`
  (formerly `test_failed_automatic_backup_preserves_previous_archive` in
  `test_backup_stability.py`).

## Tests added

`tests/hermes_cli/test_backup.py`:

- `TestShouldExclude.test_excludes_chrome_debug_transient_subpaths`
- `TestShouldExclude.test_chrome_debug_exclusion_is_scoped_to_top_level_dir`
- `TestChromeDebugTransientRace` (new class, 7 tests): vanished plain file
  is a warning not fatal; genuine `PermissionError` is still fatal; vanished
  `.db` is skipped not reported as a copy failure; a `.db` that's present
  but genuinely fails is still fatal; the automatic backup path tolerates
  both a vanished plain file and a vanished `.db` without aborting; the
  automatic backup path still aborts+preserves the previous archive on a
  genuine (non-vanished) `.db` failure.

All 55 tests in `test_backup.py` + `test_backup_stability.py` pass
(46 pre-existing + 9 new), plus the 10 pre-existing tests in
`test_curator_backup.py` and 36 in `test_execution_ledger.py` /
`test_sizefmt.py` / `test_state_db_guard.py` (other callers of
`hermes_cli.backup`) — no regressions.

## Live end-to-end verification

Launched the debug Chrome via the real Hermes code path
(`hermes_cli.browser_connect.launch_chrome_debug(9222)`, the same function
`/browser connect` uses) against the live `~/.hermes`, then ran `hermes
backup` while it was actively running, navigating a couple of real pages via
its CDP endpoint mid-backup to force genuine `Session_*`/`Tabs_*` file
churn (new session/tab files with fresh timestamps appeared under
`chrome-debug/Default/Sessions/` during the run, confirming the race
window was live, not simulated):

```
Scanning ~/.hermes ...
Backing up 4170 files ...
  500/4170 files ...
  1000/4170 files ...
  1500/4170 files ...
  2000/4170 files ...
  2500/4170 files ...
  3000/4170 files ...
  3500/4170 files ...
  4000/4170 files ...

Backup complete: /private/tmp/hermes-backup-verify/full.zip
  Files:       4170
  Original:    543.3 MB
  Compressed:  221.9 MB
  Time:        28.9s

  Excluded directories:
    chrome-debug/Default/Sessions/
    chrome-debug/Default/shared_proto_db/
    hermes-agent/
    lsp/node_modules/
    node/lib/node_modules/

Restore with: hermes import full.zip
EXIT_CODE=0
```

Confirms, live, against the real bug scenario:

- **"Backup complete"**, not "Backup incomplete" — this is the exact label
  flip the RCA identified as the actual user-facing bug.
- **`Restore with:` hint present** — previously suppressed by any nonzero
  `errors` count; chrome-debug races no longer populate `errors` at all.
- **`chrome-debug/Default/Sessions/` and `chrome-debug/Default/shared_proto_db/`
  listed as excluded directories** — the race is prevented at scan time,
  not merely caught and downgraded at archive time.
- **No chrome-debug warnings at all** in this run (vs. the original ticket's
  6 ENOENT warnings) — proof the primary fix (exclusion) eliminates the
  race in the common case, with the secondary fix (transient
  reclassification) as a backstop for anything the exclusion list doesn't
  cover.
- **Archive integrity confirmed**: `zipfile.ZipFile(...).testzip()` returns
  `None` (no CRC/corruption), 4170 entries with 0 `Sessions/`/
  `shared_proto_db/` entries and real profile data (e.g.
  `chrome-debug/Default/Cookies`) correctly still present.

### A separate, pre-existing, out-of-scope finding

While live-testing, repeated interaction with the debug Chrome (rapid
tab open/close cycles over CDP) was observed to put one or more of Chrome's
own small SQLite files under `chrome-debug/` (`first_party_sets.db`,
`Default/heavy_ad_intervention_opt_out.db`,
`Default/declarative_performance_observer.db`) into a **sustained
`SQLITE_BUSY` lock** that `sqlite3.Connection.backup()` retries
indefinitely with no overall bound (confirmed independently of
`_safe_copy_db`, and confirmed to reproduce identically against this
change's own *unpatched* pre-fix code — i.e. not a regression introduced by
this fix). This was also observed, with much lower probability, from a
completely idle debug Chrome with zero interaction from this session, and
even against the user's separate, long-running personal Chrome instance's
own `first_party_sets.db` — so it is a general characteristic of this
Chrome version's own internal use of these files, not something specific to
the Hermes-managed debug profile.

This is a different bug class from the one this task fixes (indefinite
lock-wait vs. the ENOENT-vanished-file race) and the original ticket's own
captured logs show no evidence of it (only the 6 ENOENT warnings, with a
clean 250.2s total runtime) — so it's flagged here for a possible follow-up
rather than addressed in this change. `_safe_copy_db()` would benefit from
a bounded retry (e.g. `conn.backup(target, progress=<callback that raises
past a deadline>)`) so a persistently-busy source file degrades to a
skipped-with-warning outcome instead of stalling the whole backup
indefinitely.
