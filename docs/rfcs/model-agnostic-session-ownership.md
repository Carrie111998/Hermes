# Hermes-owned model-agnostic sessions

Status: Draft implementation

## Decision

Hermes owns the durable conversation session. Clients append one idempotent
turn to a stable Hermes session; they do not replay an independent transcript
as authority. Before every model call, one Hermes context compiler projects the
canonical snapshot into a bounded, provider-neutral `CompiledTurn` using typed
`ModelCapabilities`. Provider adapters translate that value to wire format and
normalize events back to Hermes.

Provider threads, response IDs, and cache handles are optional continuations.
They are bound to a Hermes session revision and context fingerprint, may be
discarded at any time, and are never required for correct reconstruction.

## Context

The normal Hermes loop builds a complete request from persisted history and
then routes it through normalized transports. The optional Codex app-server
runtime returns before that shared request assembly and submits only the
current user input to a provider-owned thread. Gateway agent recreation,
provider process loss, and model changes can therefore change what the model
knows even though Hermes retained the visible transcript.

Seeding a first Codex turn or durably persisting its thread ID repairs only one
provider path. It does not establish a contract for Anthropic, Gemini, local
models, fallback routes, or the next adapter added tomorrow.

## Contract

- `TurnCommand` carries stable session/turn/idempotency identity and the
  expected Hermes revision.
- A durable turn lifecycle (`accepted`, `running`, `completed`, `failed`, or
  `canceled`) and an expiring execution lease serialize inference across
  processes. A dead coordinator can be replaced only after its lease expires;
  a live or terminal delivery is returned to the caller without re-execution.
- Native tool calls are journaled by stable turn/tool-call identity before
  dispatch. Completed results replay without dispatch; abandoned no-effect
  calls may retry; abandoned effect-capable calls become explicitly uncertain
  and are never repeated automatically.
- `SessionSnapshot` is a read projection of ordered canonical Hermes events
  carrying stable event IDs.
- `SessionAuthorization` is the only input accepted by canonical snapshot and
  append APIs; it contains explicit authorized session IDs and never a profile
  database selector.
- `ModelInvocation` carries the complete candidate projection for any model
  call. `CompilationMessage.required` marks instructions and the full active
  user/assistant/tool suffix, so later tool-loop calls do not masquerade as new
  user turns.
- `ModelCapabilities` declares capacity and request-shape support.
- `compile_turn()` accounts for instructions, history, current input, tools,
  adapter overhead, and output reserve once.
- `CompiledTurn` includes a content fingerprint and a content-free receipt
  naming retained/omitted event IDs and component usage.
- `ContextCompilationFailure` stops dispatch before a provider call. Prior
  history never silently collapses to an unmarked current-message-only call.
- `ModelAdapter` consumes only `CompiledTurn` and emits canonical events.

## Migration

0. Land AGC-377 as a separate addon prerequisite: preserve the stateful tool
   dispatcher while normalizing and authorizing browse-produced session
   locators before upstream dispatch. Keep its issue, PR, and verification
   independent from this broader architecture.
1. Land and exercise the contracts without changing existing provider paths.
2. Compile the normal loop's already-sanitized request through this boundary.
   Recompile after model/provider fallback using that destination's resolved
   runtime capacity.
3. Move Codex app-server behind the same compiler; serialize the compiled turn
   inside its adapter without independently selecting or budgeting history.
4. Add a native append-turn/session-event API and move Desktop to it. The v1
   gateway surface is `session.create.v1`, `session.open.v1`,
   `session.snapshot.v1`, `session.turn.append.v1`, the existing canonical
   stream events, and `session.turn.cancel.v1`. Native methods accept an exact
   session ID bound to the current client transport and never a profile or DB
   selector. A native turn forces derived compaction to remain in-place so a
   legacy compression rotation cannot change the command's stable session ID.
5. Add optional continuation reuse only after restart/model-switch tests pass
   with provider state forcibly disabled.
6. Delete provider-specific history-preamble and client-side budget compilers.

Hermes now exposes a canonical `SessionDB.read_session_snapshot()` plus
`SessionAuthorization`, an atomic revision-checked/idempotent
`SessionDB.append_turn()` primitive, durable execution leases, and the native
v1 gateway surface above. Desktop and native tools still have to adopt that
surface before their compatibility bridges can be deleted.

The initial journal is an evolutionary schema over Hermes' existing message
store: message rows are immutable source/checkpoint events, each native write
receives a per-session monotonic event revision, and transcript rewrites append
a `session_projection_revisions` transition containing exact source and
projected event IDs. Snapshot reads rebuild from those transitions rather than
trusting mutable `active` flags. Existing pre-v28 rows use their durable row ID
as the migration revision floor; no startup copy of large databases is needed.
Checkpoint rows expose their source-event lineage in `SessionSnapshot` and are
therefore derived and rebuildable. Session revisions also include sidecar and
projection transitions, so compaction, rewind, and model-visible sidecar
changes cannot make a client's expected revision move backward.

The addon `ScopedSessionDB` / `session_search_scope.py` is explicitly a
migration bridge during these slices. It reads SQLite internals, infers
compression lineage, and substitutes a discovery identity so today's Desktop
compatibility path can fail closed. Its deletion condition is a canonical
Hermes session snapshot/reader plus authorization contract that native
stateful tools consume directly. Once that contract exists, this proxy must be
removed; tool authorization must not remain coupled to raw database internals.

## Compatibility and caching

The compiler is a projection, not a transcript rewrite. Canonical events remain
append-only, and derived checkpoints retain source-event lineage. Stable
prefixes remain byte-stable unless normal compression/checkpoint selection
changes them. Existing OpenAI-compatible endpoints may translate into the same
session command internally while external stateless behavior remains available.

## Deferred decisions

- long-term pruning/retention policy for immutable source events after an
  explicit user erasure request;
- checkpoint re-generation policy beyond structural lineage validation;
- tokenizer plugin interface and estimator calibration;
- tool-catalog routing/lazy schema discovery policy;
- native session API transport version negotiation beyond the initial v1
  snapshot/append contract.
