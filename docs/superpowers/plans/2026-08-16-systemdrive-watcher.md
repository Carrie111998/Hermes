# Runner-agnostic `%SystemDrive%` Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the never-landed runner-embedded `%SystemDrive%` probe with a standalone watcher that can attribute a sighting to a writer even after that writer has exited, and fix the `--help` trap that starts a full suite run.

**Architecture:** A standalone `scripts/systemdrive_watcher.py` watches N roots for a literal `%SystemDrive%` child using `ReadDirectoryChangesW` (polling fallback). A background sampler keeps a bounded ring buffer of process *creations* so a sighting can look backward at processes that already exited. `scripts/run_tests_parallel.py` keeps the `HERMES_TEST_JUNK_PROBE` knob, but it now spawns the watcher as a sidecar instead of doing its own inline stat-probing.

**Tech Stack:** Python 3.11, `psutil==7.2.2` (already a hard dep), `ctypes` for Win32, pytest.

**Spec:** `docs/superpowers/specs/2026-08-16-systemdrive-watcher-design.md` (committed as `9aa4ab1550`)

> **Start with "Task 0: Baseline" at the bottom of this document, not Task 1.** It is numbered 0 and placed last because it produces no code, but the baseline it captures is the only valid comparison point for every later task — test outcomes in this repo are location-dependent, so a baseline taken elsewhere is worthless.

## Global Constraints

- **Python 3.11**, stdlib + `psutil==7.2.2` only. No new dependencies.
- **The watcher must never be its own suspect.** Every watcher process runs with `cwd = Path.home()`, never a watched root.
- **Never delete or walk a `%SystemDrive%` tree.** The file sizes and version counters inside it are the evidence. `exists()` only.
- **A diagnostic must never take down the run it observes.** Every ctypes call is checked, every `psutil` call is guarded per-field, every log write is wrapped. Failures degrade and *record the degradation*; they never raise into the runner.
- **JSONL is written at the moment of the sighting**, never buffered to exit.
- **The negative must be printed.** A quiet armed run states what it watched and for how long.
- **`HERMES_TEST_JUNK_PROBE` is opt-in, default off.**
- Log location derives from `Path.home()` (survives `env -i`) and lives **outside** any watched root.
- Verification runs **in this worktree at this commit only** — test outcomes here are location-dependent (22–23 failures inside the shared checkout vs 3 from a Temp-resident worktree). Never A/B across paths.
- **Committing — read this before the first commit step.** Commits go through
  `python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C <repo> -F <msgfile> -- <paths>`,
  run from **PowerShell**, with an absolute `-C`. Never `git commit -- <paths>`, never
  `git commit -a`, never `--no-verify`.

  Each task below shows its commit message inside a ```bash fence. **That fence is the
  message text, not the invocation.** Two reasons you must not run it as written:
  a `git commit` spawned from the Bash tool on this box cannot spawn the pre-commit
  hook, and PowerShell 5.1 mangles multi-line quoted strings passed to `-m`. So for
  every commit step: write the message to a file, then run the wrapper from PowerShell
  with `-F`:

  ```
  Write the message to <scratchpad>/task-N-msg.txt
  python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C <abs worktree path> -F <scratchpad>/task-N-msg.txt -- <paths>
  ```

  An exit-2 refusal (foreign staged files, a mid-operation repo, index divergence) is
  the wrapper working as designed. Report it and stop — never work around one.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/systemdrive_watcher.py` (create) | The whole watcher: records, ring buffer, both detection backends, CLI. Single file because the pieces share one lifecycle and are useless apart. |
| `scripts/run_tests_parallel.py` (modify) | `_split_argv` extraction + `-h/--help` fix; sidecar spawn/reap under the knob. |
| `scripts/run_tests.sh` (modify) | Forward `HERMES_TEST_JUNK_PROBE` through the `env -i` allowlist. |
| `tests/scripts/test_systemdrive_watcher.py` (create) | Watcher unit + integration tests. |
| `tests/test_run_tests_parallel.py` (modify) | `--help` regression + sidecar-off assertion. |

---

## Task 1: Fix the `--help` full-suite trap

Independent of the watcher and highest-risk-reduction, so it lands first: until this is fixed, any careless `--help` during later tasks starts a ~2384-file run.

**Files:**
- Modify: `scripts/run_tests_parallel.py:1254-1301` (extract), `:1117-1120` (parser)
- Test: `tests/test_run_tests_parallel.py`

**Interfaces:**
- Consumes: nothing.
- Produces: module-level `_split_argv(argv: List[str]) -> tuple[List[str], List[str]]` returning `(our_args, pytest_passthrough)`; module-level `_OUR_FLAGS: set[str]`, `_PYTEST_VALUE_FLAGS: set[str]`, `_is_our_flag(tok: str) -> bool`.

> ### ⚠ Do NOT run `python scripts/run_tests_parallel.py --help` before Step 3.
> That is the bug. It discovers ~2384 files and starts a 12-worker run that must be killed by PID tree. The pure-function tests in Step 1 are the safe way to see this fail. The subprocess test is added in Step 6, **after** the fix.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_run_tests_parallel.py`:

```python
from scripts.run_tests_parallel import _split_argv


def test_split_argv_routes_long_help_to_our_args():
    """--help must reach argparse, not pytest passthrough.

    Regression: any token starting with '-' that is not in _OUR_FLAGS is
    routed to pytest. --help was not in the set, so our_args ended up empty,
    discovery went unfiltered, and the runner started the FULL suite.
    """
    our, passthrough = _split_argv(["--help"])
    assert our == ["--help"]
    assert passthrough == []


def test_split_argv_routes_short_help_to_our_args():
    our, passthrough = _split_argv(["-h"])
    assert our == ["-h"]
    assert passthrough == []


def test_split_argv_still_routes_bare_pytest_flags():
    """The fix must not break deliberate bare-pytest-flag routing."""
    our, passthrough = _split_argv(["tests/foo.py", "-q", "-k", "expr"])
    assert our == ["tests/foo.py"]
    assert passthrough == ["-q", "-k", "expr"]


def test_split_argv_honours_explicit_separator():
    our, passthrough = _split_argv(["tests/foo.py", "--", "--tb=long"])
    assert our == ["tests/foo.py"]
    assert passthrough == ["--tb=long"]


def test_split_argv_keeps_our_own_flags():
    our, passthrough = _split_argv(["-j", "4", "--paths=tests/x", "tests/foo.py"])
    assert our == ["-j", "4", "--paths=tests/x", "tests/foo.py"]
    assert passthrough == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_run_tests_parallel.py -k split_argv -v
```

Expected: all 5 FAIL at collection with `ImportError: cannot import name '_split_argv'`.

- [ ] **Step 3: Extract the splitter to module level**

In `scripts/run_tests_parallel.py`, insert **above** `def main()` (module level):

```python
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
```

Then in `main()`, **delete** the old block at `:1254-1301` (from `OUR_FLAGS = {` through `pytest_passthrough = bare_passthrough + explicit_passthrough`) and replace with:

```python
    our_args, pytest_passthrough = _split_argv(sys.argv[1:])
    args = parser.parse_args(our_args)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_run_tests_parallel.py -k split_argv -v
```

Expected: 5 passed.

- [ ] **Step 5: Verify `--help` is now safe by hand**

```bash
python scripts/run_tests_parallel.py --help
```

Expected: argparse usage text, exit 0, **within a second**, no `Discovered N test files` line, no worker spawn. If you see a discovery line, STOP and Ctrl-C immediately — the fix did not take.

- [ ] **Step 6: Add the end-to-end regression test**

Now that the fix is in, this is safe. Append to `tests/test_run_tests_parallel.py`:

```python
def test_help_exits_without_starting_a_suite_run():
    """--help must exit 0 before discovery.

    Guarded by a short timeout on purpose: if this regresses, the failure
    mode is a full ~2384-file run, and a 30s bound keeps that from becoming
    a 12-worker stray run that needs a PID-tree kill.
    """
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_tests_parallel.py"), "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(repo_root),
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()
    assert "Discovered" not in proc.stdout
```

- [ ] **Step 7: Run the full file to check for collateral damage**

```bash
python -m pytest tests/test_run_tests_parallel.py -v
```

Expected: the 6 new tests pass. Pre-existing results in this file must be unchanged from the Task 0 baseline — compare failure **names**, not counts.

- [ ] **Step 8: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "fix(scripts): make --help exit instead of starting a full suite run

Any token starting with '-' that was not in OUR_FLAGS got routed to pytest
passthrough, so argparse never saw --help. our_args ended up empty, discovery
went unfiltered over ~2384 files, and a 12-worker run started that had to be
killed by PID tree.

Adds -h/--help to the set and lifts the splitter to a module-level
_split_argv() so the routing is assertable without starting a suite. Bare
pytest-flag routing (-q, -k expr, --tb=long) is unchanged and now covered.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/run_tests_parallel.py tests/test_run_tests_parallel.py
```

---

## Task 2: Watcher skeleton — records, log path, CLI, negative

**Files:**
- Create: `scripts/systemdrive_watcher.py`
- Test: `tests/scripts/test_systemdrive_watcher.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `JUNK_NAME: str`; `log_path() -> Path`; `write_record(log: Path, event: str, **fields) -> dict`; `build_parser() -> argparse.ArgumentParser`.

- [ ] **Step 1: Write the failing tests**

Create `tests/scripts/test_systemdrive_watcher.py`:

```python
"""Tests for the runner-agnostic %SystemDrive% watcher.

The watcher exists because the never-landed runner-embedded probe
(9a6df34e25) could only see spawns the parallel runner itself made, and the
writer it was built for reproduced from a plain SEQUENTIAL pytest run.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.systemdrive_watcher import JUNK_NAME, build_parser, write_record


def test_junk_name_is_the_literal_template():
    assert JUNK_NAME == "%SystemDrive%"


def test_write_record_appends_jsonl_immediately(tmp_path: Path):
    """The record must be on disk the moment it is written.

    Requirement 4 of the spec: this class of run gets killed, so anything
    buffered to exit is the one copy of the evidence that does not survive.
    """
    log = tmp_path / "w.jsonl"
    write_record(log, "armed", roots=["/x"])
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "armed"
    assert rows[0]["roots"] == ["/x"]
    assert "at" in rows[0] and "watcher_pid" in rows[0]


def test_write_record_appends_rather_than_truncates(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    write_record(log, "armed")
    write_record(log, "done", sightings=0)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["armed", "done"]


def test_write_record_survives_an_unwritable_log(tmp_path: Path, capsys):
    """A diagnostic must never take down the run it observes."""
    log = tmp_path / "nope"
    log.mkdir()  # a directory where a file is expected
    record = write_record(log, "armed")
    assert record["event"] == "armed"  # returned anyway


def test_parser_has_real_help():
    """The watcher must not repeat run_tests_parallel.py's --help trap."""
    parser = build_parser()
    assert parser.format_help()


def test_parser_defaults_are_conservative():
    args = build_parser().parse_args([])
    assert args.ring > 0
    assert args.sample_ms > 0
    assert args.secs > 0
    assert args.stop_file is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'scripts.systemdrive_watcher'`.

- [ ] **Step 3: Create the watcher skeleton**

Create `scripts/systemdrive_watcher.py`:

```python
"""Runner-agnostic watcher for the literal ``%SystemDrive%`` junk tree.

A literal ``%SystemDrive%/ProgramData/Microsoft/Windows/Caches/`` tree
periodically appears in repository roots on this box. ``HKLM\\...\\ProfileList``
holds ``%SystemDrive%\\ProgramData`` as a REG_EXPAND_SZ; a process whose
environment lacks SYSTEMDRIVE cannot expand it, uses the literal string as a
RELATIVE path, and builds the known-folder cache under its own CWD.

One writer was found and fixed on 2026-08-16 (``run_secret_cli``). A later
sighting in a path that fix does not touch suggests a second writer is open.

This watcher is deliberately NOT part of the parallel test runner. The probe
it replaces lived inside ``run_tests_parallel.py`` and could only observe
spawns that runner made -- but the writer reproduced from ONE plain sequential
``python -m pytest`` run, so that probe was structurally unable to see it.

Usage:
    python scripts/systemdrive_watcher.py [ROOT ...]

Runs with ``cwd`` left to the caller; the parallel runner starts it with
``cwd=$HOME`` so the watcher can never be its own suspect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

JUNK_NAME = "%SystemDrive%"

_LOG_REL = Path(".hermes") / "logs" / "systemdrive-watcher.jsonl"

DEFAULT_SECS = 36000.0
DEFAULT_SAMPLE_MS = 250
DEFAULT_POLL_MS = 250
DEFAULT_RING = 4000


def log_path() -> Path:
    """Where sightings are appended.

    Keyed off home for the same reason as the runner's slot dir:
    scripts/run_tests.sh execs under ``env -i`` and HOME/USERPROFILE are among
    the few things that survive it. Writing inside a watched root would also
    mean the watcher littering the directory it is watching.
    """
    try:
        return Path.home() / _LOG_REL
    except (RuntimeError, OSError):
        return Path(tempfile.gettempdir()) / "systemdrive-watcher.jsonl"


def write_record(log: Path, event: str, **fields) -> dict:
    """Append one record as JSONL and shout about it on stderr.

    Written at the moment of the event rather than at exit: this class of run
    gets killed, times out, or loses its terminal often enough that end-of-run
    reporting would be the one copy of the evidence that does not survive.
    """
    record = {
        "event": event,
        "at": datetime.now().isoformat(timespec="seconds"),
        "watcher_pid": os.getpid(),
        **fields,
    }
    line = json.dumps(record, default=str)
    print(f"  [junk-watcher] {line}", file=sys.stderr, flush=True)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # a diagnostic must never take the run down
        print(f"  [junk-watcher] could not write log: {exc}", file=sys.stderr, flush=True)
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="systemdrive_watcher.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "roots", nargs="*", metavar="ROOT",
        help="Directories to watch for a %%SystemDrive%% child (default: cwd)",
    )
    parser.add_argument(
        "--secs", type=float, default=DEFAULT_SECS,
        help="Self-limit. Backstop against an orphaned sidecar (default: %(default)s)",
    )
    parser.add_argument(
        "--sample-ms", type=int, default=DEFAULT_SAMPLE_MS,
        help="Process-creation sampler cadence (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-ms", type=int, default=DEFAULT_POLL_MS,
        help="Polling-backend interval, fallback only (default: %(default)s)",
    )
    parser.add_argument(
        "--ring", type=int, default=DEFAULT_RING,
        help="Process-creation ring buffer capacity (default: %(default)s)",
    )
    parser.add_argument(
        "--stop-file", type=Path, default=None,
        help="Shut down gracefully once this path exists",
    )
    parser.add_argument("--log", type=Path, default=None, help="Override the JSONL log path")
    parser.add_argument(
        "--force-polling", action="store_true",
        help="Skip ReadDirectoryChangesW; use the polling backend everywhere",
    )
    return parser
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "feat(scripts): add the systemdrive watcher skeleton

Records, log path, and CLI for a runner-agnostic %SystemDrive% watcher.
JSONL is appended at the moment of the event, never buffered to exit --
this class of run gets killed, so anything held to exit is the one copy of
the evidence that does not survive. Log path is keyed off home so it
survives run_tests.sh's env -i and never sits inside a watched root.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/systemdrive_watcher.py tests/scripts/test_systemdrive_watcher.py
```

---

## Task 3: Process-creation ring buffer

The piece that makes attribution independent of whether the writer is still alive — the documented failure of the 2026-08-16 prototype.

**Files:**
- Modify: `scripts/systemdrive_watcher.py`
- Test: `tests/scripts/test_systemdrive_watcher.py`

**Interfaces:**
- Consumes: nothing from Task 2 beyond the module.
- Produces: `class ProcessRing` with `__init__(capacity: int = DEFAULT_RING)`, `sample() -> int`, `dump() -> list[dict]`, `__len__() -> int`; module function `describe_pid(pid: int) -> dict`; `cwd_matches(entry: dict, root: Path) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_systemdrive_watcher.py`:

```python
import subprocess
import sys

from scripts.systemdrive_watcher import ProcessRing, cwd_matches, describe_pid


def test_describe_pid_captures_ancestry_fields():
    entry = describe_pid(os.getpid())
    assert entry["pid"] == os.getpid()
    assert entry["name"]
    assert isinstance(entry["ppid"], int)
    assert isinstance(entry["cmdline"], list)


def test_describe_pid_of_a_dead_process_is_kept_with_an_error():
    """A PID we saw appear and could not read is still evidence.

    Dropping it would hide the very short-lived processes this ring buffer
    exists to catch.
    """
    entry = describe_pid(999999)
    assert entry["pid"] == 999999
    assert "error" in entry


def test_describe_pid_partial_failure_keeps_the_other_fields(monkeypatch):
    """cwd is AccessDenied for many Windows processes.

    Fields are captured individually so losing cwd does not also lose the
    cmdline, which is the actual identifying field. A single try/except
    around the whole block would drop both -- so this test INDUCES the cwd
    failure rather than hoping for one.
    """
    import psutil

    def boom(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "cwd", boom)
    entry = describe_pid(os.getpid())
    assert entry["cmdline"], "cmdline must survive a cwd failure"
    assert entry["errors"]["cwd"] == "AccessDenied"


def test_first_sample_is_a_baseline_not_a_flood():
    """Priming must not record the whole process table as 'creations'.

    ~1000 processes are live on this box; recording them all would bury the
    handful that actually started inside the watch window.
    """
    ring = ProcessRing(capacity=10000)
    assert ring.sample() == 0
    assert len(ring) == 0


def test_sample_records_a_newly_spawned_process():
    ring = ProcessRing(capacity=10000)
    ring.sample()  # prime
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        ring.sample()
        pids = [e["pid"] for e in ring.dump()]
        assert child.pid in pids
    finally:
        child.kill()
        child.wait()


def test_ring_is_bounded(tmp_path):
    ring = ProcessRing(capacity=3)
    ring._primed = True
    ring._entries.extend({"pid": n} for n in range(100))
    assert len(ring) == 3
    assert [e["pid"] for e in ring.dump()] == [97, 98, 99]


def test_cwd_matches_is_true_for_the_watched_root(tmp_path: Path):
    assert cwd_matches({"cwd": str(tmp_path)}, tmp_path.resolve())


def test_cwd_matches_is_false_without_a_cwd(tmp_path: Path):
    """Best-effort: a process that exited before enrichment has no cwd."""
    assert not cwd_matches({"pid": 1, "error": "NoSuchProcess"}, tmp_path.resolve())
```

Add `import os` to the test file's imports if not already present.

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -k "ring or describe or cwd_matches or sample" -v
```

Expected: collection error, `ImportError: cannot import name 'ProcessRing'`.

- [ ] **Step 3: Implement the ring buffer**

Add to `scripts/systemdrive_watcher.py` (imports first: `collections`, `threading`, and `psutil`):

```python
import collections
import threading
from typing import Deque, List

import psutil


def describe_pid(pid: int) -> dict:
    """One process, captured as completely as permissions allow.

    Fields are read INDIVIDUALLY on purpose. ``cwd`` raises AccessDenied for
    many Windows processes, and a single try/except around the whole block
    would then also lose ``cmdline`` -- which is the field that actually names
    a writer.
    """
    entry: dict = {"pid": pid, "seen_at": datetime.now().isoformat(timespec="milliseconds")}
    try:
        proc = psutil.Process(pid)
    except Exception as exc:
        # Vanished between pids() and here. Keep it: a PID we saw appear and
        # could not read is still evidence that SOMETHING started.
        entry["error"] = type(exc).__name__
        return entry
    for field, getter in (
        ("name", proc.name),
        ("ppid", proc.ppid),
        ("create_time", proc.create_time),
        ("cmdline", proc.cmdline),
        ("cwd", proc.cwd),
    ):
        try:
            entry[field] = getter()
        except Exception as exc:
            entry.setdefault("errors", {})[field] = type(exc).__name__
    return entry


def cwd_matches(entry: dict, root: Path) -> bool:
    """Does this process hold the watched root as its working directory?

    The established mechanism REQUIRES this: the junk lands under the writer's
    CWD. This is what narrows a ~1000-process table to a shortlist.

    Best-effort by nature -- an entry whose cwd could not be read (already
    exited, or AccessDenied) simply does not match.
    """
    cwd = entry.get("cwd")
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == root
    except OSError:
        return False


class ProcessRing:
    """Bounded history of process CREATIONS.

    Sampling ``psutil.pids()`` is one cheap syscall; only the NEW pids get
    enriched. Cost therefore scales with process CHURN, not with the ~1000
    live processes, which is what makes a short cadence affordable.

    This is the piece that attacks the prototype's documented failure: on
    2026-08-16 the watcher fired correctly but the writer had already exited,
    so a full snapshot named nobody. A creation history makes attribution
    independent of whether the writer is still alive at sighting time.
    """

    def __init__(self, capacity: int = DEFAULT_RING) -> None:
        self._entries: Deque[dict] = collections.deque(maxlen=capacity)
        self._known: set = set()
        self._primed = False
        self._lock = threading.Lock()

    def sample(self) -> int:
        """One diff pass. Returns how many creations were recorded."""
        live = set(psutil.pids())
        new = live - self._known
        self._known = live
        if not self._primed:
            # The first pass is a baseline. Everything looks new, but nothing
            # actually started inside our window; recording ~1000 entries here
            # would bury the handful that matter.
            self._primed = True
            return 0
        entries = [describe_pid(pid) for pid in sorted(new)]
        with self._lock:
            self._entries.extend(entries)
        return len(entries)

    def dump(self) -> List[dict]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "feat(scripts): add a process-creation ring buffer to the watcher

This is the piece that attacks the prototype's documented failure: on
2026-08-16 the watcher fired correctly but the writer had already exited, so
a 971-process snapshot named nobody. A bounded history of process CREATIONS
makes attribution independent of whether the writer is still alive.

Only NEW pids are enriched, so cost scales with process churn rather than
with the ~1000 live processes. Fields are read individually because cwd is
AccessDenied for many Windows processes and a single guard would also lose
the cmdline. A pid that vanished before enrichment is KEPT with an error
marker -- it is still evidence that something started.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/systemdrive_watcher.py tests/scripts/test_systemdrive_watcher.py
```

---

## Task 4: Sighting handler + polling backend (first working detection)

**Files:**
- Modify: `scripts/systemdrive_watcher.py`
- Test: `tests/scripts/test_systemdrive_watcher.py`

**Interfaces:**
- Consumes: `ProcessRing`, `describe_pid`, `cwd_matches`, `write_record`, `JUNK_NAME`.
- Produces: `class Watcher` with `__init__(roots, log=None, ring_capacity=DEFAULT_RING, sample_ms=..., poll_ms=..., secs=..., stop_file=None, force_polling=False)`, attributes `sightings: int`, `roots: list[Path]`, methods `on_hit(root: Path, backend: str) -> None`, `record_preexisting() -> None`, `run() -> int`, `stop() -> None`; module function `watch_polling(root, on_hit, stop, interval_s) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_systemdrive_watcher.py`:

```python
import threading
import time

from scripts.systemdrive_watcher import Watcher, watch_polling


def _rows(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_on_hit_writes_the_sighting_before_returning(tmp_path: Path):
    """Requirement 4: JSONL at the MOMENT of the sighting, not at exit."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log)
    watcher.on_hit(root.resolve(), "test")
    assert log.exists()
    sightings = [r for r in _rows(log) if r["event"] == "SIGHTING"]
    assert len(sightings) == 1
    rec = sightings[0]
    assert rec["backend"] == "test"
    assert "watcher_has_systemdrive" in rec
    assert "live_cwd_matches" in rec
    assert "ring_cwd_matches" in rec
    assert rec["live_process_count"] > 0


def test_on_hit_reports_only_the_first_transition(tmp_path: Path):
    """Later ticks would re-report the same tree and bury the original."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log)
    watcher.on_hit(root.resolve(), "test")
    watcher.on_hit(root.resolve(), "test")
    assert len([r for r in _rows(log) if r["event"] == "SIGHTING"]) == 1
    assert watcher.sightings == 1


def test_preexisting_tree_is_recorded_and_blames_nobody(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    (root / JUNK_NAME).mkdir(parents=True)
    watcher = Watcher([root], log=log)
    watcher.record_preexisting()
    rows = [r for r in _rows(log) if r["event"] == "preexisting"]
    assert len(rows) == 1
    assert "cannot attribute" in rows[0]["note"]
    # A pre-existing tree must not later be reported as a fresh sighting.
    watcher.on_hit(root.resolve(), "test")
    assert not [r for r in _rows(log) if r["event"] == "SIGHTING"]


def test_watch_polling_detects_a_created_directory(tmp_path: Path):
    hits = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_polling,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, 0.02),
        daemon=True,
    )
    thread.start()
    time.sleep(0.05)
    (tmp_path / JUNK_NAME).mkdir()
    thread.join(timeout=5)
    assert hits, "polling backend never fired"
    assert hits[0][1] == "polling"


def test_watch_polling_stops_on_the_stop_event(tmp_path: Path):
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_polling,
        args=(tmp_path.resolve(), lambda r, b: None, stop, 0.02),
        daemon=True,
    )
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_run_emits_the_negative_when_nothing_appears(tmp_path: Path):
    """A quiet armed run must be evidence, not silence."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, poll_ms=20, sample_ms=20,
                      force_polling=True)
    assert watcher.run() == 0
    rows = _rows(log)
    assert rows[0]["event"] == "armed"
    assert "backend_by_root" in rows[0]
    done = [r for r in rows if r["event"] == "done"][0]
    assert done["sightings"] == 0
    assert "NEGATIVE" in done["note"]
    assert done["watched_secs"] >= 0


def test_run_stops_early_when_the_stop_file_appears(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    stop_file = tmp_path / "stop"
    watcher = Watcher([root], log=log, secs=30, poll_ms=20, sample_ms=20,
                      stop_file=stop_file, force_polling=True)
    threading.Timer(0.2, stop_file.touch).start()
    started = time.monotonic()
    watcher.run()
    assert time.monotonic() - started < 10, "stop-file did not shut the watcher down"
    assert [r for r in _rows(log) if r["event"] == "done"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -k "on_hit or preexisting or polling or run_emits or stop_file" -v
```

Expected: collection error, `ImportError: cannot import name 'Watcher'`.

- [ ] **Step 3: Implement `Watcher` and the polling backend**

Add to `scripts/systemdrive_watcher.py` (add `import time` to imports):

```python
def watch_polling(root: Path, on_hit, stop: threading.Event, interval_s: float) -> None:
    """Fallback backend: one ``exists()`` per tick.

    Used on non-Windows, when the root cannot be opened for a directory
    watch, or under --force-polling. Deliberately does NOT walk or stat the
    tree -- the sizes and version counters inside it are the evidence.
    """
    target = root / JUNK_NAME
    while not stop.is_set():
        try:
            if target.exists():
                on_hit(root, "polling")
                return
        except OSError:
            pass
        stop.wait(interval_s)


class Watcher:
    """Watches N roots for a literal %SystemDrive% child."""

    def __init__(
        self,
        roots,
        log: "Path | None" = None,
        ring_capacity: int = DEFAULT_RING,
        sample_ms: int = DEFAULT_SAMPLE_MS,
        poll_ms: int = DEFAULT_POLL_MS,
        secs: float = DEFAULT_SECS,
        stop_file: "Path | None" = None,
        force_polling: bool = False,
    ) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.log = log or log_path()
        self.secs = secs
        self.stop_file = stop_file
        self.force_polling = force_polling
        self._sample_s = sample_ms / 1000.0
        self._poll_s = poll_ms / 1000.0
        self._ring = ProcessRing(ring_capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hit_roots: set = set()
        self.sightings = 0

    def record_preexisting(self) -> None:
        """A tree already present at arm time blames nobody.

        It is also latched, so it can never be re-reported as a fresh
        transition later in this watch.
        """
        for root in self.roots:
            target = root / JUNK_NAME
            try:
                present = target.exists()
            except OSError:
                continue
            if present:
                with self._lock:
                    self._hit_roots.add(str(root))
                write_record(
                    self.log, "preexisting", root=str(root), path=str(target),
                    note="present before the watch started - cannot attribute; "
                         "delete it and re-run to make this root usable",
                )

    def on_hit(self, root: Path, backend: str) -> None:
        """Record the first absent->present transition for ``root``.

        Ordering is the whole point: the ring buffer is dumped FIRST because
        it is already in memory, then the live table is enumerated. Ancestry
        is the perishable part; the directory is not.
        """
        key = str(root)
        with self._lock:
            if key in self._hit_roots:
                return
            self._hit_roots.add(key)
            self.sightings += 1

        ring = self._ring.dump()                      # in memory, instant
        live = [describe_pid(pid) for pid in psutil.pids()]

        snapshot: "Path | None" = self.log.with_name(
            f"systemdrive-sighting-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        )
        try:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(
                json.dumps({"ring": ring, "live": live}, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            snapshot = None

        write_record(
            self.log, "SIGHTING",
            root=key,
            path=str(root / JUNK_NAME),
            backend=backend,
            # The falsifier for the whole mechanism story. A sighting recorded
            # while SYSTEMDRIVE IS present kills the missing-SYSTEMDRIVE
            # explanation and restarts the hunt.
            watcher_has_systemdrive="SYSTEMDRIVE" in os.environ,
            live_cwd_matches=[e for e in live if cwd_matches(e, root)],
            ring_cwd_matches=[e for e in ring if cwd_matches(e, root)],
            live_process_count=len(live),
            ring_size=len(ring),
            snapshot_file=str(snapshot) if snapshot else None,
        )

    def stop(self) -> None:
        self._stop.set()

    def _sampler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ring.sample()
            except Exception:  # sampling must never kill the watch
                pass
            if self.stop_file is not None:
                try:
                    if self.stop_file.exists():
                        self._stop.set()
                        return
                except OSError:
                    pass
            self._stop.wait(self._sample_s)

    def _start_backends(self) -> dict:
        """Start one detection thread per root; return backend by root."""
        chosen = {}
        for root in self.roots:
            target = watch_polling
            name = "polling"
            args = (root, self.on_hit, self._stop, self._poll_s)
            chosen[str(root)] = name
            threading.Thread(target=target, args=args, daemon=True).start()
        return chosen

    def run(self) -> int:
        """Watch until the deadline, the stop-file, or a stop() call."""
        self.record_preexisting()
        threading.Thread(target=self._sampler_loop, daemon=True).start()
        backends = self._start_backends()
        write_record(
            self.log, "armed",
            roots=[str(r) for r in self.roots],
            backend_by_root=backends,
            sample_ms=int(self._sample_s * 1000),
            poll_ms=int(self._poll_s * 1000),
            ring_capacity=self._ring._entries.maxlen,
            watcher_has_systemdrive="SYSTEMDRIVE" in os.environ,
            watcher_cwd=os.getcwd(),
        )
        started = time.monotonic()
        deadline = started + self.secs
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(0.2)
        self._stop.set()
        watched = round(time.monotonic() - started, 1)
        write_record(
            self.log, "done",
            sightings=self.sightings,
            roots=[str(r) for r in self.roots],
            watched_secs=watched,
            note=(
                f"NEGATIVE - watched {len(self.roots)} root(s) for {watched}s, "
                "no %SystemDrive% appeared"
                if not self.sightings else "see SIGHTING record(s)"
            ),
        )
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -v
```

Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "feat(scripts): add the sighting handler and polling backend

on_hit dumps the ring buffer FIRST -- it is already in memory -- and only
then enumerates the live process table, because ancestry is the perishable
part and the directory is not. Both sets are partitioned by whether the
process holds the watched root as its cwd, which the established mechanism
requires of the writer; that turns a ~1000-process table into a shortlist.

Only the first absent->present transition per root is reported, since later
ticks would re-report the same tree and bury it. A pre-existing tree is
recorded, latched, and blames nobody. The negative is written as a 'done'
record naming the roots and the seconds watched, so a quiet armed run is
evidence rather than silence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/systemdrive_watcher.py tests/scripts/test_systemdrive_watcher.py
```

---

## Task 5: `ReadDirectoryChangesW` backend

**Files:**
- Modify: `scripts/systemdrive_watcher.py`
- Test: `tests/scripts/test_systemdrive_watcher.py`

**Interfaces:**
- Consumes: `JUNK_NAME`, `Watcher._start_backends`.
- Produces: `parse_notifications(buf: bytes, nbytes: int) -> Iterator[tuple[int, str]]`; `watch_readdirchanges(root, on_hit, stop, handles) -> None`; constants `FILE_ACTION_ADDED = 1`, `FILE_ACTION_RENAMED_NEW_NAME = 5`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_systemdrive_watcher.py`:

```python
import struct

import pytest

from scripts.systemdrive_watcher import (
    FILE_ACTION_ADDED,
    parse_notifications,
    watch_readdirchanges,
)


def _notification(action: int, name: str, next_offset: int = 0) -> bytes:
    encoded = name.encode("utf-16-le")
    return struct.pack("<III", next_offset, action, len(encoded)) + encoded


def test_parse_notifications_reads_a_single_entry():
    buf = _notification(FILE_ACTION_ADDED, JUNK_NAME)
    assert list(parse_notifications(buf, len(buf))) == [(FILE_ACTION_ADDED, JUNK_NAME)]


def test_parse_notifications_walks_the_chain():
    """FileNameLength is in BYTES, not characters - the classic mistake."""
    alpha_len = len(_notification(FILE_ACTION_ADDED, "alpha"))
    first = _notification(FILE_ACTION_ADDED, "alpha", next_offset=alpha_len)
    second = _notification(FILE_ACTION_ADDED, JUNK_NAME)
    buf = first + second
    assert list(parse_notifications(buf, len(buf))) == [
        (FILE_ACTION_ADDED, "alpha"),
        (FILE_ACTION_ADDED, JUNK_NAME),
    ]


def test_parse_notifications_stops_at_the_declared_length():
    buf = _notification(FILE_ACTION_ADDED, JUNK_NAME) + b"\x00" * 64
    entries = list(parse_notifications(buf, len(_notification(FILE_ACTION_ADDED, JUNK_NAME))))
    assert entries == [(FILE_ACTION_ADDED, JUNK_NAME)]


@pytest.mark.skipif(sys.platform != "win32", reason="ReadDirectoryChangesW is Win32-only")
def test_readdirchanges_detects_a_created_directory(tmp_path: Path):
    """End-to-end proof the fast backend actually fires."""
    hits = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_readdirchanges,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, []),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)  # let the watch arm
    (tmp_path / JUNK_NAME).mkdir()
    thread.join(timeout=10)
    assert hits, "ReadDirectoryChangesW backend never fired"
    assert hits[0][1] == "readdirectorychanges"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-only")
def test_readdirchanges_ignores_unrelated_directories(tmp_path: Path):
    hits = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_readdirchanges,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, []),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    (tmp_path / "unrelated").mkdir()
    time.sleep(0.5)
    stop.set()
    assert not hits


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-only")
def test_watcher_records_the_backend_it_actually_got(tmp_path: Path):
    """A run must never claim a fast watch it did not get."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, sample_ms=50)
    watcher.run()
    armed = [r for r in _rows(log) if r["event"] == "armed"][0]
    assert armed["backend_by_root"][str(root.resolve())] == "readdirectorychanges"


def test_force_polling_is_reflected_in_the_armed_record(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, sample_ms=50, force_polling=True)
    watcher.run()
    armed = [r for r in _rows(log) if r["event"] == "armed"][0]
    assert armed["backend_by_root"][str(root.resolve())] == "polling"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -k "notifications or readdirchanges or backend_it_actually or force_polling" -v
```

Expected: collection error, `ImportError: cannot import name 'parse_notifications'`.

- [ ] **Step 3: Implement the Win32 backend**

Add to `scripts/systemdrive_watcher.py` (add `import ctypes` and `import struct`):

```python
# --- Win32 directory-change watching -------------------------------------
#
# Non-recursive on purpose: we want ONE specific child of the root, and a
# subtree watch over a repo checkout would deliver enormous volume during a
# test run.
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a DIRECTORY handle
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_ACTION_ADDED = 1
FILE_ACTION_RENAMED_NEW_NAME = 5
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def parse_notifications(buf: bytes, nbytes: int):
    """Walk a FILE_NOTIFY_INFORMATION chain.

    Layout: NextEntryOffset (DWORD), Action (DWORD), FileNameLength (DWORD,
    in BYTES not characters), FileName (WCHAR[]). Treating FileNameLength as
    a character count is the classic way to get this wrong.
    """
    offset = 0
    while offset + 12 <= nbytes:
        next_off, action, name_len = struct.unpack_from("<III", buf, offset)
        start = offset + 12
        name = buf[start:start + name_len].decode("utf-16-le", "replace")
        yield action, name
        if next_off == 0:
            break
        offset += next_off


def _open_directory_handle(root: Path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        ctypes.c_wchar_p(str(root)),
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {root}")
    return kernel32, handle


def watch_readdirchanges(root: Path, on_hit, stop: threading.Event, handles: list) -> None:
    """Block on ReadDirectoryChangesW until %SystemDrive% is created.

    Detection latency drops from the poll interval (up to 1.5s in the
    2026-08-16 prototype) to roughly the kernel's notification latency.

    ``handles`` collects the open handle so the owner can CancelIoEx it at
    shutdown. The thread is a daemon, so a still-blocked call can never hold
    up interpreter exit either way.
    """
    kernel32, handle = _open_directory_handle(root)
    handles.append((kernel32, handle))
    buf = ctypes.create_string_buffer(64 * 1024)
    returned = ctypes.c_ulong(0)
    try:
        while not stop.is_set():
            ok = kernel32.ReadDirectoryChangesW(
                ctypes.c_void_p(handle), buf, ctypes.sizeof(buf), False,
                FILE_NOTIFY_CHANGE_DIR_NAME, ctypes.byref(returned), None, None,
            )
            if not ok:
                return  # cancelled or handle closed
            for action, name in parse_notifications(buf.raw, returned.value):
                if name == JUNK_NAME and action in (
                    FILE_ACTION_ADDED, FILE_ACTION_RENAMED_NEW_NAME
                ):
                    on_hit(root, "readdirectorychanges")
                    return
    finally:
        try:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass
```

Replace `Watcher._start_backends` with the selecting version, and add handle bookkeeping to `__init__` (`self._handles: list = []`):

```python
    def _start_backends(self) -> dict:
        """Start one detection thread per root; return the backend actually used.

        A root that cannot be opened for a directory watch is DOWNGRADED to
        polling and the downgrade is recorded -- a run must never be able to
        claim a fast watch it did not get.
        """
        chosen = {}
        for root in self.roots:
            use_fast = sys.platform == "win32" and not self.force_polling
            if use_fast:
                try:
                    # Probe openability, then close immediately -- the watch
                    # thread opens its own handle.
                    probe_k32, probe_handle = _open_directory_handle(root)
                    probe_k32.CloseHandle(ctypes.c_void_p(probe_handle))
                except OSError as exc:
                    write_record(
                        self.log, "backend_downgrade", root=str(root),
                        reason=repr(exc),
                        note="could not open a directory handle; falling back to polling",
                    )
                    use_fast = False
            if use_fast:
                chosen[str(root)] = "readdirectorychanges"
                threading.Thread(
                    target=watch_readdirchanges,
                    args=(root, self.on_hit, self._stop, self._handles),
                    daemon=True,
                ).start()
            else:
                chosen[str(root)] = "polling"
                threading.Thread(
                    target=watch_polling,
                    args=(root, self.on_hit, self._stop, self._poll_s),
                    daemon=True,
                ).start()
        return chosen
```

And in `run()`, after `self._stop.set()` and before the `done` record, unblock any waiting call:

```python
        for kernel32, handle in self._handles:
            try:
                kernel32.CancelIoEx(ctypes.c_void_p(handle), None)
            except Exception:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py -v
```

Expected: 28 passed on Windows.

- [ ] **Step 5: Add the CLI entrypoint**

Append to `scripts/systemdrive_watcher.py`:

```python
def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    roots = [Path(r) for r in args.roots] or [Path.cwd()]
    watcher = Watcher(
        roots,
        log=args.log,
        ring_capacity=args.ring,
        sample_ms=args.sample_ms,
        poll_ms=args.poll_ms,
        secs=args.secs,
        stop_file=args.stop_file,
        force_polling=args.force_polling,
    )
    try:
        return watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Smoke-test the CLI**

```bash
python scripts/systemdrive_watcher.py --help
```

Expected: usage text, exit 0.

- [ ] **Step 7: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "feat(scripts): watch via ReadDirectoryChangesW with a polling fallback

Detection latency drops from the prototype's 1.5s poll to roughly the
kernel's notification latency. Non-recursive on purpose: we want one
specific child of the root, and a subtree watch over a repo checkout would
deliver enormous volume during a test run.

A root that cannot be opened for a directory watch is downgraded to polling
and the downgrade is RECORDED, alongside the backend actually used per root
in the armed record -- a run must never be able to claim a fast watch it did
not get. FileNameLength is in bytes, not characters; the parser is unit
tested against a hand-built chain to lock that down.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/systemdrive_watcher.py tests/scripts/test_systemdrive_watcher.py
```

---

## Task 6: Runner sidecar + `run_tests.sh` forwarding

**Files:**
- Modify: `scripts/run_tests_parallel.py` (imports, module level, `main()` after `repo_root = ...`), `scripts/run_tests.sh:91`, `:122-123`, `scripts/run_tests_parallel.py:40-45` (docstring)
- Test: `tests/test_run_tests_parallel.py`

**Interfaces:**
- Consumes: `scripts/systemdrive_watcher.py` CLI (`ROOT`, `--secs`, `--stop-file`).
- Produces: `_start_junk_watcher(repo_root: Path) -> None`, `_stop_junk_watcher() -> None`, `_JUNK_PROBE_ENV = "HERMES_TEST_JUNK_PROBE"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_run_tests_parallel.py`:

```python
from scripts import run_tests_parallel as rtp


def test_watcher_not_started_when_the_knob_is_unset(monkeypatch, tmp_path):
    """Opt-in, default off."""
    monkeypatch.delenv("HERMES_TEST_JUNK_PROBE", raising=False)
    monkeypatch.setattr(rtp, "_watcher_proc", None)
    rtp._start_junk_watcher(tmp_path)
    assert rtp._watcher_proc is None


def test_run_tests_sh_forwards_the_junk_probe_gate():
    """Without forwarding, the knob is SILENTLY INERT through the wrapper.

    run_tests.sh execs the runner under `env -i` with an explicit allowlist,
    so an unforwarded variable simply never arrives -- and the run then looks
    exactly like a clean negative.
    """
    repo_root = Path(__file__).resolve().parent.parent
    body = (repo_root / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    assert "HERMES_TEST_JUNK_PROBE" in body
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_run_tests_parallel.py -k "watcher_not_started or forwards_the_junk" -v
```

Expected: both FAIL — `AttributeError: module 'scripts.run_tests_parallel' has no attribute '_start_junk_watcher'` and an assertion failure on the shell script.

- [ ] **Step 3: Add the sidecar to the runner**

Add `import atexit` and `import tempfile` to `scripts/run_tests_parallel.py` if absent, then insert at module level above `main()`:

```python
# --- %SystemDrive% junk watcher (opt-in diagnostic) ----------------------
#
# The watcher is a SEPARATE process on purpose. Its predecessor lived inside
# this file and could only observe spawns this runner made -- but on
# 2026-08-16 the writer reproduced from ONE plain sequential pytest run, so
# an in-runner probe was structurally unable to see it.
#
# The sidecar inherits this process's environment verbatim, which is what
# makes `watcher_has_systemdrive` report OUR condition for free.
_JUNK_PROBE_ENV = "HERMES_TEST_JUNK_PROBE"
# Backstop only: the stop-file is the normal shutdown path. This bounds an
# orphaned watcher if the runner is killed outright.
_WATCHER_ORPHAN_BUDGET_SECS = 6 * 3600

_watcher_proc: "subprocess.Popen | None" = None
_watcher_stop_file: "Path | None" = None


def _start_junk_watcher(repo_root: Path) -> None:
    """Spawn the watcher for this run if the gate is set. Default off."""
    global _watcher_proc, _watcher_stop_file  # noqa: PLW0603 — invocation-scoped diagnostic state
    if not os.environ.get(_JUNK_PROBE_ENV):
        return
    script = repo_root / "scripts" / "systemdrive_watcher.py"
    if not script.exists():
        print(f"  [junk-probe] watcher missing at {script}; run is UNWATCHED", file=sys.stderr)
        return
    stop_file = Path(tempfile.gettempdir()) / f"systemdrive-watcher-stop-{os.getpid()}"
    try:
        stop_file.unlink()
    except OSError:
        pass
    try:
        _watcher_proc = subprocess.Popen(
            [
                sys.executable, str(script), str(repo_root),
                "--secs", str(_WATCHER_ORPHAN_BUDGET_SECS),
                "--stop-file", str(stop_file),
            ],
            cwd=str(Path.home()),  # NEVER the watched root
            env=os.environ,        # inherit: watcher_has_systemdrive == ours
        )
    except OSError as exc:
        # Say so loudly. An unwatched run must not be mistaken for a clean
        # negative -- that is the exact failure this whole design exists to
        # avoid.
        print(f"  [junk-probe] could not start watcher: {exc}; run is UNWATCHED", file=sys.stderr)
        return
    _watcher_stop_file = stop_file
    atexit.register(_stop_junk_watcher)
    print(f"  [junk-probe] watcher armed on {repo_root} (pid {_watcher_proc.pid})")


def _stop_junk_watcher() -> None:
    """Ask the watcher to stop through its NORMAL path.

    Registered via atexit because main() has several return points. Touching
    the stop-file lets the watcher emit its `done` record -- i.e. the
    NEGATIVE -- instead of being killed before it can report.
    """
    proc, stop_file = _watcher_proc, _watcher_stop_file
    if proc is None or proc.poll() is not None:
        return
    try:
        if stop_file is not None:
            stop_file.touch()
        proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.terminate()
        except OSError:
            pass
    finally:
        try:
            if stop_file is not None:
                stop_file.unlink()
        except OSError:
            pass
```

In `main()`, immediately after `repo_root = Path(__file__).resolve().parent.parent`:

```python
    # Arm before any spawn, so the watch covers the whole run.
    _start_junk_watcher(repo_root)
```

Update the docstring `Environment:` block (`:40-45`):

```
    HERMES_TEST_JUNK_PROBE  Spawn scripts/systemdrive_watcher.py alongside the
                            run to watch the repo root for a literal
                            %SystemDrive% tree (default: off)
```

- [ ] **Step 4: Forward the gate through `run_tests.sh`**

At `scripts/run_tests.sh:122-123`, change:

```sh
for _var in HERMES_TEST_WORKERS HERMES_TEST_PATHS HERMES_TEST_FILE_TIMEOUT \
            HERMES_TEST_FILE_RETRIES HERMES_TEST_SLICE HERMES_TEST_JUNK_PROBE; do
```

And insert above `CLEAN_ENV=(` at `:94`:

```sh
# NOTE (Windows): this allowlist has no SYSTEMDRIVE, and run_tests_parallel.py
# spawns every worker with `env=os.environ` and `cwd=repo_root`. That is
# exactly the condition that makes a process expand `%SystemDrive%\ProgramData`
# (a REG_EXPAND_SZ template) as a RELATIVE path and build the known-folder
# cache in the checkout root. One such writer was found and fixed on
# 2026-08-16 (run_secret_cli); a later sighting suggests a second is open.
# Do NOT "fix" this by adding SYSTEMDRIVE here until the watcher has caught
# one: that would erase the only reproducer we have. Arm it instead with
# HERMES_TEST_JUNK_PROBE=1 (forwarded below).
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_run_tests_parallel.py -v
```

Expected: 8 passed, pre-existing results unchanged from baseline.

- [ ] **Step 6: Prove the sidecar actually spawns and reports**

```bash
HERMES_TEST_JUNK_PROBE=1 python scripts/run_tests_parallel.py tests/scripts/test_contributor_map.py
```

Expected: a `[junk-probe] watcher armed on ...` line near the start, an `armed` record on stderr, and a `done` record with `"NEGATIVE - watched 1 root(s) for Ns"` at the end. If no `armed` banner appears, the gate is not reaching the runner — do not trust any later negative.

- [ ] **Step 7: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "feat(scripts): spawn the junk watcher as a sidecar under the existing gate

HERMES_TEST_JUNK_PROBE keeps its meaning -- opt-in, default off -- but now
starts scripts/systemdrive_watcher.py as a separate process instead of doing
its own inline stat-probing. The predecessor could only observe spawns this
runner made, and the writer reproduced from a plain sequential pytest run.

The sidecar inherits the runner's environment, so watcher_has_systemdrive
reports the runner's condition with no plumbing to drift out of sync, and
runs with cwd=\$HOME so it can never be its own suspect. Shutdown goes
through a stop-file so the watcher emits its 'done' record -- the NEGATIVE --
rather than being killed before it can report; atexit covers main()'s several
return points and --secs bounds an orphan if the runner is killed outright.

run_tests.sh forwards the gate through its env -i allowlist. Without that the
knob is silently inert through the canonical wrapper, which reads exactly
like a clean negative.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- scripts/run_tests_parallel.py scripts/run_tests.sh tests/test_run_tests_parallel.py
```

---

## Task 7: End-to-end proof and cadence cost measurement

The prototype-level proof the old probe never obtained, plus the measurement the spec deliberately left open.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-16-systemdrive-watcher-design.md` (record the measured cost)

**Interfaces:**
- Consumes: everything above. Produces: nothing consumed by later tasks.

- [ ] **Step 1: Prove a real sighting is detected and attributed**

In one terminal, watch a sentinel directory:

```bash
mkdir -p /tmp/junk-sentinel && python scripts/systemdrive_watcher.py /tmp/junk-sentinel --secs 120
```

In a second terminal, create the tree:

```bash
mkdir "/tmp/junk-sentinel/%SystemDrive%"
```

Expected in the watcher output: a `SIGHTING` record within a second, with `backend: "readdirectorychanges"`, a non-zero `live_process_count`, a `ring_size` reflecting sampled creations, and a `snapshot_file` path that exists. Confirm the snapshot file contains both `ring` and `live` keys.

- [ ] **Step 2: Prove attribution survives a writer that exits immediately**

This is the documented failure mode. From the second terminal:

```bash
python -c "import os, pathlib; d = pathlib.Path('/tmp/junk-sentinel2'); d.mkdir(exist_ok=True); os.chdir(d); os.mkdir('%SystemDrive%')"
```

with the watcher armed on `/tmp/junk-sentinel2`. The writer exits within milliseconds.

Expected: the `SIGHTING` record's `ring_cwd_matches` **or** the ring dump in the snapshot file contains the short-lived `python -c` process with its cmdline. Record the outcome honestly — if the ring missed it at the default cadence, that is a real result and belongs in the spec's Open Tradeoff section, not hidden.

- [ ] **Step 3: Measure the sampler's actual cost**

```bash
python -c "
import time
from scripts.systemdrive_watcher import ProcessRing
r = ProcessRing(); r.sample()
t = time.perf_counter()
for _ in range(20): r.sample()
print('per-sample ms:', round((time.perf_counter()-t)/20*1000, 2))
"
```

Record the number. If a sample costs more than roughly half the cadence, raise the default `--sample-ms` rather than shipping a watcher that saturates a core on a box with documented memory-pressure problems.

- [ ] **Step 4: Full verification sweep, same worktree, same commit**

```bash
python -m pytest tests/scripts/test_systemdrive_watcher.py tests/test_run_tests_parallel.py -v
```

Expected: all pass. Compare against the Task 0 baseline by failure **name**, not count.

- [ ] **Step 5: Record the measurement in the spec**

Replace the spec's "Open tradeoff" section with the measured result: the per-sample cost, the chosen default cadence, and whether Step 2's short-lived writer was captured.

- [ ] **Step 6: Commit**

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C . -m "docs(specs): record the measured sampler cost and the end-to-end result

Closes the spec's deliberately-open cadence tradeoff with a measurement
rather than a guess, and records whether the ring buffer actually captured a
writer that exited immediately -- the documented failure mode of the
2026-08-16 prototype.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" -- docs/superpowers/specs/2026-08-16-systemdrive-watcher-design.md
```

---

## Task 0 (do this FIRST): Baseline

Before Task 1, capture the baseline **in this worktree at this commit**. Test outcomes here are location-dependent, so this is the only valid comparison point.

```bash
python -m pytest tests/test_run_tests_parallel.py -v 2>&1 | tail -30
```

Record passed/failed counts **and the names of any failures**. `tests/scripts/test_systemdrive_watcher.py` does not exist yet, so it has no baseline.

---

## Notes for the implementer

- **Never run `python scripts/run_tests_parallel.py --help` before Task 1 Step 3.** That is the bug being fixed; it starts a ~2384-file run.
- **Do not add `SYSTEMDRIVE` to the `run_tests.sh` allowlist.** It looks like an obvious fix and would erase the only reproducer.
- **Do not delete any `%SystemDrive%` tree you find.** The byte sizes and version counters inside are the evidence that distinguishes a fresh mis-expanded cache build from a stray copy.
- A `preexisting` record means that root is spent for this run. Delete the tree (after recording it elsewhere) and re-arm.
- The watcher's own process appears in its own snapshots. It runs with `cwd=$HOME`, so it will never show up in `*_cwd_matches` for a repo root — that is the design, not a bug.
