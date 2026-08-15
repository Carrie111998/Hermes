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

* **~39 files — Windows path/separator handling.** The largest single theme,
  and the same family as the `shlex.split` POSIX-mode bug fixed in `ee2475a98b`
  (backslashes eaten, so a Windows path silently becomes a different string).
  This is where real production bugs are most likely to be hiding, not just
  test-portability noise.
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

1. **Freeze a baseline.** Re-run the command above and commit the failing-file
   list. Without it there is no way to tell a fix from a reshuffle, and no way
   to detect new breakage in this set.
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
