# Gateway PID-scan test guard: a working implementation

**Date:** 2026-08-17
**Baseline:** `3bc7442b9b` (worktree `objective-gates-1b04a5`, branch `claude/practical-pascal-63397b`)
**Status:** design approved, implementation not started
**Supersedes:** `backup/pre-drop-pidguard-20260817` (`e865ab09db` + `ca17d36a45`), dropped 2026-08-17

## Problem

`hermes_cli.gateway._scan_gateway_pids` is the one layer of the gateway PID lookup that
reads the **host** process table. Measured 2026-08-15: it returned the developer's LIVE
gateway PID `[47164]` in ~2-3 s per call.

Two independent problems, both real:

1. **Cost.** `pyproject.toml` `addopts` pins a 30 s per-test timeout, and that cap covers
   fixture setup. A single test that reaches the scan twice (`gateway_windows.stop()`
   does) spent ~26 s of its 30 s budget there — one slow host away from a timeout that
   reads as a flake.
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
- Per-file before/after on the five known offenders: pass/fail/skip counts identical to
  baseline, and the original timing wins reproduce (`test_cron.py` 126.4 s -> 65.4 s,
  `test_gateway_windows.py` 94.3 s -> 58.2 s).

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

## Open items

- The finder stays on `sys.meta_path` for the whole session with no teardown removal.
  Removing it would be tidier in principle, but pytest's own assertion-rewriting finder is
  equally permanent, and removal mid-session could unarm the guard for a late import.
  Decision: leave it installed.
- Two claims in the acceptance criteria are carried over from the dropped commit's message
  and have **not** been re-measured in this session: the per-file timing wins. They are
  acceptance criteria to verify during implementation, not established facts.
