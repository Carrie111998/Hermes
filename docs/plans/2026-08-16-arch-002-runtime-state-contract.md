---
title: "ARCH-002: harden the profile-scoped runtime-state contract"
status: IMPLEMENTED_USER_OVERRIDE_REVIEW_BLOCKED
date: 2026-08-16
type: architecture
ticket: ARCH-002
target_repo: hermes-agent
---

# ARCH-002: harden the profile-scoped runtime-state contract

## Status

IMPLEMENTED_USER_OVERRIDE_REVIEW_BLOCKED

The independent design-review gate remains blocked because the authenticated
Claude family did not issue a verdict. The user explicitly directed direct
implementation, overriding that design-consensus gate for this isolated
branch only. This status is not a reviewer PASS. Merge, deployment, DGX
mutation, and runtime-state migration remain separate gates.

## Context

ARCH-001 integrated a profile-scoped runtime-state repository into the Hermes
gateway. The current boundary includes `RuntimeStateManager`,
`RuntimeStateProfile`, lifecycle records, context binding, startup preflight,
and a database kept separate from the legacy session/state database.

The roadmap identifies ARCH-002 as the next core ticket before ARCH-003
audit/replay work. The contract must be explicit before more lifecycle
consumers are added; otherwise session, task, approval, and compression paths
can disagree about ownership, transitions, recovery, or profile isolation.

## Goal

Define and verify one stable runtime-state contract for profile-scoped gateway
lifecycle state, including ownership, legal transitions, compare-and-set
behavior, restart recovery, and failure classification. Preserve existing
direct `GatewayRunner(config)` construction and the separation from legacy
session state.

## Scope

1. Inventory the current runtime-state entities and call sites from the merged
   ARCH-001 integration; identify the exact invariants already relied upon.
2. Define a state/transition matrix for session, task, approval, compression,
   and gateway lifecycle records, including terminal-state immutability and
   retry/idempotency rules.
3. Define profile and context ownership boundaries: no cross-profile reads or
   writes, no ambient-context fallback for a required scoped operation, and
   deterministic behavior for direct construction and worker/task handoff.
4. Define atomic update/CAS semantics, version or generation handling, and
   stale-owner behavior without widening the database boundary.
5. Define restart/recovery behavior for interrupted non-terminal records,
   including bounded reconciliation and explicit unknown/degraded outcomes.
6. Add hermetic contract tests only after design approval; tests must cover
   isolation, legal/illegal transitions, CAS conflicts, idempotent retries,
   restart recovery, cleanup, and redaction of diagnostic metadata.

## Non-goals

- No ARCH-003 audit/replay implementation or event-sourcing redesign.
- No Telegram, platform adapter, model, provider, or UI changes.
- No replacement of the legacy `state.db`/session store in this ticket.
- No cloud storage, telemetry, external inference, or new user-facing
  `HERMES_*` configuration variables.
- No live DGX database inspection, migration, restart, or deployment during
  design/review.
- No implementation before independent reviewer consensus is recorded.

## Acceptance gates

1. Read-only code inventory maps every proposed invariant to an existing
   runtime-state surface or identifies a concrete gap.
2. The plan is reviewed by exactly one authenticated Claude reviewer and one
   authenticated AGY reviewer using the same metadata-only packet.
3. Review results are explicitly `PASS`, `REVISE`, or `BLOCKED`; a missing or
   unauthenticated reviewer is not a PASS.
4. Any `REVISE` result is reconciled into one correction set and returned to
   the same reviewer family before implementation.
5. Implementation, focused tests, CI, merge, deployment, runtime health, and
   rollback remain separate later gates.

## Design review questions

- Which lifecycle states and transitions are already contractual in ARCH-001,
  and which are only implementation details?
- How are ownership and profile scope proven across async tasks, worker
  threads, gateway turns, and direct test construction?
- What makes a transition atomic and idempotent under retries or concurrent
  owners?
- Which interrupted records are recoverable, which are terminal, and which
  must remain explicitly unknown/degraded?
- Can the contract be extended for ARCH-003 without changing the core message
  loop or breaking prompt-cache stability?
- Are diagnostics metadata-only and free of secrets, message bodies, tokens,
  absolute paths, and generated evidence text?

## Bounded review packet

The reviewer packet must contain only:

- ticket key and design objective;
- named repository surfaces and invariant categories;
- scope/non-goals and acceptance gates;
- the design questions above;
- the expected response schema: authenticated identity, verdict, findings,
  and correction-set scope.

It must not contain full source text, PDFs, evidence text, secrets, tokens,
absolute paths, prompts, or generated artifacts. Both reviewer families must
receive the identical packet and packet hash.

## Planned review route

Run the bounded packet through one real authenticated DGX Claude session and
one real authenticated DGX AGY session. Reconcile their verdicts before any
source edit or implementation branch is authorized.

## Design review evidence

- Packet: v3 advisory plan-sufficiency packet; metadata-only, no source text,
  secrets, tokens, absolute paths, prompts, or generated artifacts.
- DGX identity: SSH user `cwliao`, host `55-0940189-03`.
- AGY family: executable `/home/cwliao/.local/bin/agy`, version `1.1.13`,
  verdict `PASS`, findings none, correction set none.
- Claude family: Claude Code `2.1.197` was reached through the verified DGX
  session, but declined to issue a formal verdict because the metadata-only
  packet did not contain repository source and it would not treat the packet
  as an external authorization token. A subsequent attempt to provide source
  to the reviewer was rejected by the safety boundary because it would violate
  the metadata-only packet rule.
- Consensus: `BLOCKED`; the AGY PASS cannot substitute for the missing Claude
  verdict. No implementation, commit-to-main, merge, deployment, or DGX
  runtime mutation is authorized.

## Implementation record

- `runtime_state/contract.py` now centralizes lifecycle transition rules at
  the owner/version CAS boundary. Illegal transitions return the typed
  `InvalidTransition` result, terminal states have no outgoing transition,
  and same-state retries with the current owner token are idempotent no-ops.
- `runtime_state/__init__.py` exports the transition contract and typed error.
- `tests/runtime_state/test_runtime_state.py` covers idempotent same-state
  retry and terminal-state immutability.
- Focused verification: 24 tests passed across the runtime-state and gateway
  integration suites. Ruff was not installed in the isolated checkout and is
  recorded as NOT RUN.

## Implementation review evidence

- Packet: identical metadata-only implementation packet for both reviewer
  families; SHA-256 `ef1109ed4fd13b7e135be997f11b2aa0d0f2d06181d03063be8c118e651bba7c`.
- GitHub CI run `31932199413`: required checks and timing report completed
  successfully, including blocking Ruff/ty, Windows footgun, Python test, and
  e2e jobs.
- DGX AGY 1.1.13: `PASS`, findings none, correction set none.
- DGX Claude Code 2.1.197: declined a formal verdict because the packet had
  no source and Claude would not certify claims it could not independently
  observe.
- Implementation-review consensus: `BLOCKED`; AGY PASS does not substitute
  for the missing Claude verdict. No merge or deployment authorization follows
  from CI or the AGY advisory result.

## Current next action

Review the isolated diff and test evidence. Do not merge, deploy, mutate DGX,
or clean the primary dirty worktree without a separate explicit gate.
