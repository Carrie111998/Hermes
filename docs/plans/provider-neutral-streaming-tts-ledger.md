# Provider-neutral streaming TTS feature-dev ledger

## Run

- Run ID: `2026-07-30-provider-neutral-streaming-tts`
- Loop: Plebdev Feature Dev
- Target repo: `NousResearch/hermes-agent`
- Base branch: `main` (repo has no `staging`; explicit repository exception)
- Feature branch: `codex/provider-neutral-streaming-tts`
- Human owner: plebdev
- Started: 2026-07-30
- Current status: implementation and local review are complete through
  follow-up commit `47f249aa1`; PR #75014 is open pending upstream review.
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
- Spec issue: https://github.com/NousResearch/hermes-agent/issues/75029.
- Tickets: four approved delivery slices published in dependency order as
  #75030, #75031, #75032, and #75033.
- Ticket sessions: recorded below and committed.
- Agent briefs: three Luna-high read-only architecture briefs in current task.
- Review packets: Luna-high standards/spec reviews completed; all P1/P2 findings resolved.
- Local CodeRabbit report: initial nine findings and follow-up rounds of zero,
  three, and one finding were processed; every worthy finding was addressed
  and the affected checks were rerun.
- PR URL: https://github.com/NousResearch/hermes-agent/pull/75014 (OPEN).

## Commands

- Install: reuse root Python venv and existing `node_modules` where available.
- Typecheck: `npm --prefix apps/desktop run typecheck`
- Test: targeted pytest and Vitest suites per slice; root full suite at issue end.
- Build: `npm --prefix apps/desktop run build`
- Visual verification: Hermes Desktop against authenticated Beelink gateway.

## Ticket Ledger

| Issue | Type | Status | Review thread | Fixes needed | Verified |
| --- | --- | --- | --- | --- | --- |
| Frame contract | AFK | complete | Luna + CodeRabbit | fixed | yes |
| Gateway transport | AFK | complete | Luna + CodeRabbit | fixed | yes |
| Desktop playout | AFK | complete | Luna + CodeRabbit | fixed | yes |
| Fish qualification | AFK + production handoff | request-streaming measured; realtime thresholds failed; public admission probe failed | Spark PR #218 + Luna audit | sustained RTF > 1 and max gap > threshold; authenticated public route returned 429 | raw transport only |

## Parked HITL Slices

| Issue | Why parked | Blocks | Required human action | Final PR decision |
| --- | --- | --- | --- | --- |
| Live promotion | Feature Dev production boundary | live route only | separate promotion loop | out of feature PR |

The provider-neutral qualification probe is merged in
`finitecomputer/spark-cluster` PR #218. Raw Fish produced a valid incremental
WAV stream, but the thresholded run measured total RTF about 2.02, steady RTF
about 1.95, and a maximum inter-chunk gap about 2.31 seconds. A fresh
authenticated public-front-door probe on 2026-07-30 returned HTTP 429. This is
evidence for request-streaming transport conformance and against a realtime
claim; it is not Front-Door Model Promotion evidence or admission.

## Issue Session Ledger

| Issue | Fixed point | Worker session | Commit | Review result | Checks |
| --- | --- | --- | --- | --- | --- |
| Frame + gateway | `c9de69c6d` | Luna worker | `dd2e84929` | passed after fixes | 39 Python tests |
| Desktop playout | `dd2e84929` | Luna worker | `70087f6bc` | passed after fixes | 12 focused Vitest + typecheck + build |
| Design record | `70087f6bc` | orchestrator | `79c74999b` | passed | docs review |
| Playout/cancellation hardening | `79c74999b` | delegated worker | `a468f23f1` | passed | focused Desktop/Python tests + typecheck |
| Integration/docs follow-up | `a468f23f1` | Luna worker | `47f249aa1` | passed after standards/spec/CodeRabbit review | 16 Desktop + 77 Python tests, typecheck, build |
| Provider cancellation contract | `a468f23f1` | Luna worker | `47f249aa1` | passed after fail-closed/send-failure fixes | included in 77 Python tests |

## Open Questions

- None. The user explicitly approved the previously proposed architecture and
  delegated testing-seam and ticket-boundary decisions on 2026-07-30.

## Escalations

- Hermes has no `staging` branch and no Plebdev repo adapter. This run targets
  the repository's actual integration branch (`main`) while retaining all other
  feature-loop gates.
- Production changes are excluded from Feature Dev and will use the Spark
  Front-Door Model Promotion Loop after the implementation is reviewable.
