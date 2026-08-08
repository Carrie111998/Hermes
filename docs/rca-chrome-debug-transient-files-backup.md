# RCA: `hermes backup` reports "Backup incomplete" due to transient chrome-debug files

**Status:** root-caused; fix pending in a follow-up task (backup exclusion / warning reclassification).
**Severity:** P3 — cosmetic/reporting bug. The produced backup archive is complete, valid, and restorable; only the human-facing summary label and log noise are wrong.

## Summary

When `hermes backup` (full zip backup, `hermes_cli/backup.py`) runs while the
Hermes-launched debug Chrome process (`local.chrome-debug`, a `launchd`
`KeepAlive` job listening on CDP port 9222, `--user-data-dir=~/.hermes/chrome-debug`)
is active, the backup prints `Backup incomplete: <path>` and lists 2-6
"Warnings" for files under `chrome-debug/` such as:

```
chrome-debug/BrowserMetrics-spare.pma: [Errno 2] No such file or directory
chrome-debug/Default/Sessions/Session_<id>: [Errno 2] No such file or directory
chrome-debug/Default/Sessions/Tabs_<id>: [Errno 2] No such file or directory
chrome-debug/Default/shared_proto_db/000003.log: [Errno 2] No such file or directory
```

This reads as a failed backup to the user. **It is not.** The archive produced
is 100% structurally valid, contains every file that still existed at write
time, and is fully restorable. The "incomplete" framing is a labeling/reporting
bug, not a data-integrity bug.

## Root cause

`hermes backup` runs in two phases, both in `hermes_cli/backup.py::_run_backup_locked`
(the same pattern exists in the shared automatic-backup path,
`_write_full_zip_backup_locked`, lines 1644-1735):

1. **Scan** (`os.walk(hermes_root, ...)`, lines 625-648): builds an in-memory
   list `files_to_add: list[(abs_path, rel_path)]` — a snapshot of what
   existed *at scan time*. Fast: ~0.5-1s for ~5,800 files (confirmed in
   `agent.log`: `backup phase=scan status=complete duration_ms=588.8 files=5782`).
2. **Archive** (lines 696-744): iterates `files_to_add` and calls
   `zf.write(abs_path, ...)` (or `_safe_copy_db()` for `*.db`) per file. This
   phase is slow — 20s to 250s in the observed runs (`agent.log`:
   `backup phase=archive status=complete duration_ms=250246.2 files=5783 errors=6`),
   because it's doing real compression I/O on ~650MB of data.

The Hermes-launched Chrome process (confirmed live via `ps` and
`launchctl list`: PID actively running `Google Chrome --remote-debugging-port=9222
--user-data-dir=/Users/eugenemettsler/.hermes/chrome-debug`, `KeepAlive=true`
in `~/Library/LaunchAgents/local.chrome-debug.plist`) continuously rewrites
its own profile directory while it runs:

- `BrowserMetrics-spare.pma` — a pre-allocated "spare" metrics recording
  buffer Chrome swaps in/out as it rotates the active metrics file.
- `Default/Sessions/Session_<timestamp>` / `Tabs_<timestamp>` — tab/session
  restore state, rewritten under a **new timestamped filename** on
  navigation/tab events, with the old file deleted. Verified directly: the
  original bug report's session ids (`Session_13430660839007907`,
  `Session_13430660849807348`) differ completely from the ids present ~20
  minutes later in a fresh repro (`Session_13430660950327711`,
  `Tabs_13430660950772624`) — proof these are actively rotating, not static.
- `Default/shared_proto_db/000003.log` — a LevelDB WAL segment, rotated away
  during compaction.

Because the scan snapshot and the archive write are seconds-to-minutes apart,
a file that existed at scan time can be deleted/renamed by Chrome before
`zf.write()` reaches it, raising `FileNotFoundError` ([Errno 2]).

### Step 1: this part already works correctly

The per-file archive loop (`hermes_cli/backup.py:699-724`) already wraps each
write in:

```python
except (PermissionError, OSError, ValueError) as exc:
    errors.append(f"  {rel_path}: {exc}")
    continue
```

So a vanished file is caught, recorded, and skipped — the loop does **not**
abort, and the zip write proceeds cleanly. This is confirmed directly:

- Live repro (`hermes backup -o /tmp/backup-exitcode-test.zip` while
  `local.chrome-debug` was running): process **exit code 0**, 3 files
  skipped (same chrome-debug transient names), `Backup incomplete` printed.
- `zipfile.ZipFile(...).testzip()` on both the repro archive and the
  original ticket's own artifact (`/private/tmp/hermes-backup-test/full.zip`,
  343,742,183 bytes) returns `None` (no CRC/corruption errors) — the archives
  are intact.
- The original ticket's archive contains 5,777 entries; the scan discovered
  5,783 files; the delta is exactly 6 — matching the reported "6 files
  skipped" 1:1. Nothing besides the vanished files is missing (verified
  `kanban.db`, `config.yaml`, `.env`, etc. are all present).

A secondary variant of the same race exists for the handful of `*.db` files
under `chrome-debug/` (`first_party_sets.db`,
`Default/heavy_ad_intervention_opt_out.db`,
`Default/declarative_performance_observer.db`,
`GPUPersistentCache/GPUCache/.../cache.db`): these go through
`_safe_copy_db()` (lines 342-362) instead of a raw `zf.write`. If Chrome
deletes/replaces one mid-copy, `_safe_copy_db` catches the exception
internally, returns `False`, and the caller appends `"SQLite safe copy
failed"` to the same `errors` list (line 717) — same underlying race,
different error string, same downstream mislabeling below. Not observed in
the two captured repros (no `.db` file happened to rotate in that window),
but it is the same class of bug and should be covered by the same fix.

### Step 2: this is what actually produces the reported "error"

The summary/finalize block (`hermes_cli/backup.py:756-794`) is what the user
sees and what produces the perceived failure:

```python
if errors:
    print(f"Backup incomplete: {out_path}")   # line 759 — ALWAYS fires when
else:                                          # errors list is non-empty,
    print(f"Backup complete: {out_path}")      # regardless of whether the
...                                             # archive is actually fine.
if not errors:
    print(f"\nRestore with: hermes import {out_path.name}")  # line 793-794 —
                                                                # suppressed
                                                                # whenever any
                                                                # warning fired.
```

`errors` here is populated exclusively by benign, already-caught,
already-logged per-file skip events — there is no code path today that
distinguishes "a file vanished out from under us mid-backup (harmless)" from
"a file we genuinely could not read (permissions, disk error, real
problem)". Both land in the same list, both flip the summary to
`Backup incomplete`, and both suppress the restore hint. This is the exact
"final backup error" the bug report describes: **the finalizing/reporting
step, not scanning or zipping**, is what turns 6 harmless, already-handled
warnings into a message that reads as a failed backup — even though the
archive is complete and restorable.

The `hermes update`/`hermes claw migrate` automatic-backup path
(`_write_full_zip_backup_locked`, lines 1644-1735) has the equivalent
`errors`-counting behavior but currently only logs at `debug` level per file
(line 1713) and doesn't surface a user-facing summary at all — lower
visibility, same underlying unclassified-warning behavior.

## Why this is not the Chrome-respawn bug

This root cause is independent of whether the separate Chrome
auto-respawn/KeepAlive issue (tracked in the sibling investigation/fix tasks)
gets fixed. Even a one-shot, user-controlled Chrome session actively browsing
during a backup would hit the same scan-then-archive race on the same
transient files. The backup step needs to tolerate a running Chrome
regardless of the respawn behavior.

## Recommended fix direction (for the follow-up fix task)

Two complementary changes, both low-risk:

1. **Exclude known-transient chrome-debug subpaths from the backup walk**,
   the same way `_EXCLUDED_DIRS`/`_EXCLUDED_NAMES` already exclude
   `__pycache__`, `.venv`, etc. (`hermes_cli/backup.py:55-96`). Candidates:
   `chrome-debug/BrowserMetrics-spare.pma`,
   `chrome-debug/*/Sessions/`, `chrome-debug/*/shared_proto_db/`. None of
   this data has restore value — it's Chrome's own transient
   telemetry/session-restore/LevelDB-WAL state, regenerated on next launch,
   analogous to why `.venv`/`__pycache__` are already excluded as
   "regeneratable, not irreplaceable state." This directly eliminates the
   warning noise in the common case.
2. **Reclassify vanished-mid-backup (`ENOENT`/`FileNotFoundError`) as an
   explicit "transient, skipped" bucket, separate from `errors`**, at both
   the raw-write catch (line 722-724) and the SQLite-copy catch (line
   711-718). Only non-ENOENT failures (permissions, disk errors, genuine
   SQLite corruption) should flip the summary to `Backup incomplete` /
   suppress the restore hint. This is defense-in-depth for any other
   external process racing the backup in the future, not just Chrome.

Both changes are additive to the existing exclusion/error-handling
machinery already in `hermes_cli/backup.py` — no structural change to the
scan/archive/finalize flow is needed.

## Verification performed for this RCA

- Confirmed `local.chrome-debug` launchd job definition and live process via
  `launchctl list` / `ps aux` (`KeepAlive=true`,
  `--user-data-dir=/Users/eugenemettsler/.hermes/chrome-debug`,
  `--remote-debugging-port=9222`).
- Live-reran `hermes backup` while that Chrome process was active; captured
  exit code (0) and the exact same class of chrome-debug ENOENT warnings.
- Verified archive integrity with `zipfile.testzip()` on both the fresh
  repro archive and the original ticket's archive (`full.zip`,
  343,742,183 bytes) — both `None` (no corruption).
- Cross-checked file counts: original ticket scan found 5,783 files, zip
  contains 5,777 entries, delta 6 == reported "6 files skipped", confirming
  no files beyond the reported warnings were affected.
- Confirmed the session/tab filenames are actively rotating (different
  Session_*/Tabs_* ids between the original report and a fresh repro ~20
  minutes later), proving the transient-file theory rather than a static
  missing-file bug.
- Read `hermes_cli/backup.py` end to end for both the interactive
  (`run_backup`/`_run_backup_locked`) and automatic
  (`_write_full_zip_backup`/`_write_full_zip_backup_locked`) backup paths to
  confirm both share the same scan-then-archive structure and the same
  unclassified-`errors` reporting gap.
