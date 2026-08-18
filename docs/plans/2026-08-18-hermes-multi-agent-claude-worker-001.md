---
title: "HERMES-MULTI-AGENT-CLAUDE-WORKER-001 ticket design"
status: IMPLEMENTATION_IN_PROGRESS_AFTER_DESIGN_REVIEW_PASS_2_OF_3
date: 2026-08-18
type: ticket-design
ticket: HERMES-MULTI-AGENT-CLAUDE-WORKER-001
source_ticket: HERMES-MULTI-AGENT-ORCHESTRATION-001
target_repo: hermes-agent
base: 2b5eb8437a0bdae0529e7f4af5b8771b5f339997
base_format: full 40-hex SHA-1; verified equal to origin/main
---

# HERMES-MULTI-AGENT-CLAUDE-WORKER-001

## Status and reason

`DESIGN_REVIEW_PASS_2_OF_3`. This is a correction ticket opened after the 2026-08-18 disposable
Telegram/Kanban brainstorming spike. The spike used native Hermes, Grok, and
AGY worker lanes but did not include Claude, so it cannot be reported as an
all-agent test. The omission is a ticket/test-definition defect; no claim is
made that Claude is unauthenticated or unavailable until its runtime preflight
is performed.

The correction also records a command-construction failure observed in the
spike: `kanban swarm --worker` accepts
`PROFILE:TITLE[:SKILL,SKILL]`; free-form worker instructions must not be placed
in the third field because that field is parsed as skill names.

## Objective

Prove one disposable, metadata-only, end-to-end brainstorming path in which a
Telegram user asks Hermes to:

1. create a Kanban goal for a simple joke brainstorm;
2. invoke every explicitly configured and authenticated worker lane:
   native Hermes, Claude, Grok, and AGY;
3. run the four lanes in parallel through the existing Kanban graph;
4. gate the graph with a verifier that fails closed when any lane is missing,
   blocked, or lacks a valid handoff;
5. let Hermes synthesize one final result only after the verifier passes; and
6. return the synthesized result to the same Telegram user.

The test must use the existing Kanban dispatcher, worker skills, dashboard,
and Telegram notification path. It must not add a dispatcher target, custom
dashboard, provider integration, Telegram adapter, or alternate task store.

## Required interpretation of “all agents”

“All agents” means all four lanes explicitly named by this ticket:

- native Hermes worker;
- Claude CLI invoked through its existing worker skill;
- Grok CLI invoked through its existing worker skill; and
- AGY CLI invoked through its existing worker skill.

External CLIs remain worker tools, not first-class dispatcher profiles. The
Kanban assignee/profile remains the existing configured Hermes profile; the
skill or bounded worker instruction selects the external tool. A missing
runtime lane is a blocking precondition, not permission to silently reduce
“all” to the lanes that happen to run.

## Scope

### In scope

- add an explicit Claude lane to the test design and worker contract;
- read-only DGX preflight for Claude binary, version, authenticated smoke, and
  exact skill availability, without exposing credentials or output bodies;
- correct worker construction so title/body/skill fields cannot be confused;
- use the existing Kanban swarm graph and existing completion metadata;
- make the top-level orchestration card explicitly `goal_mode=true` with a
  bounded `goal_max_turns`, using the existing `kanban create --goal` path;
- require verifier evidence for all four lanes before synthesis;
- verify the existing Dashboard run history contains the completed runs and
  metadata-only handoffs; and
- obtain direct user confirmation of the Telegram-visible final response.

### Out of scope

- new dispatcher profiles or a new dispatcher;
- new model-provider integration, OAuth automation, or credential changes;
- changes to Telegram polling, Telegram state, or Telegram adapter code;
- custom dashboard UI or token analytics;
- message-body, prompt, token, credential, or absolute-sensitive-path logging;
- merge, deployment, timer enablement, service restart, or Telegram mutation
  before their separate explicit gates are authorized.

## Proposed test contract

The disposable goal text is:

> Ask native Hermes, Claude, Grok, and AGY for one short clean joke. Verify
> that all four lanes returned metadata-only handoffs, reject missing or
> duplicate lanes, then let Hermes choose and return one final joke.

Each worker handoff must contain only bounded, non-sensitive metadata with the
required keys `lane_id`, `outcome`, and `verified_clean`; `lane_id` must be one
of `native_hermes`, `claude`, `grok`, or `agy`, and the verifier must reject
missing or duplicate lane IDs. The native Hermes worker and the Hermes
synthesizer must use distinct role metadata so the synthesizer cannot satisfy
the native lane. Token usage is
optional and may be recorded only when the worker actually reports it under
`hermes.worker.v1`; no counts may be inferred or estimated. The verifier must
record `gate=pass` only when all four named lanes are complete and valid.

The synthesizer must not start before verifier pass. The goal card must close
only after the synthesizer result is recorded and the Telegram terminal event
is correlated to the same opaque goal/task identity. A Telegram process exit,
service health, polling progress, empty updates, or `hermes send` success is
not direct user-visible evidence.

The final joke payload is carried in the existing synthesizer task `result`
field and the existing terminal-event notification's human-readable result;
it is not placed in metadata-only correlation fields. The synthesizer writes
that result only after verifier metadata contains `role=verifier` and
`gate=pass`. Dashboard correlation and audit records contain only opaque IDs
and status fields; they never copy the result text.

## Exact graph construction and handoff fields

The implementation must use the existing `kanban swarm` topology builder (or
an equivalent thin CLI wrapper around the same existing `create_swarm` helper):

1. create one root with the exact goal text and `created_by=default`;
2. create four parallel worker cards, all assigned to the existing `default`
   Hermes profile, with `SKILL` set to empty for native Hermes or exactly one
   preflight-confirmed existing skill for the corresponding external lane;
3. supply bounded instructions through each task body, never through the
   `SKILL` portion of `PROFILE:TITLE[:SKILL,SKILL]`;
4. create one existing-protocol verifier whose parents are exactly the four
   worker IDs and whose completion must contain `gate=pass`; and
5. create one Hermes synthesizer whose only parent is the verifier and whose
   role is `synthesizer`, not `worker`.

The root/orchestrator card must use the existing goal-mode creation path with
`goal_mode=true` and `goal_max_turns=5`. Each worker must have
`max_runtime_seconds=120`. If the current `swarm` CLI cannot express these
existing task fields, implementation may add only the smallest CLI plumbing or
equivalent supported create path; it must not add a dispatcher, database
schema, or second orchestration system.

Before graph creation, runtime preflight must resolve each configured skill
name and worker command in the exact worker environment. A missing binary,
skill, or authentication result blocks graph creation; the implementation may
not hardcode an unverified `claude-code` path or silently substitute another
lane.

Each worker completion metadata must include exactly these required bounded
fields in addition to any optional safe fields:

```json
{
  "role": "worker",
  "lane_id": "native_hermes|claude|grok|agy",
  "outcome": "completed",
  "verified_clean": true
}
```

The originating worker card also persists `expected_lane_id` and
`preflight_skill_id`; the four worker cards are children of the root goal
card. The worker's existing task `result` field carries its joke payload for
the synthesizer only; the verifier and Dashboard/Telegram correlation never
copy that payload.

The synthesizer completion must use `role=synthesizer` and may not reuse a
worker `lane_id` as evidence. Each originating worker card persists an
explicit `expected_lane_id` and its preflight-confirmed skill identity. The
verifier requires one matching completion per parent card, rejects missing or
duplicate `lane_id` values, requires boolean `verified_clean=true`, and writes
`role=verifier`, `gate=pass`, `expected_lane_count=4`, and bounded opaque IDs.
The synthesizer requires `role=synthesizer`, `outcome=completed`, and
`result_present=true`; it cannot start on verifier completion unless
`gate=pass` is present.

Allowed cross-surface correlation fields are only opaque `goal_id`, `task_id`,
`run_id`, `status`, `outcome`, `result_class`, and platform message ID. No
title, prompt, message body, token, credential, or absolute path may be used
for correlation.

## Acceptance gates

1. **Identity/source gate:** work starts from the verified `origin/main` SHA,
   the primary dirty checkout is untouched, and the isolated worktree is clean.
2. **Design review gate:** independently route the same metadata-only packet
   to up to one authenticated Claude reviewer, one authenticated AGY reviewer,
   and one authenticated Grok reviewer. A quorum of at least two independent
   `PASS` verdicts out of the three available reviewer families closes the
   gate. `REVISE` and `BLOCKED` findings from the non-quorum reviewer remain in
   the reconciliation record; a concrete security, credential, or production-
   safety blocker still requires human resolution. No reviewer may be
   duplicated across platforms, and no virtual, guessed, or unauthenticated
   reviewer counts toward quorum.
3. **Runtime worker gate:** Claude, Grok, and AGY each pass a bounded binary,
   authentication, and minimal non-sensitive smoke preflight on the verified
   DGX host. All four lanes are mandatory; a missing, failed, or unavailable
   external lane blocks the test rather than being omitted.
4. **Graph construction gate:** one explicit goal card and four parallel
   worker cards exist; the verifier depends on all four; the synthesizer
   depends on the verifier; no card uses an unverified guessed profile. The
   goal card has a numeric `goal_max_turns` bound, every worker has a bounded
   runtime, and bounded worker instructions use the task body or supported
   create path rather than the swarm parser's skill field.
5. **Worker completion gate:** all four workers finish with valid metadata-only
   handoffs. Any blocked, crashed, timed-out, or missing worker is a failure.
6. **Verifier/synthesis gate:** verifier `gate=pass` precedes synthesis, and
   Hermes records one bounded final result without copying raw worker bodies
   into summary or notification metadata.
7. **Dashboard gate:** authenticated existing Dashboard/API evidence shows the
   four completed runs, verifier run, and synthesizer run by opaque IDs and
   allowed metadata fields only.
8. **Telegram gate:** the user sends the request through Telegram and directly
   confirms seeing the final synthesized response. This gate is separate from
   inbound polling, outbound audit, service health, and timer success.
9. **Cleanup gate:** all disposable scratch cards, task workspaces, and test
   artifacts are removed or archived through the supported Kanban cleanup path;
   no primary checkout files are changed.

## Design decisions to verify during implementation

- The existing Kanban create/swarm capability must be confirmed before graph
  creation; unsupported goal fields or topology fields fail closed rather than
  triggering a new dispatcher or database schema.
- The exact Claude skill path and command must come from runtime preflight;
  this ticket does not hardcode an unavailable path.
- Worker instructions use the task body/create path, while the swarm parser's
  `PROFILE:TITLE[:SKILL,SKILL]` field carries only profile/title/skills.
- The verifier uses parent-card expected lane bindings plus completion
  metadata, not names or message bodies, to enforce all four lanes.
- Dashboard and Telegram correlation uses only the explicitly allowed opaque
  fields; the final result travels through the existing synthesizer `result`
  and terminal-event notification path.

## Evidence boundary

Review packets and final reports may include ticket IDs, commit IDs, opaque
Kanban IDs, status/outcome values, schema names, provider/model labels, and
bounded counts. They must not include joke text, prompt text, Telegram message
bodies, credentials, tokens, or absolute sensitive paths.

## Authorization boundary

This ticket draft authorizes design and review only. It does not authorize
implementation, commit, push, merge, deployment, timer changes, service
restart, Claude/Grok/AGY credential changes, or Telegram state mutation.

## Initial correction record

- Current source of truth: `origin/main` at `2b5eb8437a0bdae0529e7f4af5b8771b5f339997`.
- Primary checkout remains dirty and preserved.
- The disposable spike proved only partial graph execution: Grok completed;
  the first native Hermes card was malformed by command construction; AGY was
  still running at observation cutoff; Claude had no worker card.
- The three-family retry initially found Claude in WSL. The final review
  quorum is `DESIGN_REVIEW_PASS_2_OF_3`: Claude and AGY returned `PASS`, while
  Grok returned `REVISE`. Grok's non-quorum findings are retained for
  implementation reconciliation and do not veto the two-of-three quorum.

## Review correction record

- Round 1 packet SHA-256:
  `fcf65b848be83d5aff25d315960bdf4db9c2ed6a29b7730f818d2ff36bf8f178`.
- Round 1 Grok reviewer: `REVISE`.
- Integrated findings: named lane identity and duplicate detection; explicit
  native-worker versus synthesizer role metadata; fail-closed four-lane
  preflight; numeric goal and worker bounds; required handoff keys; and an
  explicit rule that worker instructions do not occupy the swarm skill field.
- Round 1 AGY invocation was invalid because the duration lacked a unit; it
  produced no review verdict and is not counted.
- Round 2 reviewers: AGY `PASS`; Grok `REVISE`. Integrated the exact graph
  construction sequence, existing verifier dependency mechanism, role and
  expected-lane binding, `verified_clean` boolean contract, numeric goal and
  worker bounds, and the allowed Dashboard/Telegram correlation fields.
- Round 3 packet SHA-256:
  `b862b0702c217f9215f2fe5d44957172f21d92cb623c7e7ecf77d6aa793febb8`.
- Round 3 reviewers: AGY `PASS`; Grok `REVISE`. Remaining blockers are the
  payload channel for the final result, explicit skill-to-lane binding on the
  originating cards, verifier/synthesizer fields and fail-closed start
  semantics, and the unresolved CLI/skill capability questions.
- Three-family retry reviewers: Claude `REVISE`, AGY `PASS`, Grok `REVISE`.
  Integrated the existing synthesizer result delivery path, explicit
  expected-lane binding to each parent card, verifier/synthesizer required
  metadata and fail-closed gate, status convergence, and preflight-confirmed
  skill resolution.
- Final three-family retry packet SHA-256:
  `c890e70fbfada8a6b6606382304042a085e295f96a0d5e65980b9b9aa62da352`.
- Final reviewers: Claude `PASS`, AGY `PASS`, Grok `REVISE`. Claude's only
  correction was an audit-trail hash transcription check; the recorded Round
  3 digest is a 64-hex-character SHA-256 value. Grok's remaining findings
  (explicit task-result carrier, named skill/lane storage, and root-parent
  wording) are recorded as implementation reconciliation items.
- Design review is now `PASS_2_OF_3`; implementation, CI, commit, push,
  merge, deployment, restart, and Telegram testing remain separate gates.

## Implementation record

Implementation is authorized by the user's latest explicit approval for this
ticket's implementation, commit, push, merge, CI, DGX deployment/restart, and
Telegram test. The primary dirty checkout remains untouched; changes are in
the isolated ticket worktree.

The implementation currently changes only the existing swarm/worker surfaces:

- `hermes_cli/kanban_swarm.py`: four named lanes, expected lane/skill contract
  storage, bounded goal/runtime defaults, and completion validation;
- `hermes_cli/kanban.py`: repeatable lane arguments and bounded swarm options;
- `hermes_cli/kanban_db.py`: kernel-level completion validation and fail-closed
  synthesizer promotion after verifier `gate=pass`;
- `tools/kanban_tools.py`: model-tool completion validation;
- `optional-skills/devops/kanban-worker/SKILL.md`: metadata/result boundary;
- `tests/hermes_cli/test_kanban_swarm.py`: contract, bounds, parser, and gate
  coverage.

Focused evidence so far: `tests/hermes_cli/test_kanban_swarm.py` passed 6/6 and
`git diff --check` passed. A broader 153-test selection passed 153 tests but
had four environment import failures (`requests`, `prompt_toolkit`, and
`httpx` unavailable). A subsequent DB/core selection passed 379 tests and
had 29 pre-existing Windows/runtime-environment failures; none were new swarm
assertion failures. These environment failures remain open until CI provides
the authoritative repository result.

## Implementation review and reconciliation record

The implementation packet was cross-checked by distinct authenticated
reviewer families under the ticket's two-of-three rule:

- Claude: `PASS` on the metadata-only implementation packet;
- Grok: `PASS` on the same metadata-only implementation packet;
- AGY: `BLOCKED` at invocation because the DGX AGY CLI requires an interactive
  TTY in this session; no AGY verdict is counted.

The two PASS verdicts close the implementation review quorum. The review
confirmed the lane/skill binding, bounded goal/runtime settings, role-specific
completion validation, verifier-to-synthesizer fail-closed gate, result-field
boundary, parser safety, and no-schema/no-dispatcher scope. The only
reconciliation item found during the review was corrected before this record:
`native_hermes` now permits an empty preflight skill, while `claude`, `grok`,
and `agy` require a non-empty preflight-confirmed skill.

Post-reconciliation evidence: the focused swarm suite passed 6/6 using an
isolated test base directory, and `git diff --check` passed. No commit, push,
merge, CI, deployment, restart, or Telegram mutation has occurred yet.
