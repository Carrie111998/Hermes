# Forensic Hold Packet — `bafb2e74` Mixed Worktree

- Controller: Fizz
- Capture window: 2026-08-18T04:22:26Z–2026-08-18T04:36:57Z
- Classification: `BLOCKED_OVERLAPPING_WRITERS / AUTHORITY_UNPROVEN / FORENSIC_HOLD`
- Scope: read-only Git/filesystem/process/remote readback plus self-attribution reports from Comb and Sting.
- Packet path: `/Users/ninja/code/hermes-agent/.worktrees/feat-hermes-bots-chat-copy-attach-mention-channel/apps/desktop/src/plugins/hermes-bots/docs/review/evidence/forensic-hold-bafb2e74.md`
- Packet SHA-256 and final line count: recorded in `../buzz-parity-register.md` after this file is written, because a file cannot contain its own stable digest.

## Business meaning

No PR can be prepared from the current worktree. One local commit contains Comb's reported plugin work, while later uncommitted changes span the same plugin/test files plus a Sting-reported SDK/controller seam. Some overlapping uncommitted plugin/test authorship remains unknown. All bytes are preserved; no lane may resume without Herin's explicit disposition.

## 1. Identity

Captured with:

```text
$ date -u '+%Y-%m-%dT%H:%M:%SZ'
2026-08-18T04:24:27Z
$ git rev-parse --show-toplevel
/Users/ninja/code/hermes-agent/.worktrees/feat-hermes-bots-chat-copy-attach-mention-channel
$ git branch --show-current
feat/hermes-bots-chat-copy-attach-mention-channel
$ git rev-parse HEAD
bafb2e74de6b56e9b9dda935cb21fd099a27bc8d
$ git show -s --format=%P HEAD
e818025b4d2cd7b5bf622608284bf497b5babe17
$ git merge-base HEAD e818025b4d2cd7b5bf622608284bf497b5babe17
e818025b4d2cd7b5bf622608284bf497b5babe17
```

| Field | Value |
|---|---|
| Repository root | `/Users/ninja/code/hermes-agent/.worktrees/feat-hermes-bots-chat-copy-attach-mention-channel` |
| Git dir | `/Users/ninja/code/hermes-agent/.git/worktrees/feat-hermes-bots-chat-copy-attach-mention-channel` |
| Branch | `feat/hermes-bots-chat-copy-attach-mention-channel` |
| HEAD | `bafb2e74de6b56e9b9dda935cb21fd099a27bc8d` |
| HEAD parent | `e818025b4d2cd7b5bf622608284bf497b5babe17` |
| Expected baseline | `e818025b4d2cd7b5bf622608284bf497b5babe17` |
| Merge-base | `e818025b4d2cd7b5bf622608284bf497b5babe17` |
| Local refs containing HEAD | only `refs/heads/feat/hermes-bots-chat-copy-attach-mention-channel` |
| Cached remote refs containing HEAD | none |
| Upstream/tracking branch | none (`fatal: no upstream configured`) |
| Origin | `https://github.com/NousResearch/hermes-agent.git` |
| Cached origin default | `refs/remotes/origin/main` at `e818025...` |
| Live origin main | `bdc9a810f3990597b3f26203348e849e5128afb6` at 2026-08-18T04:24:45Z |

The baseline was current in the local clone at setup time but live origin main advanced to `bdc9a810...`; no fetch was run because it would mutate refs during the hold.

## 2. Commit provenance

Command and output:

```text
$ git show --no-patch --format=fuller HEAD
commit bafb2e74de6b56e9b9dda935cb21fd099a27bc8d
Author:     hrnbld <surel.herin@gmail.com>
AuthorDate: Tue Aug 18 11:10:14 2026 +0700
Commit:     hrnbld <surel.herin@gmail.com>
CommitDate: Tue Aug 18 11:10:14 2026 +0700

    feat(hermes-bots): buzz chat parity — copy, bare-@ mention, first-class channels
```

Exact committed delta:

```text
$ git diff --name-status HEAD^ HEAD
M  apps/desktop/src/plugins/hermes-bots/plugin.js
A  apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs

$ git diff --numstat HEAD^ HEAD
457  2  apps/desktop/src/plugins/hermes-bots/plugin.js
208  0  apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs

$ git diff --stat HEAD^ HEAD
2 files changed, 665 insertions(+), 2 deletions(-)
```

- Commit patch SHA-256 (`git diff --binary HEAD^ HEAD`): `2164bf40f8ba56ebb2ffde2cfb4e5e6e4e96bbb95f552737dce1f17de993b92a`.
- Live `git ls-remote origin` had no ref exactly at `bafb2e74...` at 2026-08-18T04:36:57Z.
- `gh pr list --repo NousResearch/hermes-agent --state all --head feat/hermes-bots-chat-copy-attach-mention-channel ...` returned `[]` at 2026-08-18T04:24:45Z.
- Negative scope: this proves no exact upstream ref and no PR in `NousResearch/hermes-agent` with that exact head-branch filter at capture time; it does not inspect every fork/account UI.

### Comb self-attribution — `REPORTED`

At 2026-08-18 11:23–11:25 WIB, Comb reported:

- Comb created commit `bafb2e74...` using `git add` for exactly the two files above followed by `git commit`.
- Commit output reported: `2 files changed, 665 insertions(+), 2 deletions(-)` and creation of `buzz-chat-parity.test.mjs`.
- Comb claims no edit/patch/write after the commit.
- No durable evidence packet was written; the commit is the only durable artifact.

Git metadata proves the commit/object/content and author identity configured in Git. The claim that the running Comb agent performed the command is an agent self-report, not independently attributable from Git metadata alone.

## 3. Authority chronology and contradiction

Relevant room decisions supplied to Fizz:

1. Herin requested implementation, local testing, a PR, and a merge request for four chat features.
2. Herin later said all project decisions were delegated to Chief.
3. Chief then decided to port to official `NousResearch/hermes-agent` and continue until PR, with Fizz contract, Comb implementation, Sting SDK/core review, and Bumble/Honey re-gate.
4. After overlap was detected, Chief issued a newer explicit global hold and stated that no Herin implementation ACC had reached Chief; Chief classified `bafb2e74` as `AUTHORITY_UNPROVEN` and reserved salvage/discard/testing/commit/push/PR decisions to Herin.

These records conflict on whether earlier delegated authority was sufficient for local mutation. The current explicit hold governs safety. The packet does not silently rewrite history: prior authorization messages existed, but current disposition is `AUTHORITY_UNPROVEN` until Herin resolves it.

## 4. Mixed working-tree state

`git status --porcelain=v2 --branch` at 2026-08-18T04:24:27Z:

```text
# branch.oid bafb2e74de6b56e9b9dda935cb21fd099a27bc8d
# branch.head feat/hermes-bots-chat-copy-attach-mention-channel
1 .M N... 100644 100644 100644 db3a062... db3a062... apps/desktop/src/plugins/hermes-bots/plugin.js
1 .M N... 100644 100644 100644 e82ad9c... e82ad9c... apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs
1 .M N... 100644 100644 100644 2104a64... 2104a64... apps/desktop/src/plugins/hermes-bots/tests/group-chat.test.mjs
1 .M N... 100644 100644 100644 1197981... 1197981... apps/desktop/src/sdk/index.ts
? apps/desktop/src/app/chat/attachment-controller.test.ts
? apps/desktop/src/app/chat/attachment-controller.ts
? apps/desktop/src/plugins/hermes-bots/docs/
```

### Staged / HEAD-to-index

Empty. `git diff --cached --name-status`, `--stat`, and `--summary` produced no rows.

### Tracked index-to-worktree

```text
M  apps/desktop/src/plugins/hermes-bots/plugin.js
M  apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs
M  apps/desktop/src/plugins/hermes-bots/tests/group-chat.test.mjs
M  apps/desktop/src/sdk/index.ts
4 files changed, 232 insertions(+), 33 deletions(-)
```

Exact numstat:

```text
160  31  apps/desktop/src/plugins/hermes-bots/plugin.js
55    1  apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs
3     1  apps/desktop/src/plugins/hermes-bots/tests/group-chat.test.mjs
14    0  apps/desktop/src/sdk/index.ts
```

Working tracked patch SHA-256 (`git diff --binary HEAD`): `d175b9fef215b90ca6fb476a3f51adc7c1f617410f2310b92fa35282470e915a`.

### Untracked at product capture

- `apps/desktop/src/app/chat/attachment-controller.test.ts`
- `apps/desktop/src/app/chat/attachment-controller.ts`
- `apps/desktop/src/plugins/hermes-bots/docs/acceptance/buzz-parity-v1.md`
- `apps/desktop/src/plugins/hermes-bots/docs/review/buzz-parity-register.md`

This evidence packet is an additional Fizz-owned register artifact allowed by Chief after the product capture.

## 5. Content identity

Captured at 2026-08-18T04:24:28Z and confirmed unchanged for the six product/SDK files at 2026-08-18T04:36:43Z.

| Path | Worktree SHA-256 | Lines | mtime +0700 | Corresponding HEAD SHA-256 | HEAD lines | Attribution |
|---|---|---:|---|---|---:|---|
| `apps/desktop/src/plugins/hermes-bots/plugin.js` | `0626add8e5d6c543c062af16a019ac503e29df5f5c53a79421ed3a9746a5eb6c` | 9141 | `2026-08-18T11:15:49+0700` | `190703fe2044419e69454e59fb67e93377511ba09a0e210f1260f9ece7a2284f` | 9012 | committed portion Comb REPORTED; unstaged portion UNKNOWN |
| `apps/desktop/src/plugins/hermes-bots/tests/buzz-chat-parity.test.mjs` | `46b8d8a9f9229d2032e8f31fc479e49a8a376daa7d74cf86ecf4cec2b6a06899` | 262 | `2026-08-18T11:15:44+0700` | `e98cb8d86c970a114e2425efc2bda86e0524426313faf6da2b644fe1cb63cd8d` | 208 | committed portion Comb REPORTED; unstaged portion UNKNOWN |
| `apps/desktop/src/plugins/hermes-bots/tests/group-chat.test.mjs` | `c887e82fe961e125f1c96299529cb9c85558cce8fb7af1f8a56e9992bb94bed4` | 386 | `2026-08-18T11:14:14+0700` | `85db6ad3d2c885fc22dfe6543602c46c023dc3e2fbeed3d864c9843366700d0c` | 384 | UNKNOWN |
| `apps/desktop/src/sdk/index.ts` | `e46640dcb69a292dfdb825002548d05a2055a71b89b67cb5aa4d96e61dbe9576` | 758 | `2026-08-18T11:17:39+0700` | `9ac89f6ff51fa5da5472328b3f8134d43f71beae6a9df790ed84d3962c26d2a1` | 744 | Sting REPORTED |
| `apps/desktop/src/app/chat/attachment-controller.test.ts` | `56c00e3644acbd1ba26d79dfbbc195c6b62acf506f83f9c0b1e740f4fb1ffeeb` | 294 | `2026-08-18T11:18:32+0700` | absent | — | Sting REPORTED |
| `apps/desktop/src/app/chat/attachment-controller.ts` | `a1fbefc84de2d9fe4ec1f47ad27e0b0d4e7e4574ce2ebccbb6e6b47509701feb` | 478 | `2026-08-18T11:21:56+0700` | absent | — | Sting REPORTED |
| `apps/desktop/src/plugins/hermes-bots/docs/acceptance/buzz-parity-v1.md` | `1410915d194b786310194f891851aa06d37d37ef6aa12a796aaad4d9720a0efd` | 108 | `2026-08-18T10:58:25+0700` | absent | — | Fizz PROVEN |
| `apps/desktop/src/plugins/hermes-bots/docs/review/buzz-parity-register.md` before final correction | `f469d7ca8ee5f4ba3580ee4bcd8e1068fe80e065ecc4243fe7f9948eada9a2ce` | 101 | `2026-08-18T11:22:11+0700` | absent | — | Fizz PROVEN |

Modes:

- All four tracked modified files remained index mode `100644`; `git diff --summary` reported no mode changes.
- All four captured untracked files above were `-rw-r--r--` at capture.
- No staged files or staged mode changes existed.

## 6. Overlap by file and region

Proven overlap means the same file exists in both the committed `HEAD^..HEAD` delta and the later index-to-worktree delta. It does not identify the later writer.

### `plugin.js` — overlap PROVEN, later writer UNKNOWN

Committed hunk headers included additions/changes near original lines:

```text
31, 73, 3896, 7369, 7385, 7438, 7450, 7535, 7571, 7575, 7576, 7772, 7959, 8131, 8316
```

Later unstaged hunk headers against HEAD included:

```text
192, 3969, 3973, 3990, 4042, 7668, 7671, 7696, 7722, 7777, 7780,
7999, 8012, 8017, 8081, 8198, 8261, 8264, 8293, 8521
```

Both deltas touch group mention/channel/workspace/BotsPane areas. Read-only hunk headers prove overlapping file/semantic regions, not writer identity.

### `buzz-chat-parity.test.mjs` — overlap PROVEN, later writer UNKNOWN

The commit added lines 1–208. The later unstaged delta changes line 47 and appends a 54-line block after line 208. Thus line 47 is direct region overlap; appended tests share the same file but do not overwrite committed lines.

### Non-overlap-at-commit files

`group-chat.test.mjs`, `sdk/index.ts`, and both attachment-controller files are absent from the committed delta. Their later presence does not prove who wrote them.

## 7. Writer provenance register

| Artifact/action | Attribution | Basis |
|---|---|---|
| Contract and Fizz register docs | Fizz `PROVEN` | Direct Fizz write/patch tool receipts in this orchestration run |
| Commit `bafb2e74...` execution by Comb | Comb `REPORTED` | Comb self-attribution; Git object independently proves metadata/content but not process identity |
| Committed `plugin.js` and new `buzz-chat-parity.test.mjs` | Comb `REPORTED` | Comb exact-file report matches committed tree |
| Unstaged `sdk/index.ts` + two attachment-controller files | Sting `REPORTED` | Sting self-attribution matches observed paths; no commit/process-to-byte cryptographic binding |
| Unstaged `plugin.js` | `UNKNOWN` | Comb denies post-commit edits; Sting denies touching plugin; no independent writer receipt |
| Unstaged `buzz-chat-parity.test.mjs` | `UNKNOWN` | Comb denies post-commit edits; Sting denies touching Bot Mode tests; no independent writer receipt |
| Unstaged `group-chat.test.mjs` | `UNKNOWN` | Comb and Sting both deny authoring; no independent writer receipt |

### Process/session evidence

- Comb implementation terminal process: `proc_c366555f7199`, PID `10975`, started 2026-08-18T10:59:22+0700, later exited normally.
- Sting implementation terminal process: `proc_de929d0a98ef`, PID `10963`, started 2026-08-18T10:59:21+0700; Fizz terminated it with SIGTERM/exit `-15` at approximately 11:22 WIB after the Chief hold reached Fizz.
- Comb provenance session: `20260818_112328_979d0b`; query process `proc_05f91629ecd2`.
- Sting provenance session: `20260818_110120_8f6700`; query process `proc_0d879cfc665e`.
- Chief hold confirmation session: `20260814_184507_40f13a`; query process `proc_7b7ae787079a`.

No process/session identity proves which process wrote the three UNKNOWN unstaged plugin-test files.

## 8. Test provenance — not acceptance evidence

No test was rerun by Fizz during the forensic hold. No independent raw output was admitted for 254/254 or mixed-state 259/259.

### Comb author-reported

- Baseline plugin suite: `238/238` pass.
- Focused `buzz-chat-parity.test.mjs`: `16/16` pass.
- Post-commit plugin suite: `254/254` pass.
- Mixed state plugin suite: `259/259` pass, reportedly run once after overlap discovery.
- Exact raw logs/evidence packet: none supplied.

The commit message itself also claims `node --test 'apps/desktop/src/plugins/hermes-bots/tests/*.test.mjs' = 254/254 pass`. Commit text is maker evidence, not closure evidence.

### Sting author-reported

- Initial RED: module absent; one file failed before tests ran.
- GREEN before last import edit: one file, 9 tests pass, 816 ms.
- Full plugin suite after last edit: 259 pass, 0 fail, 791.254542 ms.
- Full TypeScript blocked by incomplete dependency snapshot; Sting reported no attachment-controller diagnostic in observed output.
- ESLint blocked because `@eslint-community/eslint-utils` was unavailable.
- Final focused rerun was interrupted by Fizz's SIGTERM/hold; no focused PASS is claimed after the last edit.
- Reported log paths, not independently read/admitted:
  - `/Users/ninja/.hermes/profiles/sting/cache/terminal-output/out-1787026924-10963-f490.log`
  - `/Users/ninja/.hermes/profiles/sting/cache/terminal-output/out-1787026756-10963-d910.log`

These test claims do not close any acceptance row.

## 9. Main/default branch and remote readback

At 2026-08-18T04:24:29Z:

```text
Main checkout path: /Users/ninja/code/hermes-agent
Checked-out branch: fizz/hermes-bot-mode-buzz-parity-plan
HEAD: e818025b4d2cd7b5bf622608284bf497b5babe17
Status: one untracked apps/desktop/src/plugins/hermes-bots/BUZZ_PARITY_ACCEPTANCE.md
Local refs/heads/main: e818025b4d2cd7b5bf622608284bf497b5babe17
Cached refs/remotes/origin/main: e818025b4d2cd7b5bf622608284bf497b5babe17
Live refs/heads/main via ls-remote: bdc9a810f3990597b3f26203348e849e5128afb6
```

Negative scope: the main checkout was not clean because of one untracked planning document. It was not changed during this capture. The local/cached main refs are stale relative to live origin main; no fetch/reset/checkout was allowed.

Disk readback:

```text
/dev/disk3s5  228Gi  197Gi  2.6Gi  99%  /System/Volumes/Data
```

No install or build was run.

## 10. Safety readback

- Product/SDK file hashes and working patch hash were identical at 11:23:11 and 11:36:43 WIB.
- The latest worktree/branch reflog entry was the 11:10:14 commit; no new reset/amend/checkout/rebase/cherry-pick ref event appeared during capture.
- `git stash list` was empty at 2026-08-18T04:36:57Z.
- Staged state remained empty.
- Live origin had no ref at `bafb2e74...`, and the scoped upstream PR query returned no PR.
- Fizz did not run reset, restore, stash, checkout, clean, amend, rebase, cherry-pick, test, build, install, push, PR, merge, or deploy during capture.
- Fizz stopped the active Sting CLI process; it exited `-15`. This preserved the observed files but interrupted Sting's final focused test.
- Negative scope: reflog/status/hash stability proves the inspected local refs and listed file bytes were stable across the capture window. It cannot prove that no unobserved external process touched and restored identical bytes between samples.

## 11. Final verdict and disposition

**Verdict:** `BLOCKED_OVERLAPPING_WRITERS / AUTHORITY_UNPROVEN / FORENSIC_HOLD`

- The local commit is not an accepted candidate.
- The mixed working tree is not a candidate.
- The 254/254 and 259/259 claims are author-reported only.
- No lane resumes after this packet.
- No new worktree/writer starts.
- No test/build/install/commit/push/PR/merge/deploy is authorized.

Herin must explicitly choose one disposition:

1. `preserve-only` — keep the worktree exactly as forensic material;
2. `independent audit` — authorize a read-only technical audit of the preserved state;
3. `authorized salvage` — authorize a single named writer to port selected bytes into a fresh clean worktree based on the then-current live origin main;
4. `discard/reset` — authorize exact destructive cleanup/reset scope.

Until that decision, the byte state is preserved and the project remains blocked.
