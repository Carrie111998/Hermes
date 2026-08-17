---
title: "HERMES-RELAY-001: bounded Relay egress gate and final-args revalidation"
status: IMPLEMENTATION_REVIEW_PASS
date: 2026-08-17
type: security-reliability
ticket: HERMES-RELAY-001
target_repo: hermes-agent
---

# HERMES-RELAY-001: bounded Relay egress gate and final-args revalidation

## Status and gate

This is a repo-local ticket because the repository's GitHub issue state could
not be verified from the current authenticated session. The plan is the
source of truth until a separately verified issue is available.

Current gate: `IMPLEMENTATION_REVIEW_PASS`.

Implementation is complete in the isolated publish branch and has passed the
independent Claude and AGY review on the same metadata-only correction packet.
CI, merge, DGX deployment, runtime health, and Relay enablement remain
separate gates.

Required sequence:

1. Review this bounded design with exactly one authenticated Claude reviewer
   and one authenticated AGY reviewer using the same metadata-only packet.
2. Reconcile one correction set and obtain consensus before implementation.
3. Implement behind a disabled-by-default gate.
4. Run focused tests and supported-platform CI.
5. Re-submit the exact correction set and test evidence for independent review.
6. Enable only after separate merge, deployment, runtime-health, and delivery
   authorization gates.

No DGX mutation, Relay enablement, merge, deployment, or Telegram delivery
claim is included in this plan.

## User-visible problem

The upstream Relay tool-execution refactor wants Relay argument rewriting to
occur before Hermes policy and dispatch. In the current Hermes tree, sending a
raw tool payload to an external or managed Relay before the existing security
gates would create an ambiguous data-egress boundary. Conversely, applying
policy only to the original arguments would allow a Relay rewrite to escape
the policy decision.

The required behavior is therefore: authorize the minimum egress first,
obtain a bounded candidate rewrite, then re-run all security and side-effect
gates against the final arguments before any tool executes.

## Objective

Add an opt-in, fail-closed Relay execution boundary with two explicit phases:

1. `pre-relay egress gate`: decide whether the named tool and a minimized,
   redacted payload may enter Relay at all.
2. `final-args revalidation`: apply plugin hooks, guardrails, approval,
   checkpoint, and dispatch decisions to the final arguments returned by
   Relay in one ordered validation sequence before execution.

Relay is an argument-rewrite/proposal stage only. It must not execute Hermes
tools, mutate files, bypass approval, or emit a success result on its own.

## In scope

### R1. Pre-relay egress gate

- Add one centralized gate at the existing tool-execution boundary.
- Store activation at `config.yaml` key
  `relay.tool_execution.enabled`, default `false`; read it once at session
  setup and pass the immutable boolean into the execution boundary. No
  `HERMES_*` behavioral environment variable is introduced.
- Default to deny when Relay is unavailable, misconfigured, timed out, or
  unable to classify the payload.
- Permit only an explicit allowlist of tool classes and payload fields.
- The gate API is `pre_relay_egress(tool_name, args, context) -> decision`;
  `decision` contains only `allow`, a reason code, and a bounded payload.
  `allow` is possible only when the tool is allowlisted and every field is in
  the tool's field schema.
- Pass only the bounded schema payload: tool name, operation kind, path class
  (not the raw path), argument types, size/count metadata, and redacted
  non-sensitive options. Raw values are not part of the metadata contract.
- Any tool that can contain file contents, credentials, tokens, private URLs,
  chat identifiers, or user data is denied unless a separately declared
  metadata-only adapter exists; there is no generic fallback serializer.
- Never send `.env`, SSH material, API keys, bearer tokens, cookies, raw file
  contents, unredacted URLs, or message transcripts to Relay.
- Record only non-secret gate reason and a bounded status, not raw payloads.

### R2. Final-args revalidation

- Require Relay to return a typed JSON object with a bounded size.
- The integration point is the existing tool execution boundary: the gate
  constructs the Relay candidate request, the adapter returns a candidate
  object, and the boundary alone invokes the existing Hermes gates and
  dispatcher. Relay has no direct registry or file-system handle.
- Treat malformed output, unknown fields, argument type changes, or a changed
  tool name as a block.
- Run the existing pre-tool plugin hook on final arguments.
- Run the existing tool-loop guardrail on final arguments.
- Run edit approval on final arguments for `write_file` and `patch`.
- Create checkpoints from final paths/commands only after the gates allow
  execution.
- Dispatch at most once per process/turn, using the final arguments and the
  original tool-call identity. A process restart never automatically replays
  an in-flight call whose outcome is unknown.
- Emit a blocked result and terminal post-tool event when any gate rejects.

The execution claim is an in-memory, lock-protected record keyed by the
session/task and original tool-call id. A call must atomically transition from
`unclaimed` to `authorized` immediately before dispatch; a second claimant is
blocked. Each claim has a 30-second maximum lifetime, is released idempotently
on dispatch completion, gate failure, task cancellation, and turn/task end,
and is evicted by bounded-size cleanup. The table contains no raw arguments
and is not durable. A process crash loses the table; the ticket therefore
claims at-most-once dispatch per tool-call id within a live process/turn. A
process restart invalidates outstanding claims, and a resumed session must
mint a new tool-call id rather than reuse one whose claim may have been lost
mid-flight; crash-proof distributed exactly-once is out of scope.
After a crash or restart, an unknown in-flight outcome is surfaced as
blocked/unknown and is never automatically replayed. Durable cross-process
exactly-once is explicitly out of scope.

The metadata adapter contract is fixed and deny-by-default: it accepts only
`tool_name` and `operation_kind` strings, `argument_types` as a bounded map of
known field names to primitive type names, `size_bytes` and `item_count` as
non-negative integers, `path_class` from a fixed enum, and redacted boolean
options. The serialized payload is capped at 2048 bytes; unknown fields,
raw-value fields, non-primitive types, missing required metadata, or failed
redaction are rejected before egress. Adapter failure closes the candidate
request and returns the same bounded blocked result as any other gate failure.

### R3. Disabled-by-default activation

- Add the smallest existing `config.yaml` gate available for the optional
  Relay path; do not add a new user-facing secret or behavioral `.env` value.
- Default disabled for CLI, gateway, cron, Telegram, and delegated agents.
- Do not change prompt-cache bytes or rebuild a conversation system prompt.
- Do not add a new always-present core model tool.

## Out of scope

- Relay connector WebSocket or platform transport changes.
- Cloud inference, cloud OCR, cloud storage, or telemetry.
- Automatic DGX enablement or service restart.
- Changing approval policy, path restrictions, tool-loop semantics, or message
  alternation outside this boundary.
- Persisting raw arguments, fingerprints, transcripts, or user identifiers.
- Replacing Hermes dispatch with a second tool executor.

## Security invariants

The implementation is acceptable only if all of these remain true:

1. No Relay call occurs before the pre-relay egress gate returns allow.
2. A Relay result is never treated as authorization.
3. Every final argument reaches plugin hook, guardrail, approval, checkpoint,
   and dispatch checks in that order.
4. A failed or ambiguous gate blocks execution and does not silently fall back
   to raw Relay output.
5. Tool execution occurs at most once per original tool-call id within a live
   process/turn; after restart, unknown in-flight calls are blocked and never
   automatically replayed.
6. The default-disabled path is behaviorally identical to the current path.
7. Logs and test evidence contain statuses and bounded reason codes only.

## Verification contract

Focused deterministic tests must cover:

- disabled Relay: no egress and current dispatch behavior preserved;
- denied sensitive payload: no Relay call and no tool execution;
- allowed metadata-only payload: Relay receives only the approved fields;
- timeout, malformed JSON, oversized output, unknown field, and tool-name
  rewrite: fail closed;
- final-argument plugin block and modification;
- final-argument guardrail block;
- final-argument edit approval denial;
- checkpoint path taken from final arguments;
- at-most-once dispatch within one process/turn and original tool-call
  identity;
- sequential and concurrent execution paths;
- prompt-cache and message-role invariants.

Concurrency tests must use a deterministic two-worker harness with a
`threading.Barrier`, a locked event recorder, fixed release order, and
injected Relay and gate callbacks. The test uses stable fixture inputs rather
than random timing or a stress seed, and asserts event order, duplicate-claim
rejection, cleanup, and final result.
 A separate worker-exception test must assert idempotent release in `finally`;
 a process-restart scenario must assert that an unknown in-flight call is
 blocked and not automatically retried, rather than pretending an in-memory
 table is crash-recoverable.

When final-argument validation rejects, Relay receives a bounded cancellation
or close signal when supported, the candidate and execution claim are dropped,
no checkpoint or tool dispatch is performed, and one bounded blocked result is
emitted. A validation timeout follows the same cleanup path. Cleanup errors
are recorded as a non-secret reason code and cannot upgrade the call to
authorized.

Evidence must distinguish synthetic tests, supported Python CI, authenticated
review, DGX runtime health, and user-visible Telegram delivery. No one gate
may be upgraded from another gate's evidence.

The implementation must collect evidence with named pytest node IDs, a
metadata-only changed-file manifest, `git diff --check`, and CI run/check
identifiers. Review packets contain status codes and test names only; they do
not embed logs, source, paths, or generated artifacts.

## Review packet boundary

Reviewers receive only a metadata-only packet containing the ticket key,
scope, invariants, changed-file list, test command names, and status codes.
The packet must not contain source text, PDF/document contents, evidence text,
absolute paths, secrets, tokens, prompts, or generated artifacts.

Required reviewers: one real authenticated Claude session and one real
authenticated AGY session. A platform failure is `BLOCKED`, not a substitute
PASS. A `REVISE` result returns to the same family with the same correction
set after tests are rerun.

## Implementation authorization

Implementation is not authorized by this plan alone. It becomes authorized
only after both reviewer families return `PASS` on the same correction set.
Enablement is a later, separately authorized gate after merge, CI, deploy,
runtime health, and delivery evidence.

## Evidence log

- Design review: `PASS` (Claude+AGY consensus on the same packet)
- DGX Spark Claude design re-confirmation: `BLOCKED` on the latest packet; the
  authenticated reviewer declined to issue a formal verdict for a
  metadata-only packet without source/artifact inspection. Earlier packet
  rounds produced one PASS and later BLOCKED/REVISE results, so no PASS is
  carried forward to this final correction set.
- WSL AGY design re-confirmation: `PASS` on the latest packet. This is the
  valid AGY-family PASS retained for the consensus below; the later headless
  AGY permission block is not a contradictory verdict.
- DGX Spark headless Claude design re-review: `PASS` on packet SHA-256
  `BC7E3628808F962D0A4078A19FE1FD4FB68BC842B8804561AB09D8CBCFF83986`, using
  the authenticated Claude CLI in `/home/cwliao/.hermes` with tools disabled.
  No correction set was required.
- DGX Spark headless AGY retry: `BLOCKED`; headless mode auto-denied an
  attempted command because no explicit permission rule was available. The
  existing WSL AGY `PASS` above is retained because it reviewed this exact
  packet; no permission bypass was used.
- DGX Spark dedicated-agent re-confirmation: identity verified read-only on
  host `55-0940189-03`: Claude PID `1348802`, cwd `/home/cwlia/.hermes`,
  `pts/1`; AGY PID `1490875`, cwd `/home/cwlia/.hermes`, `pts/3`. No verdict
  was obtained: the SSH context exposed only slave TTYs, not an addressable
  interactive session/master, and the AGY log showed OAuth/TLS timeout
  failures. This is `BLOCKED`, not a reviewer PASS.
- Implementation: `AUTHORIZED`, isolated checkout only; Relay remains disabled
  by default and DGX was not modified.
- Focused tests: `PASS`; `27 passed` across the Relay revalidation and
  tool-call guardrail slices; compileall and `git diff --check` passed.
- Related plugin slice: `83 passed, 1 failed`; the single Nemo initialization
  failure reproduced identically on clean committed HEAD `9d1dbb2bb` and the
  implementation checkout. It is excluded under waiver
  `HERMES-RELAY-001-NEMO-BASELINE-WAIVER-20260817` and is not treated as
  implementation evidence.
- Implementation review packet SHA-256:
  `7BE0A28FFCEC836F76A7BD379DF0C7A0A06ABFB0FA15767ED064221E8E9CE60A`.
- Claude implementation review: `PASS` on the same corrected metadata-only
  packet after two evidence correction rounds; no correction set remains.
- AGY implementation review: `PASS` on the exact same packet in the
  authenticated DGX Spark `/home/cwliao/.hermes` environment; no correction
  set remains.
- Latest packet SHA-256 and plan SHA-256 are recorded in the session evidence;
  no older verdict is carried forward after a packet change.
- CI: `NOT_RUN`
- Merge: `NOT_AUTHORIZED`
- DGX deployment: `NOT_AUTHORIZED`
- Relay enablement: `NOT_AUTHORIZED`
- Re-review correction: live PID inspection confirmed both dedicated agents
  actually use cwd `/home/cwliao/.hermes` on host `55-0940189-03`; the earlier
  cwd spelling in this evidence entry is superseded. No new verdict was
  obtained because no safe PTY master or session-control channel was exposed;
  dedicated-agent review remains `BLOCKED`.
