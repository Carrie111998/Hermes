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

A later same-day sighting (18:29:34) appeared during a `tests/cron` run, in a path the
fix does not touch.

**Updated later on 2026-08-16: `tests/cron` has been CLEARED as that writer.** A
sentinel-cwd experiment — the full suite run from a directory no other session uses —
came back `1043 passed, 17 skipped`, exit 0, with no `%SystemDrive%` in the sentinel,
none under the basetemp, and the repo-root tree frozen at its original mtimes. A
targeted run of the only five files that spawn `sys.executable` was equally clean, and
statically no cron path meets the precondition: every cron `env=` builder copies
`os.environ`, so `SYSTEMDRIVE` always survives.

So the sighting is best explained by **another tenant of the multi-tenant shared
checkout** — some process that merely shares the checkout as its cwd. Attribution to a
specific session was not attempted and remains unproven.

**This sharpens the case for this design rather than weakening it.** The remaining
suspect is by definition not the test suite and not any one runner; it is whatever else
holds that cwd. A probe embedded in the parallel runner cannot see such a writer even in
principle. A runner-agnostic watcher that records process creations with their cmdline
and cwd is precisely the instrument that can.

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

### Measured outcome (2026-08-16, on this box)

| | Prototype | Built | Result |
|---|---|---|---|
| Detection latency | ≤1.5 s poll | `ReadDirectoryChangesW` | **0.4–2.7 ms, median 0.7 ms** (6 trials) — ~2000× |
| Sighting record durable | after a full process snapshot | after the ring dump | **~ms**; verified durable at 311 ms while `on_hit` ran to 571 ms |
| Attribution of an exited writer | impossible | ring buffer at 100 ms | **5/8 trials fully attributed** (measured 2026-08-17) — see "Sampler cadence and end-to-end capture rate" below |

**The snapshot mitigation needs a caveat, because the naive reading is wrong.**
Replacing the PowerShell spawn removed a fixed ~0.5–1 s cost, but psutil is *not* cheap
at full-table scale on Windows: `describe_pid` costs ~204 ms per process against ~968
processes, because `ppid()` and `create_time()` each take a fresh full-table snapshot
per call. A complete sweep is 60–200 s, and `process_iter(attrs=…)` is no faster
(112.87 s / 919 processes), so the cost cannot be optimised away. That measurement is
what forced the sighting record to be split — see the record formats below.

The **ring buffer is unaffected** by this: it enriches only newly-appeared PIDs, so its
cost tracks process *churn* rather than process *count*. That asymmetry is the reason
the backward-looking design is affordable at all.

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

Ordering is the whole point. Perishable data first, then **durability**, then the slow
best-effort enrichment:

1. Dump the ring buffer (already in memory — no syscalls).
2. **Append the `SIGHTING` JSONL record immediately.** Everything perishable is already
   in hand at this point, and step 3 can take minutes.
3. Enumerate live processes under a time budget and partition by `cwd`.
4. Write the sidecar snapshot JSON and the `SIGHTING_LIVE` record.

**Steps 2 and 3 were originally the other way round.** That is a correctness bug, not a
preference: it left the durable record trailing a 60–200 s enumeration in a design whose
premise is that the run gets killed. See the record-format note below for the
measurements that forced the change.

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
  `ring_cwd_matches`, `ring_size`. Written **immediately** after the ring dump and
  before any live enumeration, and marks that live data is still pending.
- **`SIGHTING_LIVE`** — `live_cwd_matches`, `live_process_count`, `snapshot_file`,
  plus explicit sweep-coverage fields (examined / total / truncated / elapsed).

**Why these are two records and not one — corrected 2026-08-16 after measurement.**
The original design put a single `SIGHTING` record *after* the live enumeration. On
this box a full sweep costs **60–200 s** (`describe_pid` ≈ 204 ms × ~968 processes;
`psutil`'s `ppid()` and `create_time()` take a fresh full-table snapshot per call on
Windows, making it quadratic). `psutil.process_iter(attrs=…)` was measured as an
alternative at 112.87 s for 919 processes — **no speedup**, so there is no cheap
batched path and the cost cannot be optimised away.

That meant the durable record landed minutes after the event, in a design whose whole
premise is that *this class of run gets killed*. The ordering contained the exact
failure it was built to prevent. Splitting the record makes the perishable evidence
durable in milliseconds and demotes the live table to best-effort enrichment.

The live sweep is time-budgeted (`live_sweep_secs`, default 30 s) and reports its own
coverage. A partial sweep that presented itself as complete would be a silent cap —
which this project treats as a defect, not a convenience.
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

## Sampler cadence and end-to-end capture rate (measured, 2026-08-17)

This was left open in the original design. It is now decided and measured, and the
decision itself needed a correction along the way.

### Per-sample cost

One `ProcessRing.sample()` call costs **16.65 ms** against ~1000 live processes with
real churn (measured during Task 3). A quiet re-check just now, with no churn, cost
**0.87 ms** — consistent with, not contradicting, the design's own claim that cost
scales with process *churn* rather than process *count* (only newly-appeared PIDs are
enriched). The 16.65 ms figure is the one that matters: it was taken under the
conditions this watcher actually runs in.

### The sizing rule that was wrong

The original plan said: raise the cadence if a sample costs more than half the
interval. By that rule, 16.65 ms against a 250 ms interval passes comfortably. But a
250 ms sampler captured only **9 of 12** short-lived Python children (each living
350–514 ms), while a 50 ms sampler caught **12 of 12**. **Cost was never the binding
constraint — capture probability was.** The cadence must be sized against the
*lifetime of the process class being hunted*, not against the sample's own cost.
Shipped default: **100 ms** (`DEFAULT_SAMPLE_MS`), ~17% of one core, chosen to sit
comfortably below the 350–514 ms lifetimes actually observed while leaving margin for
churn-induced slowdown.

### The centerpiece: does the ring buffer attribute a writer that has already exited?

This is the claim the whole ring-buffer design rests on, and the one thing no prior
measurement in this file actually tested end-to-end. Built a real reproducer
(`writer_stub.py`, matching brief Step 2's `python -c` script): a **separate** process
`mkdir`s a sentinel directory, `chdir`s into it, creates the literal `%SystemDrive%`
child, and exits — the exact failure mode the 2026-08-16 prototype hit, where the
writer was gone by the time the snapshot ran.

Ran against a fresh, never-reused sentinel directory, default 100 ms cadence, real
`ReadDirectoryChangesW` backend, 8 independent trials:

| Outcome | Trials | What it means |
|---|---|---|
| **Fully attributed** — pid, `cmdline`, argv naming the sentinel dir all present in the ring dump | **5/8** | A human reading the record can name the writer. |
| **Registered but unreadable** — a bare `{"pid": ..., "error": "NoSuchProcess"}` entry, no `cmdline` | 2/8 | The sampler saw *something* start, but the process was already gone by enrichment time. In one of these two trials, six such anonymous entries appeared in the same window (background churn), making the writer unidentifiable among them even in principle. |
| **Fully invisible** — no ring entry at all | 1/8 | The writer's entire lifetime (birth to exit) fit inside a single 100 ms inter-sample gap; `psutil.pids()` never observed it as live. |

**Capture rate: writer identified by name in 5/8 trials.** This is a real number, not
tuned or cherry-picked — cadence was left at the shipped default across all 8 trials
and the 3 misses are reported as misses.

### `ring_cwd_matches` and the `chdir` race — measured, then CORRECTED

**First measurement (2026-08-17):** `ring_cwd_matches` was empty in all 8 trials,
including all 5 successes. In every successful attribution the captured `cwd` was the
*writer's parent's* cwd, not the sentinel, because the sampler read `proc.cwd()` before
the child had executed its own `os.chdir()`. Attribution still succeeded, but only from
the raw ring array's `cmdline`, not from the pre-filtered field.

**Correction, same day — that 0/8 is an artifact of the REPRODUCER, not a property of
the instrument, and the distinction matters because the wrong reading would retire a
working discriminator.** The stub spawned a process that *then* chdir'd into the target.
The established mechanism is the opposite: the writer **inherits** `cwd = repo_root`
from its parent and never chdirs — that inheritance is the whole reason the junk lands
in the checkout root. Re-measured with both shapes, one cadence tick, otherwise
identical:

| Writer shape | Present in ring | `ring_cwd_matches` |
|---|---|---|
| `chdir` after spawn (the Task 7 stub) | 4/4 | **0/4** |
| **inherits `cwd` at spawn (the real mechanism)** | 4/4 | **3/4** |

So the `cwd` discriminator **works for the shape that actually matters**. It fails only
against a writer that changes directory after starting — a shape not yet observed here,
and one that would additionally have to defeat the inheritance mechanism to be relevant.

The operational guidance below is unchanged and still correct: the filtered field is a
shortcut, and the raw ring is the evidence. But do not conclude from the 0/8 that the
discriminator is broken — measured against the real writer shape it is the thing that
turns ~1000 processes into a shortlist, exactly as designed.

**Guidance for a future hunter:** read the whole `ring` array from the sidecar
snapshot (or the ring dump attached to `SIGHTING`), matching on `cmdline` substrings,
not just `ring_cwd_matches`. `ring_cwd_matches` is a convenience shortlist, not the
complete evidence. If the ring shows nothing at all for the sighting window (the 1/8
total-miss case above), the documented fallback is what actually found the first
writer: bisecting a deterministic reproducer, not staring harder at a process
snapshot.

### Residual tradeoff

The tradeoff between cadence and CPU is real but no longer open: 100 ms costs ~17% of
one core and is the shipped default. What remains genuinely open is that even at this
cadence, a process class living under ~100 ms (the 1/8 total-miss case, and part of
the 2/8 unreadable case) is attributable only probabilistically, not guaranteed — a
faster cadence would narrow but not close that gap, at proportionally higher constant
CPU cost on a box with documented memory-pressure problems.

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
