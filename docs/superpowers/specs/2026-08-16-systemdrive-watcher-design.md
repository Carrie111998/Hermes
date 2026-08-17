# Runner-agnostic `%SystemDrive%` watcher — design

**Date:** 2026-08-16
**Status:** approved, ready for implementation plan
**Supersedes:** `9a6df34e25` on branch `claude/friendly-chatelet-25a116` (never landed)

## Problem

A literal `%SystemDrive%/ProgramData/Microsoft/Windows/Caches/` tree periodically
appears in repository roots on this box.

The mechanism is established. `HKLM\...\ProfileList\ProgramData` is a `REG_EXPAND_SZ`
holding the template `%SystemDrive%\ProgramData`. A process whose environment lacks
`SYSTEMDRIVE` cannot expand it, uses the literal string as a **relative** path, and
builds the known-folder cache under its own working directory. Corroboration: the junk
files are byte-size-identical to the live `C:\ProgramData\Microsoft\Windows\Caches`
(16384 / 309256 / 662848 / 1240) with the version counter reset to `ver0x...0001` — a
genuine fresh cache build at a mis-expanded location, not a stray copy.

One writer was identified and fixed on 2026-08-16: `agent/secret_sources/base.py`
`run_secret_cli`, whose env allowlist omitted `SYSTEMDRIVE`, spawning the
MSIX/WindowsApps-packaged Python. Landed on `main` as `ba920d1b5e` / `c3b8083116`.

A later same-day sighting under a `tests/cron` run does not touch that path, so a
**second writer is likely still open**. This design is the instrument for finding it.

## Why the existing probe needs rework, not landing

The unlanded probe lives **inside** `scripts/run_tests_parallel.py` and observes only
spawns the parallel runner itself makes. That placement followed from a hypothesis:
that the junk required the runner's *concurrent* conditions.

**That hypothesis was falsified on 2026-08-16.** The writer reproduced from one plain
sequential `python -m pytest <file>` run (65 tests, all passing). A probe that only
watches the parallel runner's own spawns is structurally incapable of seeing that.

So the probe's *placement* is wrong. Its *instincts* were right, and three of them are
requirements below.

## Requirements carried forward from `9a6df34e25`

These are not negotiable; they encode failures already paid for.

1. **Opt-in, default off.** `HERMES_TEST_JUNK_PROBE` arms it. An unarmed run pays
   essentially nothing.
2. **Forwarded through the `env -i` allowlist.** `scripts/run_tests.sh` execs the
   runner under `env -i` with an explicit allowlist. Without forwarding the gate, the
   knob is **silently inert** through the canonical wrapper — indistinguishable from a
   clean negative.
3. **Records `*_has_systemdrive`.** This is the falsifier for the entire mechanism
   story. A sighting recorded while `SYSTEMDRIVE` *is* present kills the
   missing-SYSTEMDRIVE explanation and restarts the hunt.
4. **Writes JSONL at the moment of the sighting, never at end of run.** This class of
   run gets killed, times out, or loses its terminal. End-of-run reporting is the one
   copy of the evidence that reliably does not survive.
5. **Prints the negative.** A quiet armed run must state what it watched and for how
   long, so silence is evidence rather than ambiguity.
6. **Never deletes or walks the tree.** The file sizes and version counters inside it
   are the evidence.

## Known limitation this design must attack

The 2026-08-16 prototype (`scratchpad/junk_watcher.py`) fired correctly but **the
writer had already exited**, so its 971-process snapshot identified nothing. What
actually found the first writer was bisecting a deterministic reproducer.

Two latency sources, and the second was probably the larger:

- a 1.5 s poll interval, and
- a `powershell.exe … Get-CimInstance Win32_Process` spawn *at sighting time*,
  costing on the order of 0.5–1 s before any data was captured.

Design consequence: **detection latency and snapshot latency must both collapse, and
even then attribution must not depend on the writer still being alive.**

## Decisions

| Decision | Choice |
|---|---|
| Detection | `ReadDirectoryChangesW` via ctypes, polling fallback |
| Snapshot | In-process `psutil` (already a hard dep, `pyproject.toml:104`) |
| Backward look | Process-creation ring buffer + `cwd` discriminator |
| `HERMES_TEST_JUNK_PROBE` | Launches the watcher as a sidecar; inline probe deleted |
| `--help` fix scope | `-h`/`--help` only; bare pytest-flag routing untouched |

## Architecture

Three units.

### `scripts/systemdrive_watcher.py` (new)

Standalone and runner-agnostic. Usable by hand against any roots, and usable as a
sidecar. Runs with `cwd = Path.home()` so **the watcher can never be its own suspect**.

Internally, three pieces with clean seams so each is testable alone.

#### Detection backends

Both call one shared `_on_sighting(root, path, backend)`, so the forensic path is
identical regardless of how the hit arrived.

- **`_watch_readdirchanges(root, on_hit, stop)`** — one thread per root.
  `CreateFileW(root, FILE_LIST_DIRECTORY, FILE_SHARE_READ|WRITE|DELETE, NULL,
  OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL)`, then a blocking
  `ReadDirectoryChangesW(h, buf, len, bWatchSubtree=FALSE,
  FILE_NOTIFY_CHANGE_DIR_NAME, …)` loop. Parse `FILE_NOTIFY_INFORMATION`
  (`NextEntryOffset`, `Action`, `FileNameLength` in **bytes**, `FileName` as UTF-16),
  and fire on `FILE_ACTION_ADDED` (1) or `FILE_ACTION_RENAMED_NEW_NAME` (5) where the
  name equals `%SystemDrive%`.

  Non-recursive on purpose: we want one specific child of the root, and a subtree watch
  over a repo checkout would deliver enormous volume during a test run.

  Shutdown: threads are daemons; `CancelIoEx` on the handle is best-effort to unblock,
  and the handle is closed. A blocked `ReadDirectoryChangesW` must never hold up exit.

- **`_watch_polling(root, on_hit, stop, interval)`** — fallback for non-Windows, for a
  root that cannot be opened with the required access, or when explicitly requested.
  One `Path.exists()` per root per tick.

Backend selection is per-root and **recorded** in the `armed` record. A run must never
be able to claim a fast watch it did not get.

#### Process-creation ring buffer

A sampler thread on a short cadence:

1. `psutil.pids()` — one cheap syscall returning the live PID set.
2. Diff against the previous set.
3. For **new PIDs only**, read `ppid`, `name`, `cmdline`, `create_time`, `cwd`.
4. Append to a bounded `collections.deque`.

Cost therefore scales with process *churn*, not process *count*, and memory is flat
over a multi-hour watch. Enriching only new PIDs is what makes a short cadence
affordable; enriching all ~971 every tick would not be.

Reads of a PID that has already exited raise `NoSuchProcess`/`AccessDenied`; record
whatever fields were obtained plus the error, and never drop the entry — a PID we saw
appear and could not read is still evidence that *something* started.

#### `_on_sighting()`

Ordering is the whole point. Perishable data first:

1. Dump the ring buffer (already in memory — no syscalls).
2. Enumerate live processes and partition by `cwd`.
3. Write the sidecar snapshot JSON.
4. Append the `SIGHTING` JSONL record.

**The `cwd` discriminator.** The established mechanism *requires* the writer to have
the watched root as its working directory. So both the live enumeration and the ring
buffer are partitioned into `cwd_matches_root` and the rest. This is what turns a
971-process dump into a shortlist, and it works on ring-buffer entries too, because
`cwd` was captured at spawn.

Honest limit: `ring_cwd_matches` is best-effort. A process that exits between appearing
in `psutil.pids()` and being read has no recoverable `cwd`, so it lands in the ring
buffer with an error marker and cannot be partitioned. The buffer still proves *that*
something started in the window, which is strictly more than the prototype captured.

Only the **first** absent→present transition per root is reported; later ticks would
re-report the same tree and bury it. A tree already present when the watch starts is
recorded as `preexisting` and blames nobody.

### `scripts/run_tests_parallel.py` (modified)

- The ~180-line inline stat-probe from `9a6df34e25` is **not** carried over.
- `HERMES_TEST_JUNK_PROBE=1` spawns the watcher as a sidecar child for the run's
  duration:
  `[sys.executable, scripts/systemdrive_watcher.py, str(repo_root),
  "--secs", <run budget>, "--stop-file", <path>]` with `cwd=Path.home()` and
  `env=os.environ`. The sidecar **inherits the runner's stdout/stderr**, which is what
  makes a sighting shout in the runner's own output and what carries the `done`
  negative to the terminal.

  Env inheritance is load-bearing: the sidecar sees exactly the runner's environment,
  so `watcher_has_systemdrive` reports the runner's condition **for free**, with no
  manual plumbing to drift out of sync.
- Graceful stop: the runner touches a stop-file at end of run. The watcher's sampler
  loop checks it each cadence and shuts down through the normal path, so the `done`
  record — the negative — is emitted. The runner waits briefly, then terminates.
  The watcher also self-limits via `--secs`, so an orphaned sidecar cannot outlive
  the run indefinitely.
- `-h` / `--help` added to `OUR_FLAGS`.
- The inline argv-splitting block is lifted to a module-level `_split_argv(argv)`.

### `scripts/run_tests.sh` (modified)

The forwarding hunk from `9a6df34e25`, unchanged: `HERMES_TEST_JUNK_PROBE` joins the
forwarded list, and the `CLEAN_ENV` comment warning against "fixing" the missing
`SYSTEMDRIVE` before the instrument has caught a writer is retained.

## The `--help` fix

`scripts/run_tests_parallel.py` builds an `argparse` parser with `add_help=True`, but
**argparse never sees `--help`**. At `:1287`, any token starting with `-` that is not
in `OUR_FLAGS` is routed to pytest passthrough. `--help` is not in `OUR_FLAGS`, so:

- it is peeled into `bare_passthrough`,
- `our_args` ends up empty, so no path filter is applied,
- discovery falls through to all ~2384 test files, and
- **a full suite run starts.**

On 2026-08-16 this launched a stray 12-worker run that had to be killed by PID tree.

Fix: add `-h` and `--help` to `OUR_FLAGS` so argparse receives them and exits 0 before
any discovery or spawn.

Scope is deliberately narrow. The bare-pytest-flag routing (`-q`, `-x`, `--tb=long`,
`-k expr`) is a documented feature matching pytest muscle memory and is left intact. A
broader "refuse an unfiltered full-suite run under unknown long flags" guard was
considered and rejected: it would reject a legitimate
`run_tests_parallel.py --tb=long` full-suite run.

`_split_argv(argv)` is extracted purely for testability — identical logic, no behaviour
change — so the regression can be asserted without ever starting a suite.

## Data formats

JSONL appended to `~/.hermes/logs/systemdrive-watcher.jsonl`. Log location is derived
from `Path.home()`, which survives `env -i`, and is **outside** any watched root so the
watcher does not litter the directory it is watching. Falls back to the temp dir if
home is unresolvable.

Every record carries `event`, `at` (ISO-8601 seconds), `watcher_pid`.

- **`armed`** — `roots`, `backend_by_root`, `sample_ms`, `poll_ms`, `ring_capacity`,
  `watcher_has_systemdrive`, `watcher_cwd`.
- **`preexisting`** — `root`, `path`, `note` (explicitly: cannot attribute).
- **`backend_downgrade`** — `root`, `reason`, `note`. Emitted when a root cannot
  be opened for a directory watch and falls back to polling, so a degraded watch is
  never mistaken for a fast one.
- **`SIGHTING`** — `root`, `path`, `backend`, `watcher_has_systemdrive`,
  `live_cwd_matches`, `ring_cwd_matches`, `live_process_count`, `ring_size`,
  `snapshot_file`.
- **`done`** — `sightings`, `roots`, `watched_secs`, and a `note` that states the
  negative in words when `sightings == 0`.

The full live table and full ring buffer go to a sidecar
`systemdrive-sighting-<ts>.json` so the JSONL stays greppable.

## Error handling

A diagnostic must never take down the thing it is observing.

- Every ctypes call is checked; any failure downgrades that root to the polling backend
  and **records the downgrade** rather than failing silently.
- `psutil` exceptions during sampling are captured per-PID, never propagated.
- Log-write failures print to stderr and are swallowed.
- Sidecar spawn failure in the runner prints a warning and the run proceeds unwatched —
  but says so, because an unarmed-looking run must not be mistaken for a clean negative.

## Testing

`tests/scripts/test_systemdrive_watcher.py` (matching the existing `tests/scripts/`
convention for script tests):

- ring buffer bounding and the creation-diff logic (pure, no processes spawned);
- `_on_sighting()` record shape, including `watcher_has_systemdrive` and the `cwd`
  partition;
- the polling backend against a real tmpdir;
- the `preexisting` path;
- **JSONL is written at sighting time** — assert the record is on disk *before* any
  teardown runs, since that is requirement 4 and the easiest to regress;
- the `ReadDirectoryChangesW` backend end-to-end: create a `%SystemDrive%` directory in
  a tmpdir and assert the callback fires. Windows-gated.

`tests/test_run_tests_parallel.py`:

- `_split_argv` routes `-h`/`--help` to our args — the regression;
- bare `-q` and `-k expr` still route to pytest passthrough (guards the fix's blast
  radius);
- the sidecar is not spawned when `HERMES_TEST_JUNK_PROBE` is unset.

## Verification protocol

This repo's test outcomes are **location-dependent**: the same commit yields 22–23
failures from inside the shared checkout and 3 from a Temp-resident worktree, because
the suite probes git worktree state around itself. A prior A/B across two paths
"showed" a change fixing a test that it did not fix.

Therefore: **baseline and after-state are both measured in one worktree at one commit.**
No cross-path comparison.

1. Baseline `tests/test_run_tests_parallel.py` and `tests/scripts/` in this worktree.
2. Implement.
3. Re-run the same files, same worktree; compare failure *names*, not just counts.
4. End-to-end proof: arm the watcher against a sentinel tmpdir, `mkdir '%SystemDrive%'`,
   and confirm the sighting is detected, recorded to JSONL at the moment it happens, and
   attributed with a `cwd` shortlist. This is the prototype-level proof the old probe
   never obtained.
5. Confirm `--help` prints usage and exits 0 **without** spawning anything.

## Open tradeoff

The sampler cadence is a genuine tradeoff: too slow and sub-cadence processes are
missed even by the ring buffer; too fast and a multi-hour watch burns measurable CPU on
a box with documented memory-pressure problems. It is therefore a flag with a
conservative default, and the measured cost is to be recorded in the commit message
rather than asserted from a guess.

## Out of scope

- Fixing any newly identified writer. This is an instrument; a fix is separate work
  with its own reproducer and regression test.
- Deleting existing `%SystemDrive%` trees. They are evidence.
- Adding `SYSTEMDRIVE` to the `run_tests.sh` allowlist. Doing so before the instrument
  catches a writer would erase the only reproducer.
- ETW / `Win32_ProcessStartTrace` process-creation events. True sub-millisecond creation
  hooking needs elevation; the ring buffer is the non-elevated approximation.

## References

- Agent memory: `systemdrive-literal-dir-in-repo-root.md`
- MemPalace wing `hermes`, rooms `systemdrive-writer-identified-and-reproduced-2026-08-16`
  and `systemdrive-junk-probe-armed-and-new-sighting-2026-08-16`
- Prototype: `scratchpad/junk_watcher.py` (worktree `objective-bose-88f55f`)
- Superseded probe: `9a6df34e25` on `claude/friendly-chatelet-25a116`
