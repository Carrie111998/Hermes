# Hermes Bots Buzz Parity v1 — Finding Register

- Register authority: Fizz
- Last updated: 2026-08-18 12:55 WIB
- Contract: `../acceptance/buzz-parity-v1.md`
- Current verdict: `SALVAGE_AUTHORIZED / SINGLE_WRITER_COMB / BASELINE_PENDING`
- PR: none
- Merge/deploy: not authorized

## Live identity

| Field | Value |
|---|---|
| Repository | `https://github.com/NousResearch/hermes-agent.git` |
| Worktree | `/Users/ninja/code/hermes-agent/.worktrees/feat-hermes-bots-chat-copy-attach-mention-channel` |
| Branch | `feat/hermes-bots-chat-copy-attach-mention-channel` |
| Baseline HEAD | `e818025b4d2cd7b5bf622608284bf497b5babe17` |
| Current mixed HEAD | `bafb2e74de6b56e9b9dda935cb21fd099a27bc8d` (local-only, not an accepted candidate) |
| Baseline status | clean at 2026-08-18 10:56 WIB |
| Live origin main | `6680afba4a5580d1fcc39e1e85fcb1ac5ae9ca4c` observed 2026-08-18 12:55 WIB; Comb must independently report the exact fresh base used |
| Salvage worktree/branch/candidate | `PENDING_COMB_PACKET`; must be fresh and separate from all preserved sources |
| Free disk | `2.6 GiB` (`/System/Volumes/Data`, 99% used) at forensic readback |
| Archived prototype | `128c02472637d54a8b5d98398ecbbd89a8f58bbf` — preserved forensic reference only |
| Unadmitted isolated seam commit | `a74e3bbe616d5fd85d16e723e0351dfebbf67b1e` in `/home/ubuntu/hermes-agent-sting` on host `sumopod`; parent `e818025...`, tree `cd6006b...`; created/amended after the hold per Sting's self-report, so preserve-only and not an integration candidate |

## Authority correction

Earlier overlapping writers caused `BLOCKED_OVERLAPPING_WRITERS / AUTHORITY_UNPROVEN / FORENSIC_HOLD`. Herin has now explicitly ACCed `salvage bersih, satu writer @comb`, and Chief accepted that ACC. The hold is lifted only for Comb to build one candidate in a fresh current-origin worktree. Every preserved source remains read-only; all other agents remain product-code read-only. PR opening remains gated on independent Honey + Bumble review and Fizz fan-in; merge/release/deploy still requires separate explicit Herin ACC.

## Baseline executable evidence

Command run from `apps/desktop` on baseline `e818025b4d2cd7b5bf622608284bf497b5babe17`:

```text
$ npm run check:test:plugins
1..238
# tests 238
# pass 238
# fail 0
# skipped 0
# duration_ms 630.1905
```

Full terminal output: `/Users/ninja/.hermes/profiles/fizz/cache/terminal-output/out-1787025367-54637-7590.log`

This proves only the clean official baseline, not any replacement feature.

## Admitted frozen source

| Evidence ID | Artifact | SHA-256 | Scope |
|---|---|---|---|
| E-007 | `/Users/ninja/Projects/Hermes-Bot-Mode/.worktrees/feat-chat-copy-attach-mention-channel/docs/review/evidence/consolidated-blockers-128c024-v1.md` | `62648324d3ba4796b8824e05c9be5ec987bff5962872d88fa48484cb804f10bc` | Technical blocker source only; stale authority lines superseded above |
| E-008 | `evidence/forensic-hold-bafb2e74.md` | `f1c14bbd8f45c8312f2a87a15441040cef938fd0b02d631cb675e8bb932bde3a` | 311-line identity/provenance/safety packet; final classification `BLOCKED_OVERLAPPING_WRITERS / AUTHORITY_UNPROVEN / FORENSIC_HOLD` |
| E-009 | `evidence/unauthorized-isolated-a74e3bbe.md` | `1dc48427047caba3b8e3f930abbccc376cddd3909e336cf929e07235eaffe51d` | 104-line direct `sumopod` readback; `a74e3bbe...` was committed/amended after global hold and is `UNADMITTED / PRESERVE_ONLY` |
| E-010 | `../acceptance/buzz-parity-v1.md` | `6a68e05199bea6c8b45b8699b6e14045fe8d146575707b08d83fa4de6fb3a0f6` | 119-line active salvage contract; owner ACC, one-writer boundary, source preservation, checker gates |

## Assignment ledger

| Lane | Assigned at | Maker / checker | State | Exact next packet |
|---|---|---|---|---|
| Platform/runtime evidence | 2026-08-18 10:56 WIB | Sting / Honey | `READ_ONLY_SOURCE`; E-008/E-009 preserved | Answer exact questions only; no mutation/test/push/PR |
| Clean salvage replacement | 2026-08-18 12:55 WIB | Comb / Bumble + Honey | `ASSIGNED_SINGLE_WRITER` | Fresh worktree/base identity first, then one complete freeze-candidate SHA/diff/source-map/raw-test/risk/rollback packet |
| UX/live proof | 2026-08-18 10:56 WIB | Bumble / Fizz dedupe | `WAITING_FREEZE_CANDIDATE` | One full-sweep packet after exact SHA handoff; no product edits |
| Safety/acceptance | 2026-08-18 10:56 WIB | Honey / Fizz gate | `WAITING_FREEZE_CANDIDATE` | One full safety packet after exact SHA handoff; no implementation |
| Control/dedupe | 2026-08-18 10:56 WIB | Fizz / Chief oversight | `SALVAGE_CONTROL_ACTIVE` | Enforce one writer, fan in checker packets once, then issue PR gate verdict |

## Progress signals

- `2026-08-18T11:10:49+0700` — Comb checkpoint independently identified as `bafb2e74de6b56e9b9dda935cb21fd099a27bc8d`: committed diff is `plugin.js` +457/-2 and `tests/buzz-chat-parity.test.mjs` +208. Comb reports full plugin suite `254/254` (238 baseline + 16 new); Fizz has not rerun it and it is maker evidence only pending candidate packet/checker.
- The checkpoint covers copy, mention, and first-class channel work; attachment remains explicitly reserved for Sting's seam. It is not a freeze candidate.
- At the same readback, the worktree had an additional unstaged modification to `plugin.js`, untracked `apps/desktop/src/app/chat/attachment-controller.test.ts`, and untracked contract/register docs. Therefore `bafb2e74` is not the complete current working state and no clean-diff claim is admitted.
- Bumble independently matched this contract hash and is ready for one freeze-candidate UX/live sweep. Honey independently matched this contract/baseline and is ready for Sting's diff-scoped seam check followed by the full freeze gate. Neither checker has a current candidate verdict.
- `2026-08-18T11:16:51+0700` — overlap incident confirmed at HEAD `bafb2e74`: unstaged `plugin.js` +160/-31, `buzz-chat-parity.test.mjs` +55/-1, `group-chat.test.mjs` +3/-1, plus untracked `attachment-controller.test.ts`. File mtimes ranged 11:07–11:15 WIB. Comb denies authorship of all unstaged changes and reports combined suite `259/259`; that test result is not ownership proof and has not been rerun by Fizz.
- Fizz froze both maker lanes and sent Sting the one required 20-minute critical-path ownership ping. No commit/reset/stash/delete/overwrite/additional test is allowed. Negative scope: current evidence proves overlap and Comb's denial, not authorship of the plugin files.
- Sting provenance declaration: Sting created only `apps/desktop/src/app/chat/attachment-controller.ts` and `.test.ts`, and modified only `apps/desktop/src/sdk/index.ts`; no stage/commit/push/PR. Sting explicitly denies touching `plugin.js`, `buzz-chat-parity.test.mjs`, `group-chat.test.mjs`, or docs. His pre-final-patch focused run passed 9 tests; the final focused rerun was interrupted by hold (`exit -15`), so no final focused PASS is admitted. Full typecheck/lint were blocked by incomplete dependency snapshot; no seam candidate SHA or evidence packet exists.
- Comb self-attribution: Comb authored only commit `bafb2e74` (`plugin.js` and new `buzz-chat-parity.test.mjs`) and denies all post-commit edits. Thus the unstaged plugin/test writer remains **UNKNOWN**. Process inspection showed no open file handles at the snapshot; that is not proof the writer stopped.
- `2026-08-18T11:24:24+0700` snapshot showed additional Sting-owned seam paths had appeared (`sdk/index.ts` +14 and untracked controller source/test) after the earlier overlap snapshot. The state is preserved, not reconciled. Chief requires Herin ACC before Fizz may assign a writer or resume any lane.
- `2026-08-18T11:40:00+0700` — E-008 published with stable product/SDK hashes, local/remote identity, Comb/Sting self-attribution labeled as reports, UNKNOWN ownership retained for the unstaged plugin/test files, and no independent admission of 254/254 or 259/259. All lanes remain stopped after packet completion.
- `2026-08-18T12:33:00+0700` — E-009 published after direct read-only SSH verification of Sting's isolated runner. Branch `sting/desktop-attachment-controller` is clean at local-only `a74e3bbe...`, but reflog proves initial commit at 11:32 WIB and amend at 11:54 WIB, both after the 11:22 WIB hold. Reported 178/178, 238/238, and lint results remain transcript-only; the artifact is preserve-only and cannot enter salvage without Herin ACC.
- `2026-08-18T12:24:47+0700` coordinator receipt — Sting disclosed a separate clean checkout `/home/ubuntu/hermes-agent-sting` on `sumopod`, branch `sting/desktop-attachment-controller`, commit `a74e3bbe616d5fd85d16e723e0351dfebbf67b1e` (parent `e818025...`, tree `cd6006b...`). Direct SSH later verified the six-file local-only commit and reflog. Correction: Git metadata `12:32`/`12:54` is `+08:00`, which normalizes to 11:32/11:54 WIB; both are before the 12:24 WIB coordinator receipt but after the 11:22 WIB hold. There is no clock inconsistency. E-009 therefore admits the post-hold breach from direct reflog plus Sting's self-report; 178/178, 238/238, and lint remain transcript-only, and the full UI run has no PASS.
- `2026-08-18T12:55:00+0700` — Herin ACCed `salvage bersih, satu writer @comb`; Chief lifted the hold only for that lane. E-010 freezes the unchanged v1 acceptance criteria plus the fresh-current-origin, one-writer, preserve-source, checker, and no-PR-before-fan-in controls. Fizz's live read showed origin/main `6680afba...`; this is not admitted as Comb's base until his fresh-worktree packet confirms it.

Critical-path liveness: if a lane has no progress signal within 20 minutes of assignment, Fizz pings once with the exact packet requested. A further 20 minutes of silence becomes `STALLED` and the lane is reassigned or reported naming the unresponsive agent.

## Deduplicated findings

| ID | Severity | Requirement | State | Maker | Checker | Closure proof needed |
|---|---|---|---|---|---|---|
| CB-01 | CRITICAL | Safe optional attachment capability/authority | OPEN | Sting + Comb adapter | Honey | SDK/controller tests, capability-negative plugin test, exact diff review |
| CB-02 | CRITICAL | Attachment send lifecycle and intent preservation | OPEN | Comb | Honey + Bumble | Mounted races/failures/unknown outcome plus live recovery proof |
| CB-03 | HIGH | First-class persistent channel authority | OPEN | Comb | Honey + Bumble | Isolation/migration/reload/reopen tests and live proof |
| CB-04 | HIGH | Bare-@, actual caret, routing, accessibility | OPEN | Comb | Bumble + Honey | Mounted mid-draft/a11y/routing tests and live proof |
| CB-05 | HIGH | User-journey, integration, mutation-sensitive tests | OPEN | Sting + Comb | Honey + Bumble | Named tests demonstrably fail when each invariant is removed |
| EH-01 | HOLD | Copy live behavior and honest failure | OPEN | Comb | Bumble + Honey | Live Desktop clipboard/keyboard proof on freeze SHA |
| LC-01 | HOLD | Candidate lifecycle and PR evidence | OPEN | Fizz | Chief oversight | Exact SHA/diff/risks/rollback, clean status, CI/PR read-back |

No maker may close its own row. Only the named independent checker can return closure evidence; Fizz records and deduplicates it.

## Gate matrix

| Gate | Candidate timing | Status | Evidence |
|---|---|---|---|
| Fresh current-origin salvage baseline | before salvage edits | MISSING | Comb must return clean worktree/branch/base SHA, live origin readback, free space, and baseline command output |
| Preserved mixed/off-host sources | salvage input | READ_ONLY_ONLY | E-008/E-009; neither source is a candidate or accepted test proof |
| Comb focused SDK/plugin/integration tests | salvage iterations | MISSING | Raw commands/output on exact candidate lineage required |
| Freeze full plugin suite | exact freeze SHA | MISSING | `npm run check:test:plugins` on candidate required |
| Freeze lint/type/build | exact freeze SHA | MISSING | Exact commands/output required; block honestly if environment inadmissible |
| Bumble live Desktop proof | exact freeze SHA | WAITING_CANDIDATE | Clipboard, picker/recovery, bare-@ caret/a11y, channel reload/reopen |
| Honey full safety gate | exact freeze SHA | WAITING_CANDIDATE | One complete independent packet, all findings |
| Fizz final diff/register/lifecycle gate | after checker fan-in | WAITING_CANDIDATE | Dedupe once; verify PR/CI/rollback; no merge/deploy |

## Stop and rollback

Only Comb may create and modify one fresh salvage worktree/branch, run admitted tests there, and commit a local freeze candidate. Preserve the mixed macOS worktree, archived prototype, E-008/E-009, and `/home/ubuntu/hermes-agent-sting` unchanged. Every other agent remains product/test read-only. Push/PR waits for Honey + Bumble + Fizz gates; merge/release/deploy remains a separate explicit Herin decision. Rollback for salvage is removal of only the fresh task-owned worktree/branch after preserving evidence and receiving exact cleanup authority; never reset/delete preserved sources.
