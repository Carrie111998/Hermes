# laptop-monitor persistent-`unknown` blindness alarm — design

Date: 2026-08-13
Status: designed, not implemented
Author: agent session (Claude Code), from Diego's brief
Touches: `C:\Users\diego\laptop-monitor.ps1`, `C:\Users\diego\laptop-monitor.tests.ps1`

Follows from `2026-08-13-browser-harness-chrome-running-fix-guard-design.md` (its DEFERRED
section) and `2026-07-27-laptop-monitor-tail-starvation-design.md`, whose
`Get-ProbeStarvationAlarm` is the model for shape and placement here.

## Problem

A `Probe-Component` row that answers `unknown` produces **zero alert**, and nothing notices
it is stuck there. Two mechanisms combine, each individually correct:

1. `Get-ObservedStateTransitions` (`laptop-monitor.ps1` ~:12596) deliberately raises no edge
   **into or out of** `unknown`. That is right and load-bearing: `unknown` means "not checked
   this tick," not "broken." Measured on the live transitions log, 36120 of 47931 recorded
   transitions (75.4%) involved `unknown` — the alert channel ran ~3:1 noise to signal until
   this rule landed.
2. `Probe-Component` returns `probed = $true` on the **genuine-unknown** path (~:8673 — every
   path that actually ran `& $Test`, whatever it answered). So `Get-CarriedProbeStamps` stamps
   the row fresh every pass and `Get-ProbeStarvationAlarm` never fires either.

Net effect: a row whose `-Test` keeps returning the string `'unknown'` shows only as `[??]`
DarkGray in a console table nobody watches on a scheduled run. It never reaches `status.json`
consumers as a problem.

### Why it matters now

The row `browser-harness _chrome_running() fix present (vip-scan gate)` (~:12390) was added
precisely because its failure mode is SILENCE — a reverted fix silently blocks all sentinel-vip
scans. That row has four paths to `unknown`: the active `admin.py` missing, unreadable, an
unparseable `.pth`, or a relocated install. Each one means the guard has gone blind, and today
none of them says so.

**A guard that can silently stop guarding has the same defect it was built to detect.**

## Non-goals

These are excluded by design, not by omission:

- **`unknown` must not alert directly, and must not redden.** That would break the
  never-condemn-on-no-evidence rule and reintroduce the flap noise `Get-ObservedStateTransitions`
  was written to kill. The pinned test `'bh-fixguard: active copy missing -> unknown, not down'`
  exists to stop exactly that, and must still pass unchanged.
- **No change to per-row grading.** Every catalog row keeps the state it has today.
  `status.json` keeps showing the honest `[??]` for a row that could not verify.
- **No per-row detail annotation.** Considered (it was one of the two suggested directions) and
  dropped: the alarm's detail already names the worst row and its streak, so an annotation would
  be a second copy of the same fact reaching no channel the alarm does not already reach.
- **No exemption list.** A row that is legitimately blind forever — a retired canary left in the
  catalog — will alarm permanently. That is the intended behaviour: the remedy is to remove the
  row, not to teach the alarm to ignore blindness.
- **No widening of the probe budget, and no change to the 90 < 210 < 330 < 480 chain.**

## The distinction this rests on: two kinds of genuine `unknown`

The design finding that shapes everything below. `probed = $true` + `state = 'unknown'` is **not
one thing**. There are two structurally different producers, and only one of them is blindness:

| producer | means | accountable elsewhere? |
|---|---|---|
| a row's own `-Test` answering `'unknown'` — bh-fixguard, cloudflared version drift, the SR-470 conformance canaries, agent-src code drift | **this row cannot see** | no — terminal |
| `RequiresDocker` dependency deferral (~:8510); `Invoke-BudgetGatedCachedCheck` budget-closed paths (~:1167-1188) | "didn't ask; upstream is down or the budget is spent" | yes — Docker's own row is already red |

Live evidence, `status.json` 2026-08-13 (101 rows): exactly one genuine `unknown`, and it is the
second kind — `Task Scheduler 329 kills (24h, R61-class)`, detail *"deferred: probe budget spent
and no usable cached verdict (no spawn attempted)."*

Without this distinction a catalog-wide streak counter would count that row on every loaded pass,
and a Docker-off overnight would count ~12 rows at once. The alarm would spend most of its life
restating things that already have a red row — and this file's own rule applies: *"a row that
reddens for a working system gets ignored before the day it is right"* (~:8064).

**The vocabulary already exists.** `Invoke-BudgetGatedCachedCheck` already emits
`deferred = $true` on all three of its budget-closed return paths. The signal is produced, named,
and then discarded at the `-Test` boundary, because `-Test` can only say `$true` / `$false` /
`'unknown'`. This design carries it the last two inches.

## Architecture

Three parts, each a strict sibling of machinery already in the file.

### 1. `'deferred'` — a fourth `-Test` verdict

`Probe-Component`'s grader (~:8646) gains one branch:

```
$result = & $Test
'unknown'   -> state='unknown',  deferred=$false   # I ran, and I cannot see
'deferred'  -> state='unknown',  deferred=$true    # I did not ask; something else owns this
truthy      -> state='healthy'
falsy       -> state='down'
throw       -> state='error'
```

This is the same move the file already made when it introduced `probed`, for the same reason:
*"`state` alone cannot distinguish 'not probed' from 'probed, answer unknown'"* (~:8270). Now
`probed` alone cannot distinguish *"I cannot see"* from *"I didn't ask."*

`deferred` is emitted on every `Probe-Component` return path (`$false` on the budget-skip path
and the main path; `$true` on the `RequiresDocker` deferral at ~:8510).

Producers of the new verdict, and **only** these:

- The `RequiresDocker` deferral object gains `deferred = $true` directly — it is inside
  `Probe-Component`, so it needs no `-Test` round trip.
- The four `Invoke-BudgetGatedCachedCheck` call sites — `manifest-model-path` ~:10310,
  `pg5432topo` ~:11546, `taskkill329` ~:11589, `clickhouse-mem` ~:11862 — change their verdict
  tail from `default { 'unknown' }` to
  `default { if ($r.deferred) { 'deferred' } else { 'unknown' } }`. All four carry the identical
  `switch ($r.class) { 'good' {…} 'bad' {…} default { 'unknown' } }` shape today (verified
  2026-08-13), so the edit is uniform. Note the in-file comment at the `-AlwaysRun` block calls
  these "the three formerly budget-skipped rows" — it predates `manifest-model-path`; there are
  four.
  Budget-**open** verdicts come from `Invoke-CachedDeepCheck`, whose objects carry no `deferred`
  property, so `$r.deferred` reads `$null` → falsy → `'unknown'`. Correct by default: a check
  that actually ran and was inconclusive is blindness, not deferral.

Everything else is untouched. `deferred` is read defensively downstream —
`try { [bool]$c.deferred } catch { $false }` — exactly as `Get-CarriedProbeStamps` already reads
`probed`, so the session-bridge rows, the two alarm rows, the low-RAM defer marker and the
truncation payload need no edits.

**Collision check:** no `-Test` in the file returns the literal string `'deferred'` today
(verified 2026-08-13). Note that today an unrecognised non-empty string falls through to
`elseif ($result)` and grades **healthy** — so before this change a row returning `'deferred'`
would have been silently green. Nothing does, and after this change the string is claimed.

**The bh-fixguard row needs no change.** All four of its blind paths route through
`Get-BrowserHarnessChromeRunningFixStatus` → `'unknown' { 'unknown' }`, which is genuine
blindness by construction.

### 2. Streak persistence — a strict sibling of the `lastProbedAt` trio

New script-scope map beside `$script:ProbeLastProbedAt` (~:8299):

```powershell
$script:ProbeUnknownStreak        = @{}   # name -> [int] consecutive passes answered genuinely unknown
$script:ProbeBlindnessAlarmPasses = 300   # N: streak at which the catalog-level alarm fires
```

**`Get-PriorUnknownStreaks -PriorComponents -`** — LOAD side, called at ~:9814 alongside
`Get-PriorProbeStamps`, from the same already-parsed `$StateFile` object. Reads `unknownStreak`
off each row. Absent, blank, unparseable, or negative → `0`.

Every state file written before this lands has no such field, so the first pass after it lands
starts the whole catalog at zero and **structurally cannot storm** — the same discipline as the
`Get-PriorProbeStamps` seed, arrived at the same way.

**`Get-CarriedUnknownStreaks -Prior -Components`** — WRITE side, folds this pass's rows into the
prior map:

| this pass | streak |
|---|---|
| affirmative verdict — `healthy` / `down` / `error` | **reset to 0** |
| genuine unknown (`probed=$true`, `deferred` falsy) | **prior + 1** |
| deferred unknown (`probed=$true`, `deferred=$true`) | **carry unchanged** |
| budget-skipped (`probed=$false`) | **carry unchanged** |
| row not present in `$Prior` | treated as prior 0, then the rule applies |

**Only an affirmative verdict resets.** A deferral carries rather than resets, so an
intermittently-deferring row cannot launder away real blindness — and carrying is the rule
`state` and `lastProbedAt` already apply to "didn't ask."

A row present in `$Prior` but absent from `$Components` keeps its entry, matching
`Get-CarriedProbeStamps`, which copies the whole prior map before folding. Such orphans are inert:
the alarm iterates `$Components` and looks streaks up, so a removed row's entry is never read.

Persisted at ~:12658 as a fourth field on each state row, beside `name` / `tier` / `state` /
`lastProbedAt`:

```powershell
[pscustomobject]@{ name=…; tier=…; state=$st; lastProbedAt=$lp; unknownStreak=$us }
```

**Consequence, and it is the correct one** (inherited from `Get-CarriedProbeStamps`): the low-RAM
defer and the soft-deadline truncation both exit BEFORE the `$StateFile` write, so a deferred or
truncated tick neither advances nor resets any streak. Nothing was asked, so nothing is counted —
in either direction.

### 3. `Get-ProbeBlindnessAlarm` — the catalog-level row

Modelled on `Get-ProbeStarvationAlarm` (~:8402), appended in the same block (~:12409),
immediately after it, and computed from the streak map this pass **would** persist — so a row
that answered this tick reads 0 and the alarm reports catalog state *after* the pass had its
chance, not before. Appended after the catalog, so it is never itself subject to the probe budget.

```
name     'Probe blindness (rows stuck unknown across passes)'
category 'harness'
tier     'important'     # reaches the alert channel WITHOUT reddening the tray headline
probed   $true
deferred $false
state    'down'    when any row's streak >= $script:ProbeBlindnessAlarmPasses
         'healthy' otherwise
```

`>=` at the threshold matches `Test-ProbeStarved`'s convention.

- `down` detail: the count of rows at or past N, the **worst** row by name and its streak, and
  the remediation framing — this is a row that has stopped grading, not a service that is down.
- `healthy` detail: the worst streak currently seen, so the margin is visible **before** it fires.
- Excludes itself by `$RowName`, as `Get-ProbeStarvationAlarm` does.
- The starvation alarm row is in `$Components` when this runs (it is appended first) and is
  harmless: it is only ever `healthy` or `down`, never `unknown`, so its streak stays 0.

#### Threshold: N = 300 passes

`LaptopMonitor-Prober` repeats at `PT1M`; the measured effective rate is 786 passes in 24h
(~1.8 min/pass, the gap being `IgnoreNew` plus pass duration). So **N = 300 ≈ 9h of real
observation.**

Sizing constraint: `Get-CloudflaredVersionStatus` caches for 6h, so an offline afternoon
legitimately holds that row `unknown` for ~240 passes. N must clear a plausible offline stretch —
300 clears a full offline workday and fires overnight.

**Why a count and not wall-clock hours.** A stamp-based `lastVerdictAt` would have been a more
literal mirror of `lastProbedAt`, but it inherits a false-positive mode: laptop suspended for
three days → first pass back, the row is still `unknown` → the stamp is instantly 72h old → the
alarm fires on **one** observation. A count measures the evidence directly — *we asked N times and
got no answer N times* — and is immune to power-off, suspend and idle. It also needs no
`ConvertTo-ProbeStamp` parse, no future-stamp refusal, and no negative-age guard, so it is
strictly simpler than the machinery it sits beside. (Clock-skew hazards on this box are real; see
`feedback_resumed_session_stale_time_reads`.)

The cost is that N is cadence-coupled: if `PT1M` changes, N must be re-tuned. It is a documented
`$script:` tunable next to the starvation constants for exactly that reason.

#### Row identity is deliberately number-free

`Get-ProbeStarvationAlarm`'s row is named `'Probe starvation (tail rows unprobed >24h)'` — the
threshold is baked into the identity string, and that string is the key in `$StateFile` and in the
transitions log. Tuning `M` there would orphan the row's history and re-fire it.

This row is named **`'Probe blindness (rows stuck unknown across passes)'`** — no number — so N
stays tunable without breaking row identity. Not proposing to rename the existing row; just not
repeating the pattern.

## What alerts, and what deliberately still does not

The alarm row is `tier='important'` with a real `healthy -> down` edge, so
`Get-ObservedStateTransitions` raises it, `laptop-monitor-transitions.log` records it, and
BurntToast fires. It also lands in `status.json` as a `down` row for programmatic consumers.

**Nothing about `unknown` handling changes.** Rule 1 (never raise an edge into or out of
`unknown`) and rule 2 (carry the last observed state forward as the baseline) are untouched. The
blind row itself stays gray in the tray and raises no transition. A **separate** row goes red and
names it.

Expected behaviour for the motivating case: `admin.py` goes missing → the bh-fixguard row reads
`[??]` exactly as it does today → ~9h later `Probe blindness` goes `down` with detail naming
`browser-harness _chrome_running() fix present (vip-scan gate)` at 300 passes → toast + transitions
log + `status.json`.

## Testing

`laptop-monitor.tests.ps1` is **not Pester**: a custom `Assert -Name -Condition -Detail` harness
that AST-**extracts** function definitions from `laptop-monitor.ps1` rather than dot-sourcing it
(the monitor runs its whole catalog at top level). Two consequences bind this work:

- Helper data lives in `param()` defaults, never top-level `$script:` variables.
- Every function called by an extracted function MUST be listed in `$required` (~:677) or it
  throws `CommandNotFoundException` and **hard-exits the whole suite**.

New `$required` entries: `Get-PriorUnknownStreaks`, `Get-CarriedUnknownStreaks`,
`Get-ProbeBlindnessAlarm`. All three are pure and call nothing new. `Probe-Component` is already
extracted (~:552), so the `'deferred'` mapping is directly unit-testable rather than
static-asserted.

New sections are appended immediately above the `# Colour is computed INLINE` comment block near
the end of the file.

### Cases

**Verdict contract**
- `-Test` returning `'unknown'` → `state='unknown'`, `deferred=$false`, `probed=$true`
- `-Test` returning `'deferred'` → `state='unknown'`, `deferred=$true`, `probed=$true`
- `-Test` returning `$true` / `$false` / throwing → healthy / down / error, `deferred=$false`
- the budget-skip path still returns `probed=$false` and is not marked deferred
- static AST assert: the `RequiresDocker` deferral object carries `deferred = $true`
- static AST assert: no `-Test` in the catalog returns the bare string `'deferred'` other than
  through an `$r.deferred` guard (guards the collision noted above)

**`Get-PriorUnknownStreaks`**
- a state file with no `unknownStreak` field on any row → every streak 0 (the legacy-file case;
  this is the anti-storm guarantee)
- blank / non-numeric / negative values → 0
- valid integers round-trip

**`Get-CarriedUnknownStreaks`** — one case per row of the table in §2
- healthy / down / error each reset a nonzero prior to 0
- genuine unknown increments
- deferred unknown carries unchanged (nonzero prior stays, does **not** reset)
- `probed=$false` carries unchanged
- a row absent from `$Prior` answering genuine unknown lands at 1, not 0
- a row absent from `$Prior` answering healthy lands at 0
- a prior entry whose row left the catalog is retained and inert

**`Get-ProbeBlindnessAlarm`**
- fires `down` at exactly N; `healthy` at N-1 (the `>=` boundary)
- names the worst row and its streak in the `down` detail
- reports the worst streak in the `healthy` detail
- excludes its own row by name
- an empty catalog / empty streak map → `healthy`, no crash
- garbage and negative streak values are treated as 0, never as "infinitely blind"

**Integration and regression**
- a bh-fixguard-shaped row driven through 300 simulated passes of genuine `unknown` takes the
  alarm from `healthy` to `down`; injecting one `healthy` verdict at pass 299 resets it to 0
- a Docker-deferred row driven through 300 passes leaves the alarm `healthy` (the whole point of
  §1)
- regression pin: a blind row's own state stays `unknown`, and `Get-ObservedStateTransitions`
  raises no transition for it
- the existing `'bh-fixguard: active copy missing -> unknown, not down'` test still passes,
  unmodified

### Baseline

`C:\Users\diego` is deliberately **not** a git repo — do not run `git init` there.

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\laptop-monitor.tests.ps1 -SkipLive -SkipDocker -NoLease
```

Baseline as of 2026-08-13: **1923 passed, 1 failed, 9 skipped.** The single failure is a
pre-existing `Resolve-RealPython` extraction gap, unrelated to this work. Done means: the new
sections pass, and the failure count is still exactly 1 and still that one.

## References

- `C:\Users\diego\laptop-monitor.ps1` — `Get-ObservedStateTransitions` ~:12555,
  `Probe-Component` ~:8460, `Get-ProbeStarvationAlarm` ~:8402, `Get-CarriedProbeStamps` ~:8368,
  `Get-PriorProbeStamps` ~:8343, `Invoke-BudgetGatedCachedCheck` ~:1110,
  `Get-BrowserHarnessChromeRunningFixStatus` ~:8051, the bh-fixguard catalog row ~:12390,
  the starvation-alarm append block ~:12409, the `$StateFile` write ~:12652
- `docs/superpowers/specs/2026-07-27-laptop-monitor-tail-starvation-design.md` — the adjacent
  problem (rows not probed **at all**) and the model for this alarm's shape and placement
- `docs/superpowers/specs/2026-08-13-browser-harness-chrome-running-fix-guard-design.md` — the
  guard whose silence motivated this; see its DEFERRED section
- MemPalace drawer `browser-harness/fix-revert-guard-2026-08-13`

## Verified 2026-08-13

Facts checked against the live box before this design was written, not assumed:

- `Invoke-BudgetGatedCachedCheck` already emits `deferred = $true` on all three of its
  budget-closed return paths (~:1167, ~:1174, ~:1183). The signal exists and is already named;
  only the `-Test` boundary discards it.
- All four of its call sites currently end in the identical
  `switch ($r.class) { … default { 'unknown' } }` tail, so §1's edit is one uniform change
  repeated four times, not four distinct ones.
- No `-Test` block in `laptop-monitor.ps1` returns the literal string `'deferred'`.
- `Probe-Component` is present in the tests' `$required` array (`laptop-monitor.tests.ps1:552`),
  so the new verdict branch is unit-testable, not static-assert-only.
- `LaptopMonitor-Prober` trigger repetition interval is `PT1M`; the 24h measured rate quoted in
  the file is 786 passes, i.e. ~1.8 min/pass.
- Live `status.json` holds 101 component rows, of which exactly one is a genuine
  (`probed=$true`) `unknown`: `Task Scheduler 329 kills (24h, R61-class)`, and it is a
  budget-deferral — the case §1 exists to exclude.
- The bh-fixguard row's four blind paths all originate in
  `Get-BrowserHarnessChromeRunningFixStatus`'s `'unreadable'` and `default` branches (~:8154,
  ~:8158), both of which set `state = 'unknown'` with no deferral semantics.
