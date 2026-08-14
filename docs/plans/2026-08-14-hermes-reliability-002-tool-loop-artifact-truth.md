---
title: "HERMES-RELIABILITY-002: Cross-turn loop breaker and artifact truth"
status: READY_FOR_CI_REVIEW
date: 2026-08-14
type: reliability
ticket: HERMES-RELIABILITY-002
target_repo: hermes-agent
---

# HERMES-RELIABILITY-002: Cross-turn loop breaker and artifact truth

## Status and gate

This is a repo-local ticket because GitHub Issues are disabled for this
repository. The implementation is isolated to this branch. No DGX mutation,
deployment, merge, or Telegram delivery claim is included.

Current gate: `IMPLEMENTED_PENDING_REVIEW`.

Required next sequence: independent cross-review, reconcile any correction
set, then use separate CI, merge, and DGX deployment gates.

## User-visible problem

An unattended Telegram interaction can keep retrying a failed or non-progressing
tool path across multiple assistant turns and then report an artifact as
complete even when the requested file is missing, truncated, or malformed.

The observed DGX interaction attempted to write
`~/.hermes/memory/jokes.md`, although the authoritative memory path is
`~/.hermes/memories/MEMORY.md`. The interaction continued after missing-path,
approval, workdir, heredoc, and parse failures. The available DGX-side
`~/.hermes/skills/daily-briefing/jokes.jsonl` was not a valid JSONL document
under a parser check, so a success claim would not have been justified.

## Evidence boundary

The following observations motivate the ticket but are not themselves a fix:

- `agent/tool_guardrails.py` stores exact-failure, same-tool-failure, and
  idempotent no-progress state in per-controller dictionaries.
- `agent/turn_context.py` calls `reset_for_turn()` at the start of every
  turn, so current counts do not span turns.
- `agent/agent_init.py` selects an unattended hard-stop default for cron and
  non-interactive platforms, but `ToolCallGuardrailConfig.from_mapping()` lets
  an explicit `hard_stop_enabled` value override that default.
- The deployed DGX configuration explicitly sets
  `tool_loop_guardrails.hard_stop_enabled: false`.
- File mutation tools already perform post-write read-back in the patch path,
  and the turn-end coding verifier tracks failed file mutations. Those are
  useful primitives, but they do not prove an arbitrary requested document's
  schema, record count, completeness, or user-visible delivery.

Telegram transport reconnect failures and polling timeouts are a separate
failure class. They must not be silently treated as an agent-loop fix; they
need a companion transport ticket if still reproducible after this ticket's
scope is isolated.

## Objective

Make unattended execution fail closed when the agent repeats an unchanged
failed outcome across turns, and prevent completion language unless the
requested artifact has evidence appropriate to the requested operation.

## In scope

### U1. Cross-turn progress ledger

Extend the existing guardrail boundary with a bounded, in-memory,
session/task-scoped progress ledger. The ledger must record only non-secret
identifiers and outcomes, such as a tool name, normalized target identity,
failure class, and result fingerprint. It is not a new durable memory store or
SQLite schema.

The design must specify:

- the lifetime and ownership boundary (conversation/task, not global user
  state);
- bounded memory and expiry/eviction;
- reset conditions for genuine progress, a changed target, or an explicit
  operator intervention;
- how concurrent tool calls are serialized or reconciled; and
- a fail-closed result when the same failed outcome crosses the current
  per-turn reset boundary.

The identity used for a cross-turn blocker must be target-aware: changing
arguments for the same target and deterministic blocker must not evade the
breaker. A genuinely changed target or verified progress must reset the
relevant streak.

Raw arguments, credentials, file contents, and URLs with secrets must not be
persisted in the ledger.

The implementation must also define whether any identity/fingerprint is
allowed into transcript or log metadata. Raw paths, URLs, arguments, file
contents, user/chat identifiers, and linkable unkeyed digests must not be
durable output. If an internal fingerprint is needed, its scope, retention,
and redaction rules must be tested.

### U2. Terminal handling for deterministic blockers

Define a small, testable set of blocker classes that cannot be cleared by
blind retry, including missing target/path, denied approval, invalid working
directory, malformed command/script, and parse/schema failure.

When a blocker is reached, the agent must either change strategy with evidence
or return a controlled blocker to the user/operator. A warning alone is not
enough for unattended execution when the same blocker persists.

This ticket does not weaken approval gates, path restrictions, or Unicode
security scanning.

### U3. Artifact completion contract

Define an explicit completion contract for a declared file/document-producing
operation. The declaration may come from an existing skill, CLI workflow, or
tool result metadata; this ticket does not require the model to invent a
validator for every arbitrary file. At minimum, a successful machine-readable
completion record must distinguish:

- write persistence: the target exists and the write/read-back contract passed;
- content validation: the requested format/parser or an equivalent declared
  validator passed;
- completeness: any user-requested count, section, or boundary check passed;
- delivery: any requested downstream send/upload operation succeeded.

Before implementation, the ticket must name the authoritative producer and
the exact status vocabulary. The minimum proposed vocabulary is
`persisted`, `validated`, `complete`, `delivered`, `unverified`, and `blocked`,
with provenance for each positive status. A natural-language final answer is
not the producer of any positive status.

The implementation must not add a generic unconditional validator to the core
tool schema and must not pretend that a natural-language claim is proof.
Prefer extending existing file-operation results, a CLI/skill workflow, or a
service-gated verifier. A missing validator must produce `unverified` or
`blocked`, not a fabricated success claim; the final response can then report
that status plainly.

### U4. Unattended configuration contract

Review the current precedence where an explicit
`hard_stop_enabled: false` disables the unattended safety default. The
recommended contract is:

- `hard_stop_enabled: auto` (and a missing value) resolves to enabled for
  Telegram/cron and disabled for interactive CLI/TUI;
- explicit `false` remains an interactive opt-out. On unattended surfaces it
  resolves back to the safe default and emits a high-visibility warning;
- an unattended soft mode is permitted only through the separately named
  `tool_loop_guardrails.unattended_soft_mode: true` opt-in and remains visibly
  degraded at runtime; and
- config migration/default generation must avoid silently converting the
  shipped interactive default into an unattended opt-out.

The parser must define compatibility for legacy boolean values and the new
`auto` value. It must preserve the interactive opt-out, make unattended
degraded mode intentional and observable, and cover old config files with
migration/precedence tests.

## Implementation evidence

- `agent/tool_guardrails.py` now keeps a bounded in-memory target ledger for
  deterministic blocker classes, preserves it across turns, expires/evicts
  entries, resets a target after verified success, and clears it on session
  reset. Only the private controller keeps target identities; decision metadata
  no longer emits argument digests.
- `agent/turn_context.py` remains per-turn reset only; `run_agent.py` clears
  the ledger only during `reset_session_state()`.
- `tools/file_operations.py` is the authoritative artifact producer for
  `write_file`, `patch_replace`, and malformed V4A patch results. It emits the
  `hermes.artifact.v1` contract with `persisted`, `validated`, `complete`,
  `delivered`, `unverified`, or `blocked` state and read-back evidence. It does
  not claim delivery for file operations.
- `hermes_cli/config.py` ships `hard_stop_enabled: auto`, bounded-ledger
  defaults, and config version 36. Unattended explicit false is ignored unless
  `unattended_soft_mode: true`; the runtime logs the degraded-policy warning.
- `py_compile` passed for all changed Python modules.
- Focused test command passed: `52 passed` across guardrail, turn-context,
  runtime, patch verification, and config migration coverage. A broader local
  run also passed the relevant tests; two pre-existing Windows `find` fallback
  tests failed because the shell fixture cannot resolve Windows paths, and the
  full config run has environment-specific Hermes-home/log ACL failures. These
  are not represented as green CI evidence.
- No Telegram transport code, DGX runtime path, network call, or `/home/cwliao/.hermes`
  mutation was used by this implementation.

## Out of scope

- Telegram long-poll reconnect, HTTP client pool, or delivery retry changes;
- changing approval/security scan policy to make a command run automatically;
- writing directly to the DGX runtime or `/home/cwliao/.hermes`;
- treating a service-active check as proof of Telegram end-to-end delivery;
- adding a new always-present core model tool when an existing tool, CLI
  command, skill, or service-gated path is sufficient;
- automatic repair or cleanup of the existing `jokes.jsonl` artifact.
- changes to Telegram transport/reconnect code; agent-loop tests must be
  hermetic and make no network calls.

## Acceptance criteria

1. Focused tests prove that a repeated deterministic blocker is stopped or
   returned as a controlled blocker after crossing a turn boundary, including
   the same target with changed arguments; a genuinely changed target or
   verified progress can continue/reset.
2. A focused test proves that distinct blocker classes are not collapsed into a
   misleading generic success and that approval/path restrictions remain intact.
3. A real-path test using a temporary `HERMES_HOME` identifies the authoritative
   completion-status producer and proves the status transition from persisted
   through validation/completeness/delivery. Malformed or incomplete content
   remains `unverified`/`blocked` and cannot produce a completed-artifact
   result.
4. The selected unattended `hard_stop_enabled` policy is covered by config
   precedence and migration tests for Telegram/cron and interactive CLI/TUI,
   including missing, `auto`, explicit `false`, and explicit unsafe opt-in
   cases.
5. The ledger is bounded, session/task scoped, non-secret, and covered by
   reset/expiry/concurrency/privacy tests; no raw or linkable identity reaches
   durable transcript/log output.
6. The agent-loop implementation and tests do not modify Telegram transport
   code and do not make network calls; transport work is tracked separately.
7. The final report names separate evidence for local tests, GitHub CI, DGX
   service state, and Telegram user-visible delivery.

## Review questions

- Is a cross-turn ledger the smallest correct extension, or can an existing
  session/task state mechanism carry the same contract without new permanent
  infrastructure?
- Which artifact validators belong in existing file tools or skills, and which
  must remain caller-supplied?
- Should unattended explicit `hard_stop_enabled: false` remain supported as a
  visible degraded mode, or be rejected?
- What exact result metadata is safe and sufficient for the final response to
  distinguish persisted, validated, complete, and delivered?

## Review record

- Local independent review round 1: `REVISE`. Findings were an overly broad
  validator surface, an underspecified config-default migration, and wording
  that could be read as a natural-language enforcement promise.
- Revision applied: the ledger is explicitly in-memory and scoped, artifact
  validation is declaration-driven, completion is machine-readable, and the
  unattended config contract now has an `auto`/migration/explicit-opt-in
  direction.
- Local independent review round 2: `PASS for implementation planning` was
  superseded by the cross-review below; it is not a consensus result.
- Cross-review round 1, reviewer B: `REVISE`. Findings: artifact status
  schema/producer was not testable; same-target/different-argument retries
  could evade the breaker; ledger privacy did not constrain linkable digests;
  `auto` parsing/migration was undefined; and Telegram separation lacked an
  enforceable no-network criterion.
- Reconciliation: ticket revised with target-aware blocker identity,
  completion-status producer/provenance requirements, explicit privacy tests,
  named unattended soft-mode opt-in, legacy/`auto` parsing requirements, and
  a no-network/no-transport-code acceptance criterion.
- Cross-review round 1, reviewer A replacement: `BLOCKED`; no bounded final
  result was returned and the reviewer was safely closed after timeout.
- Cross-review disposition: `REVISE / CONSENSUS_BLOCKED`. The available
  independent review requires the revisions above, but a second completed
  reviewer result is still missing. Implementation proceeded only after the
  user's direct request to reimplement; the missing independent review remains
  an explicit gate before commit/merge/deploy.
- Direct AGY/Claude connector: unavailable in the current tool context; no
  claim of AGY/Claude review is made.
- Current routing preflight: DGX Spark SSH returned
  `Permission denied (publickey,password)`, so DGX review requires
  re-authentication. An existing WSL Claude process with MCP children was
  found, but no safe interactive dispatch bridge was verified; no new
  headless session was started and no external review result is claimed.
- Recursive review consensus update (2026-08-14): the stale blocked disposition
  above was re-opened after the implementation was revised. DGX Spark had no
  uniquely addressable live AGY/Claude session, so a bounded packet-only AGY
  fallback was used after binary/auth/reachability preflight. WSL had a logged-in
  Claude CLI but no safe bridge to the existing process, so a bounded read-only
  Claude fallback was used only after the DGX candidate was unavailable.
- Review round 2: Claude `REVISE` found target identity fallback collisions for
  URL- and name-based tools. The implementation added target keys and URL/name
  regression tests.
- Review round 3: Claude `REVISE` found skipped linters being reported as
  validated/complete, plus missing TTL/eviction, malformed-content, and
  migration coverage. The implementation now reports skipped formats as
  `persisted`, adds real-path malformed JSON and unvalidated JSONL tests, and
  adds the missing ledger/config tests.
- Review round 4: Claude `REVISE` found that syntax validation was still being
  called `complete`, that `patch_v4a` did not always emit an artifact contract,
  and that apply-phase failure lacked coverage. The implementation now uses
  `validated` for syntax-only success, emits `hermes.artifact.v1` for V4A
  validation/apply/success paths, and tests `unverified` apply failure.
- Final independent consensus: DGX packet-only AGY `PASS`; WSL packet-only
  Claude `PASS`. The correction set is accepted for the next CI/review gate.
  Local focused evidence is `83 passed, 1 warning`; the warning is the known
  Windows pytest cache ACL warning. A config migration test also logged an
  existing OpenRouter metadata attempt blocked by the Windows socket policy;
  this is not claimed as network-free CI evidence and is outside this ticket's
  implementation scope.
- Current disposition: `PASS_FOR_CI_REVIEW`. Commit, push, GitHub CI, merge,
  DGX deployment, runtime state, and Telegram delivery remain separate gates.
