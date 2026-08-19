#!/usr/bin/env python3
"""Per-file parallel test runner.

The minimum-viable replacement for pytest-xdist + a subprocess-isolation
plugin. Discovers test files under ``tests/`` (excluding integration/e2e
unless explicitly requested), then runs one ``python -m pytest <file>``
subprocess per file, with bounded parallelism (default: ``os.cpu_count()``).

Concurrency is bounded twice over. ``-j`` bounds THIS invocation; a
machine-global slot limiter bounds every invocation on the host together,
so two concurrent runs take turns instead of stacking to 2x the core
count. A commit-charge gate additionally holds off a spawn while memory
is tight. See the "Host-saturation guards" block below for why both exist
(2026-08-12: ~50 stacked workers starved a dashboard boot for 29 minutes).

Why per-file rather than per-test?
    Per-test spawn overhead (~250ms × 17k tests = 70min CPU minimum)
    swamped the actual work. Per-file spawn (~250ms × ~850 files = ~3.5min)
    fits in the budget while still giving every file a fresh Python
    interpreter — the only isolation boundary that actually matters
    (cross-file module-level state leakage was the original flake source;
    intra-file state is the test author's responsibility).

Why drop xdist entirely?
    xdist's persistent workers accumulate state across files, which is
    exactly the leakage we wanted to fix. xdist also adds complexity
    (loadfile vs loadscope, --max-worker-restart, internal control plane)
    that we don't need when the unit of work is "run pytest on one file".
    A subprocess.Popen pool gated by a semaphore is ~60 lines and does
    the job.

Usage:
    python scripts/run_tests_parallel.py [pytest_args...]

    Common pytest args pass through to each per-file pytest invocation
    (e.g. ``-q``, ``-v``, ``-x``, ``--tb=long``, ``-k 'pattern'``, ``--lf``)
    with no special separator — a bare ``-q`` "just works". Anything after
    a literal ``--`` is also passed through, and stacks with bare flags.

Environment:
    HERMES_TEST_WORKERS  Override worker count (default: os.cpu_count())
    HERMES_TEST_PATHS    Override discovery roots (colon-sep, default: 'tests')

    NOTE: scripts/run_tests.sh execs this script under ``env -i`` with an
    explicit allowlist, so a variable only reaches us if that allowlist
    names it. The host-saturation guards are therefore configured by CLI
    flag (--no-host-limit / --host-slots / --min-free-commit-gb), not by
    environment, and key their state off ``Path.home()`` — one of the few
    things that survives the clean environment.

Exit code: 0 if every file's pytest exited 0; 1 otherwise.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Dict, List, Tuple


# Default test discovery roots.
_DEFAULT_ROOTS = ["tests"]

# Directories to skip during discovery — these suites require real
# external services (a model gateway, a docker daemon with a prebuilt
# image, etc.) and are run in their own dedicated CI jobs:
#
#   tests/e2e/         — .github/workflows/tests.yml :: e2e job
#   tests/integration/ — historical; legacy --ignore flags
#   tests/docker/      — .github/workflows/docker.yml ::
#                        build-amd64 job (runs against the freshly-loaded
#                        nousresearch/hermes-agent:test image, via
#                        ``HERMES_TEST_IMAGE`` so the fixture skips
#                        rebuild). The full pytest-shard runner can't
#                        host these because the session-scoped
#                        ``built_image`` fixture would do a 3-7min
#                        ``docker build``,
#                        so the build is guaranteed to die in fixture
#                        setup. The dedicated job sidesteps both costs.
_SKIP_PARTS = {"integration", "e2e", "docker"}

# Per-file wall-clock cap. Override
# via --file-timeout or HERMES_TEST_FILE_TIMEOUT.
#
# Deliberately generous: a large-collection file pays interpreter startup
# plus a heavy import graph before any test logic runs, and that overhead
# dilates under load, producing false "no tests ran" timeouts on files that
# finish comfortably on a quiet box. The Docker build matrix jobs take 7-10
# min anyway, so this headroom costs nothing on total CI wall time while
# keeping a genuinely hung file bounded.
#
# Raised 300s -> 1800s on 2026-08-12, from measurement. The 300s value was
# reached by exactly the reasoning above but set below what this hardware
# actually needs. A full `tests/hermes_cli` run (467 files, ~8770 tests, -j 8
# on 12 cores) put 28 files in the "no tests ran" bucket. Re-running just
# those 28 with the cap lifted and retries off gave max 664.3s, p90 557.2s,
# median 95.4s -- only 4 of 28 over 300s, so most were killed for being slow
# under sustained load rather than being slow in themselves.
#
# Do NOT re-derive this from "observed max x margin": on this box the same
# file's wall time swings hard with ambient load, and not monotonically with
# -j. `test_web_server.py` finished inside 664s in a 28-file -j 8 batch and
# took 1234.3s in a 4-file -j 4 batch. An intermediate 1200s trial still
# killed it. Treat any single number as a lower bound on what a legitimate
# file can take here.
#
# 1800s is therefore chosen as "well past any plausible legitimate run" rather
# than as a tight fit. That is the right bias, because the two failure modes
# are wildly asymmetric:
#
#   too tight -> SIGKILL the tree, report "no tests ran", and silently discard
#                every real result in the file. Measured cost of the 300s cap:
#                `test_commands.py` reported nothing while actually passing
#                175/175 (647.1s), and `test_config.py` reported nothing while
#                actually surfacing 11 genuine failures (1090.2s). A green-
#                looking run was hiding real red.
#   too loose -> one worker of 8 idles until a genuinely hung file is killed.
#
# The cap's job is bounding a hang, not policing slowness. Prefer masking
# nothing.
_DEFAULT_FILE_TIMEOUT_SECONDS = 1800.0

# One-shot retry of failing test FILES. A file that exits non-zero is re-run
# once in a fresh subprocess; if the re-run passes, the file counts as passed
# but is loudly reported as FLAKY so it gets fixed rather than hidden.
# Deterministic failures fail both attempts — a real regression can never be
# laundered into green by this (it would have to flake in our favor twice in
# a row on the same runner, which is exactly the definition of a flake).
# Set to 0 to disable (env: HERMES_TEST_FILE_RETRIES).
_DEFAULT_FILE_RETRIES = 1

# Duration cache: maps relative file paths to last-observed subprocess
# wall-clock seconds. Used by ``--slice`` to distribute files across
# CI jobs by estimated total time, so no one job gets all the slow files.
_DURATIONS_FILE = "test_durations.json"

# One root per runner invocation, one child per test file. Pytest's default
# tmp_path base is shared per OS user and rotates numbered ``pytest-N`` dirs;
# concurrent pytest subprocesses can therefore delete each other's live temp
# roots during retention cleanup. Explicit per-file --basetemp paths remove
# that cross-process race. Lazily created so imports/tests that only exercise
# pure helpers don't dirty the filesystem.
_BASETEMP_ROOT: Path | None = None
_basetemp_lock = threading.Lock()


# ── Host-saturation guards ──────────────────────────────────────────────
# On 2026-08-12 a Hermes dashboard startup was starved for 29 minutes and
# died without ever reaching its uvicorn bind: 5.1s CPU / 3 threads / 83MB
# RSS after 29 min, against 6.1s / 6 threads / 110MB for a healthy boot.
# RSS *below* the healthy figure is the tell — the working set was never
# faulted in. That is thrashing, not a deadlock.
#
# The trigger was ~50 concurrent pytest subprocesses on a 12-core box.
# Nothing here could have produced 50 from one invocation (the old default
# topped out at cpu_count*2 = 24), so at least two invocations stacked.
# Three separate gaps allowed it, and each has its own guard below:
#
#   1. The default was cpu_count*2 despite the docstring promising
#      cpu_count. -j 24 is known to oversubscribe this host; the failure
#      tail vanishes at -j 12.  →  _default_worker_count()
#   2. Nothing coordinated across invocations, and a per-process
#      semaphore cannot: the stacking runs live in different worktrees,
#      in different shells, with no shared parent.  →  _acquire_global_slot()
#   3. Nothing consulted memory pressure, even though the resource that
#      actually ran out was commit charge, not CPU.  →  _await_commit_headroom()
#
# Why file locks rather than a named semaphore or a PID registry: an OS
# file lock is released by the kernel when the holding process dies, however
# it dies. A registry of PIDs (or a counter file) would need stale-entry
# reaping, and a reaper that runs while the box is thrashing is exactly the
# code least likely to get scheduled. Ctrl-C'ing a run must never wedge the
# host, and with file locks that is a property of the kernel, not of our
# cleanup path.

# Machine-global slot directory. This must sit OUTSIDE any repo: the
# invocations we need to serialize typically run in different git
# worktrees, so a repo-relative path would give each its own private
# limiter and coordinate nothing.
#
# ``Path.home()`` (not an env var) is deliberate. scripts/run_tests.sh
# execs the runner under ``env -i`` with a fixed allowlist, so almost
# every HERMES_* variable is stripped before main() ever sees it —
# HOME/USERPROFILE are among the few that survive. A limiter keyed on an
# env var would silently no-op through the primary entry point.
_SLOT_DIR_NAME = Path(".hermes") / "locks" / "test-slots"

# How long to wait for commit headroom before giving up and spawning
# anyway (loudly). Bounded on purpose: if the box is out of commit for a
# reason unrelated to us, blocking forever converts a slow suite into a
# hung one, which is a worse failure than the one we are preventing.
_DEFAULT_COMMIT_WAIT_SECONDS = 120.0

# Commit headroom required before spawning another pytest subprocess.
# A worker costs roughly 200-400MB here, so ~4GB keeps a full complement
# of in-flight workers plus the OS and the resident Hermes services
# (gateway, mempalace, gbrain) off the cliff edge.
_DEFAULT_MIN_FREE_COMMIT_BYTES = 4 * 1024**3

_commit_warned = threading.Event()
_slot_wait_warned = threading.Event()

# Invocation-scoped limiter config, populated by main(). Module-level so
# the worker threads can read it without threading it through every call
# site (same pattern as _SKIP_PARTS).
_HOST_LIMIT_ENABLED = True
_SLOT_CAPACITY = max(1, os.cpu_count() or 4)
# None means "resolve via _default_slot_dir() on first use". Not eagerly
# resolved here: _default_slot_dir() is defined below, and a literal path
# default risks scattering lock files into whatever cwd an importer has.
_SLOT_DIR: "Path | None" = None
_MIN_FREE_COMMIT = _DEFAULT_MIN_FREE_COMMIT_BYTES


def _default_worker_count() -> int:
    """Default ``-j``: one worker per core, never a multiple of it.

    Historically ``cpu_count * 2``, on the theory that per-file pytest
    subprocesses are partly I/O bound (interpreter startup, imports) so
    the box could absorb the oversubscription. It cannot: each worker is
    a full CPython process with the repo's import graph resident, so the
    cost that actually binds is committed memory, and doubling the worker
    count doubles it.
    """
    return max(1, os.cpu_count() or 4)


def _global_slot_capacity() -> int:
    """Total pytest subprocesses allowed on this HOST, across all runs."""
    return max(1, os.cpu_count() or 4)


def _default_slot_dir() -> Path:
    """Machine-global slot directory, with a temp-dir fallback.

    ``Path.home()`` raises when the home directory is unresolvable. That
    must not take down the test runner at import time, so fall back to the
    shared temp root — still machine-global, which is the property the
    limiter actually needs.
    """
    try:
        return Path.home() / _SLOT_DIR_NAME
    except (RuntimeError, OSError):
        return Path(tempfile.gettempdir()) / "hermes-test-slots"


def _try_lock(handle) -> bool:
    """Take an exclusive, non-blocking OS lock on *handle*'s first byte."""
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _slot_dir_usable(slot_dir: "Path | str") -> bool:
    """True if *slot_dir* exists (or can be created) and can hold locks.

    Kept separate from acquisition because "cannot use slots at all" and
    "every slot is currently busy" demand opposite responses — proceed
    immediately vs. wait for a peer to finish. Collapsing them into one
    ``None`` return made an uncreatable directory (read-only HOME, exotic
    sandbox) spin forever instead of degrading to unlimited.
    """
    try:
        Path(slot_dir).mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _try_acquire_any_slot(capacity: int, slot_dir: "Path | str"):
    """Grab any free slot, or return None if all *capacity* are taken.

    Returns the open file handle on success — the caller must keep it
    alive for as long as it holds the slot, since closing the file drops
    the lock. Pass it to :func:`_release_slot` when done.
    """
    directory = Path(slot_dir)
    if not _slot_dir_usable(directory):
        return None

    for index in range(capacity):
        path = directory / f"slot-{index:02d}.lock"
        try:
            handle = open(path, "a+b")
        except OSError:
            continue
        # msvcrt.locking() needs a byte to actually exist at the offset.
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
        except OSError:
            handle.close()
            continue
        if _try_lock(handle):
            return handle
        handle.close()
    return None


def _release_slot(handle) -> None:
    """Drop a slot acquired via :func:`_try_acquire_any_slot`."""
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Closing the handle releases the lock regardless.
        pass
    try:
        handle.close()
    except OSError:
        pass


class _acquire_global_slot:
    """Context manager holding one machine-global test-execution slot.

    Blocks until a slot frees up. Blocking is the intended behaviour: two
    concurrent invocations should take turns rather than both running at
    full width. The wait is announced once so a run that looks stalled is
    explicable.
    """

    def __init__(
        self,
        capacity: int,
        slot_dir: "Path | str | None" = None,
        enabled: bool = True,
    ):
        self._capacity = capacity
        self._slot_dir = _default_slot_dir() if slot_dir is None else slot_dir
        self._enabled = enabled
        self._handle = None

    def __enter__(self):
        if not self._enabled:
            return self
        if not _slot_dir_usable(self._slot_dir):
            # No lockable location. Degrade to the historical unlimited
            # behaviour rather than blocking tests from running at all.
            if not _slot_wait_warned.is_set():
                _slot_wait_warned.set()
                print(
                    f"  [host-limit] cannot create slot dir {self._slot_dir} — "
                    f"running without a machine-global limit.",
                    flush=True,
                )
            return self
        while True:
            self._handle = _try_acquire_any_slot(self._capacity, self._slot_dir)
            if self._handle is not None:
                return self
            if not _slot_wait_warned.is_set():
                _slot_wait_warned.set()
                print(
                    f"  [host-limit] all {self._capacity} machine-global test "
                    f"slots are busy — another test run is active on this box. "
                    f"Waiting rather than stacking on top of it.",
                    flush=True,
                )
            # Poll rather than block on a single slot: waiting on slot 0
            # specifically would ignore slot 5 freeing up first.
            time.sleep(0.25)

    def __exit__(self, *exc_info) -> None:
        _release_slot(self._handle)
        self._handle = None
        return None


def _available_commit_bytes() -> "int | None":
    """Bytes of commit charge still available, or None if unknown.

    Commit — not physical RAM — is the metric that matters. The 08-12
    incident exhausted commit while physical memory still showed free
    pages, so a ``virtual_memory()``-style reading would have reported a
    healthy box right up until the dashboard died.
    """
    if sys.platform == "win32":
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        try:
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
        except (OSError, AttributeError):
            return None
        # ullAvailPageFile is "how much more can be committed" — exactly
        # the headroom a new process needs to reserve its working set.
        return int(status.ullAvailPageFile)

    # POSIX: MemAvailable + SwapFree is the closest analogue to Windows'
    # commit headroom.
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemAvailable", "SwapFree"):
                    values[key] = int(rest.strip().split()[0]) * 1024
        if "MemAvailable" not in values:
            return None
        return values["MemAvailable"] + values.get("SwapFree", 0)
    except (OSError, ValueError, IndexError):
        return None


def _await_commit_headroom(
    min_free_bytes: int = _DEFAULT_MIN_FREE_COMMIT_BYTES,
    deadline_seconds: float = _DEFAULT_COMMIT_WAIT_SECONDS,
) -> bool:
    """Wait for *min_free_bytes* of commit headroom.

    Returns True once headroom is available, False if the deadline passed
    first — in which case the caller spawns anyway. Returning False rather
    than raising is deliberate: a runner that refuses to run tests when
    memory looks tight would be its own outage.

    Also returns True when headroom is unmeasurable, so an unsupported
    platform degrades to today's behaviour rather than stalling.
    """
    if min_free_bytes <= 0:
        return True
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    while True:
        available = _available_commit_bytes()
        if available is None or available >= min_free_bytes:
            return True
        if time.monotonic() >= deadline:
            if not _commit_warned.is_set():
                _commit_warned.set()
                print(
                    f"  [host-limit] only {available / 1024**3:.1f}GB commit free "
                    f"(want {min_free_bytes / 1024**3:.1f}GB) after "
                    f"{deadline_seconds:.0f}s — spawning anyway. Expect slow "
                    f"tests and starved background services.",
                    flush=True,
                )
            return False
        time.sleep(0.5)


def _basetemp_for(file: Path, repo_root: Path) -> Path:
    """Return a unique, stable basetemp for *file* in this runner invocation."""
    global _BASETEMP_ROOT  # noqa: PLW0603 — invocation-scoped lazy state
    with _basetemp_lock:
        if _BASETEMP_ROOT is None:
            _BASETEMP_ROOT = Path(tempfile.mkdtemp(prefix="hermes-parallel-"))
            # The end-of-run call is on the normal path only; nothing runs it
            # when the process leaves by another door (Ctrl-C, an unhandled
            # exception, a file-timeout kill, the host-saturation abort). Each
            # of those stranded the whole tree: 60 roots / 1526.3 MB were
            # measured in %TEMP% on 2026-08-19, accruing ~26/day. Registering
            # here rather than at import keeps the "don't dirty the filesystem
            # for pure-helper imports" property. _cleanup_basetemps() clears
            # _BASETEMP_ROOT, so the normal path and this handler cannot
            # double-remove.
            atexit.register(_cleanup_basetemps)

    try:
        relative = file.resolve().relative_to(repo_root.resolve())
    except ValueError:
        relative = file.resolve()
    # Include the whole normalized path rather than only the basename: files
    # such as tests/cron/test_status.py and tests/gateway/test_status.py must
    # not collide. The process-local hash suffix bounds accidental slug
    # collisions without relying on randomized hash() output.
    slug = "-".join(relative.parts).replace(":", "").replace(" ", "_")
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in slug)
    return _BASETEMP_ROOT / safe


def _cleanup_basetemps() -> None:
    """Remove the invocation's basetemp tree after every worker has exited."""
    global _BASETEMP_ROOT  # noqa: PLW0603 — invocation-scoped lazy state
    if _BASETEMP_ROOT is not None:
        root, _BASETEMP_ROOT = _BASETEMP_ROOT, None
        shutil.rmtree(root, ignore_errors=True)
        # ignore_errors keeps cleanup non-fatal, but on its own it also makes a
        # locked tree indistinguishable from a clean sweep -- that silence is
        # why 6 roots holding 76-374 MB each sat unnoticed. Say so instead.
        if root.exists():
            print(
                f"warning: could not fully remove basetemp root {root} "
                "(locked or in use); it will be left behind",
                file=sys.stderr,
            )


def _split_file_list(value: str) -> List[str]:
    """Split a colon-separated ``--files`` value without splitting drive roots."""
    files: List[str] = []
    entry_start = 0

    for index, char in enumerate(value):
        if char != ":":
            continue
        is_windows_drive = (
            index == entry_start + 1
            and value[entry_start].isalpha()
            and index + 1 < len(value)
            and value[index + 1] in ("/", "\\")
        )
        if is_windows_drive:
            continue
        entry = value[entry_start:index]
        if entry.strip():
            files.append(entry)
        entry_start = index + 1

    entry = value[entry_start:]
    if entry.strip():
        files.append(entry)
    return files


def _approximately_count_tests(
    files: List[Path], repo_root: Path
) -> dict[Path, int]:
    """
    Make a decent estimate at individual tests per file.
    Running ``pytest --co -q`` is WAY too slow because it actually imports everything.

    Returns a mapping ``{file_path: test_count}``. Files with zero
    collected tests are omitted from the dict (not an error — e.g. the
    file only defines fixtures / conftest helpers).

    """

    results = {}

    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            contents = f.read()
        results[path] = contents.count("def test_")

    return results


def _discover_files(roots: List[Path]) -> List[Path]:
    """Return every ``test_*.py`` under the given roots (sorted).

    Roots may be directories (recursed for ``test_*.py``) or explicit
    ``.py`` files (included as-is, even if they don't match the
    ``test_*`` prefix — caller knows what they want).

    Exclude any file whose path contains a component in ``_SKIP_PARTS``,
    UNLESS the user explicitly named it as a root (in which case the
    user's intent overrides the skip filter). This makes
    ``scripts/run_tests.sh tests/docker/`` work locally the same way
    ``pytest tests/docker/`` does — the CI-level skip exists to keep
    the sharded matrix from blowing up, not to block targeted runs.
    """
    seen: set[Path] = set()
    out: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            # Explicit file: include it as-is, skip the _SKIP_PARTS filter
            # since the user named it directly.
            real = root.resolve()
            if real not in seen:
                seen.add(real)
                out.append(root)
            continue
        # If the explicit root itself sits inside a skipped dir (e.g.
        # the user said ``tests/docker``), the user has overridden the
        # skip for that subtree. Compute the set of skip-parts the user
        # opted into, and only filter files whose path crosses a
        # skip-part *outside* that opt-in.
        root_skip_overrides = {
            part for part in root.parts if part in _SKIP_PARTS
        }
        effective_skips = _SKIP_PARTS - root_skip_overrides
        for path in root.rglob("test_*.py"):
            if any(part in effective_skips for part in path.parts):
                continue
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            out.append(path)
    return sorted(out)


def _kill_tree(proc: "subprocess.Popen", pgid: int | None = None) -> None:
    """Kill the pytest subprocess and every descendant it spawned.

    A test run can spin up uvicorn servers, async runtimes, or other
    long-running grandchildren that survive the pytest subprocess exit
    if we don't kill the whole tree. ``subprocess.Popen.kill()`` only
    targets the immediate child; grandchildren reparent to PID 1
    (Linux) / get adopted by services.exe (Windows) and leak.

    POSIX: the caller must pass ``pgid`` — the process group id captured
    immediately after Popen (via ``os.getpgid(proc.pid)``). We can't
    look it up here in the happy path because by the time we get
    called the leader process has already been reaped and its pid is
    gone from the kernel's process table, even though descendants in
    the group are still alive. SIGKILL'ing the captured pgid takes out
    everything in that group atomically.

    Windows: ``taskkill /F /T /PID`` walks the recorded ppid chain and
    terminates the whole tree, even when the root has already exited.

    Why not psutil: psutil walks the parent-child tree, but in the
    happy path the root has already been reaped so ``psutil.Process(pid)``
    can't find it; grandchildren reparented to PID 1 are also
    unreachable by tree walk at that point. The platform-native
    primitives (process groups / taskkill) handle both cases correctly
    without an extra abstraction layer.
    """
    if proc.pid is None:
        return

    if sys.platform == "win32":
        try:
            
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )  # windows-footgun: ok
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    else:
        # POSIX: kill the captured pgid. Local-import signal so the
        # SIGKILL attribute is never referenced on Windows.
        if pgid is not None:
            try:
                import signal as _signal
                os.killpg(pgid, _signal.SIGKILL)  # windows-footgun: ok
            except (ProcessLookupError, PermissionError, OSError):
                pass

    # Belt-and-suspenders: ensure subprocess.communicate() sees the exit.
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _run_one_file(
    file: Path,
    pytest_args: List[str],
    repo_root: Path,
    file_timeout: float,
    retries: int = 0,
) -> Tuple[Path, int, str, dict[str, int], float]:
    """Run ``python -m pytest <file> <pytest_args>`` in a fresh subprocess.

    Returns (file, returncode, captured_combined_output, summary_counts, subprocess_wall_seconds).

    ``retries`` > 0 enables the one-shot flake retry: a non-zero exit is
    re-run in a fresh subprocess; if the re-run passes, the file counts as
    passed but the output is prefixed with a FLAKY banner and the file/output
    are recorded in ``_FLAKY_RESULTS`` so the summary can call it out. A
    deterministic failure fails every attempt, so real regressions cannot
    be laundered green.

    ``summary_counts`` is the result of ``_parse_pytest_summary(output)`` —

    pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):
        0 = all tests passed
        1 = some tests failed
        2 = test execution interrupted
        3 = internal error
        4 = pytest CLI usage error
        5 = no tests collected

    We treat exit 5 as a pass: it just means every test in the file was
    skipped or filtered by a marker (e.g. ``-m 'not integration'`` skips
    files where every test is marked integration). That's intentional and
    not a failure mode.

    On per-file timeout (``file_timeout`` seconds) or any other exception
    during ``communicate()``, we kill the whole process group / process
    tree so grandchildren (uvicorn servers, async runtimes, etc.) do not
    orphan onto PID 1. This outer timeout exists only to
    bound a pathologically slow or hung file as a whole.
    """
    file, rc, output, summary, subproc_wall = _run_one_file_once(
        file, pytest_args, repo_root, file_timeout
    )
    attempt = 0
    while rc != 0 and attempt < retries:
        attempt += 1
        first_output = output
        file, rc, output, summary, subproc_wall2 = _run_one_file_once(
            file, pytest_args, repo_root, file_timeout
        )
        subproc_wall += subproc_wall2
        if rc == 0:
            output = (
                f"⚠ FLAKY: failed on attempt 1, passed on retry "
                f"(attempt {attempt + 1}). Fix the flake — do not ignore this.\n"
                f"--- first-attempt output ---\n{first_output}\n"
                f"--- retry output ---\n{output}"
            )
            with _flaky_lock:
                _FLAKY_RESULTS.append((file, output))
    return file, rc, output, summary, subproc_wall


# Files that failed once and passed on retry, with both attempts' output.
# Keeping the traceback is load-bearing: a self-healed flake without its
# failing assertion is only a filename, which forces another expensive full
# run to rediscover the race.
_FLAKY_RESULTS: List[Tuple[Path, str]] = []
_flaky_lock = threading.Lock()


def _run_one_file_once(
    file: Path,
    pytest_args: List[str],
    repo_root: Path,
    file_timeout: float,
) -> Tuple[Path, int, str, dict[str, int], float]:
    """Single attempt of a per-file pytest subprocess (see _run_one_file)."""
    # Two host-saturation guards, both held for the FULL lifetime of the
    # subprocess rather than just its spawn. A slot released at spawn time
    # would bound the spawn rate but not the number of workers actually
    # resident, which is the quantity that exhausts commit.
    with _acquire_global_slot(_SLOT_CAPACITY, _SLOT_DIR, enabled=_HOST_LIMIT_ENABLED):
        if _HOST_LIMIT_ENABLED:
            _await_commit_headroom(min_free_bytes=_MIN_FREE_COMMIT)
        return _spawn_pytest(file, pytest_args, repo_root, file_timeout)


def _spawn_pytest(
    file: Path,
    pytest_args: List[str],
    repo_root: Path,
    file_timeout: float,
) -> Tuple[Path, int, str, dict[str, int], float]:
    """Run one pytest subprocess to completion (no concurrency guards)."""
    cmd = [sys.executable, "-m", "pytest", str(file), *pytest_args]
    # Give each file its own tmp_path base so concurrent subprocesses can't
    # delete one another's numbered pytest temp roots during retention
    # cleanup (the WinError 3 teardown-error family). Respect an explicit
    # caller-supplied --basetemp if present.
    if not any(
        arg == "--basetemp" or arg.startswith("--basetemp=") for arg in pytest_args
    ):
        basetemp = _basetemp_for(file, repo_root)
        basetemp.parent.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--basetemp={basetemp}")

    subproc_start = time.monotonic()
    # launch the pytest process
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ,
        # POSIX: place the child at the head of its own process group so
        # _kill_tree can SIGKILL the group atomically.
        # Windows: this maps to CREATE_NEW_PROCESS_GROUP in CPython 3.12+;
        # _kill_tree handles the Windows path via taskkill /F /T.
        start_new_session=True,
    )

    # Capture the pgid NOW, before the leader can exit and be reaped. Once
    # the leader is reaped, os.getpgid(proc.pid) raises ProcessLookupError
    # even though grandchildren in that group are still alive — defeating
    # the whole cleanup. None on Windows where the pgid concept doesn't apply.
    pgid: int | None = None
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None

    try:
        output, _ = proc.communicate(timeout=file_timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc, pgid=pgid)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = "(file timeout exceeded; output unavailable)"
        rc = 124  # de facto convention for "killed by timeout".
        output = (
            f"({file_timeout:.0f}s exceeded; "
            f"process tree SIGKILL'd)\n{output}"
        )
    except BaseException:
        # KeyboardInterrupt / runner crash — make sure no zombie
        # grandchildren outlive us.
        _kill_tree(proc, pgid=pgid)
        raise
    else:
        # Happy path: pytest exited on its own. Kill the group anyway in
        # case it left grandchildren behind; already-dead is a no-op.
        _kill_tree(proc, pgid=pgid)

        output +=  "\n"

    if rc == 5:
        # No tests collected — every test in the file was filtered out.
        # Treat as a pass; surface info in a slightly distinct status
        # so the operator can spot it.
        rc = 0
    summary = _parse_pytest_summary(output)
    subproc_wall = time.monotonic() - subproc_start
    return file, rc, output, summary, subproc_wall


def _parse_pytest_summary(output: str) -> dict[str, int]:
    """Extract per-file test pass/fail/skip counts from pytest output.

    pytest prints a summary line like ``12 passed, 3 skipped, 1 failed in 2.1s``
    as the last non-empty line before the short test summary.  We scrape that
    line for the individual counts so the progress display can show test-level
    granularity instead of just file-level pass/fail.

    Returns a dict with keys ``passed``, ``failed``, ``skipped``, ``errors``,
    ``xfailed``, ``xpassed`` (only keys found in the output are present).
    """
    import re

    result: dict[str, int] = {}
    # Walk backwards from the end — the summary line is always near the tail.
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Match "N passed", "N failed", "N skipped", "N errors", "N xfailed", "N xpassed"
        for m in re.finditer(r"(\d+)\s+(passed|failed|skipped|errors|xfailed|xpassed)", line):
            result[m.group(2)] = int(m.group(1))
        # Also match "N error" (singular — pytest uses this sometimes).
        for m in re.finditer(r"(\d+)\s+error\b", line):
            result.setdefault("errors", result.get("errors", 0) + int(m.group(1)))
        if result:
            # Found the counts line — done.
            break
        # Stop at the short test summary header (if any) — everything above
        # that is individual failure details, not the counts line.
        if line.startswith("FAILED") or line.startswith("SHORT TEST SUMMARY"):
            break
    return result


def _format_file(file: Path, repo_root: Path) -> str:
    """Render a test-file path for display: strip the repo-root prefix
    when possible so output reads ``tests/acp/test_auth.py`` instead of
    ``/home/runner/work/hermes-agent/hermes-agent/tests/acp/test_auth.py``.

    Falls back to the absolute path for anything outside the repo root.
    """
    try:
        return str(file.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(file)


def _print_progress(
    tests_done: int,
    approx_total_tests: int,
    file: Path,
    rc: int,
    dur: float,
    repo_root: Path,
    tests_passed: int,
    tests_failed: int,
    test_counts: dict[Path, int],
    file_summary: dict[str, int] | None = None,
    subproc_wall: float | None = None,
) -> None:
    """Single-line live progress.

    When ``file_summary`` is provided (parsed from pytest output), the
    per-file parenthetical shows individual test pass/fail counts instead
    of just the total test count.

    ``subproc_wall`` is the actual subprocess wall-clock time (excluding
    queue-wait). When available, the display shows both the subprocess
    time and the queue-inclusive elapsed time.
    """
    status = "✓" if rc == 0 else "✗"
    pct = min((tests_done / approx_total_tests * 100), 100) if approx_total_tests else 0
    # Digit width for left-side counter padding (derived from total file count).
    fw = len(str(tests_passed + tests_failed))
    # Build per-file test count string.
    if file_summary:
        parts = []
        p = file_summary.get("passed", 0)
        f = file_summary.get("failed", 0)
        s = file_summary.get("skipped", 0)
        e = file_summary.get("errors", 0)
        if p:
            parts.append(f"{p}✓")
        if f:
            parts.append(f"{f}✗")
        if s:
            parts.append(f"{s}s")
        if e:
            parts.append(f"{e}e")
        # xfailed/xpassed are rare; include if present.
        xf = file_summary.get("xfailed", 0)
        xp = file_summary.get("xpassed", 0)
        if xf:
            parts.append(f"{xf}xf")
        if xp:
            parts.append(f"{xp}xp")
        test_str = " ".join(parts) + ", " if parts else ""
    else:
        n_tests = test_counts.get(file, 0)
        test_str = f"{n_tests} tests, " if n_tests else ""
    # Show subprocess time when available; fall back to queue-inclusive dur.
    if subproc_wall is not None:
        time_str = f"{subproc_wall:.1f}s"
    else:
        time_str = f"{dur:.1f}s"
    msg = (
        f"[{pct:5.1f}% | {tests_done:>5}/~{approx_total_tests}"
        f" | ✓{tests_passed:>{fw}} | ✗{tests_failed:>{fw}}] "
        f"{status} {_format_file(file, repo_root)} ({test_str}{time_str})"
    )
    # Truncate to terminal width if available (no clobbering ANSI lines).
    try:
        cols = os.get_terminal_size().columns
        if len(msg) > cols:
            msg = msg[: cols - 1] + "…"
    except OSError:
        pass
    print(msg, flush=True)


def _print_inline_failure(
    file: Path, output: str, repo_root: Path, pytest_passthrough: List[str]
) -> None:
    """Print a compact failure summary immediately when a file fails.

    Shows the tail of the pytest output (the failure section with stack
    traces) and a ready-to-run repro command, so the developer doesn't
    have to wait for the full run to finish before seeing what broke.
    """
    rel = _format_file(file, repo_root)
    # Build a repro command the developer can copy-paste.
    passthrough_str = " ".join(pytest_passthrough) if pytest_passthrough else ""
    repro = f"python -m pytest {rel}"
    if passthrough_str:
        repro += f" {passthrough_str}"

    # Grab just the failure lines (last ~30 lines of pytest output —
    # typically the FAILED summary + short test info).
    lines = output.rstrip().splitlines()
    tail = "\n".join(lines[-30:])

    print(flush=True)
    print(f"  ╔╍ Failed: {rel} ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍", flush=True)
    for line in tail.splitlines():
        print(f"  ║ {line}", flush=True)
    print("  ║", flush=True)
    print(f"  ║  Repro: {repro}", flush=True)
    print("  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍", flush=True)
    print(flush=True)


def _load_durations(repo_root: Path) -> dict[str, float]:
    """Read the duration cache from the repo root.

    Returns a dict mapping relative file paths (e.g.
    ``tests/tools/test_code_execution.py``) to wall-clock seconds from
    the last run. Missing or corrupt file → empty dict (safe fallback).
    """
    path = repo_root / _DURATIONS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print("[ERROR] Failed to load json durations file! {e}")
        return {}


def _save_durations(
    file_times: List[Tuple[Path, float]],
    repo_root: Path,
) -> None:
    """Write the duration cache so future ``--slice`` runs can use it.

    Merges with any existing cache so entries from files not in the
    current run (e.g. from a different slice) are preserved. Keys are
    repo-relative paths so the cache is portable across checkouts
    and CI runners.
    """
    data: dict[str, float] = _load_durations(repo_root)
    for f, t in file_times:
        key = _format_file(f, repo_root)
        data[key] = round(t, 3)
    path = repo_root / _DURATIONS_FILE
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _compute_lpt_slices(
    files: List[Path],
    slice_count: int,
    durations: dict[str, float],
    repo_root: Path,
) -> List[List[Path]]:
    """Distribute files across N slices using LPT (Longest Processing Time first).

    Sorts files by estimated duration descending, then greedily assigns each
    file to the slice with the smallest accumulated time so far. This
    minimizes the makespan (max slice duration) and keeps CI jobs balanced.

    Files with no cached duration get a default estimate of 2.0s (roughly
    the P50 from profiling). This means first-time runs (no cache) still
    get reasonable distribution, and new files don't all land in one slice.

    Returns a list of N file-lists, one per slice (0-indexed).
    """
    if slice_count < 2:
        return [files]

    default_dur = 2.0
    file_durs: List[Tuple[Path, float]] = []
    for f in files:
        rel = _format_file(f, repo_root)
        dur = durations.get(rel, default_dur)
        file_durs.append((f, dur))

    # Sort longest first (LPT).
    file_durs.sort(key=lambda x: x[1], reverse=True)

    # Greedy assignment: for each file, add it to the slice with the
    # smallest current total.
    bucket_files: List[List[Path]] = [[] for _ in range(slice_count)]
    bucket_totals: List[float] = [0.0] * slice_count

    for f, dur in file_durs:
        min_idx = min(range(slice_count), key=lambda i: bucket_totals[i])
        bucket_files[min_idx].append(f)
        bucket_totals[min_idx] += dur

    return bucket_files


def _slice_files(
    files: List[Path],
    slice_index: int,
    slice_count: int,
    durations: dict[str, float],
    repo_root: Path,
) -> List[Path]:
    """Return the subset of *files* belonging to slice *slice_index*.

    Uses :func:`_compute_lpt_slices` for LPT distribution.

    ``slice_index`` is 1-indexed (1..slice_count) for ergonomics —
    ``--slice 1/4`` reads more naturally than ``--slice 0/4``.
    """
    if slice_count < 2:
        return files
    if not (1 <= slice_index <= slice_count):
        print(
            f"error: --slice index must be 1..{slice_count}, got {slice_index}",
            file=sys.stderr,
        )
        sys.exit(2)

    bucket_files = _compute_lpt_slices(files, slice_count, durations, repo_root)

    target = bucket_files[slice_index - 1]
    target_dur = sum(
        durations.get(_format_file(f, repo_root), 2.0) for f in target
    )
    total_dur = sum(
        durations.get(_format_file(f, repo_root), 2.0)
        for bucket in bucket_files
        for f in bucket
    )
    print(
        f"Slice {slice_index}/{slice_count}: {len(target)} files "
        f"(~{target_dur:.0f}s estimated of {total_dur:.0f}s total)",
        flush=True,
    )

    return target


# Flags that belong to THIS script. Every other token starting with "-" is
# routed to the per-file pytest invocation by _split_argv.
#
# -h/--help MUST be here. Without it, argparse never sees --help: the token
# is peeled into pytest passthrough, our_args ends up empty, discovery runs
# unfiltered over ~2384 files, and a full 12-worker suite run starts. That
# happened on 2026-08-16 and had to be killed by PID tree.
_OUR_FLAGS = {
    "-h", "--help",
    "-j", "--jobs", "--paths", "--include-integration",
    "--file-timeout", "--file-retries", "--slice", "--generate-slices", "--files",
    "--no-host-limit", "--host-slots", "--min-free-commit-gb",
}
# pytest short flags that consume the NEXT token as their value.
_PYTEST_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-c", "-r", "-W"}


def _is_our_flag(tok: str) -> bool:
    # Match exact (``-j``, ``--paths``), ``=``-joined (``--paths=x``),
    # and attached short-value (``-j4``) forms of our own options.
    if tok in _OUR_FLAGS:
        return True
    head = tok.split("=", 1)[0]
    if head in _OUR_FLAGS:
        return True
    # Attached short value, e.g. ``-j4`` -> ``-j``.
    if len(tok) > 2 and tok[:2] in _OUR_FLAGS and not tok[1] == "-":
        return True
    return False


def _split_argv(argv: List[str]) -> "tuple[List[str], List[str]]":
    """Split argv into (our args, pytest passthrough).

    Two ways to pass args through to the per-file pytest invocation:
      1. Explicit ``--`` separator: everything after it goes to pytest.
      2. Bare pytest flags anywhere before ``--``: any token starting with
         ``-`` that isn't one of OUR options is routed to pytest, so a bare
         ``-q`` / ``-v`` / ``-x`` / ``--tb=long`` / ``-k expr`` "just works".

    Value-taking pytest flags given in space-separated form (``-k expr``)
    would otherwise leave ``expr`` looking like a positional path and clobber
    discovery, so the following token is peeled along with such flags.
    ``=``-joined forms are self-contained and need no lookahead.

    Extracted from main() so the routing can be asserted without starting a
    suite run.
    """
    if "--" in argv:
        sep = argv.index("--")
        before, explicit_passthrough = argv[:sep], argv[sep + 1:]
    else:
        before, explicit_passthrough = argv, []

    our_args: List[str] = []
    bare_passthrough: List[str] = []
    i = 0
    while i < len(before):
        tok = before[i]
        if tok.startswith("-") and not _is_our_flag(tok):
            bare_passthrough.append(tok)
            # Pull the value token for space-separated value flags.
            if tok in _PYTEST_VALUE_FLAGS and i + 1 < len(before):
                bare_passthrough.append(before[i + 1])
                i += 2
                continue
        else:
            our_args.append(tok)
        i += 1

    # Bare flags run before any explicit ``--`` passthrough so ordering is
    # intuitive (``run_tests.sh tests/foo.py -q -- --tb=long`` -> ``-q --tb=long``).
    return our_args, bare_passthrough + explicit_passthrough


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=int(os.environ.get("HERMES_TEST_WORKERS") or _default_worker_count()),
        help="Parallel worker count (default: $HERMES_TEST_WORKERS or cpu_count)",
    )
    parser.add_argument(
        "--no-host-limit",
        action="store_true",
        help=(
            "Disable the machine-global concurrency slot limiter and the "
            "commit-pressure spawn gate. A CLI flag rather than an env var "
            "because scripts/run_tests.sh execs under 'env -i', which strips "
            "HERMES_* vars before this process starts."
        ),
    )
    parser.add_argument(
        "--host-slots",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Total pytest subprocesses allowed on this HOST across ALL "
            f"concurrent invocations (default: cpu_count = {_global_slot_capacity()})."
        ),
    )
    parser.add_argument(
        "--min-free-commit-gb",
        type=float,
        default=_DEFAULT_MIN_FREE_COMMIT_BYTES / 1024**3,
        metavar="GB",
        help=(
            "Wait for this much free commit charge before spawning a worker "
            "(0 disables). After "
            f"{_DEFAULT_COMMIT_WAIT_SECONDS:.0f}s the runner spawns anyway "
            "with a warning rather than hanging. Default: "
            f"{_DEFAULT_MIN_FREE_COMMIT_BYTES / 1024**3:.0f}GB."
        ),
    )
    parser.add_argument(
        "--paths",
        default=os.environ.get("HERMES_TEST_PATHS", ":".join(_DEFAULT_ROOTS)),
        help="Colon-separated discovery roots (default: 'tests')",
    )
    parser.add_argument(
        "--include-integration",
        action="store_true",
        help="Don't skip integration/ e2e/ during discovery",
    )
    parser.add_argument(
        "--file-timeout",
        type=float,
        default=float(
            os.environ.get("HERMES_TEST_FILE_TIMEOUT", _DEFAULT_FILE_TIMEOUT_SECONDS)
        ),
        help=(
            "Per-file wall-clock cap in seconds. On timeout, the pytest "
            "subprocess and its full process tree are SIGKILL'd. "
            f"Default: {_DEFAULT_FILE_TIMEOUT_SECONDS}s ({round(_DEFAULT_FILE_TIMEOUT_SECONDS/60)} min), env: HERMES_TEST_FILE_TIMEOUT."
        ),
    )
    parser.add_argument(
        "--file-retries",
        type=int,
        default=int(
            os.environ.get("HERMES_TEST_FILE_RETRIES", _DEFAULT_FILE_RETRIES)
        ),
        help=(
            "Re-run a failing test FILE this many times in a fresh subprocess "
            "before declaring it failed. A pass-on-retry counts as passed but "
            "is reported as FLAKY in the summary. 0 disables. "
            f"Default: {_DEFAULT_FILE_RETRIES}, env: HERMES_TEST_FILE_RETRIES."
        ),
    )
    parser.add_argument(
        "--slice",
        metavar="I/N",
        help=(
            "Run only slice I of N (e.g. --slice 1/4). "
            "Files are distributed across slices using cached durations "
            "so each slice takes roughly equal wall time. "
            "Without a duration cache, files are distributed by count. "
            "Env: HERMES_TEST_SLICE (format: I/N)."
        ),
    )
    parser.add_argument(
        "--generate-slices",
        metavar="N",
        type=int,
        help=(
            "Discover test files, distribute them across N slices using "
            "LPT on cached durations, and print a JSON matrix to stdout "
            "then exit (no tests run). The JSON has the shape "
            "'{\"slices\": [{\"index\": 1, \"files\": [\"tests/foo.py\", ...]}, ...]}' "
            "so the CI generate job can feed it directly into a matrix."
        ),
    )
    parser.add_argument(
        "--files",
        metavar="LIST",
        help=(
            "Explicit colon-separated list of test files to run. Bypasses "
            "discovery entirely — used by CI matrix jobs that receive their "
            "file list from the generate job."
        ),
    )
    parser.add_argument(
        "paths_positional",
        nargs="*",
        metavar="PATH",
        help=(
            "Restrict discovery to these paths (directories or .py files). "
            "Mutually exclusive with --paths. Anything after a literal '--' "
            "separator is passed through to each per-file pytest invocation."
        ),
    )
    our_args, pytest_passthrough = _split_argv(sys.argv[1:])
    args = parser.parse_args(our_args)

    # Parse --slice (or HERMES_TEST_SLICE) early so we can exit on bad input
    # before doing any expensive discovery.
    slice_raw = args.slice or os.environ.get("HERMES_TEST_SLICE")
    slice_index: int | None = None
    slice_count: int = 1
    if slice_raw:
        try:
            idx_s, count_s = slice_raw.split("/", 1)
            slice_index = int(idx_s)
            slice_count = int(count_s)
        except (ValueError, AttributeError):
            print(f"error: --slice must be I/N (e.g. 1/4), got: {slice_raw!r}", file=sys.stderr)
            sys.exit(2)

    # Publish limiter config for the worker threads before any spawn.
    global _HOST_LIMIT_ENABLED, _SLOT_CAPACITY, _SLOT_DIR, _MIN_FREE_COMMIT  # noqa: PLW0603 — config knobs
    _HOST_LIMIT_ENABLED = not args.no_host_limit
    _SLOT_CAPACITY = max(1, args.host_slots or _global_slot_capacity())
    _SLOT_DIR = _default_slot_dir()
    _MIN_FREE_COMMIT = int(max(0.0, args.min_free_commit_gb) * 1024**3)

    repo_root = Path(__file__).resolve().parent.parent

    # --files: explicit file list from the CI generate job — skip discovery.
    if args.files:
        files = [repo_root / f for f in _split_file_list(args.files)]
        roots = []
    else:
        # Resolve discovery roots: positional path args override --paths if any
        # were supplied, otherwise --paths (which itself defaults to 'tests').
        if args.paths_positional:
            roots = [repo_root / p for p in args.paths_positional]
        else:
            roots = [repo_root / p for p in args.paths.split(":") if p]

        if args.include_integration:
            # Caller takes responsibility — typically used via explicit -k filter.
            global _SKIP_PARTS  # noqa: PLW0603 — config knob
            _SKIP_PARTS = set()

        files = _discover_files(roots)

    if not files:
        print("No test files to run", file=sys.stderr)
        return 1

    # --generate-slices: compute LPT distribution and emit JSON, then exit.
    if args.generate_slices is not None:
        durations = _load_durations(repo_root)
        slices = _compute_lpt_slices(
            files, args.generate_slices, durations, repo_root
        )
        matrix = {
            "slice": [
                {
                    "index": i + 1,
                    "files": ":".join(_format_file(f, repo_root) for f in bucket),
                }
                for i, bucket in enumerate(slices)
            ]
        }
        # Print to stdout so the CI step can capture it with $().
        print(json.dumps(matrix))
        return 0

    # Count individual tests per file
    test_counts = _approximately_count_tests(files, repo_root)
    approx_total_tests = sum(test_counts.values())

    # Apply slicing if requested — distribute files across CI jobs by
    # estimated duration so no one job gets all the slow files.
    if slice_index is not None:
        durations = _load_durations(repo_root)
        files = _slice_files(files, slice_index, slice_count, durations, repo_root)
        # Recount after slicing.
        test_counts = {f: test_counts[f] for f in files if f in test_counts}
        approx_total_tests = sum(test_counts.values())

    if roots:
        roots_str = [str(r.relative_to(repo_root)) if r.is_relative_to(repo_root) else str(r) for r in roots]
        print(
            f"Discovered {len(files)} test files (~{approx_total_tests} tests) under "
            f"{roots_str}; running with -j {args.jobs}",
            flush=True,
        )
    else:
        print(
            f"Running {len(files)} test files (~{approx_total_tests} tests) "
            f"with -j {args.jobs}",
            flush=True,
        )

    # Capture and print on completion (out-of-order is fine — keeps the
    # terminal clean rather than interleaving N parallel pytest outputs).
    failures: List[Tuple[Path, str, Dict[str, int]]] = []
    file_times: List[Tuple[Path, float]] = []  # (file, subprocess_wall) for distribution
    started = time.monotonic()
    files_done = 0
    tests_done = 0
    pass_count = 0
    fail_count = 0
    tests_passed = 0
    tests_failed = 0
    lock = threading.Lock()

    def _on_done(file: Path, started_at: float, fut: "Future[Tuple[Path, int, str, dict[str, int], float]]") -> None:
        nonlocal files_done, tests_done, pass_count, fail_count, tests_passed, tests_failed
        n_tests = test_counts.get(file, 0)
        try:
            fpath, rc, output, summary, subproc_wall = fut.result()
        except Exception as exc:  # noqa: BLE001 — must always advance counter
            with lock:
                files_done += 1
                tests_done += n_tests
                fail_count += 1
                failures.append((file, f"runner crashed: {exc!r}", {}))
                _print_progress(
                    tests_done, approx_total_tests, file, 1,
                    time.monotonic() - started_at,
                    repo_root, tests_passed, tests_failed,
                    test_counts,
                    subproc_wall=0.0,
                )
            return
        with lock:
            files_done += 1
            tests_done += n_tests
            # Accumulate test-level counts from parsed summary.
            tests_passed += summary.get("passed", 0)
            tests_failed += summary.get("failed", 0)
            file_times.append((fpath, subproc_wall))
            if rc == 0:
                pass_count += 1
            else:
                fail_count += 1
                failures.append((fpath, output, summary))
            _print_progress(
                tests_done, approx_total_tests, fpath, rc,
                time.monotonic() - started_at,
                repo_root, tests_passed, tests_failed,
                test_counts,
                file_summary=summary,
                subproc_wall=subproc_wall,
            )
            if rc != 0:
                _print_inline_failure(fpath, output, repo_root, pytest_passthrough)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures: List[Future] = []
        for file in files:
            t0 = time.monotonic()
            fut = pool.submit(
                _run_one_file, file, pytest_passthrough, repo_root,
                args.file_timeout, args.file_retries,
            )
            fut.add_done_callback(lambda f, file=file, t0=t0: _on_done(file, t0, f))
            futures.append(fut)
        # Block until everything's done. ThreadPoolExecutor.__exit__ waits
        # for all submitted work, but doing it explicitly here makes the
        # control flow obvious.
        for fut in futures:
            fut.result() if fut.exception() is None else None

    # Every worker has exited — reclaim the per-file basetemp tree so nightly
    # runs don't accumulate temp dirs in the shared checkout's OS temp space.
    _cleanup_basetemps()

    elapsed = time.monotonic() - started
    print()
    pct = min(100, (tests_done / approx_total_tests * 100)) if approx_total_tests else 0
    print(f"=== Summary: {len(files)} files, {tests_passed} tests passed, {tests_failed} failed ({pct:.0f}% complete) in {elapsed:.1f}s ({args.jobs} workers) ===")

    # Flaky files: failed once, passed on the automatic retry. Green, but
    # loudly reported so they get fixed instead of silently re-flaking.
    if _FLAKY_RESULTS:
        print()
        print(f"=== ⚠ {len(_FLAKY_RESULTS)} FLAKY file{'s' if len(_FLAKY_RESULTS) != 1 else ''} (failed once, passed on retry — fix these) ===")
        for f, output in _FLAKY_RESULTS:
            print(f"  {_format_file(f, repo_root)}")
            print(output.rstrip())

    # Save durations for future --slice runs. Each slice writes its own
    # partial test_durations.json; a CI merge step joins them later.
    # Locally, _save_durations merges with any existing cache so entries
    # from previous runs aren't lost.
    if file_times:
        _save_durations(file_times, repo_root)
        print(f"  Durations cached to {_DURATIONS_FILE} ({len(file_times)} files)")

    # Per-file time distribution (throwaway diagnostic — shows how
    # subprocess time is distributed so we can see if startup dominates).
    if file_times:
        times = sorted([t for _, t in file_times])
        total_subproc = sum(times)
        median_t = times[len(times) // 2]
        p50 = median_t
        p90 = times[int(len(times) * 0.90)]
        p95 = times[int(len(times) * 0.95)]
        p99 = times[min(int(len(times) * 0.99), len(times) - 1)]
        max_t = times[-1]
        # How many files finish in <1s? That's roughly "just startup".
        fast = sum(1 for t in times if t < 1.0)
        fast_2s = sum(1 for t in times if t < 2.0)
        print()
        print("=== Per-file subprocess time distribution ===")
        print(f"  Files:   {len(times)}")
        print(f"  Total subprocess CPU-wall: {total_subproc:.1f}s  (runner wall: {elapsed:.1f}s, parallelism: {args.jobs}x)")
        print(f"  P50: {p50:.2f}s  P90: {p90:.2f}s  P95: {p95:.2f}s  P99: {p99:.2f}s  Max: {max_t:.2f}s")
        print(f"  <1s: {fast} files ({fast/len(times)*100:.0f}%)  <2s: {fast_2s} files ({fast_2s/len(times)*100:.0f}%)")
        # Top 10 slowest files — likely the ones dragging the run.
        slowest = sorted(file_times, key=lambda x: x[1], reverse=True)[:10]
        print("  Top 10 slowest:")
        for f, t in slowest:
            print(f"    {t:>6.2f}s  {_format_file(f, repo_root)}")

    if failures:
        print()
        print("=== Failure output ===")
        for file, output, _summary in failures:
            print()
            print(f"--- {_format_file(file, repo_root)} ---")
            print(output.rstrip())
        print()
        # Split: files with actual test failures vs non-zero exit for other reasons
        test_fail_files = [(f, s) for f, _o, s in failures if s.get("failed", 0) > 0]
        all_passed_but_nonzero = [(f, s) for f, _o, s in failures
                                  if s.get("failed", 0) == 0 and s.get("passed", 0) > 0]
        no_tests_ran = [(f, s) for f, _o, s in failures
                        if s.get("failed", 0) == 0 and s.get("passed", 0) == 0]
        if test_fail_files:
            total_tf = sum(s.get("failed", 0) for _, s in test_fail_files)
            print(f"=== {len(test_fail_files)} file{'s' if len(test_fail_files) != 1 else ''} with test failures ({total_tf} test{'s' if total_tf != 1 else ''} failed) ===")
            for file, s in test_fail_files:
                nf = s.get("failed", 0)
                print(f"  {_format_file(file, repo_root)}  ({nf} test{'s' if nf != 1 else ''} failed)")
        if all_passed_but_nonzero:
            print(f"=== {len(all_passed_but_nonzero)} file{'s' if len(all_passed_but_nonzero) != 1 else ''} where all tests passed but pytest exited non-zero (warnings-as-errors, hook failures, etc.) ===")
            for file, s in all_passed_but_nonzero:
                print(f"  {_format_file(file, repo_root)}  ({s.get('passed', 0)} passed)")
        if no_tests_ran:
            print(f"=== {len(no_tests_ran)} file{'s' if len(no_tests_ran) != 1 else ''} where no tests ran (collection/import error, timeout before collection, etc.) ===")
            for file, s in no_tests_ran:
                print(f"  {_format_file(file, repo_root)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
