# The ungated test backlog

**Measured 2026-08-14** against `035802cd67`, using the project's own harness:

```bash
python scripts/run_tests_parallel.py --paths tests -- -q -p no:cacheprovider
```

Result: **2376 files, 49,480 tests passed, 397 failed** (100% complete, 3271.9s,
12 workers). The 397 land in **135 files**.

## This is a backlog, not a regression

The nightly gate does **not** run this set. `nightly_gate.py::_pytest_argv`
scopes to `tests/events:tests/cron:tests/gateway` — 612 files, 12,597 tests —
which is green as of `035802cd67`. Everything below sits in the other ~1760
files, which no cron watches, so these have almost certainly been failing for a
long time and nobody was paged.

Do not read "397" as something that broke recently. Nothing here was measured
before today, so there is no baseline to regress from — establishing one is the
first item of work, not the last.

## Where they are

| Failing files | Area |
|---:|---|
| 42 | `tests/hermes_cli` |
| 38 | `tests/tools` |
| 6 | `tests/cli` |
| 5 | `tests/run_agent` |
| 4 | `tests/session_bridge` |
| 3 | `tests/skills` |
| 2 each | `tests/acp`, `tests/hermes_state`, `tests/tui_gateway` |
| 1 each | `tests/acp_adapter`, `tests/agent`, `tests/honcho_plugin`, + 13 loose `tests/test_*.py` |

## What they are

Terminal exception per failing file:

| Files | Failure |
|---:|---|
| 56 | `AssertionError` |
| 22 | no exception line (hang / killed) |
| 14 | bare `assert` |
| 13 | `OSError` |
| 10 | `AttributeError` |
| 18 | `RuntimeError` / `ValueError` / `FileNotFoundError` / `ModuleNotFoundError` / `PermissionError` / `KeyError` / `IndexError` / `TypeError` / `NameError` |

Cross-cut by signature, the themes that actually matter:

* **The bulk — POSIX-only tests running on Windows.** Corrected 2026-08-14
  after sampling the real failures rather than pattern-matching the log. A
  first pass guessed "~39 files, Windows path/separator handling, most likely
  to hide real production bugs". Re-running `tests/hermes_cli` and reading the
  actual assertions says otherwise — the dominant failures are POSIX
  assumptions the platform cannot satisfy:

      ModuleNotFoundError: No module named 'pwd'
      assert mode & stat.S_IRUSR        # Unix permission bits
      assert not (33206 & 32)           # 0o100666
      ✗ error: [WinError 193] %1 is not a valid Win32 application   # .sh hook

  In every one of those the production code is behaving correctly and the test
  cannot express Windows semantics. `test_hooks_cli.py` is the clearest case:
  it installs `.sh` hooks, and Windows rightly refuses to execute them.
  The fix for these is a platform skip, not a code change — and the suite
  already has that convention (26 files use a `skipif` on `os.name`/
  `sys.platform`, 10 of them inside `tests/hermes_cli` itself).

  Method note: two different regex signatures over the same log produced "39
  files" and then "81 files". Neither number was trustworthy. Cluster counts
  mined out of a log are a hypothesis; run the tests and read the assertions
  before believing one.

* **One confirmed production bug, found this way — see `atomic_replace`.**
  The cluster was still worth mining: `utils.py::atomic_replace` handled only
  `EXDEV`/`EBUSY` and propagated Windows' `EACCES` (WinError 5), so
  `atomic_json_write` was **not atomic under concurrency on Windows** — 3 of 10
  concurrent writers lost their write. Exposed callers are ordinary runtime
  paths (`hermes_cli/main.py`, `hermes_cli/web_server.py`,
  `events/cluster_detector.py`, `gateway/channel_directory.py`,
  `gateway/drain_control.py`). Fixed with a bounded, Windows-only retry.
  Ratio so far: one real defect per ~40 files inspected.
* **13 files — live network calls.** Tests reaching real sockets;
  `tests/agent/test_verification_stop_caching.py` hangs in `sock.connect`
  through `detect_local_server_type` → `httpx` when no local LLM server is up.
  Verified pre-existing: the pre-`02708c66ac` version of that file hangs
  identically, so it is not fallout from the `sys.modules` restore work.
* **3 files — Unix-only modules** (`_curses`, `pwd`) imported without a
  platform skip.
* **2 files — the live-system guard firing correctly**, blocking a
  `subprocess.Popen` the test should have stubbed. These are the guard doing
  its job, not guard bugs.

## Before fixing any of it

1. **Freeze a baseline.** Done — `ungated-test-baseline-20260814.tsv`
   (135 files / 397 tests at `035802cd67`). Regenerate and diff to tell a fix
   from a reshuffle.

   **Read per-file counts as approximate.** They are harness- and
   order-dependent, because the harness omits `-p no:randomly`.
   `tests/test_atomic_replace_symlinks.py` is recorded as 1 there and reports 5
   under a direct run — with and without any local change, verified by
   reverting. Treat the *file list* as the signal and the counts as a hint.
2. **Decide whether to gate it.** These stay invisible until something watches
   them. Widening `_pytest_argv` is the obvious move but not a free one — read
   its docstring first: the gate already lives inside a tuned budget, and the
   monolithic-run history there is a cautionary tale (33 consecutive RED nights
   reporting nothing but a timeout).
3. **Start with the Windows-path cluster.** Highest density, clearest theme,
   and the one most likely to contain real defects rather than stale
   assertions — tonight's `shlex` case silently disabled a tamper check in
   production, and it looked exactly like these do.

## Method note

Two false starts worth not repeating. A serial `python -m pytest tests/` does
**not** complete — it hangs, which is what made this set look unmeasurable. The
per-file harness completes it in 55 minutes because it gives each file a fresh
interpreter and bounds parallelism. Use the harness.

And the harness's own repro line omits `-p no:randomly`, so file-level ordering
is randomized; a file that passes under a fixed order can hang under a random
one. Reproduce with the exact `Repro:` line the harness prints, not a tidied
version of it.
