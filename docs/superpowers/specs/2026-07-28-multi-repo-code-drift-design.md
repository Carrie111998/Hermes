# Multi-repo CODE_DRIFT detection — design

**Date:** 2026-07-28
**Status:** IMPLEMENTED — but not by this document's names. Read the AS-BUILT
section before treating any identifier below as real.
**Supersedes nothing.** Extends `2026-07-21-code-drift-event-producer-design.md`.

## AS BUILT (reconciled 2026-07-28)

The design was implemented **independently** on `main` as `a719fc7dd`, in parallel
with the branch that carried this spec. The *architecture* below landed intact —
`WatchedRepo`, per-repo trunk refs, present-but-unevaluable alerts instead of
silence, one monitor and one state file per repo, executed-dir alert gating. Several
**names** did not. This section is authoritative where the body disagrees; the body
is kept as the design record, not as an API reference.

| Concern | This spec says | Landed as | Why |
|---|---|---|---|
| Repo key | `hermes-home` | **`hermes`** | Incumbent and deployed. `agent-src` is the other key, so `hermes` is already unambiguous in operator-facing text (`code drift [hermes]`). |
| `WatchedRepo` field | `key` + `label` | **`name`** + `trunk_name` property | `label` was computed but never rendered; `trunk_name` (derived, `main`/`master`) is what remediation text actually needs. |
| State file | `code_drift_state_hermes_home.json` | **`code_drift_state.hermes.json`** | `code_drift_state_path(repo_name)` slugs on `.`; agent-src keeps the un-suffixed legacy filename so its in-flight episode state survived the cutover. |
| Unevaluable repo | `state="trunk_missing"`, HEAD-unresolvable → `None` | **`state="misconfigured"` for BOTH**, plus a `detail` string | Adjudicated below. |
| `DriftSample.alertable` | `alertable` | **`alerts`** | Rename only; the load-bearing "never gate an unevaluable repo into silence" clause landed verbatim in spirit (see `DriftSample.alerts`). |
| `DriftSample.main` | `main` | **`trunk`** | The field holds whatever trunk the repo has; calling it `main` re-imports the hardcoded-`main` assumption this spec exists to remove. |
| `branch` field | included, emitted | **dropped** | It was computed but never reached a payload — dead weight. |

### Adjudication: `trunk_missing` vs `misconfigured`

**`misconfigured` wins, covering both causes.** This spec routed an unresolvable HEAD
to `None` on the reasoning that "if HEAD resolved, git is demonstrably fine, so only a
missing trunk is a config error." The inverse does not hold: a repo that is *present*
(`.git` exists) but whose HEAD will not resolve is equally unevaluable, and returning
`None` there recreates exactly the fail-silent shape this spec was written to kill —
"nothing to evaluate" is indistinguishable from "clean" at every downstream surface.

The rule that survived is simpler and has no seam to get wrong:

> **Absent repo (no `.git`) = skip. Present repo we cannot evaluate = shout.**

Cause is preserved as data rather than as a second state: `DriftSample.detail` carries
`MISCONFIG_TRUNK_UNRESOLVED` or `MISCONFIG_HEAD_UNRESOLVED`, named constants precisely
so `events_doctor` can branch on which one it got — the trunk case earns a "fix the
watched-repo entry's `trunk_ref`" remediation line and the HEAD case must not, since
that would be wrong advice. One state, two details beats two states.

### Section 9 (`events_doctor` unification) — implemented 2026-07-28

`a719fc7dd` parameterised the doctor and shared `watched_repos()`, but left the
duplicate `_git` probe in place. That duplicate is now **gone**: `check_code_drift()`
renders `sample_code_drift()` and no longer touches git itself. Deltas from §9 as
written:

- Trunk ref is an explicit `trunk_ref` parameter, **not** inferred from the registry
  by path — `run_doctor()` already loops `watched_repos()` and passes each repo's own
  ref, so inference would be a second source of truth for the thing this spec made
  data.
- The doctor passes `executed_dirs=()`. Per Diego, executed-dir gating is deliberately
  **not** adopted here: a diagnostic run on purpose reports every divergence. Sharing
  the probe does not mean sharing the alert policy — the gate is a parameter, so this
  surface declines it. Pinned by `test_doctor_asks_the_probe_for_an_UNGATED_sample`.
- "The doctor must stay a light import" was considered and rejected as a blocker:
  measured, `events.producers.code_drift_monitor` imports in ~312 ms, comparable to
  `events.paths` (~350 ms), which the doctor already imports.

## Problem

`~/.hermes` has ZERO drift detection despite its working tree being production.

`events/producers/code_drift_monitor.py` hardcodes
`_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"` and resolves drift as
`HEAD` vs `refs/heads/main`. On an unresolvable ref it returns `None`, documented as
"nothing to evaluate".

The `~/.hermes` repo's trunk is `master` and it has **no `main` branch at all**
(verified 2026-07-28: 100 refs under `refs/heads/`, none named `main`). Pointing the
current monitor at it would resolve `refs/heads/main` with `rc != 0` and silently
return `None` — reporting clean forever. It is a **fail-silent** path, which is the
worst failure mode a detector can have.

`hermes_cli/events_doctor.py::check_code_drift()` carries a near-duplicate copy of the
same probe (`_git`, `_agent_src_root`, hardcoded `refs/heads/main`) and has the
identical blind spot, degrading an unresolvable ref to a skip note.

### Why it matters

Cron script-slot jobs and Windows Scheduled Tasks resolve absolute paths under
`~/.hermes/scripts/`, `~/.hermes/profiles/*/scripts/` and `~/.hermes/ops/`. That
working tree **is** the deployment surface. On 2026-07-28 it was found 62 commits
behind `master` with a CLEAN `git status` — it sat on `feat/manifest-router`, whose own
HEAD it matched, so every existing check read green.

### Current state (2026-07-28)

The specific incident is already remediated — `~/.hermes` HEAD is on `master`, 0 behind.
This work builds the detector for the recurrence, not a live fire. Live episode state
(`~/.hermes/notifications/code_drift_state.json`) is quiescent (`alerting: false`), so
rollout cannot produce an alert storm.

## Design

### 1. `WatchedRepo` — trunk ref becomes data

```python
@dataclass(frozen=True)
class WatchedRepo:
    key: str                              # state-file identity + payload field
    path: Path
    trunk_ref: str                        # "refs/heads/main" | "refs/heads/master"
    label: str                            # "~/.hermes/agent-src" | "~/.hermes"
    executed_dirs: Tuple[str, ...] = ()   # git pathspecs; empty = no gating
```

`watched_repos() -> List[WatchedRepo]` returns both entries:

| key | path resolver | trunk_ref | executed_dirs |
|---|---|---|---|
| `agent-src` | `HERMES_AGENT_SRC` env or `~/.hermes/agent-src` | `refs/heads/main` | *(none)* |
| `hermes-home` | `events.paths.get_default_hermes_root()` | `refs/heads/master` | `scripts`, `ops`, `profiles/*/scripts` |

`~/.hermes` resolves through the existing blessed resolver rather than a new env var.
Per CLAUDE.md, `get_default_hermes_root()` returns an `HERMES_HOME` pointing outside
`~/.hermes` **as-is** — which is precisely what keeps `tests/events` hermetic under
pytest's per-test tempdir. Do not substitute `hermes_constants.get_hermes_home()`;
that resolves profile-scoped.

`executed_dirs` ship as **git pathspecs**, so `profiles/*/scripts` is matched by git.
A newly created profile is covered automatically; no filesystem enumeration. Args are
passed as a list to `subprocess.run` (no shell), so the wildcard reaches git unexpanded.

Git's default pathspec wildcard semantics are subtle — a bare `profiles/*/scripts` is
**not** guaranteed to match `profiles/main/scripts/foo.ps1`, because `*` matches `/` by
default and the trailing literal must still align. The exact pathspec form (bare glob,
`:(glob)profiles/*/scripts/**`, or an explicitly enumerated list) is **pinned
empirically by test 8**, not assumed. Whichever form passes is the one that ships.

### 2. Fail-loud, discriminated from transient

The core correction. Two probe failures that look alike must not behave alike:

| Probe result | Meaning | Behavior |
|---|---|---|
| `HEAD` unresolvable | git broken, empty repo, transient | `None` → **no-op** (unchanged) |
| `HEAD` ok, trunk unresolvable | wrong trunk name, branch deleted | `state="trunk_missing"` → **alerts** |

If `HEAD` resolves, the repo and the git binary are demonstrably fine, so an
unresolvable trunk is a configuration/topology error and never a transient. Genuine
transients keep returning `None`, preserving the invariant that the poll loop never
fabricates drift or recovery.

`trunk_missing` rides the existing edge machine unchanged (rising edge, 6 h re-ping,
falling-edge resolve) and emits with `status="warn"`, tags `["code", "drift",
"trunk_missing"]`.

### 3. Executed-dir gating

For `~/.hermes` the useful signal differs from agent-src's: the checkout may sit on a
long-lived feature branch by design, and a branch 62 commits behind may still have
byte-identical scripts. Alerting is therefore gated on whether the **deployment
surface** actually differs:

```
git diff --name-only HEAD..<trunk_ref> -- <executed_dirs...>
```

An empty result means nothing stale is running, regardless of `behind_count`.

`evaluate()` changes by exactly one line — `if sample.state == "in_sync":` becomes
`if not sample.alertable:` — backed by a derived property:

```python
@property
def alertable(self) -> bool:
    if self.state == "in_sync":
        return False
    if self.state == "trunk_missing":
        return True          # config error — loud even if no files differ
    if self.executed_gated and not self.executed_changed:
        return False         # commits drifted; deployment surface identical
    return True
```

**The `trunk_missing` clause is load-bearing.** Without it, `trunk_missing` on a gated
repo has an empty changed-file set, falls through to `False`, and silently re-creates
the exact blind spot this work exists to close. It must be tested directly.

**Accepted trade-off:** a `~/.hermes` checkout 62 commits behind with byte-identical
scripts stays silent on the phone and surfaces only in `events_doctor`. This is
deliberate — `~/.hermes` accrues docs/notes commits constantly, and an alert that fires
on those gets tuned out. `behind_count` still rides in the payload as context.

### 4. `~/.hermes`-appropriate semantics

`branch` is read from `rev-parse --abbrev-ref HEAD` (`"HEAD"` when detached). Bodies
read *"on `feat/manifest-router`, behind `master` by 62"* rather than assuming
detached-HEAD-vs-main semantics.

`~/.hermes` has a permanently dirty working tree (`ops/`, `profiles/` modified as of
2026-07-28). `dirty` is payload-only and is **not** part of `shape`, so it neither
triggers nor suppresses an alert. No change needed; noted so a future reader does not
"fix" it.

### 5. `DriftSample` additions

All new fields defaulted, so existing constructions remain valid:

- `trunk_ref: str = "refs/heads/main"`
- `branch: str = ""`
- `executed_changed: Tuple[str, ...] = ()`
- `executed_gated: bool = False` — set by the sampler to
  `bool(watched_repo.executed_dirs)`. It is a property of the *repo config*, not of the
  sample: a gated repo whose executed dirs happen to be unchanged still reports
  `executed_gated=True` with an empty `executed_changed`. Conflating the two would make
  "gated and clean" indistinguishable from "not gated", which is the case that must
  stay silent versus the case that must alert.

`shape` stays `[state, behind_count, ahead_count]`. `trunk_missing` yields
`["trunk_missing", 0, 0]`.

### 6. Instance model — one monitor per repo

`CodeDriftMonitor` stays single-repo and single-episode. `evaluate()`'s signature and
edge logic are untouched, so the edge core remains a pure function of
`(sample, now) + persisted state` and stays testable without git, sleeps, or live
`~/.hermes` I/O — the property the brief asked to preserve.

A thin `build_monitors(bus) -> List[CodeDriftMonitor]` factory constructs one per
`WatchedRepo`. Each gets its own state file and its own independent 15-min probe gate.

### 7. State — zero migration

`agent-src` keeps `code_drift_state.json` and its existing flat schema, byte-untouched.
`hermes-home` gets a sibling `code_drift_state_hermes_home.json`. No migration code, no
schema version, no risk of a spurious ping from a reshaped file.

`code_drift_state_path(key: Optional[str] = None)` returns the legacy
`code_drift_state.json` when `key` is `None` or `"agent-src"`. Otherwise it returns
`code_drift_state_<key with '-' replaced by '_'>.json` — so `"hermes-home"` yields
`code_drift_state_hermes_home.json`. Both live in `notifications_home()`, unchanged.

### 8. Gateway wiring

`_code_drift_monitors: List[CodeDriftMonitor]`; the poll loop iterates and calls
`check()` per repo inside the existing try/except. `get_code_drift_monitor()` is
retained (returns the agent-src monitor) for back-compat; `get_code_drift_monitors()`
is added.

### 9. `events_doctor` unification

`check_code_drift()` drops its duplicate `_git` / `_agent_src_root` and consumes
`sample_code_drift()` + `watched_repos()`, rendering every watched repo. This removes
the second implementation, so the blind spot cannot be re-introduced in one surface and
not the other.

- `check_code_drift(repo_path: Optional[Path] = None) -> int` **signature is preserved**
  — 8 existing tests pin it. When `repo_path` is passed, only that repo is checked
  (trunk ref inferred from the registry by path, defaulting to `refs/heads/main`).
- `trunk_missing` becomes a real `FAIL` (returns 1) instead of today's silent skip.
- A missing repo still degrades to a skip note, so the doctor stays usable on boxes
  without the shared checkout.

### 10. Formatting

`events/formatting.py::code_drift_body()` gains a `status="warn"` / `trunk_missing`
branch and includes branch context plus the changed executed files. `repo` is already
in the payload; add `key` and `branch`.

## Testing

TDD throughout — test first, watch it fail, then implement.

**Edge core (existing, must pass unmodified):** all 20 tests in
`tests/events/producers/test_code_drift_monitor.py`. `executed_gated` defaults `False`,
so `behind(3).alertable` is `True` and `in_sync().alertable` is `False`.

**New sampler tests** (throwaway `tmp_path` repos, real git):

1. `master`-trunk repo, HEAD behind by N → `state="behind"`, `behind_count=N`
2. Repo with no `main` and HEAD resolvable → `state="trunk_missing"`, **not** `None`
3. Repo with unresolvable HEAD (freshly `git init`, no commits) → `None`
4. Missing repo → `None` (existing behavior)
5. `branch` is the branch name when attached, `"HEAD"` when detached
6. Gated repo, drift touching an executed dir → `executed_changed` non-empty
7. Gated repo, drift touching only non-executed paths → `executed_changed` empty
8. `profiles/*/scripts` pathspec matches a profile dir created after config

**New `alertable` tests:**

9. `trunk_missing` + `executed_gated=True` + empty `executed_changed` → `True`
   *(the load-bearing regression test)*
10. `behind` + `executed_gated=True` + empty `executed_changed` → `False`
11. `behind` + `executed_gated=True` + non-empty `executed_changed` → `True`
12. Gated-silent → gated-alerting transition fires a rising edge

**Rewritten wiring tests:** the 3 in `tests/events/test_gateway_integration.py` assert
on **literal source text** (`"_code_drift_monitor = CodeDriftMonitor(_bus)" in src`) and
must be updated to match the list form.

**Doctor tests:** the 8 existing tests must pass unchanged; add one for `trunk_missing`
returning 1 and one for both repos rendering.

## Non-goals

- The monitor never fast-forwards. Remediation stays a deliberate operator action.
- No new env var for the `~/.hermes` path.
- No change to `EventType.CODE_DRIFT` routing (`watchdog_alerts`) or priority.
- No change to `~/laptop-monitor.ps1`. Its bounded git probes are a separate surface;
  extending them is follow-up work, not part of this spec.

## Risk

- **Alert storm on rollout:** ruled out — `~/.hermes` is currently in sync and episode
  state is quiescent.
- **Doctor exit-code change:** `trunk_missing` now returns 1 where it returned 0. This
  is the intended fix, but it means a box with a genuinely misconfigured trunk starts
  failing `events_doctor`. Correct, and preferable to silence.
- **Re-introducing silence:** guarded by test 9. Any future refactor of `alertable`
  that drops the `trunk_missing` early return fails that test.
