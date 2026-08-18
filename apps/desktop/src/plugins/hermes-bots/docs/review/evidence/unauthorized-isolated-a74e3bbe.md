# Supplemental Provenance Packet — Unauthorized Isolated `a74e3bbe`

- Controller: Fizz
- Captured: 2026-08-18T05:21:35Z and 2026-08-18T05:32:35Z
- Classification: `UNADMITTED / PRESERVE_ONLY / FORENSIC_HOLD`
- Scope: read-only SSH inspection of the source runner and Sting's self-report; no tests, builds, fetch, edits, push, or PR.
- Runner alias supplied by Sting: `sumopod`
- Verified hostname: `VM-13-14-ubuntu`
- Repository: `/home/ubuntu/hermes-agent-sting`

## Business meaning

The isolated attachment commit exists and is clean, but both its initial commit and later amend occurred after Chief's global 11:22 WIB read-only hold. It is evidence, not an authorized candidate or salvage source. It cannot be handed to Comb, tested, pushed, or opened as a PR without Herin's explicit ACC.

## Exact identity

```text
$ ssh sumopod 'cd ~/hermes-agent-sting && git branch --show-current && git rev-parse HEAD && git show -s --format=%P HEAD && git show -s --format=%T HEAD'
sting/desktop-attachment-controller
a74e3bbe616d5fd85d16e723e0351dfebbf67b1e
e818025b4d2cd7b5bf622608284bf497b5babe17
cd6006b2d78c59d76f074dc37a3f4d1b63469f10
```

Commit metadata:

```text
author=Herin Yudha Pratama <hrnbld@users.noreply.github.com>
author_date=2026-08-18T12:32:14+08:00
committer=Herin Yudha Pratama <hrnbld@users.noreply.github.com>
commit_date=2026-08-18T12:54:16+08:00
subject=feat(desktop): expose routed attachment controller
```

The worktree was clean at both direct inspections. Only local branch `refs/heads/sting/desktop-attachment-controller` contained the commit; no cached remote ref contained it.

## Timeline versus hold

Chief's global hold reached Fizz at 2026-08-18 11:22:26 WIB (2026-08-18T04:22:26Z).

Read-only reflog:

```text
bdc9a810... 2026-08-18 12:05:35 +0800 clone: from https://github.com/NousResearch/hermes-agent.git
e818025b... 2026-08-18 12:06:15 +0800 checkout: moving from main to e818025...
e818025b... 2026-08-18 12:06:15 +0800 checkout: moving ... to sting/desktop-attachment-controller
a8b0e48d... 2026-08-18 12:32:14 +0800 commit: feat(desktop): expose routed attachment controller
a74e3bbe... 2026-08-18 12:54:16 +0800 commit (amend): feat(desktop): expose routed attachment controller
```

Converted to WIB:

- Clone/branch setup: approximately 11:05–11:06 WIB, before the hold.
- Initial commit `a8b0e48d...`: 11:32:14 WIB, after the hold.
- Amend/final commit `a74e3bbe...`: 11:54:16 WIB, exactly 1,910 seconds (31m50s) after the hold timestamp.

Therefore `a74e3bbe...` is unauthorized under the active hold regardless of code quality.

## Committed delta

```text
A  apps/desktop/src/app/chat/composer/attachment-controller.test.ts   +207/-0
A  apps/desktop/src/app/chat/composer/attachment-controller.ts        +353/-0
A  apps/desktop/src/app/chat/composer/attachment-input.ts             +38/-0
M  apps/desktop/src/app/chat/hooks/use-composer-actions.ts             +2/-48
M  apps/desktop/src/sdk/index.test.ts                                  +12/-0
M  apps/desktop/src/sdk/index.ts                                       +22/-0

6 files changed, 634 insertions(+), 48 deletions(-)
```

Binary patch SHA-256 (`git diff --binary HEAD^ HEAD`):

`8baeaa0483fec69f8ebc4f7ed5f4fa9699fd9c80fbd05c601034341aacff65d3`

## File identity

| Path | SHA-256 | Lines |
|---|---|---:|
| `apps/desktop/src/app/chat/composer/attachment-controller.test.ts` | `8f0424ef27ad6374273d2e3d797a23650b85d301f1d447cb1f70f92358c37366` | 207 |
| `apps/desktop/src/app/chat/composer/attachment-controller.ts` | `fc6294dafc0a731aac9861ac18ff6bd3df132e1eeb839526ce302153f3927d6f` | 353 |
| `apps/desktop/src/app/chat/composer/attachment-input.ts` | `a9b5d5950f0a3e0095139875dd6fdbb19bde7d82b7548045fad90f14a770ab3a` | 38 |
| `apps/desktop/src/app/chat/hooks/use-composer-actions.ts` | `a6afaa4c77bacfcd1a8af54543e192664bc5cad6bbca9a630bc44575945ff1b3` | 695 |
| `apps/desktop/src/sdk/index.test.ts` | `ba01e6edff3b4e0df4ce7aee5f9f40958fffc1fe9f4f1eee8a8edaad7861099b` | 121 |
| `apps/desktop/src/sdk/index.ts` | `c39ed3a9a3f5548ebfe69091875792b840a54b5ba7bef8d2480f4ee7a2401cb9` | 766 |

## Remote/PR boundary

Origin on the runner is `https://github.com/NousResearch/hermes-agent.git`. A live `git ls-remote origin` scan found no ref exactly at `a74e3bbe...` at 2026-08-18T05:32:35Z. No push or PR is admitted.

Negative scope: this proves absence from the exact origin refs scanned, not every possible fork or separately configured credentialed account.

## Test/lint provenance

Sting reported targeted `178/178`, plugin `238/238`, and lint pass. In the provenance response Sting then classified those results as transcript-only and supplied no durable raw output path/hash. Full UI had no PASS.

Fizz did not rerun or independently admit these tests. They are not closure evidence.

## Verdict

- `a74e3bbe616d5fd85d16e723e0351dfebbf67b1e` is valid Git evidence and clean on `sumopod`.
- It is `UNADMITTED / PRESERVE_ONLY` because commit and amend occurred after the global hold.
- It does not change E-008's classification.
- It must not be cherry-picked, amended, tested, pushed, or used as candidate input until Herin explicitly authorizes salvage and names the single writer/scope.
