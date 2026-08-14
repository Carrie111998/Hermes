# RCA: `hermes backup` can stall indefinitely on a busy/locked SQLite file

**Status:** root-caused and fixed (see `docs/fix-backup-sqlite-busy-retry-unbounded.md`).
**Severity:** P3 — latent risk, not yet reported by a real user as a stalled
backup. Discovered as a side finding while live-verifying
`docs/fix-chrome-debug-transient-files-backup.md` (a different bug: transient
ENOENT races, not lock contention).

## Summary

`hermes_cli/backup.py::_safe_copy_db()` copies each `.db` file via
`sqlite3.Connection.backup()`. That call has **no bounded retry**: when the
source is `SQLITE_BUSY` (another process holds a write transaction), it
retries with `sqlite3_sleep()` indefinitely until the lock clears, regardless
of the `timeout=` either connection was opened with.

While repeatedly killing/relaunching the Hermes-managed debug Chrome and
driving it via CDP during unrelated live testing, one or more of Chrome's own
small SQLite files under `chrome-debug/` (`first_party_sets.db`,
`Default/heavy_ad_intervention_opt_out.db`,
`Default/declarative_performance_observer.db`) went into a sustained locked
state that stalled a raw `_safe_copy_db()` call for 5-10+ minutes on that one
file.

## Root cause

### 1. `Connection.backup()`'s retry loop has no deadline

CPython's C implementation
(`Modules/_sqlite/connection.c::pysqlite_connection_backup_impl`) runs:

```c
do {
    rc = sqlite3_backup_step(bck_handle, pages);
    if (progress != Py_None) { /* call the progress= callback */ }
    if (rc == SQLITE_BUSY || rc == SQLITE_LOCKED) {
        sqlite3_sleep(sleep_ms);   /* default 250ms */
    }
} while (rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED);
```

There is no time budget anywhere in this loop. It retries for as long as the
source stays busy/locked, full stop.

### 2. The connection's `timeout=` parameter does not help

`sqlite3.connect(..., timeout=N)` only bounds how long *ordinary statement
execution* waits to acquire a lock before raising `OperationalError`. It has
no effect on the C loop above. Confirmed empirically: opening both the
source and destination connections with `timeout=2` while an external
`BEGIN EXCLUSIVE` held the source locked for 8s still blocked `backup()` for
the full 8s — the short timeout was silently ignored by this code path.

### 3. Deterministic isolated repro

A background thread/connection holding `BEGIN EXCLUSIVE` on a copy of one of
the affected `.db` files while `_safe_copy_db` runs against it reproduces the
indefinite retry in isolation, 100% of the time, regardless of hold duration
(6s, 8s, 10s all block for the full hold).

### 4. Live, real-world in-situ reproduction

At the time of this investigation, the user's actual, live
`chrome-debug/first_party_sets.db` was independently found to be
`SQLITE_BUSY` (`sqlite3 ... "SELECT 1"` → `database is locked (5)`, repeated
over 5s of polling) with **zero interaction from this session** — not a
synthetic setup. Running the real (unpatched) `_safe_copy_db()` against it
directly blocked for **61.4 seconds** before the lock happened to clear on
its own. The user's separate, unrelated, long-running personal Chrome
profile's own copy of `first_party_sets.db` was *also* locked at the same
moment. This corroborates the original finding: this is a general
characteristic of this Chrome version's own use of these files/locking
pattern, not something specific to the Hermes-managed `chrome-debug/`
profile, and not an artifact of the interactive CDP testing that first
surfaced it.

### 5. A second, subtler contributor: the connection's default `timeout=5.0`

While prototyping the fix (bound the retry via `backup(progress=...)`, which
fires on every step including busy ones, and can raise to abort), an
apparently-working prototype turned out to be **flaky** — a deadline of 2s
against an 8s external lock sometimes still took the full ~8s. Root cause:
`sqlite3.connect()`'s **default** `timeout=5.0` installs SQLite's own
internal busy-handler, which retries *inside a single*
`sqlite3_backup_step()` call for up to that many seconds before ever
returning `SQLITE_BUSY` back to Python — i.e. before the `progress=`
callback (and therefore any deadline check inside it) gets a chance to run
at all. Measured directly: with the connection default, an 8s external lock
produced **exactly one** progress callback, ~8s in; with `timeout=0` on both
connections, the same scenario produced ~8 callbacks at roughly the
`backup()` retry cadence (250ms), and a deadline check inside the callback
behaved as expected. Any fix for this bug must open both connections with
`timeout=0` in the bounded path, or the deadline enforcement is
coarse-grained and unreliable.

## Why this is a separate bug from the chrome-debug ENOENT race

The original bug report (`t_6f9583fe`) that spawned this investigation chain
shows a clean 250.2s `hermes backup` run with only 6 ENOENT warnings — no
indication of a multi-minute stall. This lock-contention behavior is not what
that user hit; it was a latent risk surfaced by harder-than-normal
interactive testing (rapid repeated tab open/close via CDP), confirmed here
to also occur under completely idle conditions.

## Verification performed for this RCA

- Read CPython's `Modules/_sqlite/connection.c` (backup_impl) directly to
  confirm the retry loop and its exact busy/locked exit conditions.
- Deterministic isolated repro: background thread holds `BEGIN EXCLUSIVE`
  for N seconds; unpatched `_safe_copy_db()` blocks for ~N seconds every
  time, across multiple hold durations.
- Confirmed connection `timeout=` does not bound `backup()`: explicit short
  timeout (2s) vs. long external lock (8s) — `backup()` still took ~8s.
- Live in-situ repro against the user's actual currently-locked
  `chrome-debug/first_party_sets.db` (not synthetic): 61.4s real stall via
  the real, unpatched `_safe_copy_db()`.
- Cross-checked the user's separate personal Chrome profile's own
  `first_party_sets.db`: also locked at the same moment, supporting the
  "general Chrome/SQLite characteristic, not Hermes-specific" theory.
- Isolated the `timeout=5.0` masking effect with a dedicated experiment
  varying only the connection timeout (5.0 vs 0 vs 0.05) against an
  identical 10s lock hold and a 2s deadline.
