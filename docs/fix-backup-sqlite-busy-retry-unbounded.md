# Fix: bound `_safe_copy_db`'s SQLite busy/locked retry

**Status:** fixed and verified (unit tests + live in-situ against a real lock).
**Follows:** `docs/rca-backup-sqlite-busy-retry-unbounded.md` (root cause).

## What changed

One change in `hermes_cli/backup.py::_safe_copy_db()`: the previously-bare
`conn.backup(backup_conn)` call is now bounded against a sustained
`SQLITE_BUSY`/`SQLITE_LOCKED` source.

```python
def _abort_past_deadline(status, remaining, total):
    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
        raise _SafeCopyBusyTimeout(...)

conn.backup(backup_conn, progress=_abort_past_deadline)
```

### Design choices and why

1. **Bound via `progress=`, not a wrapper timeout/thread.**
   `Connection.backup()`'s `progress=` callback fires after *every*
   `sqlite3_backup_step()` call, including ones that returned
   `SQLITE_BUSY`/`SQLITE_LOCKED`, and CPython already treats a raising
   callback as "abort backup and bail" — it still calls
   `sqlite3_backup_finish()` internally, so no backup handle is leaked. This
   needed no new thread, signal, or subprocess machinery.

2. **Gate the deadline check on the status code, not raw elapsed time.**
   With the default `pages=-1`, a healthy, *uncontended* copy — however
   large — completes in a single `sqlite3_backup_step()` call, and the
   progress callback only fires *after* that (possibly slow, for a
   many-GB `state.db`) step finishes. A time-only check would incorrectly
   discard an already-successful large, non-busy copy just because it took
   a while. Gating on `status in (SQLITE_BUSY, SQLITE_LOCKED)` keeps the
   deadline meaningful only for genuine lock contention. Verified: an 87MB
   non-busy source completes normally even against a 1ms deadline; a
   167MB/300k-row non-busy source copies correctly with a 1ms deadline too.

3. **Open both connections with `timeout=0` in the bounded path.**
   This is load-bearing, not cosmetic (see the RCA's point 5): the
   `sqlite3.connect()` default `timeout=5.0` installs SQLite's own internal
   busy-handler, which silently retries *inside* a single
   `sqlite3_backup_step()` call for up to that many seconds before ever
   returning control to the progress callback. With `timeout=0`, each step
   call fails fast on contention and control returns to the callback at
   roughly `backup()`'s own retry cadence (`sleep=`, default 250ms), so the
   deadline is enforced with the intended granularity instead of a coarse
   ~5-8s one. This doesn't change success-path behavior: an uncontended
   copy never touches the busy-handler either way, and the destination is
   always a fresh tempfile the caller just created (never independently
   contended).

4. **The raised exception is a plain `Exception` subclass
   (`_SafeCopyBusyTimeout`).** It's caught by `_safe_copy_db`'s existing
   `except Exception as exc:` clause with zero control-flow changes: a
   busy-timeout abort degrades to the exact same "SQLite safe copy failed"
   warning + `False` return as any other `backup()` failure, which both
   call sites (`_run_backup_locked`'s per-file loop and
   `_write_full_zip_backup_locked`'s automatic-backup path) already handle
   via the vanished-vs-genuine-failure distinction landed in
   `docs/fix-chrome-debug-transient-files-backup.md`. A file that stays
   locked past the deadline still exists on disk, so it's classified as a
   genuine `errors` entry (not a benign `transient_skipped` vanish) in both
   paths — the safest default, since we don't actually know whether it will
   eventually copy successfully or not, just that it exceeded its budget.

5. **Deadline is configurable, with an explicit escape hatch.**
   Default 30 seconds (`_SAFE_COPY_BUSY_DEADLINE_SECONDS`). Override via
   `HERMES_BACKUP_SQLITE_BUSY_DEADLINE_SECONDS` (float, seconds). A
   non-positive value disables bounding entirely, restoring the exact
   pre-fix indefinite-retry behavior for anyone who wants it.

## Tests added

`tests/hermes_cli/test_backup.py::TestSafeCopyDbBusyBounding` (6 tests):

- `test_busy_source_is_bounded_not_indefinite` — a source locked longer than
  the deadline returns `False` in roughly the deadline window, not after the
  lock clears.
- `test_short_busy_window_still_succeeds` — a lock that clears *before* the
  deadline still succeeds normally (bounding must not penalize ordinary WAL
  contention).
- `test_connection_timeout_parameter_does_not_bound_backup_loop` —
  documents/locks in *why* a naive `timeout=` fix wouldn't have worked (the
  RCA's point 2), as a guard against someone "simplifying" the fix later.
- `test_large_non_busy_copy_is_unaffected_by_bounding` — an absurdly tiny
  deadline (1ms) does not abort a large (20k-row), non-busy copy, proving
  the status-gate (not time alone) governs the deadline.
- `test_negative_deadline_disables_bounding` — the escape hatch restores
  indefinite retry.

All pre-existing tests in `test_backup.py` (54), `test_backup_stability.py`,
`test_curator_backup.py`, `test_execution_ledger.py`, `test_state_db_guard.py`,
and `test_sizefmt.py` (106 total across the backup-adjacent surface) still
pass — no regressions. `ruff check` clean on both changed files.

## Live end-to-end verification

At the time of this fix, the user's actual `chrome-debug/first_party_sets.db`
was independently confirmed locked (`sqlite3 ... "SELECT 1"` →
`database is locked (5)`), with zero interaction from this session:

- **Before the fix**, the real (unpatched) `_safe_copy_db()` against this
  live file blocked for **61.4 seconds** before the lock happened to clear.
- **After the fix**, with a shortened 10s deadline override (to avoid
  waiting out the full 30s default for this one verification run), the
  same live file — while still genuinely locked — was aborted cleanly at
  **10.1 seconds**, logging `SQLite safe copy failed for
  .../first_party_sets.db: source stayed SQLITE_BUSY/SQLITE_LOCKED for
  over 10s` and returning `False`, exactly as designed.

This confirms the fix against the actual real-world condition the RCA
describes, not just a synthetic repro.

## Follow-ups intentionally not done here

- Not adding a user-facing summary line distinguishing "busy-timeout skip"
  from other `errors` entries — out of scope for this task; the existing
  generic "SQLite safe copy failed" warning already surfaces the file and
  underlying exception message (which now includes
  "stayed SQLITE_BUSY/SQLITE_LOCKED for over Ns"), which is enough signal
  for a human reading backup output to understand what happened.
- Not investigating whether this is specific to a particular Chrome version
  or has always been present — the RCA notes it reproduces against the
  user's separate personal Chrome profile too, suggesting it's a general
  Chrome/SQLite characteristic rather than a regression, but pinning down
  *which* Chrome versions would need a wider survey out of scope here.
