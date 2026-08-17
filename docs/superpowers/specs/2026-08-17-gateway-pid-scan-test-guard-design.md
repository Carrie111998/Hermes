# Gateway PID-scan test guard: a working implementation

**Date:** 2026-08-17
**Baseline:** `3bc7442b9b` (worktree `objective-gates-1b04a5`, branch `claude/practical-pascal-63397b`)
**Status:** implemented and verified 2026-08-17
**Supersedes:** `backup/pre-drop-pidguard-20260817` (`e865ab09db` + `ca17d36a45`), dropped 2026-08-17

## Problem

`hermes_cli.gateway._scan_gateway_pids` is the one layer of the gateway PID lookup that
reads the **host** process table. Measured 2026-08-15: it returned the developer's LIVE
gateway PID `[47164]` in ~2-3 s per call *standalone*. Inside a loaded suite it is far
worse — **10–16 s per sweep**, derived from the per-file sweep timings measured
2026-08-17 (see Test plan): 34.03 s / 3 sweeps, 19.91 s / 2, 15.62 s / 1.

Two independent problems, both real:

1. **Cost.** `pyproject.toml` `addopts` pins a 30 s per-test timeout, and that cap covers
   fixture setup. At 10–16 s a sweep, a single test that reaches the scan twice
   (`gateway_windows.stop()` does) can consume the entire budget. This is not a
   projection: an unguarded baseline run of `test_cron.py` **was killed by pytest-timeout
   inside `subprocess.communicate` under the real sweep** while measuring this change, and
   never reached `sessionfinish`.

   (The dropped commit put the two-call case at "~26 s of its 30 s budget". That specific
   test was not re-measured here, so the figure is inherited, not confirmed — though
   2 × the measured per-sweep range brackets it.)
2. **Determinism.** The sweep finds whatever gateway happens to be running on the
   developer's box, so an unstubbed test's result depends on machine state. `HERMES_HOME`
   is already tempdir-redirected by `_hermetic_environment` and `_get_service_pids` is
   inert off Linux, so this sweep is the **only** remaining host leak in the PID path.

The original instrumentation found five offending tests across three files, none of which
mention `find_gateway_pids` anywhere — they reach it transitively, so grep could not have
found them.

## Why this needs a fresh design

Two implementations were cherry-picked and dropped on 2026-08-17. Both are ruled out on
evidence, not taste. Do not re-derive them.

| Candidate | Why it fails |
|---|---|
| `import hermes_cli.gateway` inside the autouse fixture (`e865ab09db`) | Pulls `agent.model_metadata` into every trivial test's setup. `tests/test_conftest_import_cost.py::test_autouse_reset_fixture_does_not_import_heavy_modules` fails immediately. |
| `sys.modules.get("hermes_cli.gateway")` (`ca17d36a45`) | Cheap and import-free, but leaves the guard **unarmed** whenever the module is not yet imported at fixture-setup time — the majority case. |

Two measurements taken on this box on 2026-08-17 pin both failures:

- `import hermes_cli.gateway` costs **3.6 s cold and pulls 463 modules.** That is the
  whole of candidate 1's defect.
- Test files importing `hermes_cli.gateway`: **15 at module scope, 17 function-body-only.**
  The runner gives each file its own process, so under candidate 2 the first
  gateway-touching test in each of those 17 files runs unguarded.
  (The task brief said 22 rather than 17. The count here matches only the three literal
  import spellings of `hermes_cli.gateway` itself; the brief's larger figure presumably
  also counts indirect reachers. Nothing in this design turns on which number is right —
  either way the unarmed set is large and includes known offenders such as
  `tests/hermes_cli/test_status.py`, which reaches the scanner through `show_status`.)

A third fact kills a tempting near-miss: `test_conftest_import_cost.py` snapshots
`sys.modules` at **`pytest_sessionfinish`**, not at fixture setup. So "import it once at
conftest load instead of per-test" also fails. **No design may import
`hermes_cli.gateway` anywhere in a test process.**

The self-test is what catches candidate 2:
`tests/test_live_system_guard_self_test.py::test_gateway_pid_scan_is_stubbed_by_default`
imports the module **inside the test body** and asserts the scanner is stubbed. A real
test in the repo has the same shape and the opposite requirement —
`tests/test_windows_subprocess_no_window_flags.py::test_gateway_pid_scan_hides_wmic_and_powershell_windows`
does `from hermes_cli import gateway` inside its body **and** must get the real scanner.
Any design must serve both.

## Approach considered and rejected: the subprocess seam

The pre-design recommendation was to intercept at the subprocess layer, where
`tests/conftest.py` already wraps `subprocess.run` for the live-system guard. Rejected for
two reasons:

1. **Mechanism.** `_check_subprocess_cmd` *raises* `RuntimeError`, and
   `_scan_gateway_pids` catches only `OSError` / `subprocess.TimeoutExpired`
   (`hermes_cli/gateway.py:588`). A raise would propagate and break the test rather than
   yield `[]`. The seam would have to **substitute** a
   `CompletedProcess(returncode=0, stdout="")`, not block. Fixable, but it means the
   guard's behaviour is expressed as a fake transport rather than as an intent.
2. **Coverage.** The POSIX arm walks `/proc` directly via `os.listdir`
   (`hermes_cli/gateway.py:528-549`) with no subprocess at all. Guarding it would require
   patching `os.path.isdir("/proc")` — a global behaviour change in a conftest that
   affects 2385 test files, capable of breaking any unrelated test that legitimately
   probes `/proc`.

It also cannot satisfy the self-test's contract, since nothing at the function level is
stubbed.

## Chosen approach

**Patch at import time via `sys.meta_path`; decide behaviour at call time via a flag.**

That split is the entire design. Patching at import time arms the guard regardless of
*when* the module is first imported, which is what candidate 2 could not do. Deciding at
call time is what lets a test import the module fresh **inside a marked test body** and
still get the real scanner — an install-time decision cannot, because at install time the
guard would have to commit to one behaviour for the rest of the process.

### Precedent

This is not a new mechanism for this codebase. `cli.py:852-910` already ships the same
pattern in **production** on this branch: a `sys.meta_path` finder inserted at index 0,
whose `find_spec` opens with a `fullname` check returning `None`, wrapping
`spec.loader.exec_module` to patch `openai._base_client` after execution — installed for
exactly the same reason, to avoid an eager import's cost (~166 ms / ~30 MB there).

One deliberate divergence: `cli.py` disarms and removes itself after firing, because it
patches a class once. Ours stays armed so `importlib.reload` re-applies the guard. That is
also why ours delegates by iterating `sys.meta_path` (skipping self) rather than calling
`importlib.util.find_spec` — the iteration is re-entrancy-free by construction and needs
no disarm dance.

### Components

Three pieces in `tests/conftest.py`, placed with the existing live-system-guard block —
two for the arming mechanism, one for the per-test switch.

**1. `_install_pid_scan_guard(module)`** — replaces `module._scan_gateway_pids` with a
wrapper closing over the real function:

```python
def _guarded(*a, **k):
    return real(*a, **k) if _PID_SCAN_ALLOW_REAL[0] else []
```

- Idempotent: returns early if `_hermes_pid_scan_guard` is already set, so `reload` or a
  double registration is harmless.
- Carries `_hermes_pid_scan_guard = True` and `_hermes_pid_scan_real = <real fn>`.
- Keeps `__name__ = "_guarded_scan_gateway_pids"`. Deliberately **not** `functools.wraps`,
  which would disguise the wrapper as the real function in tracebacks.

**2. `_PidScanGuardFinder.find_spec`** — first statement is
`if fullname != "hermes_cli.gateway": return None`. On a match, it walks `sys.meta_path`
skipping itself, takes the first resolved spec with a loader, and patches
`spec.loader.exec_module` on the loader **instance** (the `cli.py` shape — no proxy class
needed; `FileFinder` builds a fresh `SourceFileLoader` per spec, so the attribute is local
to this import).

Installed from `pytest_configure`: patch immediately if the module is already in
`sys.modules`, then `sys.meta_path.insert(0, ...)`.

**3. The autouse fixture** does nothing but flip the flag:

```python
@pytest.fixture(autouse=True)
def _gateway_pid_scan_guard(request):
    _PID_SCAN_ALLOW_REAL[0] = (
        request.node.get_closest_marker(_REAL_GATEWAY_PID_SCAN_MARK) is not None
    )
    try:
        yield
    finally:
        _PID_SCAN_ALLOW_REAL[0] = False
```

No import, no `sys.modules` lookup, no monkeypatch. That is why it cannot regress
`test_conftest_import_cost.py`.

### Failure posture

Both `_install_pid_scan_guard` and the matched branch of `find_spec` swallow exceptions and
fall back to the unguarded normal import. A conftest bug must never break imports for 2385
test files.

The cost is that a silent failure is possible. That is exactly what the self-test canary
exists to catch, so the failure mode is one loud red test rather than a guard that quietly
stopped working.

### Marker contract

`@pytest.mark.real_gateway_pid_scan` restores real scanner **behaviour** for the marked
test. The wrapper object itself stays installed permanently; the marker flips what it
delegates to. Consequence: the dropped work's identity assertions
(`__name__ == "<lambda>"` / `== "_scan_gateway_pids"`) cannot survive as written, and are
replaced — see Test plan.

Registered in **both** `tests/conftest.py::pytest_configure` (via `addinivalue_line`) and
`pyproject.toml`'s `markers` list.

## Reversing the 2026-05-10 stance

`tests/conftest.py:870` currently documents the opposite position:

> We intentionally do NOT stub `find_gateway_pids` / `_scan_gateway_pids` here... Discovery
> without delivery is harmless.

That comment is replaced. The claim it makes stays **true for signal safety** — the
`os.kill` and `systemctl` guards do stop a scanned PID from ever being signalled — so the
replacement preserves it and scopes the reversal to the two things it does not cover:
cost and determinism. The measurements above and the date of Diego's approval
(2026-08-15) go in the comment, so the next reader sees why the stance flipped rather than
merely that it did.

Stubbing `_scan_gateway_pids` rather than `find_gateway_pids` remains deliberate:
`find_gateway_pids`'s composition logic (PID-file merge, service PIDs, exclude/ancestor
handling, restart-manager gating) stays under real coverage and simply sees an empty
contribution from the process table — the correct hermetic default.

## Files touched

| File | Change |
|---|---|
| `tests/conftest.py` | Finder + install + autouse fixture + marker registration + comment reversal |
| `pyproject.toml` | `real_gateway_pid_scan` in `markers` |
| `tests/hermes_cli/test_gateway.py` | 2 markers (verbatim from `e865ab09db`) |
| `tests/hermes_cli/test_gateway_proc_fallback.py` | file-level `pytestmark` (verbatim) |
| `tests/test_windows_subprocess_no_window_flags.py` | 1 marker (verbatim) |
| `tests/test_live_system_guard_self_test.py` | 3 canaries, rewritten (below) |

## Test plan

### Canaries in `tests/test_live_system_guard_self_test.py`

1. `test_gateway_pid_scan_is_stubbed_by_default` — imports the module **inside the body**,
   asserts `_hermes_pid_scan_guard` is set and that calling it returns `[]` in under 0.5 s.
2. `test_gateway_pid_scan_stub_leaves_find_gateway_pids_real` — `find_gateway_pids.__name__`
   is unchanged.
3. `test_real_gateway_pid_scan_marker_restores_the_scanner` — marked; forces
   `is_windows`, stubs `shutil.which` and `subprocess.run` with canned `Get-CimInstance`
   output, and asserts the scan parses `[98765]` out of it.

Canary 3 is deliberately stronger than the dropped version, which asserted function
identity and never called anything — vacuous if the wrapper's delegation broke. Stubbing
the transport proves delegation to the real implementation reached the parser, without a
host sweep.

### Acceptance criteria

- `tests/test_conftest_import_cost.py` green against the real conftest.
- `tests/test_live_system_guard_self_test.py` green, including all three new canaries.
- The four marked scanner tests green.
- Per-file before/after on the known offenders: pass/fail/skip counts identical to
  baseline, and zero real host sweeps afterwards.

**Result (measured 2026-08-17, after implementation).** Both arms run at
`--timeout=300`, because at the pinned 30 s cap the *unguarded* baseline dies: a
`test_cron.py` BEFORE run was killed by pytest-timeout inside `subprocess.communicate`
under the real sweep, never reaching `sessionfinish`. That is the cost defect reproducing
directly, but it also means a comparable baseline needs the raised cap.

Sweep counts come from a probe plugin that wraps `subprocess.run` and matches the
`Win32_Process` / `wmic` argv, so it measures identically on both arms and is independent
of the guard's mechanism.

| File | BEFORE | AFTER | Sweeps eliminated |
|---|---|---|---|
| `tests/hermes_cli/test_cron.py` | 16 passed, 102.08 s | 16 passed, 52.31 s | 3 / 34.03 s |
| `tests/hermes_cli/test_gateway_windows.py` | 51 passed, 47.22 s | 51 passed, 22.06 s | 2 / 19.91 s |
| `tests/hermes_cli/test_status.py` | 17 passed, 56.55 s | 17 passed, 33.63 s | 1 / 15.62 s |

Counts identical on every file; zero sweeps after, on every file. `test_status.py`
confirms it reaches the scanner transitively through `show_status`.

**The dropped commit's figures (`test_cron.py` 126.4 s -> 65.4 s,
`test_gateway_windows.py` 94.3 s -> 58.2 s) did NOT reproduce and are not carried
forward.** The direction and rough magnitude hold, the numbers do not — unsurprising on a
differently-loaded box. The table above supersedes them.

An earlier wall-clock harness for this measurement was discarded as unusable: its elapsed
time disagreed with pytest's own reported duration by ~59 s on one run, and it keyed log
and probe filenames on label+round without the file name, so the two files collided. The
figures above come from pytest's own summary line and a per-file probe path.

## Evidence

All measured 2026-08-17 on this host, repo venv, in an isolated probe outside the repo tree
(so only the probe conftest applied).

| Claim | Result |
|---|---|
| Guard arms on a lazy in-body import | 4/4 probe tests pass |
| Marker works when the module is first imported *inside* the marked test | passes in its own process |
| `find_gateway_pids` stays real | asserted, passes |
| Finder overhead | 646 ns/call x 310 calls on the heaviest import chain = **0.2 ms/process** |
| Cost of candidate 1 | `import hermes_cli.gateway` = 3.6 s, 463 modules |
| Scale of candidate 2's gap | 15 module-scope vs 17 function-body-only importers |

The probe was run in both the loader-proxy shape and the `cli.py` instance-patch shape;
both pass, and the simpler instance-patch shape is the one specified above.

The 0.2 ms figure retires the "high blast radius" objection on the cost axis. The residual
risk is correctness — a buggy `find_spec` would break every import — mitigated by the
first statement being a string compare that returns `None`, and by the swallow-and-fall-
through posture above.

## Addendum, same day: the dashboard scanner joins the same guard

`hermes_cli.main._find_stale_dashboard_pids` (`main.py:6315`) is a second host
process-table scanner — stale `hermes dashboard` / `hermes serve` servers, killed at the
end of `hermes update`. It is now guarded by the same mechanism, with the finder, the
installer and the fixture generalised to a list of `_PidScanSpec(module, attr, marker)`
rather than duplicated. Adding a third scanner is one entry in `_PID_SCAN_SPECS` plus a
marker registration.

**It is the sharper hazard of the two, and four things about it differ from the gateway
case. Do not inherit the gateway's analysis.**

1. **No subprocess to intercept.** Commit `7b4b3a40ad` replaced the `wmic` branch with an
   in-process `psutil.process_iter(["pid","cmdline"])` walk (wmic is gone from Windows 11,
   so every scan was raising `FileNotFoundError` and returning `[]`). The
   `subprocess.run`-argv probe used for the gateway sweep above is therefore structurally
   blind to this one and reports zero. Instrumentation has to wrap `psutil.process_iter`
   and attribute by caller frame.
2. **Discovery here IS delivery.** `_kill_stale_dashboard_processes` (`main.py:6568`) feeds
   every returned PID straight into `subprocess.run(["taskkill", "/PID", pid, "/F"])`. The
   "discovery without delivery is harmless" stance never applied to this scanner at all.
3. **`import hermes_cli.main` does not trip `test_conftest_import_cost.py`'s stated
   assertion** — 2.1 s / 252 modules, but zero of its `FORBIDDEN_PREFIXES`. The no-import
   rule still holds, via a different mechanism: that test runs its child pytest with
   `-p import_probe`, and main's import-time `_apply_profile_override()` reads `-p` as
   `--profile` and `sys.exit(1)`s, so the child aborts and `assert proc.returncode == 0`
   fails. (Same reason a throwaway probe plugin must be loaded with `PYTEST_PLUGINS` +
   `PYTHONPATH`, never `-p`.)
4. **The `sys.modules.get` gap is far bigger**: 176 test files import `hermes_cli`/
   `hermes_cli.main` only inside function bodies versus 136 at module scope (15/17 for
   gateway).

**Stub value.** A plain `[]`, not a `_DashboardPids`. `main._scan_ok()` reads the flag with
`getattr(pids, "scan_ok", True)`, so a plain list means "looked, found nothing" — which is
what the many existing tests patching this function with `return_value=[]` already assume.
A failed-scan stub would make the reaper print its "could not scan the process table"
warning on every update test instead of returning silently.

**Markers are per-scanner** (`real_gateway_pid_scan`, `real_dashboard_pid_scan`) rather than
one shared marker, so opting into the dashboard walk does not silently re-enable the gateway
sweep. Verified by probe: across all 18 tests of a marked file the flags read
`_scan_gateway_pids=False, _find_stale_dashboard_pids=True`.

### Files touched (addendum)

| File | Change |
|---|---|
| `tests/conftest.py` | `_PidScanSpec` + spec list; installer/finder/fixture generalised; fixture renamed `_gateway_pid_scan_guard` -> `_pid_scan_guard`; new marker; two guard error messages rewritten |
| `pyproject.toml` | `real_dashboard_pid_scan` in `markers` |
| `tests/hermes_cli/test_dashboard_process_scan.py` | file-level `pytestmark` |
| `tests/hermes_cli/test_update_stale_dashboard.py` | file-level `pytestmark` |
| `tests/test_windows_subprocess_no_window_flags.py` | 1 marker on `test_stale_dashboard_windows_scan_spawns_nothing` |
| `tests/test_live_system_guard_self_test.py` | 3 canaries |

The last two files were **not** in the task brief, which named only
`test_dashboard_process_scan.py`. They drive the real scanner with `psutil.process_iter`
faked beneath it and would have gone red under the default stub — found by running them,
not by grep.

### Evidence (measured 2026-08-17, PowerShell, worktree `admiring-yonath-fbcfaa`)

The confirmed offender, `tests/hermes_cli/test_update_zip_symlink_reject.py::
test_update_via_zip_accepts_normal_member`, reaches the scan through `_update_via_zip` ->
`_kill_stale_dashboard_processes` and names neither "dashboard" nor the scanner:

| | BEFORE | AFTER |
|---|---|---|
| result | 2 passed | 2 passed |
| real host walks | **1** | **0** |
| processes walked | **831** | **0** |
| seconds inside the scan | **11.54** | **0.00** |

Walk counts come from a `psutil.process_iter` probe that attributes by caller frame, so it
measures identically on both arms. **Do not gate this A/B on wall clock**: the same file
measured 22.23 s unguarded and 35.26 s guarded on two adjacent runs — a load artefact that
inverts the true result. The probe's attributed seconds are the honest figure, and the
guarded run of the same file later came in at 11.22 s.

The RED step for canary 2 is worth recording: with the guard removed, calling the real
reaper printed "⟲ Stopping 3 dashboard process(es)" against the developer's live dashboards
and was stopped at `taskkill /PID 1784 /F` by `_is_foreign_pid_kill` alone. That is the
whole argument for this guard, reproduced live.

## Open items

- The finder stays on `sys.meta_path` for the whole session with no teardown removal.
  Removing it would be tidier in principle, but pytest's own assertion-rewriting finder is
  equally permanent, and removal mid-session could unarm the guard for a late import.
  Decision: leave it installed.
- ~~Two claims carried over from the dropped commit's message have not been re-measured:
  the per-file timing wins.~~ **Resolved:** measured after implementation; they did not
  reproduce and were replaced by the table under Test plan.
- The unguarded baseline is not reliably runnable at the pinned 30 s timeout — it dies
  inside the sweep. Anyone re-measuring must raise the cap on **both** arms.
