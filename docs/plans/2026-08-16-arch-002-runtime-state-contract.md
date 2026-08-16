---
title: "ARCH-002: harden the profile-scoped runtime-state contract"
status: DESIGN_REVIEW_BLOCKED_CLAUDE_VERDICT_UNAVAILABLE
date: 2026-08-16
type: architecture
ticket: ARCH-002
target_repo: hermes-agent
---

# ARCH-002: harden the profile-scoped runtime-state contract

## Status

DESIGN_REVIEW_BLOCKED_CLAUDE_VERDICT_UNAVAILABLE

This is a plan-only ticket. No implementation, merge, deployment, DGX
mutation, or runtime-state migration is authorized by this plan.

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

## Current next action

Obtain a valid Claude-family verdict through an approved metadata-only review
route, or explicitly re-scope the review requirement. Do not send repository
source to an external reviewer, and do not implement ARCH-002 while this gate
is blocked.
