# Hermes Bots — Buzz Chat Parity v1 Acceptance Contract

- Status: `ACTIVE_SALVAGE_CONTRACT`
- Frozen: 2026-08-18 10:56 WIB
- Decision authority: Herin explicitly ACCed `salvage bersih, satu writer @comb`; Chief accepted the ACC and lifted the forensic hold only for that bounded salvage lane.
- Controller: Fizz
- Official repository: `https://github.com/NousResearch/hermes-agent.git`
- Preserved forensic worktree: `/Users/ninja/code/hermes-agent/.worktrees/feat-hermes-bots-chat-copy-attach-mention-channel` (read-only; never the candidate worktree)
- **Rebase baseline (current): `daca38696738524ffdb901c18dbdbef64c1a97a9`** — live `origin/main` at fetch time, HEAD includes upstream #89049 (`@-mentions now autocomplete in Bot Mode group composers`), so the MN lane is satisfied upstream and is NOT re-ported in this candidate.
- Prior salvage baseline: `e818025b4d2cd7b5bf622608284bf497b5babe17` (stale; superseded)
- Rebase worktree/branch: `/Users/ninja/code/hermes-agent/.worktrees/parity-rebase` on `feat/chat-parity-rebase`
- Rollback: return the branch to the frozen baseline only after preserving any candidate/evidence and with owner authorization; no reset/delete is implied by this contract.

## Authority correction and source freeze

The archived-repository packet remains authoritative for its deduplicated technical findings only:

- Source: `/Users/ninja/Projects/Hermes-Bot-Mode/.worktrees/feat-chat-copy-attach-mention-channel/docs/review/evidence/consolidated-blockers-128c024-v1.md`
- SHA-256: `62648324d3ba4796b8824e05c9be5ec987bff5962872d88fa48484cb804f10bc`
- Archived prototype: `128c02472637d54a8b5d98398ecbbd89a8f58bbf` (forensic reference only; never the PR candidate)

Correction: the source packet's old statements that a Herin target decision and separate PR-opening decision were still pending are superseded by Herin's delegation and Chief's later decision to port to the official repository and continue through PR. The five blocker classes and evidence holds are frozen; no new acceptance criteria may enter v1 without Chief's explicit current-phase decision under that delegation.

## Salvage authority addendum — 2026-08-18

- Herin's ACC lifts `FORENSIC_HOLD` only for one named writer: **Comb**.
- Comb creates one fresh clean worktree/branch from current live `origin/main`, records exact base SHA before edits, and is the only process allowed to modify salvage product/test files.
- The mixed local worktree, archived prototype, E-008, E-009, and `sumopod` commit `a74e3bbe...` remain read-only evidence. They may be inspected and selectively reapplied by Comb; they must not be mutated, reset, amended, deleted, pushed, or treated as accepted candidates.
- Because later plugin/test writers in E-008 remain unknown, Comb must not copy the mixed tree wholesale. Comb owns the final diff and must justify each salvaged path against this contract.
- Sting, Honey, Bumble, Chief, and Fizz remain read-only with respect to product/test code. Honey and Bumble may execute independent checks only after Comb returns one exact freeze-candidate SHA.
- Comb returns one complete packet: fresh base SHA, candidate SHA, full diff/stat, source-selection map, focused/full commands with raw outputs, risks, rollback, and clean status. No blocker drip.
- No push/PR until Bumble + Honey finish one full sweep and Fizz records the fan-in verdict. PR opening after all gates is authorized; merge/release/deploy still requires Herin's separate explicit ACC.

## Requested business outcomes

A user in Hermes Bots group chat can:

1. Copy a single chat message's text reliably.
2. Select and attach a file, understand staging/error state, and send it without losing or misrouting the draft/file.
3. Type bare `@` and immediately choose a current-room participant at the real caret position. — **SATISFIED UPSTREAM by #89049** (`GroupMentionInput` + `mentionTokenAt`); verified present on the rebase baseline; not re-ported by this candidate.
4. Create and reopen a persistent channel whose membership is independent from roster grouping.

Default interaction behavior is Buzz Apps parity where the contract is not more specific. Any intentional deviation must be recorded in the candidate packet and accepted by the independent checker.

## Frozen requirement-to-evidence table

| ID | Requirement | Required proof | Checker | Initial status |
|---|---|---|---|---|
| CP-01 | Copy action copies exactly one selected message and reports failure honestly. Prefer the existing SDK `CopyButton`; do not add a second clipboard authority. | Mounted test plus live Desktop clipboard proof on the freeze SHA. | Bumble (UX), Honey (false-success safety) | missing |
| CP-02 | Keyboard/focus/accessibility behavior remains usable after copy. | Mounted keyboard test plus live Desktop proof. | Bumble | missing |
| AT-01 | The plugin uses an optional, feature-detected, generic Desktop attachment controller; no direct `FileReader`/`file.attach` fallback. Capability absence disables attachment with an explanation. | SDK/controller tests, plugin capability-negative test, exact diff review. | Honey | missing |
| AT-02 | Staging is route/session/occurrence aware, uses canonical refs and existing limits/hardening, and does not duplicate file bytes into chat text/state. | Local/remote/multi-profile route negatives and metadata assertions. | Honey | missing |
| AT-03 | Send lifecycle uses an immutable snapshot; pending/error blocks send; attachment-only send works; failure/unknown outcome preserves draft/files; stale completions are ignored; cancel/retry are explicit and safe. | Mounted race/failure/unknown-outcome tests plus live picker/send/recovery proof. | Honey (integrity), Bumble (UX) | missing |
| MN-01 | Typing bare `@` opens current-room candidates immediately at actual `selectionStart`/selection range; choosing replaces that token and restores focus/caret. | **UPSTREAM (#89049)** — `GroupMentionInput` + `mentionTokenAt` on baseline; live Desktop proof lane Bumble. | Bumble | upstream |
| MN-02 | Candidates are room-scoped, preserve canonical routing and `@everyone`/`@all`, and disambiguate duplicate handles from multiple sources. | **UPSTREAM (#89049)** + live duplicate-label UX proof when fixture is available. | Honey (routing), Bumble (UX) | upstream |
| MN-03 | Picker exposes combobox/listbox/active-option semantics. | **UPSTREAM (#89049)** + live keyboard navigation. | Bumble | upstream |
| CH-01 | Channel creation is single-flight, awaits an authoritative result, and exposes duplicate/cancel/failure without false success. | Mounted create success/duplicate/cancel/failure tests. | Honey (state), Bumble (UX) | missing |
| CH-02 | Channels have stable first-class identity and channel-owned membership separate from `botMeta.group`; a bot may belong to multiple channels without moving roster groups. | Persistence schema/code proof and isolation/migration tests. | Honey | missing |
| CH-03 | Create → send → reload → reopen preserves channel and membership with cross-channel isolation. | Integration test plus live Desktop reload/reopen proof. | Honey (data), Bumble (UX) | missing |
| RG-01 | Existing Hermes Bots plugin behavior and legacy SDK capability absence remain compatible. | `npm run check:test:plugins` on exact freeze SHA: all tests pass, zero failures. | Honey | missing |
| RG-02 | Actual changed paths satisfy lint/type/build constraints. | Focused tests during repair; `npm run check:lint` and appropriate type/build checks on freeze SHA. If a command is inadmissible because of storage/runtime, mark BLOCKED rather than waiving it. | Honey | missing |
| LC-01 | Exact candidate SHA, diff, risks, rollback, worktree state, PR/CI state, and evidence hashes are recorded. | Candidate packet and register read-back. | Fizz | missing |

## Five deduplicated root blockers to close

### CB-01 — Attachment capability and authority

Create the smallest optional generic controller in the official Desktop SDK/core boundary. It must reuse the existing picker/drop partition, core limits/hardening, occurrence-aware scope, route/session-aware staging, and canonical refs. The plugin must consume it by feature detection and fail closed when absent.

### CB-02 — Attachment send lifecycle

Use immutable occurrence-scoped send snapshots. Block send during required pending/error states; support attachment-only sends once ready; preserve user intent on failure/unknown outcome; never auto-retry unknown operations; ignore stale completions after remove/re-add, submit, channel change/delete, or route/session replacement; keep logical attachment metadata separate from human text.

### CB-03 — Channel authority and persistence

Implement stable first-class channels and channel-owned membership, separate from roster groups. Preserve existing groups, support one bot in multiple channels, await authoritative create results, and prove reload/reopen, isolation, migration, duplicate, failure, and partial-outcome behavior.

### CB-04 — Mention trigger, caret, routing, accessibility

Bare `@` opens immediately. Use the real selection range, replace the token at that position, restore caret/focus, keep room-only canonical candidates, support `@everyone`/`@all`, disambiguate duplicate identities, and expose standard combobox/listbox semantics.

### CB-05 — User-journey tests

Use mounted/integration tests for CB-01 through CB-04. Source regexes may supplement but never substitute. Named tests must turn RED when route binding is removed, send is allowed during staging, group metadata is reused for channels, or end-of-draft caret is substituted for the real caret.

## Lanes and contracts

| Lane | Maker / reviewer | Inputs | Allowed scope | Stop / handoff | Independent checker |
|---|---|---|---|---|---|
| Platform/runtime seam evidence | Sting | E-008/E-009 and source questions from Comb | Read-only explanation only; no source/product/test mutation | Answer exact provenance/capability questions; no candidate handoff | Honey |
| Clean salvage replacement | Comb | This contract, fresh current `origin/main`, E-007/E-008/E-009 as read-only sources | Sole writer for SDK/core + Hermes Bots plugin + mounted/integration tests in one fresh worktree; no unrelated refactor | Return one complete replacement candidate packet; no blocker drip; maker cannot close findings | Bumble for UX; Honey for routing/data/safety |
| UX/live proof | Bumble | Exact freeze SHA and this contract | Read-only review/evidence; no product/contract/register edits | One full-sweep packet with all findings | Fizz dedupes; Honey closes safety overlap |
| Safety/acceptance | Honey | Exact freeze SHA, this contract, all maker evidence | Read-only executable checks/evidence; no implementation | One full-sweep verdict with all findings | Fizz final gate |
| Control/dedupe | Fizz | All packets and current git/PR/CI state | Contract, register, evidence fan-in only; no product implementation | `READY_FOR_OWNER` only when every row is proved | Nex/Chief oversight; Herin retains merge/deploy authority |

No overlapping file writes are allowed. Comb is the only salvage writer. Any other product/test mutation immediately returns the project to `FORENSIC_HOLD`.

## Evidence and gate policy

1. Maker establishes focused RED → GREEN proof for its own changed boundary and returns all known findings in one packet.
2. Test/config/workflow-only repairs receive diff-scoped independent re-verification. Prior evidence carries forward only when the changed diff cannot reach that requirement and the checker says why.
3. Run the full independent re-gate once on the freeze candidate: maker full tests, Honey full safety gate, Bumble live Desktop UX gate, then Fizz register/diff/lifecycle gate.
4. The old baseline evidence proved only `e818025...`: from `apps/desktop`, `npm run check:test:plugins` returned `238` pass, `0` fail. It does not carry forward to the new current-origin salvage baseline or candidate; Comb and Honey must rerun the required gates.
5. Every negative claim must name the inspected paths/commands. Reasoning alone cannot close a row.

## Current environment admission

The preserved macOS worktree had only `2.6 GiB` free at forensic readback. Comb must report the fresh salvage runner/worktree and current free space before testing. Existing dependency-backed tests may run when admitted; new installs, broad cache creation, or storage-heavy builds remain blocked if the chosen runner lacks safe space. Never convert an unexecuted gate into a pass.

## Criteria freeze and deferred items

Not v1 blockers: copy-all/transcript action, mention suppression inside code spans, and attachment drag-and-drop UX. These enter a later phase unless Chief explicitly accepts them into v1 under Herin's delegated project authority.

## Terminal states and authorization

- Any missing/weak/contradicted required row: `CHANGES_REQUIRED` or `BLOCKED`; no PR.
- All rows proved and CI/PR packet complete: `READY_FOR_OWNER`; Chief's current decision authorizes opening the decision-ready PR and requesting maintainer review.
- Opening/updating a PR does **not** authorize merge, release, or deploy.
- Merge/deploy remains blocked until Herin gives explicit ACC after the plain-language summary: what changed, what can break, and how to roll back.
