# browser-harness `_chrome_running()` fix-revert guard — design

**Date:** 2026-08-13
**Status:** designed, not yet implemented

## Problem

On 2026-08-13 a fix to browser-harness `_chrome_running()` was hand-carried into
this machine's live installs because it was not upstreamed (the user declined
opening a PR against `github.com/browser-use/browser-harness`). The fix makes
the check tri-state:

```python
def _chrome_running():
    """Returns True, False, or None (the probe could not complete)."""
```

The defective original mapped every exception — including
`subprocess.TimeoutExpired` on a 5-second bound — onto `False`, so a slow
process enumeration was indistinguishable from an absent browser. `tasklist`
has been measured at 8–73 s on a loaded Windows host. The consequence was not
theoretical: all three daily sentinel-vip scans were blocked for 26h+ while
Chrome was running the whole time. `--doctor` printed `[FAIL] chrome running`,
exited 1, and `cron_vip_scan_orchestrator.py:573` returned 3 on the
`status: blocked` path.

### The doctor verdict is a hard gate, and CDP does not rescue it

Verified end to end, because the guard's whole justification rests on it:

1. `parse_doctor_ok:269-278` sets `chrome_ok` only when a line containing
   `chrome running` is *also* "okish". `[FAIL] chrome running` leaves it `False`.
2. The same function returns `'ok': chrome_ok and daemon_ok`, so one false
   `chrome_ok` sinks the whole verdict.
3. `:555` and `:568` both gate on `if not doctor_ok or not cdp_ok`. That is AND
   semantics for *proceeding*: **a false `doctor_ok` blocks the scan even when
   the CDP probe is healthy.** A live browser connection cannot outvote it.

So a reverted `_chrome_running()` blocks scans on its own, with no second
condition required. There is one refresh retry (`:556-566`) between the two
gates, and it does not help — `refresh_harness()` cannot fix a process scan that
times out.

A backup named `cron_vip_scan_orchestrator.py.bak-pre-cdp-authoritative-20260813`
sits next to the live file, suggesting an intent to make CDP authoritative on
the same day — which would have softened this gate. It did not land: the live
file is **byte-identical** to that backup (both 29661 bytes, `diff` exit 0). The
gate described above is the code that runs today. If CDP is ever made
authoritative, revisit this section — the guard stays useful, but its urgency
drops from "scans stop" to "scans lose a health signal".

The fix lives in local git only, on branch
`fix/chrome-running-timeout-false-negative` @ `1a36129`. Anything that restores
the upstream file — `uv tool upgrade`, `browser-harness --update`, a branch
switch, a bad merge — silently reproduces that 26h outage with no warning. The
failure mode is *silence*: scans stop and nothing says why.

## Which copy actually runs — the brief's premise was wrong

The task was framed as guarding the uv-tool install at
`AppData\Roaming\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py`.
That is not what the crons execute. `cron_vip_scan_orchestrator.py:62-64`:

```python
HARNESS_DIR = Path('C:/Users/diego/Developer/browser-harness')
HARNESS_EXE = HARNESS_DIR / '.venv' / 'Scripts' / 'browser-harness.exe'
...
return str(HARNESS_EXE if HARNESS_EXE.exists() else 'browser-harness')
```

That exe **exists**, and its venv holds an *editable* install
(`__editable__.browser_harness-0.1.8.pth` → `Developer\browser-harness\src`).
So the active code is the dev checkout source, and the uv-tool copy is only the
fallback for when that venv exe disappears.

This adds a revert vector the brief did not consider, and a likelier one than
an upgrade: **`git switch main` in the dev checkout reverts the fix**, because
`1a36129` exists on no other branch. No upgrade required, no file overwritten.

Verified state at design time — both copies hold the fix, so **this guard passes
on this machine today**. It is a regression guard; the hermetic fixture tests
are what prove it detects anything.

| Copy | Role | Fix present | psutil |
|---|---|---|---|
| `Developer\browser-harness\src\browser_harness\admin.py` | **active** (editable) | yes | no |
| `...\uv\tools\browser-harness\...\browser_harness\admin.py` | fallback | yes | 7.2.2 |

psutil's absence from the active venv is not a defect. It is an accelerator that
lets the check read the process table in-process; without it the code falls back
to the 30 s `tasklist` bound, which still returns `None` rather than `False` on
failure. The tri-state return is the load-bearing part, and it does not depend
on psutil.

## Non-goals

- **Re-applying the patch automatically.** A post-update hook that heals the
  symptom would mask the fact that the fix is still un-upstreamed, which is the
  actual root problem. Detection keeps that visible; a self-healing hook would
  bury it until the day the hook itself breaks.
- **Grading psutil.** Reported in the detail line, never in the verdict. It is
  absent from the active venv today and the fix is correct without it.
- **Guarding upstream.** If the fix ever lands upstream this guard becomes
  redundant and should be deleted, not maintained.

## Placement, and why

The check asserts a property of **this machine's installs**, not of any repo.
It cannot be a pytest test in the browser-harness checkout: the property is
about which file is on disk in two venvs, and such a test would fail for anyone
who has not hand-patched — so it would be written to skip, and then no-op
everywhere.

`laptop-monitor.ps1` already owns exactly this shape. Its component catalog has
`-Category 'risk'` rows that read a file and grade it: the SR-470 backend
conformance canary (`:12172`), cloudflared version drift (`:12160`), the Codex
OAuth refresh sentinel (`:11871`), the JobFlow shadow-gate verdict (`:11544`).
Those rows are driven by a `Get-XxxStatus` helper with injectable inputs, so the
grading logic is unit-testable without touching the real environment — the
`Get-CloudflaredVersionStatus` / `Get-CloudflaredInstalledVersion -Exe` pattern
(`:7971`).

Using that catalog also means **no new scheduled task**: the
`LaptopMonitor-Prober` task already runs the catalog on a cadence, and the row's
state flows into `status.json` → the notification layer for free.

## Architecture

One helper plus one catalog row.

### `Get-BrowserHarnessChromeRunningFixStatus`

Placed beside `Get-CloudflaredVersionStatus`. Returns a `pscustomobject`:
`{ activeSource; activePath; activeState; fallbackState; psutil; state; detail }`.

Every path is a parameter with a real-path default, so Pester drives it against
tempdir fixtures and never reads the live installs.

**Resolution mirrors the orchestrator, deliberately.** If
`Developer\browser-harness\.venv\Scripts\browser-harness.exe` exists, the active
copy is the editable target, read from the `.pth` (falling back to the
conventional `src\browser_harness\admin.py` when that file is unreadable).
Otherwise the active copy is the uv-tool `admin.py`. If this resolution ever
diverges from the callers', the guard grades the wrong file — so the mirroring
is called out in a comment at both ends.

**Two cron entrypoints share this resolution**, which is why mirroring it is
safe rather than a coincidence: `cron_vip_scan_orchestrator.py:62-64` and
`raw_linkedin_scan_cron_runner.py:27-30` compute the same dev-exe-then-PATH
fallback, and both call `--doctor` (`:281` and `:269`/`:278` respectively). One
probe row therefore covers both. `harness_probe.py` and `linkedin_scan.py` are
run *through* the harness (`browser-harness -c "exec(...)"`), so they inherit
whichever exe their caller resolved and add no third path.

**Both files are read on every pass regardless of which way resolution went.**
"Active" and "fallback" are labels applied after resolution, not a choice of
what to read — when the dev exe is absent and the uv-tool copy becomes active,
the dev src copy is graded as the fallback. `activeSource` records which way it
went (`dev-editable` or `uv-tool`) so the detail line is never ambiguous about
which file carried the verdict. A copy that is `missing` in the fallback role is
normal, not a warning: a machine with no dev checkout has nothing to report.

### Per-file grade

A file is `ok` only when it contains **both** markers:

- `_chrome_running_via_psutil`
- `_PROCESS_SCAN_TIMEOUT`

Otherwise `reverted` (readable, markers absent), `unreadable`, or `missing`.

Grepping content rather than checking a git branch is deliberate and strictly
stronger: one test catches `git switch main`, `uv tool upgrade`,
`browser-harness --update`, a bad merge, and a hand-edit. A branch-name check
would catch only the first, and would need a `git` spawn this monitor is careful
to avoid.

Requiring **both** markers rather than one guards a partial restore — a merge
that keeps the helper but drops the timeout constant leaves the 5 s bound in
place, which is the original defect.

## Row grading

| Active copy | Row state | Detail |
|---|---|---|
| `ok` | healthy | `active=dev-editable OK`, plus a fallback warning when the fallback is reverted |
| `reverted` | **down** | names the reverted path, reference commit `1a36129`, and the `.bak` restore path |
| `missing` / `unreadable` | unknown | names the path and the reason |

`unknown` for missing/unreadable follows the catalog's stated convention:
*"'unknown' is the honest verdict — 'not checked this tick' — and never condemns
a service the way a false 'down' would."* If the active `admin.py` is absent the
harness is broken in a way another probe owns; this row cannot assert its
invariant, so it says so rather than guessing.

**The fallback copy never drives row state, only the detail string.**
`Probe-Component` has no warn tier (`healthy` / `down` / `unknown` / `error`),
and a `uv tool upgrade` while the dev checkout still holds the fix is a latent
hazard, not an outage — scans keep working. Grading it `down` would raise a
critical-looking alarm for a working system, and a row that cries wolf gets
ignored before the day it is right.

Registered `-Category 'risk' -Tier 'important' -AlwaysRun`, matching the Codex
OAuth sentinel row. The `-AlwaysRun` budget exemption is warranted: the cost is
two ~48 KB file reads with no process spawn and no network, and the whole point
of the row is to not go silent when the pass budget tightens.

## Failure output

The detail line must be actionable without opening this document — it names what
reverted, what to restore from, and the reference commit:

```
REVERTED: active copy C:\Users\diego\Developer\browser-harness\src\browser_harness\admin.py
lost _chrome_running_via_psutil/_PROCESS_SCAN_TIMEOUT -- vip scans will block.
Restore from git 1a36129 (branch fix/chrome-running-timeout-false-negative)
or .bak-pre-chromerunning-fix-20260813.
```

## Testing

Pester cases in `laptop-monitor.tests.ps1`, hermetic against tempdir fixtures:

- Both copies hold the markers → `healthy`, detail names the active copy.
- Active copy reverted → `down`, detail contains the restore guidance.
- Fallback reverted, active `ok` → **`healthy`** with the fallback warning in
  the detail. This pins the noise decision above so a later edit cannot quietly
  turn it into an alarm.
- Dev exe absent → resolution flips, the uv-tool copy is graded as active.
- Active file missing → `unknown`, not `down`.
- Only one of the two markers present → `reverted`.

### End-to-end, before this is called done

The fixtures prove the grading; they do not prove the marker strings match the
real artifact. So the guard is additionally run against a deliberately reverted
**copy** of the real file — `admin.py.bak-pre-chromerunning-fix-20260813` laid
over a scratch tree — confirming `down` fires with the right detail, then the
scratch tree is discarded. The live installs are never modified.

This step is not ceremony. On the editable-finder guard (2026-08-11) every
fixture passed while the guard emitted misleading remediation on the one
scenario the fixtures did not model.

## References

- Reference copy of the fix: local git
  `C:\Users\diego\Developer\browser-harness`, branch
  `fix/chrome-running-timeout-false-negative`, commit `1a36129`
- Backup of the defective original:
  `...\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py.bak-pre-chromerunning-fix-20260813`
- Orchestrator: `~\.hermes\profiles\sentinel\workspace\cron_vip_scan_orchestrator.py`
  — harness resolution `:62-64`, blocked-path `return 3` at **`:573`**. (The
  originating brief cited `:604`; the only `return 3` in the file is at 573.
  Recorded so nobody chases a line that does not exist.)
- MemPalace: drawers `browser-harness/fixes` (2026-08-13, two)
- GBrain: `systems/browser-harness`, timeline entry 2026-08-13
- Precedent for a drift guard on a hand-carried install state:
  `docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md`
- Catalog rows this one is modelled on: `laptop-monitor.ps1:11871` (Codex OAuth
  sentinel), `:12160` (cloudflared drift), `:12172` (SR-470 canary); helper
  pattern at `:7971`

## Verified 2026-08-13

### The marker strings match the real artifact

A scratch tree was built holding genuine files copied out of the two real
installs (never the reverse): the dev-editable `admin.py` was the true
pre-fix body from
`...\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py.bak-pre-chromerunning-fix-20260813`
(45264 bytes, defective), and the uv-tool `admin.py` was the current fixed
copy from `C:\Users\diego\Developer\browser-harness\src\browser_harness\admin.py`
(48270 bytes). `Get-BrowserHarnessChromeRunningFixStatus`, AST-extracted from
`laptop-monitor.ps1` and dot-sourced (never the whole file, which would run a
full probe sweep), graded this tree:

    state         : down
    activeSource  : dev-editable
    activeState   : reverted
    fallbackState : ok
    detail        : REVERTED: the ACTIVE copy ...\bh-e2e\dev\src\browser_harness\admin.py
                    has lost _chrome_running_via_psutil/_PROCESS_SCAN_TIMEOUT. ... Restore
                    from git 1a36129 (branch fix/chrome-running-timeout-false-negative) in
                    C:\Users\diego\Developer\browser-harness, or from
                    admin.py.bak-pre-chromerunning-fix-20260813.

This is the result that matters: the guard is not a silent no-op against the
real defective body. Had this printed `healthy`, the conclusion would have
been that the marker strings do not match the real file and the entire
four-task effort was worthless — it did not.

### The inverse holds against all three real files directly

`Get-BrowserHarnessAdminFileState`, run on the genuine files with no scratch
copying, graded exactly:

    REAL fixed  -> ok
    REAL .bak   -> reverted
    REAL uvtool -> ok

confirming both live installs (`Developer\browser-harness` and the uv-tool
site-packages copy) carry the fix, and the preserved `.bak` still reads as
the reverted original.

### The live catalog row and the installs are green

After the scratch tree was deleted, `grep -c "_chrome_running_via_psutil"`
against both real `admin.py` files returned `2` for each, and
`git status --porcelain` in `C:\Users\diego\Developer\browser-harness` printed
nothing — the live installs were never written to, only read from.

### Regression suite

`laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease` →
**1918 passed, 1 failed, 9 skipped**. The one failure is the pre-existing,
unrelated `Resolve-RealPython` extraction gap ("every function an extracted
function INVOKES is itself extracted", invoked by `Restart-CodexProxy`) —
not touched by this guard.
