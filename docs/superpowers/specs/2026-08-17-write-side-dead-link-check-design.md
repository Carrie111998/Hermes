# Write-side dead-link check — design

Date: 2026-08-17
Status: implemented 2026-08-18, landed INERT (CLAUDE_MEMORY_TOMBSTONE_WARN unset). Acceptance
sweep: 2 hits, both PREEXISTING_DEAD. Final whole-branch review found the hit path exceeded
the hook's 10s registered timeout; fixed and re-verified at 328ms (see "Fitting the hook
timeout"). 48 unit tests, full hooks suite 250 passed. Awaiting Diego's review before activation.
Scope: `~/.claude/hooks/memory-index-size-guard.py` (single file; **no `settings.json` change**)
Related: `specs/2026-08-17-link-aware-memory-deletion-design.md` (the deletion guard this
completes), `plans/2026-08-17-link-aware-memory-deletion.md`,
`C:\Users\diego\memory-link-lint.ps1` (READ-ONLY, in `~/.homeops.git`), MemPalace drawers
`claude-code/link-aware-memory-deletion-*-2026-08-17`

## Problem

The three-layer guard shipped 2026-08-17 gates **deletions** that would orphan an inbound
wikilink. Nothing checks that a link being **written** resolves.

That gap is not theoretical. Twice on 2026-08-17, sessions created memory files linking to
names that commit `486258b` destroyed on 2026-08-14 — the same commit that motivated the
deletion guard. Nothing was deleted to cause either one. Both are pinned with git
provenance in `hooks/test_linter_conformance.py` as `PREEXISTING_DEAD`:

| Root | Referrer | Dead target |
|---|---|---|
| `hermes-agent-src` | `tests-tools-windows-baseline.md:82` | `detached-launch-can-silently-never-start` |
| `hermes-agent-src` | `cron-pytest-gates-flipped-interpreter-2026-08-15.md:85` | `read-test-durations-json-before-reproducing-a-gate-flake` |

The second appeared at `2026-08-17T22:13:03Z`, minutes after the guard went live. The
mechanism is durable: `486258b` deleted 118 memory files, and agents keep citing names they
still remember. The tombstones do not decay.

## Verified current state

Measured 2026-08-17 against the live corpus, not assumed. Method: build the live identity
set with `memory_links.identities_from_text` over every `*.md` in `derive_roots()`; sweep
every link with `memory_links.iter_links`; derive tombstones from
`git log --diff-filter=D --name-only -- "projects/*/memory/*.md"` in the `~/.claude` repo.
The measurement was run from a session scratchpad, which is not durable; the acceptance
sweep in **Rollout** re-derives these numbers from the same primitives.

| Fact | Value |
|---|---|
| Corpus | 10 versioned roots, 1175 files, 1362 live identities |
| Total link occurrences | **5455** |
| Links that do not resolve to a live name | **54** |
| Names ever deleted from a memory root (`git log --diff-filter=D`) | **136** |
| Tombstoned names that are live again (deleted, then re-created) | **0** |
| Unresolved links whose target is tombstoned | **2** — exactly `PREEXISTING_DEAD` |
| `git log --diff-filter=D` cost | 1.43s |
| `git rev-parse HEAD` cost | 0.34s |
| Python hook spawn, short-circuit path | **370–740ms** (5 runs) |

Rename detection (`-M`) changes neither the tombstone count nor the hit count.

### The measurement that chose the design

The obvious formulation — *warn when a written link does not resolve* — fires on **54**
links, of which **52 are legitimate**: a ~96% false-positive rate. It also contradicts the
harness's own memory instruction, which blesses the pattern explicitly:

> Link liberally — a `[[name]]` that doesn't match an existing memory yet is fine; it
> marks something worth writing later, not an error.

27 of those 54 hits are in Diego's own `~` root. A check that fires on a sanctioned
pattern would be turned off within a day, and turning it off is the correct response.

The real failure mode is not *"a link to a name that never existed"* — it is *"a link to a
name that was destroyed."* That is a **git-history** question, not a resolution question.
Filtering the same 54 against the 136-name tombstone set leaves exactly the 2 known
instances and nothing else: 100% precision and 100% recall on the entire evidence base.

### Two cost facts that shaped the architecture

1. **Tombstone-first inverts the cost model.** Membership in a 136-name set is a dict
   lookup. The hot path never builds the cross-root index, so `memory_links`'s 6s budget
   and its documented ~20.5s cold-cache case — the reason the deletion gate can fail open —
   never enter this design at all.

2. **A new hook on `Edit|Write|MultiEdit` is not free.** At 370–740ms per spawn, adding a
   second hook to the busiest matcher in the harness taxes every write in every session on
   this machine. But that matcher is **already registered**: `memory-index-size-guard.py`
   runs there today and already short-circuits on non-memory paths. Folding the check into
   that existing hook makes the marginal cost of the common case approximately zero.

### A registration trap in the obvious formulation

Folding into an already-registered hook means the check is **live the moment the code
lands**. Diego's standing preference on this project is to defer activation until after
review, so "fold in" and "defer registration" cannot both be satisfied through
`settings.json`.

Renaming the file to match its broadened scope is strictly worse: the existing
registration would point at a missing file and the size guard would die silently, which is
precisely the class of failure this project exists to prevent.

**Resolution:** the check lands behind a default-off environment flag, mirroring the
`CLAUDE_MEMORY_INDEX_WARN_LINES` precedent already in that file. The code ships inert;
Diego enables it after review. There is no `settings.json` edit to defer and no rename.

## Decisions

1. **Tombstone membership, not link resolution.** Warn only when a newly written link
   target was deleted from a memory root's git history.
2. **Fold into `memory-index-size-guard.py`.** No new hook, no rename, no `settings.json`
   change.
3. **Default off.** Gated on `CLAUDE_MEMORY_TOMBSTONE_WARN`; unset means inert.
4. **Report, never block.** `PostToolUse` cannot block — the write already landed — and a
   warning is the right shape regardless, because the agent can repair in place.
5. **Only newly written text is scanned.** Pre-existing links in a file that is merely
   touched never nag.
6. **Fail open on every error.** Consistent with all three existing layers.

## Non-goals

- **Not a resolution check.** Aspirational links to names that never existed are correct
  usage and must stay silent. This is the single most important behavioural constraint.
- **Not a blocker.** No `permissionDecision`, no non-zero exit.
- **Not a repair tool.** It names the dead target and the commit that destroyed it; the
  agent decides whether to repoint, inline, or drop.
- **No change to `memory-link-lint.ps1`.** It is READ-ONLY (Diego, 2026-08-10).
- **No change to the three deletion layers**, to `memory_links.py`'s public behaviour, or
  to `auto-commit-claude-memory.py`.
- **Not a `settings.json` change.** Activation is a one-line env-var decision for Diego.

## Architecture

One file changes. The tombstone check is an independent function beside the existing size
check, each wrapped in its own `try/except`, both preserving the file's contract: **exit 0
always, never block.**

### Control flow

1. Tool is `Edit`/`Write`/`MultiEdit` — already checked by the existing guard.
2. `CLAUDE_MEMORY_TOMBSTONE_WARN` is truthy, else return. *This is the 99.9% path and costs
   nothing beyond the spawn already paid today.*
3. `file_path` matches `/.claude/projects/<proj>/memory/*.md`, else return. Note this is a
   **broader** predicate than the existing `_is_auto_memory_index`, which matches only
   `MEMORY.md`; the two predicates coexist, each serving its own check.
4. Extract **newly written text only**:
   - `Write` → `tool_input.content`
   - `Edit` → `tool_input.new_string`
   - `MultiEdit` → every `tool_input.edits[].new_string`, joined by newline
5. Parse links from that text with `memory_links.iter_links` + `normalize` — identical
   semantics to the linter and the deletion gate, so a link this hook flags is a link the
   linter would call DEAD.
6. Intersect targets with the cached tombstone set. Empty → return.
7. On a hit only, resolve the memory roots and confirm the name is not live again, so a
   re-created name cannot produce a false warning. Zero names are resurrected today; this
   keeps the check correct on the day one is. Both the root resolution (a `git check-ignore`
   spawn) and the liveness scan are paid only on a candidate hit — see **Fitting the hook
   timeout** below, which is what makes that true in practice rather than only on paper.
8. Emit a `systemMessage` naming each dead target, the commit that destroyed it, and its
   date. If the size guard also fired, the two messages are merged into the single JSON
   object a hook is allowed to emit.

### Fitting the hook timeout — added 2026-08-18 after the final review

The first implementation of step 7 was correct and unusable. The hook is registered with
**`timeout: 10`** (seconds), and the hit path measured **7765 ms warm** — `_memory_roots()`
5578 ms, the liveness scan 2171 ms — with the scan measured at **106 974 ms cold**. Because
a hit happens roughly never, its OS file cache is *always* cold when it finally does, so the
check would have been killed mid-scan on the first real hit: no warning, and no log line
either. Worse, both warnings ride a single `print()` at the end of `main()`, so the hang
would also have destroyed the pre-existing index-size warning. The per-check `try/except`
guards against exceptions, not against wall clock.

Four changes brought the hit path to **328 ms warm**, verified end-to-end:

1. **The liveness scan is candidate-scoped.** It answers "are *these* few keys live?", not
   "what are all 1370 identities?", and stops as soon as every candidate is resolved.
2. **Filename pass first.** Comparing `normalize(path.stem)` across the roots costs no file
   reads at all; only keys still unmatched require opening anything, and then only the first
   `FRONTMATTER_SCAN_LINES` (12) lines rather than the whole file.
3. **It is budgeted.** A `memory_links.Budget` of 5 s bounds the scan, and `BudgetExceeded`
   returns no warning — failing silent, the same safe direction as the cache. Exhaustion
   emits a distinguishable `deadlink-check-budget-exceeded` log line so "gave up" can be
   told apart from "clean".
4. **Root resolution is lazy.** It happens *after* the "no candidates" early return, so the
   5.6 s git spawn is no longer paid on every memory write that contains no tombstoned link
   — which is essentially all of them.

A subtlety worth recording, because it silently defeated the first attempt at this fix: the
budget must be created **after** root resolution. Created before, its 5 s clock was already
spent by the 5.6 s git spawn, and the check returned no warning on every live hit while
appearing correct in tests.

**This couples the hook to a constant that lives outside it.** The 5 s budget exists because
`settings.json` registers this hook with `timeout: 10`. Changing either without the other
re-opens the defect.

### Tombstone cache

`~/.claude/logs/memory-tombstones.json`, holding the normalized names plus, per name, the
deleting commit and its date.

Built **lazily**, only when all three hold: the flag is on, the write targets a memory
root, and the cache is missing or older than `MAX_AGE` (24h). The 1.43s `git log` is
therefore paid at most once a day, on one write, and never on the hot path. A cache that is
missing, unreadable, or malformed yields no warnings — failing open, like every other
layer.

Git history is append-only for deletions, so a stale cache under-reports and never
over-reports: the failure direction is a missed warning, never a false one.

A failed refresh **backs off** rather than retrying on the next write. It rewrites the cache
with the stale names plus a `failed_at` stamp so the mtime advances, and retries after 15
minutes. Without that, a failure left the mtime stale, so every subsequent memory write
re-attempted the full `git log` — which on a saturated box would exceed the hook timeout on
every write and take the index-size guard dark with it. The `git log` timeout is 4 s for the
same reason: a single attempt must not be able to exhaust the hook's budget on its own.

The `git` subprocess clears `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`
and `GIT_OBJECT_DIRECTORY` for the child, reusing `memory_links._GIT_ENV_HIJACKERS` — those
variables hijack even an explicit `git -C`, and this box runs ~20 worktrees with concurrent
agents.

## Data flow — the 22:13Z instance replayed

A sibling session writes `cron-pytest-gates-flipped-interpreter-2026-08-15.md` into the
`hermes-agent-src` root, containing
`[[read-test-durations-json-before-reproducing-a-gate-flake]]`.

1. `PostToolUse` fires on `Write`; flag on; path is under a memory root.
2. `content` is scanned; `iter_links` yields the target at line 85.
3. `normalize` → `read_test_durations_json_before_reproducing_a_gate_flake`.
4. Present in the tombstone cache, deleted by `486258b` on 2026-08-14.
5. Not live in any root → confirmed dead.
6. `systemMessage`: the link is dead, `486258b` destroyed the target on 2026-08-14, and the
   fact should be inlined rather than repointed if the survivor lives in another root.

Under today's guard this write produces nothing at all.

## Error handling

| Condition | Behaviour |
|---|---|
| Flag unset or falsy | Return immediately; zero observable change |
| Malformed stdin JSON | Existing handler logs `parse-error`, returns 0 |
| `git` missing, fails, or times out | No cache written; no warnings; logged |
| Cache missing / unreadable / malformed | No warnings; logged |
| Cache stale beyond `MAX_AGE` | Rebuild attempted; on failure the stale cache is still used |
| Tombstone check raises | Caught; the size-guard message is still emitted |
| Size check raises | Caught; the tombstone message is still emitted |

Every path exits 0. No path blocks.

## Testing

Extends `hooks/test_memory_index_size_guard_message.py`. Run with the repo venv, never bare
`python`:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

| # | Test | Asserts |
|---|---|---|
| 1 | Flag unset | No tombstone output, byte-identical to today's behaviour |
| 2 | New link to a tombstoned name | Warns, naming target and commit |
| 3 | **Aspirational link to a never-existing name** | **Silent** — the sanctioned pattern; the regression test that matters most |
| 4 | Link to a live memory name | Silent |
| 5 | Resurrected name (tombstoned but live again) | Silent |
| 6 | Tombstoned link outside the edited region | Silent — only new text is scanned |
| 7 | Non-memory file path | Silent |
| 8 | `MultiEdit` with the link in the second edit | Warns |
| 9 | Cache missing / malformed | Silent, exit 0 |
| 10 | Both checks fire | One JSON object carrying both messages |
| 11 | Existing size-guard cases | Unchanged |

Fixtures inject the tombstone cache and the root set directly; no test shells out to `git`
or depends on the live corpus, so a sibling's unrelated memory write cannot redden the
suite.

**Acceptance, run by hand once:** a sweep of all 5455 corpus links through the tombstone
filter fires on exactly the two `PREEXISTING_DEAD` entries and nothing else, and the
`test_linter_conformance.py` DEAD set is unchanged.

## Rollout

1. Implement test-first; full hook suite green.
2. Run the acceptance sweep; confirm 2 hits, both pinned.
3. Land the code **inert**. No `settings.json` edit.
4. Diego reviews, then enables by setting `CLAUDE_MEMORY_TOMBSTONE_WARN=1`.
5. If it proves noisy in practice, unsetting the variable is a complete rollback.

## Open question deferred by design

The 2 warnings this check would fire both belong to `hermes-agent-src`, a root owned by
other sessions. Repairing them is not this build's business — the deletion-guard spec
already recorded Diego's 2026-08-17 decision to accept them rather than edit a sibling's
files. This check exists to stop the **next** one being written, not to fix these two.
