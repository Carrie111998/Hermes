# Local patch ledger

Every commit on local `main` that is not in `origin/main`. Each entry records
why it exists, what upstream files it touches, what test protects it, and the
condition under which it can be dropped.

Regenerate the raw list with:

```bash
git log --oneline origin/main..HEAD
```

Verify the ledger has not drifted with:

```bash
python scripts/check_patch_ledger.py
```

That check fails if a local commit is missing here, which is what stops the
ledger quietly rotting while the fork keeps diverging.

**Status at last update:** 28 local commits; `origin/main` is 114 commits
ahead. Nothing has been pushed.

## Classification

| Class | Meaning | Merge risk |
|---|---|---|
| `upstreamable` | a genuine upstream bug; should be offered upstream | low — upstream may fix it independently |
| `local-policy` | this deployment's rules; never upstream | low — new files, few conflicts |
| `core-patch` | edits an upstream file for a local requirement | **high — re-apply by hand after a merge** |
| `test-only` | test correctness/portability | medium |

---

## core-patch — re-verify these after every upstream merge

| Commit | Purpose | Upstream files | Protected by | Remove when |
|---|---|---|---|---|
| `6e13fcb38` | reasoning-effort truth + `agent.reasoning_passthrough` opt-in | `run_agent.py`, `hermes_cli/doctor.py`, `acp_adapter/entry.py` | `tests/agent/test_reasoning_status.py`, contract #8 | upstream reports effective (not configured) effort |
| `4e0a148fc` | fail fast on unsupported Python / vulnerable SQLite | `hermes_cli/main.py` | `tests/hermes_cli/test_runtime_guard.py`, contract #8 | upstream enforces `requires-python` at entry |
| `1d7c79786` | neutralize forged steer markers in tool results | `agent/tool_dispatch_helpers.py` | `tests/agent/test_steer_marker_forgery.py`, contract #11 | upstream makes the steer marker unforgeable (nonce/typed field) |
| `25e84ae33` | crashed turns are not completions | `agent/turn_finalizer.py` | `tests/agent/test_turn_completion_honesty.py`, contract #13 | upstream sets `failed=True` on crash exits |
| `326ebcf2f` | batch-delegation cancellation must not block | `tools/delegate_tool.py` | `tests/tools/test_delegate_batch_cancellation.py`, contract #12 | upstream stops using a joining `with` block |
| `10ba6cc4f` | alert cursor advances only after a durable hand-off | `gateway/worker_bridge_watchers.py` | `tests/gateway/test_worker_bridge_alert_handoff.py` | the adapter gains a real delivery acknowledgement |
| `8364110ab` | `_worker` arg shape branches on the running CPython | `tools/daemon_pool.py` | `tests/tools/test_daemon_pool.py`, contract #7 | upstream stops mirroring the private stdlib function |
| `077df8c92` | force push via `+refspec` classified dangerous; `.env` write-denied (H-24, H-25) | `tools/approval.py`, `agent/file_safety.py` | `tests/tools/test_destructive_gate_gaps.py`, contract #15 | upstream gates both |
| `0c7763004` | warn before overwriting a file never read (H-23) | `tools/file_state.py` | `tests/tools/test_file_staleness.py`, contract #16 | upstream adds read-before-write feedback |
| `07be9aa79` | recalled memory threat-scanned and framed as evidence (H-09) | `agent/memory_manager.py` | `tests/agent/test_memory_context_trust.py`, contract #17 | upstream scans provider prefetch |
| `7069765d9` | injected guidance stops overriding identity approval rules (H-06) | `agent/prompt_builder.py` | `tests/agent/test_guidance_does_not_override_identity.py`, contract #18 | upstream removes the bypass phrasing |
| `2fdfe381d` | contract tests + regression simulations for the five above | — (local-policy) | self-verifying via the harness | never |
| `222ef7f83` | memory bounded on read, unreadable files reported, trust fence unforgeable (H-10, H-11, H-12) | `tools/memory_tool.py`, `agent/tool_dispatch_helpers.py` | `tests/tools/test_memory_bounds_and_trust.py`, contract #11 | upstream bounds recall and strips its own fences from tool output |
| `75bf49c3e` | the parent classifies a crashed child as failed (H-01 completion); CLI reasoning resync (H-18); Bedrock drop reported (H-17) | `agent/turn_finalizer.py`, `tools/delegate_tool.py`, `cli.py` | `tests/agent/test_turn_completion_honesty.py`, contract #13 | upstream propagates crash state to the parent |
| `0f24558bf`, `4673c6360` | git exit 1 is only "expected" for query subcommands (H-26) | `tools/terminal_tool.py` | `tests/tools/test_exit_code_interpretation.py` | upstream makes the note subcommand-aware |
| `a800784b4` | Bedrock transport name + harness anchor corrections | — (test-only) | the harness itself (15/15) | never |
| `5c80e6f1d` | `files_written` no longer always empty; compaction stops naming deleted sections (H-21, H-14) | `tools/file_state.py`, `tools/delegate_tool.py`, `agent/context_compressor.py` | `tests/tools/test_writes_since_filter.py`, `tests/agent/test_compaction_prompt_sections.py` | upstream fixes the filter and the prompt |
| `5dee6aa46` | prompt precedence stated; inert tool-use enforcement reported (H-08, H-07) | `agent/prompt_builder.py`, `agent/system_prompt.py`, `hermes_cli/doctor.py` | `tests/agent/test_prompt_precedence_and_enforcement.py` | upstream states precedence itself |
| `dc11cb87e` | transcript kept clean, refunds actually grant iterations, cron turn abandonable (H-02, H-03, H-04) | `agent/chat_completion_helpers.py`, `agent/iteration_budget.py`, `agent/conversation_loop.py`, `cron/scheduler.py` | `tests/agent/test_loop_budget_and_transcript.py` | upstream adopts each |

### Superseded by the peer session — take THEIRS at merge

`4e0a148fc` (runtime guard) is **superseded**. The peer's version in the main
tree is correct and mine is not: I made vulnerable SQLite advisory on the
reasoning that the risk is probabilistic, but `hermes-agent/venv` is Python
3.11.15 — *inside* `requires-python`, so my own Python check waves it through —
while linking SQLite 3.50.4 against ~10 of 11 already-WAL databases. Warning-only
left a live corruption path on a runtime the guard itself called supported.

Worse, my `HERMES_SUPPRESS_SQLITE_WARNING` returned True **before probing**, so a
single cosmetic env var cleared the check entirely. Their split — a cosmetic
suppressor that cannot clear a real vulnerability, plus a loud, never-silenced
`HERMES_ALLOW_VULNERABLE_SQLITE` for deliberate acceptance — is the right shape.

**Highest merge sensitivity:** `run_agent.py` (~17.8k lines) and
`agent/tool_dispatch_helpers.py`. A whole-file conflict resolution on either
silently drops a security fix — which is exactly what
`scripts/verify_protected_behavior.py` simulates.

## upstreamable — offer to NousResearch

| Commit | Purpose | Notes |
|---|---|---|
| `8364110ab` | CPython 3.14 `_worker` signature change | breaks every `delegate_task` on 3.14; affects all users |
| `04d221234` | decode update output as UTF-8, not the locale codepage | Windows-wide defect |
| `1b0b84a41` | stop patching `os.name` in tests (breaks `pathlib` process-wide) | test-infra correctness |
| `d4f3806a0`, `dedb1492a`, `220600b8d` | failure-successor recursion / unservable-parent aborts | gateway correctness |

None submitted yet — pushing requires authorization.

## local-policy — never upstream

| Commit | Purpose |
|---|---|
| `8dea266dd`, `027a90172` | protected-behavior contract suite + regression harness |
| `6c41512b0` | reasoning-effort probe findings |
| `2484ec0a0`, `6cd47d16e` | worker auto-dispatch guards for this deployment |
| `0e32d21f1`, `f66dfbdee` | stop test artifacts leaking into the repo root |
| `7f9e41aaf` | this ledger, its drift check, and `scripts/safe_update.py` |
| `a54349771`, `cbaff4773` | `scripts/rehearse_rollback.py` + `ROLLBACK.md` — restore rehearsal (20/20) and the tested procedure |
| `d9f3996d8` | behavioral evaluation harness + `.github/workflows/hermes-audit-guards.yml` |
| `6fab15b17` | FakeRunner composes the ultra mixin; XFAIL disposition recorded |
| `7efb1104e` | drift check exempts ledger-only commits (it could not converge) |

## test-only

`7709d03fa`, `8290ef298`, `fbc61c971`, `ce40aa7be` — platform pinning and
hermes-home isolation in tests.

## merge history — not patches, but load-bearing

| Commit | What it is |
|---|---|
| `763e4c2f8` | the merge that reconciled local `main` with `origin/main` (1,241 upstream commits). The base every entry above sits on. |
| `75a67b162` | reconciles this branch with `origin/main` (**160** upstream commits). One conflict, `gateway/run.py`, resolved BY HUNK: an adjacent-addition clash where ours added `_start_worker_bridge_watchers()` and theirs added `on_spawn=None` to `_spawn_supervised()`. Both kept — `on_spawn` is used 5× in upstream's body, and the local method is called at `gateway/run.py:8734` with zero upstream equivalent, so **each whole-side choice breaks the other outright**. Branch is now 0 behind upstream. |
| `6ab037f1e` | **"restore upstream changes lost by file-level conflict resolution"** — a previous merge resolved a conflict by taking one whole side, silently dropping upstream work, and this commit put it back. |

`6ab037f1e` is the precedent for this entire ledger: whole-file conflict
resolution has already destroyed work in this repository once. That is why
`scripts/verify_protected_behavior.py` simulates exactly that loss, and why the
updater refuses `git checkout --ours/--theirs` on a protected file.

## Rehearsal result — 2026-07-27, against 114 upstream commits

`python scripts/safe_update.py` merged `origin/main` in a throwaway worktree:

```
[5] 1 conflict(s):  gateway/run.py
[6] ok — 11 protected files retained local content
RESULT: DO NOT APPLY — review above
```

So the update is **one hand-resolved conflict away** from applying, and it is
in `gateway/run.py` — which carries local auto-dispatch and failure-successor
work (`2484ec0a0`, `6cd47d16e`, `d4f3806a0`) and had uncommitted edits from the
concurrent session at the time. Resolve it **by hunk**; taking either whole side
is precisely `6ab037f1e`.

Every other protected file merged without losing local content. Steps 7–9
(contract suite, regression harness, targeted suites) were deliberately skipped
because the tree was conflicted — re-run them in the rehearsal worktree once the
conflict is resolved, before applying for real.

## Merge-sensitive regions

Re-check by hand after any upstream merge:

1. `run_agent.py::_supports_reasoning_extra_body` — the override must stay first.
2. `agent/tool_dispatch_helpers.py::make_tool_result_message` — neutralization
   must wrap `_maybe_wrap_untrusted`, not replace it.
3. `agent/turn_finalizer.py` — the `completed` expression must keep `not crashed`.
4. `tools/delegate_tool.py` — the batch branch must not return to a `with` block.
5. `gateway/worker_bridge_watchers.py::_worker_bridge_tick` — order must stay
   persist → queue → advance.
6. `hermes_cli/main.py`, `run_agent.py`, `acp_adapter/entry.py` — all three
   console scripts must call `runtime_guard.enforce`.

## Profile-side patches (outer repo)

Tracked separately in `profiles/aletheon/AUDIT_STATUS.md`; they live in the
hermes-home repo, not this one, and upstream never touches them. The
delegation-guard lease store, compaction-guard/feedback-gate kwarg fixes,
`cfg_get` corrections and profile-path fixes are all in that repo.

## XFAIL disposition

`tests/gateway/test_worker_bridge_watchers_mixin.py` carries **19 non-strict
xfails**, all with precise per-test reasons. Verified 2026-07-27: **19 xfailed,
0 XPASS** — none is secretly passing.

They are the specification for an in-progress feature ("Step 8"), and they split
into two genuinely different kinds:

**Unfinished work** — will XPASS when Step 8 lands, which the CI `xpass-guard`
job now detects:
`test_free_slots_*` / `test_stale_lease_pid_frees_capacity` (slot counting is
not leases-backed yet), the six `*alert_text*` / `*nudge*` cases
(auto-dispatch-aware wording), the four `*auto_dispatch*` / `*capacity*` /
`*spawn*` cases, and `test_skip_audit_deduped_until_reason_changes`.

**Provably invalid specification** — the plan was superseded for stated
correctness reasons, so these should be *revised or removed* rather than
implemented:

| Test | Why the spec is wrong |
|---|---|
| `test_selects_only_created_and_queued` | the plan gives this watcher `created`, but the shipped design reserves `created -> queued` in `GatewayWorkerTaskDispatcherMixin`; letting both select `created` races two watchers for one task |
| `test_requeued_retry_is_dispatchable` | same root cause — the fixture seeds `created`, which this watcher deliberately does not own |
| `test_claim_marks_task_and_returns_snapshot` | expects `dispatch_claim`; the runtime key is `gateway_dispatch_claim` and is already persisted in live `bridge.db` rows — renaming orphans in-flight claims |
| `test_release_leaves_row_alone_if_live_pid_took_over` | same key-rename problem |

**Deliberately not changed by this audit.** They belong to the Step-8
workstream, they are non-strict (cannot fail CI), their reasons are precise, and
`gateway/run.py` — the one file that conflicted in the upstream rehearsal — is
the same area a concurrent session is editing. Unilaterally deleting another
workstream's specification tests would destroy information, which is the failure
mode this whole ledger exists to prevent. The four invalid ones are flagged here
for their owner.

Also fixed at HEAD: `FakeRunner` did not compose `GatewayWorkerBridgeUltraMixin`
while the production code calls `_ultra_route_transitions`, so 2 tests failed on
a clean checkout. Now 58 passed, 19 xfailed, 0 failed.

## Finding-ID reconciliation

`profiles/aletheon/scripts/ledger_status.py` derives status by grepping commit
subjects for the finding ID. Several fixes landed under descriptive subjects
that never named the ID, so the tool reported them **open** and the concurrent
session nearly re-did them. Mapping, so it does not:

| ID | Fixed by | Where |
|---|---|---|
| H-19 delegation-guard never enforces | `9281e87` | outer `main` |
| H-20 batch-delegation interrupt cannot escape | `326ebcf2f` | inner `audit/claude-items-2-4` |
| H-22 daemon_pool breaks on CPython 3.14 | `8364110ab` | inner, both branches |

**Convention going forward: name the finding ID in the commit subject.** A
reconciliation tool that reads git is only as good as what the commits say, and
the cost of omitting it is two sessions doing the same work — which already
happened for H-01, H-05, H-24 and H-25.

## H-26 — reasoning-only child reported as completed (post-merge regression)

Found by the independent review of the 160-commit upstream merge, verified by
reading both ends of the chain.

Upstream `214ae7b77` improved the empty terminal: when the model *did* think but
produced no visible answer, `conversation_loop` now delivers a labeled
"⚠️ ...only internal reasoning..." excerpt instead of the bare `(empty)`
sentinel. For a human reader that is strictly better. But `delegate_tool`
consumes the same field programmatically, and its H-01 fix detected a no-answer
child by matching the literal `"(empty)"`. The banner is non-empty and is not a
crash prefix, so a delegated child that never answered came back to the parent
as `status="completed"` — with raw chain-of-thought as the delegated result.

The literal only ever covered the no-reasoning half of the terminal. The exit
reason `empty_response_exhausted` is set unconditionally at
`conversation_loop.py:6410`, *above* the branch that picks banner vs sentinel,
so it is the signal that holds for both. Promoted to
`turn_finalizer.EMPTY_TERMINAL_EXIT_REASON` — a sibling of `CRASH_EXIT_PREFIXES`,
which exists as a module-level constant for exactly this "two consumers must
agree" reason — and both real consumers now key on it:

| Consumer | Change |
|---|---|
| `tools/delegate_tool.py` `_empty_sentinel` | ORs the exit reason. **This was the live bug.** |
| `agent/turn_finalizer.py` `_is_empty_terminal` | ORs the exit reason, so the completion explainer still flags a no-answer turn |
| `gateway/run.py:14307` | **deliberately unchanged** — that branch exists to turn the raw sentinel into a friendly message, and upstream's banner already *is* one |

Tests: `tests/agent/test_turn_completion_honesty.py`. Only
`test_delegate_tool_consults_the_empty_terminal_exit_reason` binds the fix —
reverting the patch fails that test and nothing else. The three rule cases
reproduce the classification (`_run_single_child` is 660 lines and needs a live
child agent) and therefore document behaviour rather than detect drift in it;
this is the same known limitation as the pre-existing `_completed_for` helper
above them, and is stated in the helper's docstring rather than left implied.
`test_banner_and_sentinel_share_one_exit_reason` guards the producer contract
against future upstream drift — it does not fail today.

Regression class worth noting for the next merge: an upstream change that is
purely cosmetic *at the point of edit* can still be semantic at a consumer that
parses the same field. String-literal coupling between a producer and a distant
consumer is the thing to grep for, not the diff's own line count.

---

# Integration: `integration/hermes-core-audit-20260727`

Final reconciliation of both audit sessions, the upstream delta, and Codex's
continuation repair onto one branch.

## Topology as found (the brief's numbers were stale)

| Ref | vs origin/main | Note |
|---|---|---|
| `origin/main` @ `731aa0ccc` | — | fetched 2026-07-27 |
| `audit/claude-items-2-4` @ `bdac397c3` | 56 ahead / 10 behind | already carried the 160-commit merge `75a67b162` |
| `main` = `integration/audit-merge` @ `f45bfab66` | 61 ahead / 170 behind | peer session; merged this branch at `002266661` |

The brief described "28 unpushed audit commits, origin/main ~114 ahead". Neither
held: `audit..main` is **10** commits (51 of main's 61 were already here),
`audit..origin/main` is **10**, and `main..audit` is **165**. `main` is ~160
upstream commits behind what this branch already carried, so merging in that
direction would have been a large regression.

Backup refs, created before any history change:

```
refs/backup/pre-integration-audit-20260727       bdac397c3
refs/backup/pre-integration-main-20260727        f45bfab66
refs/backup/pre-integration-originmain-20260727  731aa0ccc
```

## Merge order and result

`ebe6b9e93` merged `origin/main` first (10 commits: nine desktop/UI, one browser
CDP fix) so the peer's work would land on a current base. Clean.

`e76e0d9cb` then merged `main`. **No textual conflict.** The only overlap is the
two files both sessions independently fixed for H-01, and the auto-merge
composed rather than chose:

| File | Theirs | Mine | Result |
|---|---|---|---|
| `agent/turn_finalizer.py` | `turn_crashed()` shared predicate | `EMPTY_TERMINAL_EXIT_REASON` (H-26) | both, module-level |
| `tools/delegate_tool.py` | `child_crashed`, `exit_reason="crashed"` | exit-reason-keyed `_empty_sentinel` | both, same branch chain |

Neither side was a superset; a whole-side resolution would have dropped real
behaviour either way. Two fixups the merge itself required are in `e76e0d9cb`:
a test anchored on the literal `CRASH_EXIT_PREFIXES` (stale once the predicate
was extracted — re-anchored to accept either spelling, ordering assertion
unchanged) and a comment block left describing a `_crashed` assignment the merge
had replaced.

## `gateway/run.py`

Contrary to the brief, **neither commit range touches it** —
`git diff --stat 002266661 main -- gateway/run.py` is empty and the upstream
range changes nothing. The only `gateway/run.py` work was Codex's, applied by
hunk in `8b8d73fbe`:

| Codex hunk | Disposition |
|---|---|
| import `GatewayWorkerBridgeUltraMixin` | already present (`b26927b2c`, found independently) — **not reapplied** |
| insert it into the `GatewayRunner` MRO | already present (`b26927b2c`) — **not reapplied** |
| widen the `_start_worker_bridge_watchers` enable gate | **applied** |

Taking their whole file would have duplicated an import; taking ours would have
dropped the gate fix. The gate hunk fixes a live defect: this branch already
called `_worker_bridge_review_continuations` and `_worker_bridge_stage_successors`
inside `_worker_bridge_notifier_watcher`, but refused to start that watcher
unless alerts or failure successors were enabled — so a deployment enabling only
stage successors got a watcher that never ran, silently.

## Codex repair provenance

Not on any branch, ref, stash or note. It existed **only as uncommitted files in
the main worktree** `AppData/Local/hermes/hermes-agent`: `gateway/stage_successors.py`,
`STAGE_SUCCESSORS.md` and three test files untracked, plus `gateway/run.py`,
`FAILURE_SUCCESSORS.md` and `tests/gateway/test_worker_bridge_watcher_wiring.py`
modified. Recovered and committed for the first time in `8b8d73fbe`; the
originals are left in place untouched.

This also closed a **clean-checkout breakage**: `worker_bridge_watchers.py:29`
imports `gateway.stage_successors`, a module that did not exist in this tree.
The import resolved only because the venv's editable-install finder served it
from the *other* worktree. Any fresh clone or CI runner would have failed at
gateway import. Verified after the fix that zero `gateway`/`agent`/`tools`/
`hermes_cli` modules load from outside this worktree.

## The 19 XFAILs — all dispositioned, 18 resolved

Classified by reading the shipped code, not by trusting the file's own header —
which proved wrong on three of its four claims. Independently re-verified, and
one "invalid spec" verdict was overturned on challenge.

| # | Test | Disposition |
|---|---|---|
| 1–3 | `test_free_slots_counts_live_leases`, `test_stale_lease_pid_frees_capacity`, `test_free_slots_never_negative` | **Valid — implemented** (`a7d23517e`). Header called leases-backed capacity superseded; `orchestrator.py:258` acquires `("global", maximum_concurrency)` and the live bridge.db has the table. |
| 4–5 | `test_capacity_limits_spawn_budget`, `test_zero_free_slots_spawns_nothing_and_audits_no_capacity` | **Valid — passed untouched** once capacity was leases-backed. Filed under "spawn/audit semantics differ"; that reason was wrong. |
| 6–11 | `test_format_alert_text_*` (3), `test_tick_includes_pending_work_in_alert`, `test_nudge_lists_blocked_when_auto_dispatch_enabled`, `test_nudge_unchanged_when_auto_dispatch_disabled` | **Valid — implemented** (`a43209ad2`). Auto-dispatch-aware wording, failures sorted first, `(blocked: reason)`. |
| 12 | `test_resolve_target_uses_most_recent_session_when_unpinned` | **Valid — implemented** (`a43209ad2`). Alert target re-stamped with the bridge's system identity, so its own injected turn stops becoming the next target. |
| 13–14 | `test_auto_dispatch_spawns_and_audits`, `test_spawn_exception_does_not_corrupt_task` | **Valid — implemented** (`a43209ad2`). `_audit_dispatch` emitted `task.gateway_dispatch`; nothing consumed it. Now `task.autodispatch` with `status_before`/`by`/`pid`. |
| 15 | `test_skip_audit_deduped_until_reason_changes` | **Valid — implemented** (`a43209ad2`). Skips audited, deduped per (task, reason). |
| 16–17 | `test_claim_marks_task_and_returns_snapshot`, `test_release_leaves_row_alone_if_live_pid_took_over` | **Valid — implemented** (`71ffe2316`). Key renamed to `dispatch_claim`; the orphan risk the header cited is removed by reading the legacy key and never writing it. |
| 18 | `test_requeued_retry_is_dispatchable` | **Test-only fixture fix** (`71ffe2316`). `_retries_exhausted` was already correct; the fixture seeded `created`, a status this watcher does not select, so the guard was never reached. Zero production change. |
| 19 | `test_selects_only_created_and_queued` | **Open question — remains a documented non-strict xfail.** See below. |

Three defects were found *while* implementing and fixed in the same commits:
`float(ad.get("interval") or DEFAULT)` turned an explicit `0` into 15 s;
`has_active_worker_tasks` was defined as `count_free_global_slots(db_path, 1) == 0`
so the leases change silently moved the idle-nudge threshold (now standalone);
and `release_dispatch_claim` restored its snapshot unconditionally, erasing a
live runner's pid when a spawn raised after that runner had already stamped it —
one task, two runners.

## The one open question: who owns `created` tasks

Not missing work. A deployment decision with live blast radius, so it is
recorded rather than decided.

The plan gives this watcher both `created` and `queued`, and the live event log
shows it once did — 105 `task.autodispatch` events with `status_before='created'`
(2026-07-12..16), against 212 `task.auto_dispatched` events from the thin
dispatcher (07-22..27), with **zero task_ids in both sets**. The fork was
sequential, not concurrent: the watcher path died with the source loss and the
thin dispatcher was written on 07-22 as its replacement.

What makes this more than bookkeeping: `hermes_worker_bridge.dispatch.dispatch_pending`
**applies none of the Step-8 guards**. It honours only `spec.context.auto_dispatch is False`,
so for every `created` task the manual-hold, `depends_on`, pending-permission,
pending-input and retries-exhausted gates are bypassed. The guards this
workstream exists to enforce currently apply only to orphaned `queued` tasks.

Resolving it means either giving the guarded watcher sole ownership of `created`
(and retiring the thin dispatcher), or moving the guards into `dispatch_pending`
— which lives in the **production profile plugin**, outside this repo and outside
the authorisation for this work. Both change live dispatch behaviour. The test is
left non-strict so whichever lands produces an XPASS immediately.

## Verification

```
tests/contract/                                35 passed
evals/behavioral/                              13 passed, 4 skipped
scripts/verify_protected_behavior.py           16/16 simulated regressions caught
scripts/rehearse_rollback.py                   every rollback path restores and verifies
tests/gateway/test_worker_bridge_watchers_mixin.py + _watchers.py + _watcher_wiring.py
                                               91 passed, 1 xfailed  (was 58 passed, 19 xfailed)
tests/gateway/ stage successors + continuation + e2e + wiring   21 passed, 1 skipped
tests/tools/ -k delegat                        352 passed, 3 skipped
```

## Remaining risk

* **The `created`-ownership question above.** Until it is answered, `created`
  tasks bypass every Step-8 guard. This is the highest-value open item here.
* **Pre-existing suite failures, not introduced by this work.** An independent
  read-only review extracted both merge parents with `git archive` and ran
  `tests/gateway/` + `tests/tools/` on each: **228 failed on both, with
  byte-identical failure name sets**; the merge adds 74 net passing tests.
  `tests/gateway/test_worker_bridge_ultra.py` is untracked and its failures are
  arithmetic inside the test, not the gateway.
* **A whole-repo `pytest tests/` run aborts with 50 collection errors.** Every
  affected file collects cleanly on its own (`test_clipboard.py` alone: 107
  tests) and two-file combinations are clean, so this is cross-file import
  pollution in the suite, pre-existing and independent of this branch. Run
  per-directory to get a real signal.
* **The editable-install cross-worktree leak is a property of the venv, not the
  branch.** It is closed for `stage_successors`, but the finder will still serve
  any *other* missing module from the sibling worktree. Only a clean clone
  proves a module is really present.
