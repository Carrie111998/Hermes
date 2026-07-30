# Provider-neutral streaming TTS feature-dev ledger

## Run

- Run ID: `2026-07-30-provider-neutral-streaming-tts`
- Loop: Plebdev Feature Dev
- Target repo: `NousResearch/hermes-agent`
- Base branch: `main` (repo has no `staging`; explicit repository exception)
- Feature branch: `codex/provider-neutral-streaming-tts`
- Human owner: plebdev
- Started: 2026-07-30
- Current status: implementation
- Skill setup status: GitHub issue tracker inferred from origin; root and Desktop
  AGENTS guidance loaded; no repo-local Plebdev issue/triage/domain adapter exists.

## Goal

Implement a robust provider-independent streaming TTS path with a stable audio
contract, adaptive continuous Desktop playback, qualification gates, and Fish
Audio as the first conforming front-door provider.

## Durable Artifacts

- CONTEXT updates: not required; terms live in the adjacent plan.
- ADRs: `docs/plans/provider-neutral-streaming-tts.md`
- Prototype source branch, if any: none; direct cadence evidence answered the
  design question.
- Spec issue: pending publication.
- Tickets: four delivery slices in the spec; delegated approval recorded below.
- Ticket sessions: pending.
- Agent briefs: three Luna-high read-only architecture briefs in current task.
- Review packets: pending.
- Local CodeRabbit report: pending.
- PR URL: pending.

## Commands

- Install: reuse root Python venv and existing `node_modules` where available.
- Typecheck: `npm --prefix apps/desktop run typecheck`
- Test: targeted pytest and Vitest suites per slice; root full suite at issue end.
- Build: `npm --prefix apps/desktop run build`
- Visual verification: Hermes Desktop against authenticated Beelink gateway.

## Ticket Ledger

| Issue | Type | Status | Review thread | Fixes needed | Verified |
| --- | --- | --- | --- | --- | --- |
| Frame contract | AFK | in progress | pending | pending | no |
| Gateway transport | AFK | pending | pending | pending | no |
| Desktop playout | AFK | pending | pending | pending | no |
| Fish qualification | AFK + production handoff | pending | pending | pending | no |

## Parked HITL Slices

| Issue | Why parked | Blocks | Required human action | Final PR decision |
| --- | --- | --- | --- | --- |
| Live promotion | Feature Dev production boundary | live route only | separate promotion loop | out of feature PR |

## Issue Session Ledger

| Issue | Fixed point | Worker session | Commit | Review result | Checks |
| --- | --- | --- | --- | --- | --- |
| Frame contract | `07447bd5d` | pending | pending | pending | pending |

## Open Questions

- None. The user explicitly approved the previously proposed architecture and
  delegated testing-seam and ticket-boundary decisions on 2026-07-30.

## Escalations

- Hermes has no `staging` branch and no Plebdev repo adapter. This run targets
  the repository's actual integration branch (`main`) while retaining all other
  feature-loop gates.
- Production changes are excluded from Feature Dev and will use the Spark
  Front-Door Model Promotion Loop after the implementation is reviewable.
