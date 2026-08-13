# browser-harness `_chrome_running()` fix-revert guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect, on a recurring schedule, when the hand-carried `_chrome_running()` fix disappears from whichever browser-harness install the sentinel crons actually execute — before scans silently stop for another 26 hours.

**Architecture:** Two pure PowerShell helpers plus one `Probe-Component` catalog row in `laptop-monitor.ps1`. The helpers mirror the orchestrator's harness resolution, grep both installs' `admin.py` for two fix markers, and grade the row `down` only when the *active* copy lost them. No new scheduled task (the existing `LaptopMonitor-Prober` task runs the catalog), no process spawn, no network, no git.

**Tech Stack:** Windows PowerShell 5.1; `laptop-monitor.ps1` component catalog; the custom `Assert` harness in `laptop-monitor.tests.ps1` (this is **not** Pester).

**Spec:** `docs/superpowers/specs/2026-08-13-browser-harness-chrome-running-fix-guard-design.md`

## Global Constraints

- **Windows PowerShell 5.1 only.** No `&&`/`||`, no ternary, no `??`, no `?.`. Use `;` and `if/else`.
- **The two fix markers, verbatim:** `_chrome_running_via_psutil` and `_PROCESS_SCAN_TIMEOUT`. A file is `ok` only when it contains **both**.
- **Reference commit:** `1a36129` on branch `fix/chrome-running-timeout-false-negative` in `C:\Users\diego\Developer\browser-harness`.
- **Backup of the defective original:** `admin.py.bak-pre-chromerunning-fix-20260813` in the uv-tool `browser_harness` directory.
- **Row name, verbatim and identical in all three files:** `browser-harness _chrome_running() fix present (vip-scan gate)`
- **No top-level `$script:` constants for helper data.** The test harness AST-extracts *function definitions only*; a top-level variable a helper depends on will be `$null` under test and the test will silently pass against broken logic. Put defaults in `param()` blocks.
- **Every function called by an extracted function must be listed in `$required`** in `laptop-monitor.tests.ps1`, or it throws `CommandNotFoundException` and silently kills every test section below it.
- **Never modify the live installs.** All verification uses scratch copies.
- **Scratch dir for this work:** `C:\Users\diego\AppData\Local\Temp\claude\C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1\bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d\scratchpad`

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `C:\Users\diego\laptop-monitor.ps1` | Modify (~`:8008`) | Add `Get-BrowserHarnessAdminFileState` + `Get-BrowserHarnessChromeRunningFixStatus` after `Get-CloudflaredVersionStatus` |
| `C:\Users\diego\laptop-monitor.ps1` | Modify (~`:12220`) | Add the `Probe-Component` row before `# End component catalog` |
| `C:\Users\diego\laptop-monitor.tests.ps1` | Modify (`:676`) | Add both helper names to `$required` |
| `C:\Users\diego\laptop-monitor.tests.ps1` | Modify (`:3372-3375`) | Add the row name to `$freezeDetectorRows` to pin `-AlwaysRun` |
| `C:\Users\diego\laptop-monitor.tests.ps1` | Modify (before the summary block, ~`:11170`) | Append the new test section |

Neither file is in a git repo (`C:\Users\diego` is not a work tree), so there is nothing to commit for the code changes. Commits in this plan apply **only** to the spec/plan documents in `~/.hermes/agent-src`. Where a task says "commit", it means: copy the changed monitor files to the scratchpad as a snapshot, then commit any doc updates. Do not `git init` anything.

---

### Task 1: The per-file marker grader

**Files:**
- Modify: `C:\Users\diego\laptop-monitor.ps1` — insert after line 8008 (the closing `}` of `Get-CloudflaredVersionStatus`, immediately before the `# Per-probe timing seam` comment block at 8010)
- Modify: `C:\Users\diego\laptop-monitor.tests.ps1:676` — add to `$required`
- Test: `C:\Users\diego\laptop-monitor.tests.ps1` — new section appended above the summary

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Get-BrowserHarnessAdminFileState -Path <string> [-Markers <string[]>] -> string` returning exactly one of `'ok'`, `'reverted'`, `'unreadable'`, `'missing'`. Task 2 calls it twice.

- [ ] **Step 1: Add the helper name to `$required` first**

If you skip this, every test you write in this task throws `CommandNotFoundException` and silently kills the rest of the suite.

In `C:\Users\diego\laptop-monitor.tests.ps1`, immediately **before** the closing `)` on line 676, add:

```powershell
    # browser-harness _chrome_running() fix-revert guard (2026-08-13). The catalog row's
    # -Test AND -Detail both call Get-BrowserHarnessChromeRunningFixStatus, which calls
    # Get-BrowserHarnessAdminFileState, so BOTH must end up extracted or the row throws
    # CommandNotFoundException and kills every section below it. Task 2 adds the second
    # name when that function lands -- see the ordering note below.
    'Get-BrowserHarnessAdminFileState'
```

**Register only the function that exists.** The extraction loop (`:687-700`) collects
missing names and hard-exits the whole suite before any later section runs. Listing
`Get-BrowserHarnessChromeRunningFixStatus` here, one task before it exists, drops the
run from ~1889 passing to 128 and hides every other section. Task 2 adds it.

- [ ] **Step 2: Write the failing test**

Append this immediately **above** the `# Colour is computed INLINE` comment block near the end of `laptop-monitor.tests.ps1` (that file grows by appending sections directly above the summary):

```powershell
# ------------------------------------------------------------------
# browser-harness _chrome_running() fix-revert guard (2026-08-13).
#
# WHY: the fix is hand-carried and un-upstreamed. `uv tool upgrade`,
# `browser-harness --update`, or a plain `git switch main` in the dev checkout
# restores the defective version, and the ONLY symptom is that vip scans stop:
# doctor prints "[FAIL] chrome running", parse_doctor_ok ANDs it into ok=False,
# and cron_vip_scan_orchestrator.py:568 blocks with return 3 even when the CDP
# probe is healthy. That went unnoticed for 26h+ on 2026-08-13.
# ------------------------------------------------------------------
Write-Host "`n== browser-harness _chrome_running() fix-revert guard =="

$bhRoot = Join-Path $env:TEMP ("bh-fixguard-" + [guid]::NewGuid().ToString('N').Substring(0,8))

# Fixture bodies mirror the REAL file shapes (per feedback_test_with_real_inputs):
# FIXED carries both markers; DEFECTIVE carries the 5s bound + bare-except that
# mapped TimeoutExpired onto False, and neither marker.
$bhFixedBody = @'
_PROCESS_SCAN_TIMEOUT = 30
def _chrome_running_via_psutil(names):
    import psutil
    return False
def _chrome_running():
    seen = _chrome_running_via_psutil(names)
    if seen is not None:
        return seen
    return None
'@
$bhDefectiveBody = @'
def _chrome_running():
    try:
        out = subprocess.check_output(cmd, text=True, timeout=5)
    except Exception:
        return False
    return any(n in out.lower() for n in names)
'@

function New-BhAdminFile {
    param([string]$Path, [string]$Body)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Set-Content -LiteralPath $Path -Value $Body -Encoding UTF8
}

$bhFixed      = Join-Path $bhRoot 'fixed\admin.py'
$bhDefective  = Join-Path $bhRoot 'defective\admin.py'
$bhPartial    = Join-Path $bhRoot 'partial\admin.py'
New-BhAdminFile -Path $bhFixed     -Body $bhFixedBody
New-BhAdminFile -Path $bhDefective -Body $bhDefectiveBody
# Partial restore: the helper survived a merge but the timeout constant did not,
# which leaves the original 5s bound in place -- i.e. still defective.
New-BhAdminFile -Path $bhPartial   -Body "def _chrome_running_via_psutil(names):`n    return None`n"

Assert 'bh-fixguard: a file with BOTH markers grades ok' (
    (Get-BrowserHarnessAdminFileState -Path $bhFixed) -eq 'ok'
) ("got=" + (Get-BrowserHarnessAdminFileState -Path $bhFixed))

Assert 'bh-fixguard: the pre-fix upstream body grades reverted' (
    (Get-BrowserHarnessAdminFileState -Path $bhDefective) -eq 'reverted'
) ("got=" + (Get-BrowserHarnessAdminFileState -Path $bhDefective))

Assert 'bh-fixguard: only ONE of the two markers still grades reverted (partial restore leaves the 5s bound)' (
    (Get-BrowserHarnessAdminFileState -Path $bhPartial) -eq 'reverted'
) ("got=" + (Get-BrowserHarnessAdminFileState -Path $bhPartial))

Assert 'bh-fixguard: an absent path grades missing (never reverted)' (
    (Get-BrowserHarnessAdminFileState -Path (Join-Path $bhRoot 'nope\admin.py')) -eq 'missing'
) ("got=" + (Get-BrowserHarnessAdminFileState -Path (Join-Path $bhRoot 'nope\admin.py')))

Assert 'bh-fixguard: an empty path grades missing rather than throwing' (
    (Get-BrowserHarnessAdminFileState -Path '') -eq 'missing'
) ("got=" + (Get-BrowserHarnessAdminFileState -Path ''))
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: the run aborts or reports FAIL at the extraction stage with `CommandNotFoundException: Get-BrowserHarnessAdminFileState`, because the function does not exist in `laptop-monitor.ps1` yet. That abort is the expected failure for this step.

- [ ] **Step 4: Write the minimal implementation**

In `C:\Users\diego\laptop-monitor.ps1`, insert after line 8008 (after `Get-CloudflaredVersionStatus`'s closing brace, before the `# Per-probe timing seam` block):

```powershell
# ------------------------------------------------------------------
# browser-harness _chrome_running() fix-revert guard (2026-08-13).
#
# The fix that makes _chrome_running() tri-state (True / False / None-on-timeout)
# is hand-carried into this machine's installs and is NOT upstreamed. Any restore
# of the upstream file -- `uv tool upgrade`, `browser-harness --update`, `git switch
# main` in the dev checkout, a bad merge -- silently reinstates the defect, whose
# only symptom is that the sentinel vip scans stop: doctor prints "[FAIL] chrome
# running", parse_doctor_ok ANDs chrome_ok into ok=False, and both gates in
# cron_vip_scan_orchestrator.py (:555, :568) block with return 3 EVEN WHEN the CDP
# probe is healthy. That is not a theoretical chain -- it blocked all three daily
# scans for 26h+ on 2026-08-13 with Chrome running the whole time.
#
# Markers live in param() defaults, NOT in a top-level $script: constant: the test
# harness AST-extracts function definitions only, so a top-level variable would be
# $null under test and the tests would pass against broken logic.
# ------------------------------------------------------------------
function Get-BrowserHarnessAdminFileState {
    # Returns 'ok' (both fix markers present), 'reverted' (readable, markers absent),
    # 'unreadable', or 'missing'. BOTH markers are required: a partial restore that
    # keeps the psutil helper but drops the timeout constant leaves the original 5s
    # bound in place, which IS the defect.
    param(
        [string]$Path,
        [string[]]$Markers = @('_chrome_running_via_psutil', '_PROCESS_SCAN_TIMEOUT')
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { return 'missing' }
    if (-not (Test-Path -LiteralPath $Path)) { return 'missing' }
    $txt = $null
    try { $txt = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop } catch { return 'unreadable' }
    if ($null -eq $txt) { return 'unreadable' }
    foreach ($m in $Markers) {
        if ($txt -notlike ("*" + $m + "*")) { return 'reverted' }
    }
    return 'ok'
}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: five `PASS  bh-fixguard:` lines, and `0 failed` in the summary. If the suite reports failures in *other* sections, check they are pre-existing by stashing your edit and re-running — do not proceed with an unexplained red suite.

- [ ] **Step 6: Snapshot**

```bash
cp C:/Users/diego/laptop-monitor.ps1 C:/Users/diego/AppData/Local/Temp/claude/C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1/bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d/scratchpad/laptop-monitor.ps1.after-task1
```

---

### Task 2: Resolution + row grading

**Files:**
- Modify: `C:\Users\diego\laptop-monitor.ps1` — append directly after `Get-BrowserHarnessAdminFileState`
- Test: `C:\Users\diego\laptop-monitor.tests.ps1` — append to the section started in Task 1

**Interfaces:**
- Consumes: `Get-BrowserHarnessAdminFileState -Path <string> -> string` (Task 1).
- Produces: `Get-BrowserHarnessChromeRunningFixStatus [-DevExe] [-DevSitePackages] [-DevSrcAdmin] [-UvToolAdmin] -> pscustomobject` with fields `activeSource` (`'dev-editable'`|`'uv-tool'`), `activePath`, `activeState`, `fallbackPath`, `fallbackState`, `psutil`, `state` (`'healthy'`|`'down'`|`'unknown'`), `detail`. Task 3's catalog row calls it from both `-Test` and `-Detail`.

- [ ] **Step 0: Register the function name — but only after it exists**

Task 1 deliberately left `Get-BrowserHarnessChromeRunningFixStatus` out of `$required`,
because the extraction loop (`laptop-monitor.tests.ps1:687-700`) hard-exits the whole
suite on any missing name. **Write the function first (Step 3 below), then** replace the
placeholder comment Task 1 left in `$required` with:

```powershell
    'Get-BrowserHarnessAdminFileState', 'Get-BrowserHarnessChromeRunningFixStatus'
```

Delete Task 1's ordering note above it — it describes a state that no longer exists.
If you add the name before the function exists, the suite drops from ~1889 passing to
128 and every section below extraction goes unrun.

- [ ] **Step 1: Write the failing test**

Append to the same test section, after the Task 1 asserts:

```powershell
# --- resolution + grading -----------------------------------------
# Fixture layout mirrors the real one: a dev checkout whose .venv holds an EDITABLE
# install pointing at src\, plus the uv-tool site-packages copy.
function New-BhTree {
    param(
        [string]$Root,
        [ValidateSet('ok','reverted','missing')][string]$Dev  = 'ok',
        [ValidateSet('ok','reverted','missing')][string]$Uv   = 'ok',
        [bool]$DevExe = $true,
        [bool]$Psutil = $false
    )
    $devVenvSp = Join-Path $Root 'dev\.venv\Lib\site-packages'
    $devSrc    = Join-Path $Root 'dev\src'
    $uvSp      = Join-Path $Root 'uv\Lib\site-packages'
    New-Item -ItemType Directory -Path $devVenvSp -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $devVenvSp '__editable__.browser_harness-0.1.8.pth') -Value $devSrc -Encoding UTF8
    if ($DevExe) {
        $sc = Join-Path $Root 'dev\.venv\Scripts'
        New-Item -ItemType Directory -Path $sc -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $sc 'browser-harness.exe') -Value 'stub' -Encoding UTF8
    }
    if ($Psutil) { New-Item -ItemType Directory -Path (Join-Path $devVenvSp 'psutil') -Force | Out-Null }
    if ($Dev -ne 'missing') {
        New-BhAdminFile -Path (Join-Path $devSrc 'browser_harness\admin.py') `
            -Body $(if ($Dev -eq 'ok') { $bhFixedBody } else { $bhDefectiveBody })
    }
    if ($Uv -ne 'missing') {
        New-BhAdminFile -Path (Join-Path $uvSp 'browser_harness\admin.py') `
            -Body $(if ($Uv -eq 'ok') { $bhFixedBody } else { $bhDefectiveBody })
    }
    [pscustomobject]@{
        DevExe          = Join-Path $Root 'dev\.venv\Scripts\browser-harness.exe'
        DevSitePackages = $devVenvSp
        DevSrcAdmin     = Join-Path $devSrc 'browser_harness\admin.py'
        UvToolAdmin     = Join-Path $uvSp 'browser_harness\admin.py'
    }
}
function Get-BhStatus {
    param([pscustomobject]$Tree)
    Get-BrowserHarnessChromeRunningFixStatus -DevExe $Tree.DevExe -DevSitePackages $Tree.DevSitePackages `
        -DevSrcAdmin $Tree.DevSrcAdmin -UvToolAdmin $Tree.UvToolAdmin
}

# Case 1 -- the state of this machine today: both copies fixed, dev exe present.
$bhT1 = New-BhTree -Root (Join-Path $bhRoot 't1') -Dev 'ok' -Uv 'ok' -DevExe $true
$bhS1 = Get-BhStatus -Tree $bhT1
Assert 'bh-fixguard: both copies fixed -> healthy'            ($bhS1.state -eq 'healthy') "state=$($bhS1.state) detail=$($bhS1.detail)"
Assert 'bh-fixguard: dev exe present -> active is the EDITABLE copy, not the uv-tool one' (
    ($bhS1.activeSource -eq 'dev-editable') -and ($bhS1.activePath -eq $bhT1.DevSrcAdmin)
) "activeSource=$($bhS1.activeSource) activePath=$($bhS1.activePath)"

# Case 2 -- THE outage condition: the copy that actually runs lost the fix.
$bhT2 = New-BhTree -Root (Join-Path $bhRoot 't2') -Dev 'reverted' -Uv 'ok' -DevExe $true
$bhS2 = Get-BhStatus -Tree $bhT2
Assert 'bh-fixguard: ACTIVE copy reverted -> down'            ($bhS2.state -eq 'down') "state=$($bhS2.state) detail=$($bhS2.detail)"
Assert 'bh-fixguard: down detail names the reverted path, the commit, and the .bak' (
    ($bhS2.detail -match [regex]::Escape($bhT2.DevSrcAdmin)) -and
    ($bhS2.detail -match '1a36129') -and
    ($bhS2.detail -match 'bak-pre-chromerunning-fix-20260813')
) "detail=$($bhS2.detail)"
Assert 'bh-fixguard: down detail explains the consequence (scans block), not just the diff' (
    $bhS2.detail -match 'vip scan'
) "detail=$($bhS2.detail)"

# Case 3 -- PINS THE NOISE DECISION. A reverted FALLBACK is a latent hazard, not an
# outage: scans still work off the dev checkout. It must warn in the detail and NOT
# turn the row red, or a routine `uv tool upgrade` cries wolf and the row gets ignored
# before the day it is right.
$bhT3 = New-BhTree -Root (Join-Path $bhRoot 't3') -Dev 'ok' -Uv 'reverted' -DevExe $true
$bhS3 = Get-BhStatus -Tree $bhT3
Assert 'bh-fixguard: reverted FALLBACK only -> row stays healthy (no false alarm)' (
    $bhS3.state -eq 'healthy'
) "state=$($bhS3.state) detail=$($bhS3.detail)"
Assert 'bh-fixguard: reverted FALLBACK is still surfaced in the detail' (
    ($bhS3.detail -match 'WARNING') -and ($bhS3.detail -match [regex]::Escape($bhT3.UvToolAdmin))
) "detail=$($bhS3.detail)"

# Case 4 -- resolution flips when the dev venv exe is gone (harness_exe() falls back
# to `browser-harness` on PATH, i.e. the uv tool). The uv-tool copy is then load-bearing.
$bhT4 = New-BhTree -Root (Join-Path $bhRoot 't4') -Dev 'ok' -Uv 'reverted' -DevExe $false
$bhS4 = Get-BhStatus -Tree $bhT4
Assert 'bh-fixguard: no dev exe -> active flips to the uv-tool copy' (
    ($bhS4.activeSource -eq 'uv-tool') -and ($bhS4.activePath -eq $bhT4.UvToolAdmin)
) "activeSource=$($bhS4.activeSource) activePath=$($bhS4.activePath)"
Assert 'bh-fixguard: the SAME reverted uv-tool copy is DOWN once it is the active one' (
    $bhS4.state -eq 'down'
) "state=$($bhS4.state) detail=$($bhS4.detail)"

# Case 5 -- cannot read the active copy: 'unknown' is the honest verdict. Never 'down'
# (that would condemn on no evidence) and never 'healthy'.
$bhT5 = New-BhTree -Root (Join-Path $bhRoot 't5') -Dev 'missing' -Uv 'ok' -DevExe $true
$bhS5 = Get-BhStatus -Tree $bhT5
Assert 'bh-fixguard: active copy missing -> unknown, not down'  ($bhS5.state -eq 'unknown') "state=$($bhS5.state) detail=$($bhS5.detail)"
Assert 'bh-fixguard: unknown detail names the path it could not read' (
    $bhS5.detail -match [regex]::Escape($bhT5.DevSrcAdmin)
) "detail=$($bhS5.detail)"

# Case 6 -- psutil is an ACCELERATOR, never a verdict input. Its absence is the state
# of the active venv today and the fix is correct without it.
$bhT6 = New-BhTree -Root (Join-Path $bhRoot 't6') -Dev 'ok' -Uv 'ok' -DevExe $true -Psutil $false
$bhS6 = Get-BhStatus -Tree $bhT6
Assert 'bh-fixguard: missing psutil does NOT affect the verdict' ($bhS6.state -eq 'healthy') "state=$($bhS6.state)"
Assert 'bh-fixguard: missing psutil is reported in the detail'   ($bhS6.detail -match 'psutil') "detail=$($bhS6.detail)"

# Case 7 -- an ABSENT .pth degrades to the conventional src location.
#
# NOTE (amended 2026-08-13 after review): this case was originally commented as
# testing an "unreadable" .pth, which it does not -- deleting the file makes
# $pth.Count 0, so the try/catch around Get-Content is never entered and the
# catch{} fail-soft path goes unexercised. Case 8 below covers that branch. A
# guard whose own failure mode is a silent no-op is the exact bug class this
# work exists to catch, so the two cases must stay distinct.
$bhT7 = New-BhTree -Root (Join-Path $bhRoot 't7') -Dev 'ok' -Uv 'ok' -DevExe $true
Remove-Item -LiteralPath (Join-Path $bhT7.DevSitePackages '__editable__.browser_harness-0.1.8.pth') -Force
$bhS7 = Get-BhStatus -Tree $bhT7
Assert 'bh-fixguard: absent .pth falls back to the conventional src path' (
    ($bhS7.activePath -eq $bhT7.DevSrcAdmin) -and ($bhS7.state -eq 'healthy')
) "activePath=$($bhS7.activePath) state=$($bhS7.state)"

# Case 8 -- an UNREADABLE .pth reaches the same fallback through the catch{} block.
# The .pth path is created as a DIRECTORY on purpose: Get-ChildItem -Filter still
# matches it, so $pth.Count is 1 and the try IS entered, but Get-Content on a
# directory throws ("Access to the path ... is denied") and the catch fires. That
# is the only way to prove the fail-soft path actually works -- Case 7 reaches the
# same answer via the count-zero branch without touching it. Do not "fix" this
# into a file; it would silently revert the coverage to Case 7's branch.
$bhT8 = New-BhTree -Root (Join-Path $bhRoot 't8') -Dev 'ok' -Uv 'ok' -DevExe $true
Remove-Item -LiteralPath (Join-Path $bhT8.DevSitePackages '__editable__.browser_harness-0.1.8.pth') -Force
New-Item -ItemType Directory -Path (Join-Path $bhT8.DevSitePackages '__editable__.browser_harness-0.1.8.pth') -Force | Out-Null
$bhS8 = Get-BhStatus -Tree $bhT8
Assert 'bh-fixguard: unreadable .pth (directory) falls back to the conventional src path via the catch block' (
    ($bhS8.activePath -eq $bhT8.DevSrcAdmin) -and ($bhS8.state -eq 'healthy')
) "activePath=$($bhS8.activePath) state=$($bhS8.state)"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: abort/FAIL with `CommandNotFoundException: Get-BrowserHarnessChromeRunningFixStatus`. Task 1's five asserts still PASS.

- [ ] **Step 3: Write the implementation**

Append directly after `Get-BrowserHarnessAdminFileState` in `laptop-monitor.ps1`:

```powershell
function Get-BrowserHarnessChromeRunningFixStatus {
    # Returns { activeSource; activePath; activeState; fallbackPath; fallbackState;
    #           psutil; state; detail }.
    #
    # RESOLUTION MIRRORS THE CRON ENTRYPOINTS, DELIBERATELY. Both
    # cron_vip_scan_orchestrator.py:62-64 and raw_linkedin_scan_cron_runner.py:27-30
    # compute the same "dev .venv exe if it exists, else `browser-harness` on PATH"
    # fallback, and both call --doctor. If this resolution ever diverges from theirs,
    # this row grades a file nothing runs. Keep the three in step.
    #
    # Only the ACTIVE copy drives the verdict. A reverted fallback is a latent hazard,
    # not an outage -- scans keep working -- so it is folded into the detail. There is
    # no warn tier here (healthy/down/unknown/error), and a row that reddens for a
    # working system gets ignored before the day it is right.
    param(
        [string]$DevExe          = (Join-Path $env:USERPROFILE 'Developer\browser-harness\.venv\Scripts\browser-harness.exe'),
        [string]$DevSitePackages = (Join-Path $env:USERPROFILE 'Developer\browser-harness\.venv\Lib\site-packages'),
        [string]$DevSrcAdmin     = (Join-Path $env:USERPROFILE 'Developer\browser-harness\src\browser_harness\admin.py'),
        [string]$UvToolAdmin     = (Join-Path $env:USERPROFILE 'AppData\Roaming\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py')
    )

    # Resolve the editable target from the .pth when it is readable; the version is in
    # its filename, so glob rather than pin 0.1.8. Fail soft to the conventional path.
    $devAdmin = $DevSrcAdmin
    $pth = @(Get-ChildItem -LiteralPath $DevSitePackages -Filter '__editable__.browser_harness-*.pth' -ErrorAction SilentlyContinue |
             Sort-Object Name | Select-Object -First 1)
    if ($pth.Count -eq 1) {
        try {
            $root = (Get-Content -LiteralPath $pth[0].FullName -Raw -ErrorAction Stop).Trim()
            if (-not [string]::IsNullOrWhiteSpace($root)) { $devAdmin = Join-Path $root 'browser_harness\admin.py' }
        } catch { }
    }

    if (Test-Path -LiteralPath $DevExe) {
        $activeSource = 'dev-editable'; $activePath = $devAdmin;    $fallbackPath = $UvToolAdmin
    } else {
        $activeSource = 'uv-tool';      $activePath = $UvToolAdmin; $fallbackPath = $devAdmin
    }

    $activeState   = Get-BrowserHarnessAdminFileState -Path $activePath
    $fallbackState = Get-BrowserHarnessAdminFileState -Path $fallbackPath

    # psutil is reported, never graded. Without it _chrome_running() falls back to the
    # 30s tasklist scan, which still returns None (not False) on failure -- the
    # tri-state return is the load-bearing part.
    if ($activeSource -eq 'dev-editable') {
        $psutilDir = Join-Path $DevSitePackages 'psutil'
    } else {
        $psutilDir = Join-Path (Split-Path -Parent (Split-Path -Parent $UvToolAdmin)) 'psutil'
    }
    if (Test-Path -LiteralPath $psutilDir) { $psutil = 'psutil present (in-process fast path)' }
    else                                   { $psutil = 'no psutil (30s tasklist fallback; correct but slow)' }

    switch ($activeState) {
        'ok' {
            $state  = 'healthy'
            $detail = "active=$activeSource OK (both fix markers present at $activePath); $psutil"
            if ($fallbackState -eq 'reverted') {
                $detail += " -- WARNING: the inactive fallback copy $fallbackPath has LOST the fix. Not on the execution path today, but it becomes active if $DevExe disappears."
            }
        }
        'reverted' {
            $state  = 'down'
            $detail = "REVERTED: the ACTIVE copy $activePath has lost _chrome_running_via_psutil/_PROCESS_SCAN_TIMEOUT. browser-harness --doctor will report [FAIL] chrome running on a loaded box and the sentinel vip scans will BLOCK (cron_vip_scan_orchestrator.py:568 -> return 3) even with a healthy CDP probe. Restore from git 1a36129 (branch fix/chrome-running-timeout-false-negative) in C:\Users\diego\Developer\browser-harness, or from admin.py.bak-pre-chromerunning-fix-20260813."
        }
        'unreadable' {
            $state  = 'unknown'
            $detail = "cannot verify: the active copy $activePath exists but could not be read"
        }
        default {
            $state  = 'unknown'
            $detail = "cannot verify: the active copy $activePath is missing (browser-harness install is absent or relocated)"
        }
    }

    [pscustomobject]@{
        activeSource = $activeSource; activePath = $activePath; activeState = $activeState
        fallbackPath = $fallbackPath; fallbackState = $fallbackState
        psutil = $psutil; state = $state; detail = $detail
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: all `bh-fixguard:` asserts PASS (five from Task 1, thirteen from this task), `0 failed`.

- [ ] **Step 5: Snapshot**

```bash
cp C:/Users/diego/laptop-monitor.ps1 C:/Users/diego/AppData/Local/Temp/claude/C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1/bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d/scratchpad/laptop-monitor.ps1.after-task2
```

---

### Task 3: The catalog row

**Files:**
- Modify: `C:\Users\diego\laptop-monitor.ps1` — insert before `# End component catalog ---` (line ~12222)
- Modify: `C:\Users\diego\laptop-monitor.tests.ps1:3372-3375` — `$freezeDetectorRows`
- Test: `C:\Users\diego\laptop-monitor.tests.ps1` — append to the same section

**Interfaces:**
- Consumes: `Get-BrowserHarnessChromeRunningFixStatus -> pscustomobject` with `.state` and `.detail` (Task 2).
- Produces: a catalog row named exactly `browser-harness _chrome_running() fix present (vip-scan gate)`, tier `important`, category `risk`, carrying `-AlwaysRun`.

- [ ] **Step 1: Write the failing test**

Append to the same test section. This uses `Get-WslProbeRow`, the file's existing generic `Probe-Component` row extractor (defined at `:2603`; the WSL-specific name is historical — it takes any row name):

```powershell
# --- catalog wiring -----------------------------------------------
# Row-level asserts, because the helper being correct is worthless if the catalog
# row does not call it, or calls it with the wrong tier / without -AlwaysRun.
$bhRowName = 'browser-harness _chrome_running() fix present (vip-scan gate)'
$bhRow = Get-WslProbeRow $bhRowName      # generic extractor despite the name (tests:2603)
Assert 'bh-fixguard: catalog row is tier=important' ($bhRow.Tier -eq 'important') "tier=$($bhRow.Tier)"

# Drive the REAL row through the REAL Probe-Component, with the helper shadowed so the
# row's grading is exercised without touching the live installs.
$bhSavedPS = $script:PassStart; $bhSavedPBS = $script:ProbeBudgetStart
$bhSavedBud = $script:ProbeBudgetSec; $bhSavedSdl = $script:PassSoftDeadlineSec
$bhSavedSeam = $env:LM_FAKE_PASS_ELAPSED_SEC
$script:ProbeBudgetSec = 90; $script:PassSoftDeadlineSec = 330; $env:LM_FAKE_PASS_ELAPSED_SEC = '10'
$script:PassStart = (Get-Date); $script:ProbeBudgetStart = (Get-Date)

function Get-BrowserHarnessChromeRunningFixStatus { [pscustomobject]@{ state='healthy'; detail='test: fixed' } }
$bhEmitOk = Probe-Component -Name $bhRowName -Category 'risk' -Tier $bhRow.Tier -Test $bhRow.Test -Detail $bhRow.Detail
Assert 'bh-fixguard: row emits healthy when the helper says healthy' (
    ($bhEmitOk.state -eq 'healthy') -and ($bhEmitOk.detail -eq 'test: fixed')
) "state=$($bhEmitOk.state) detail=$($bhEmitOk.detail)"

function Get-BrowserHarnessChromeRunningFixStatus { [pscustomobject]@{ state='down'; detail='test: REVERTED' } }
$bhEmitDown = Probe-Component -Name $bhRowName -Category 'risk' -Tier $bhRow.Tier -Test $bhRow.Test -Detail $bhRow.Detail
Assert 'bh-fixguard: row emits down when the helper says down' (
    ($bhEmitDown.state -eq 'down') -and ($bhEmitDown.detail -eq 'test: REVERTED')
) "state=$($bhEmitDown.state) detail=$($bhEmitDown.detail)"

function Get-BrowserHarnessChromeRunningFixStatus { [pscustomobject]@{ state='unknown'; detail='test: cannot verify' } }
$bhEmitUnk = Probe-Component -Name $bhRowName -Category 'risk' -Tier $bhRow.Tier -Test $bhRow.Test -Detail $bhRow.Detail
Assert 'bh-fixguard: row emits unknown (not down) when the helper cannot determine' (
    $bhEmitUnk.state -eq 'unknown'
) "state=$($bhEmitUnk.state) detail=$($bhEmitUnk.detail)"

# The row must survive a budget-exhausted pass: its entire purpose is to not go silent,
# and a budget-skipped row emits 'unknown' with a fresh timestamp, which looks fine.
$script:ProbeBudgetStart = (Get-Date).AddSeconds(-100)
function Get-BrowserHarnessChromeRunningFixStatus { [pscustomobject]@{ state='down'; detail='test: REVERTED' } }
$bhEmitBudget = Probe-Component -Name $bhRowName -Category 'risk' -Tier $bhRow.Tier -Test $bhRow.Test -Detail $bhRow.Detail -AlwaysRun
Assert 'bh-fixguard: the row still reports DOWN on a 100s-over-budget pass (-AlwaysRun)' (
    $bhEmitBudget.state -eq 'down'
) "state=$($bhEmitBudget.state) detail=$($bhEmitBudget.detail)"

$script:PassStart = $bhSavedPS; $script:ProbeBudgetStart = $bhSavedPBS
$script:ProbeBudgetSec = $bhSavedBud; $script:PassSoftDeadlineSec = $bhSavedSdl
$env:LM_FAKE_PASS_ELAPSED_SEC = $bhSavedSeam

# RESTORE THE REAL FUNCTION. The three shadows above overwrite the AST-extracted
# definition in the script function table, and this file GROWS BY APPENDING sections
# immediately above the summary -- so without this, the next section anyone adds
# inherits a stub that always returns 'healthy'/'test: fixed' and its assertions are
# meaningless while still green. Re-extract from the same $ast the harness parsed.
$bhRealFn = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Get-BrowserHarnessChromeRunningFixStatus'
}, $true) | Select-Object -First 1
. ([scriptblock]::Create($bhRealFn.Extent.Text))
Assert 'bh-fixguard: the real helper is restored after shadowing (protects sections appended later)' (
    (Get-BrowserHarnessChromeRunningFixStatus -DevExe (Join-Path $bhRoot 'none.exe') `
        -DevSitePackages (Join-Path $bhRoot 'none') -DevSrcAdmin (Join-Path $bhRoot 'none\admin.py') `
        -UvToolAdmin (Join-Path $bhRoot 'none\admin.py')).state -eq 'unknown'
) 'shadow still in place -- the real function was not restored'

Remove-Item -LiteralPath $bhRoot -Recurse -Force -ErrorAction SilentlyContinue
```

Then add the row name to `$freezeDetectorRows` at `laptop-monitor.tests.ps1:3372-3375`, so the AST guard pins `-AlwaysRun` on the live catalog row:

```powershell
$freezeDetectorRows = @(
    'Hermes OAuth refresh age (warn >14d)',
    'Hermes Codex OAuth refresh failure (sentinel)',
    # Same class: a cheap, spawn-free detector whose ONLY job is to not go quiet.
    # Budget-skipping it would restore exactly the silence it exists to break.
    'browser-harness _chrome_running() fix present (vip-scan gate)'
)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: FAIL with `Probe-Component row not found in catalog: browser-harness _chrome_running() fix present (vip-scan gate)` (thrown by `Get-WslProbeRow`), plus a FAIL from the `$freezeDetectorRows` guard reporting `found=False hasAlwaysRun=False`.

- [ ] **Step 3: Write the implementation**

In `C:\Users\diego\laptop-monitor.ps1`, insert immediately before the `# End component catalog ---` comment (~line 12222), after the two SR-470 rows:

```powershell
# -- browser-harness _chrome_running() fix-revert guard (2026-08-13). 'down' ONLY when
#    the copy the cron entrypoints actually execute has lost the fix; a reverted
#    inactive fallback is a detail-line warning. -AlwaysRun because the row's whole
#    purpose is to not go silent, and it costs two file reads with no spawn.
$components += Probe-Component -Name 'browser-harness _chrome_running() fix present (vip-scan gate)' -Category 'risk' -Tier 'important' -AlwaysRun `
    -Test {
        switch ((Get-BrowserHarnessChromeRunningFixStatus).state) {
            'healthy' { $true }
            'unknown' { 'unknown' }
            default   { $false }
        }
    } `
    -Detail { (Get-BrowserHarnessChromeRunningFixStatus).detail }
```

- [ ] **Step 3b: Bump the catalog-size literal**

Added 2026-08-13 after implementation: adding a row makes the catalog bigger than
`$script:PassProbeTotal` (`laptop-monitor.ps1:9116`), a **manually-maintained literal**
used for the "N of \<total\> probed" truncation wording. Two pre-existing AST guards
(`passtotal` and the session-bridge wiring assert) fail until it matches. Bump it by one
(95 → 96) and add a dated history comment above it, newest on top, matching the
`# 97 -> 95 on 2026-08-13` entry already there — including its 35-space continuation
indent. The `passtotal` assert is ground truth for the count; do not guess it.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: every `bh-fixguard:` assert PASS, the `probe-budget: catalog row '...vip-scan gate...' carries -AlwaysRun` assert PASS, and only the one known pre-existing `Resolve-RealPython` failure.

- [ ] **Step 5: Confirm the live row is green against the real machine**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:USERPROFILE\laptop-monitor.ps1 -JsonOnly" 2>$null | ConvertFrom-Json | ForEach-Object { $_.components } | Where-Object { $_.name -like '*vip-scan gate*' } | Format-List name,state,detail
```

Expected: `state : healthy`, and a detail naming `active=dev-editable` and `no psutil (30s tasklist fallback...)` — both copies currently hold the fix and psutil is absent from the dev venv, so this is the correct green.

- [ ] **Step 6: Snapshot**

```bash
cp C:/Users/diego/laptop-monitor.ps1 C:/Users/diego/AppData/Local/Temp/claude/C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1/bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d/scratchpad/laptop-monitor.ps1.after-task3
```

---

### Task 4: End-to-end proof against the real artifact

The fixtures prove the grading. They do **not** prove the marker strings match the real file. On the editable-finder guard (2026-08-11) every fixture passed while the guard emitted misleading remediation on the one scenario the fixtures did not model. This task closes that gap.

**Files:**
- Create: scratch tree under the scratchpad (deleted at the end)
- Modify: none

**Interfaces:**
- Consumes: `Get-BrowserHarnessChromeRunningFixStatus` (Task 2), the live catalog row (Task 3).
- Produces: nothing consumed downstream.

- [ ] **Step 1: Build a scratch tree holding the REAL defective file**

Never touch the live installs. `admin.py.bak-pre-chromerunning-fix-20260813` is the genuine pre-fix upstream body.

```bash
SP="C:/Users/diego/AppData/Local/Temp/claude/C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1/bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d/scratchpad/bh-e2e"
UV="C:/Users/diego/AppData/Roaming/uv/tools/browser-harness/Lib/site-packages/browser_harness"
mkdir -p "$SP/dev/src/browser_harness" "$SP/dev/.venv/Scripts" "$SP/dev/.venv/Lib/site-packages" "$SP/uv/Lib/site-packages/browser_harness"
cp "$UV/admin.py.bak-pre-chromerunning-fix-20260813" "$SP/dev/src/browser_harness/admin.py"
cp "C:/Users/diego/Developer/browser-harness/src/browser_harness/admin.py" "$SP/uv/Lib/site-packages/browser_harness/admin.py"
echo "stub" > "$SP/dev/.venv/Scripts/browser-harness.exe"
printf '%s' "$SP/dev/src" | tr '/' '\\' > "$SP/dev/.venv/Lib/site-packages/__editable__.browser_harness-0.1.8.pth"
ls -la "$SP/dev/src/browser_harness/admin.py" "$SP/uv/Lib/site-packages/browser_harness/admin.py"
```

Expected: both files listed; the dev one ~45264 bytes (defective), the uv one ~48270 bytes (fixed).

- [ ] **Step 2: Confirm the guard fires DOWN on the real defective body**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -Command "
\$ErrorActionPreference='Stop'
\$t=\$null;\$e=\$null
\$ast=[System.Management.Automation.Language.Parser]::ParseFile(\"\$env:USERPROFILE\laptop-monitor.ps1\",[ref]\$t,[ref]\$e)
foreach(\$n in @('Get-BrowserHarnessAdminFileState','Get-BrowserHarnessChromeRunningFixStatus')){
  \$f=\$ast.FindAll({param(\$x) \$x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and \$x.Name -eq \$n},\$true)|Select-Object -First 1
  . ([scriptblock]::Create(\$f.Extent.Text))
}
\$r='C:\Users\diego\AppData\Local\Temp\claude\C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1\bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d\scratchpad\bh-e2e'
Get-BrowserHarnessChromeRunningFixStatus -DevExe \"\$r\dev\.venv\Scripts\browser-harness.exe\" -DevSitePackages \"\$r\dev\.venv\Lib\site-packages\" -DevSrcAdmin \"\$r\dev\src\browser_harness\admin.py\" -UvToolAdmin \"\$r\uv\Lib\site-packages\browser_harness\admin.py\" | Format-List state,activeSource,activeState,fallbackState,detail
"
```

Expected: `state : down`, `activeSource : dev-editable`, `activeState : reverted`, `fallbackState : ok`, and a detail naming the scratch path, `1a36129`, and the `.bak`. **If this prints `healthy`, the marker strings do not match the real file — stop and fix `$Markers`, do not adjust the test.**

- [ ] **Step 3: Confirm the inverse — the real FIXED body grades ok**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -Command "
\$t=\$null;\$e=\$null
\$ast=[System.Management.Automation.Language.Parser]::ParseFile(\"\$env:USERPROFILE\laptop-monitor.ps1\",[ref]\$t,[ref]\$e)
\$f=\$ast.FindAll({param(\$x) \$x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and \$x.Name -eq 'Get-BrowserHarnessAdminFileState'},\$true)|Select-Object -First 1
. ([scriptblock]::Create(\$f.Extent.Text))
'REAL fixed  -> ' + (Get-BrowserHarnessAdminFileState -Path 'C:\Users\diego\Developer\browser-harness\src\browser_harness\admin.py')
'REAL .bak   -> ' + (Get-BrowserHarnessAdminFileState -Path 'C:\Users\diego\AppData\Roaming\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py.bak-pre-chromerunning-fix-20260813')
'REAL uvtool -> ' + (Get-BrowserHarnessAdminFileState -Path 'C:\Users\diego\AppData\Roaming\uv\tools\browser-harness\Lib\site-packages\browser_harness\admin.py')
"
```

Expected exactly:

```
REAL fixed  -> ok
REAL .bak   -> reverted
REAL uvtool -> ok
```

- [ ] **Step 4: Destroy the scratch tree and confirm the live installs are untouched**

```bash
rm -rf "C:/Users/diego/AppData/Local/Temp/claude/C--Users-diego-Developer-browser-harness--claude-worktrees-modest-mclean-1344a1/bcbc72c2-6f40-41cb-b3ec-5c0fbfce603d/scratchpad/bh-e2e"
grep -c "_chrome_running_via_psutil" "C:/Users/diego/Developer/browser-harness/src/browser_harness/admin.py" "C:/Users/diego/AppData/Roaming/uv/tools/browser-harness/Lib/site-packages/browser_harness/admin.py"
git -C "C:/Users/diego/Developer/browser-harness" status --porcelain
```

Expected: both files report `2`; `git status` prints nothing (clean); the scratch tree is gone.

- [ ] **Step 5: Full suite, one more time, clean**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Expected: `0 failed`. Record the pass count in the completion report.

- [ ] **Step 6: Record the outcome**

Append a `## Verified YYYY-MM-DD` section to the spec (matching the editable-finder guard's format), stating: the marker strings match the real artifact, the guard fires `down` on the genuine pre-fix body, the live row is green, and the suite's pass/fail counts. Then:

```bash
cd C:/Users/diego/.hermes/agent-src && git add -f docs/superpowers/specs/2026-08-13-browser-harness-chrome-running-fix-guard-design.md docs/superpowers/plans/2026-08-13-browser-harness-chrome-running-fix-guard.md && git commit -m "docs: record verification of the browser-harness fix-revert guard"
```

If `pre-commit` fails with `Detect hardcoded secrets....Failed` while its body says `no leaks found`, that is the known line-ending auto-fix colliding with other sessions' unstaged changes — simply re-run the same `git commit`. If it fails with `Unable to create '.git/index.lock'`, check whether a *real* writer holds it (`Get-CimInstance Win32_Process -Filter "Name='git.exe'"`) before removing anything.

---

## Notes for the implementer

**Do not add a self-healing hook.** It was considered and rejected in the spec: re-applying the patch automatically would mask that the fix is still un-upstreamed, which is the actual root problem.

**If the fix ever lands upstream**, delete this guard rather than maintaining it.

**The suite has a run lease.** It is not concurrency-safe with itself and many agent sessions run it. `-NoLease` (used throughout this plan) bypasses the lease and prints a banner; `-ForceRun` is the other documented override. Use `-SkipLive -SkipDocker` to avoid binding real ports and restarting the Docker canary.

**Do not `git init` `C:\Users\diego`.** The monitor scripts are deliberately untracked there.
