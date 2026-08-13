# Hermes-owned model-agnostic sessions

Status: Proposed

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
- `SessionSnapshot` is a read projection of ordered canonical Hermes events
  carrying stable event IDs.
- `ModelCapabilities` declares capacity and request-shape support.
- `compile_turn()` accounts for instructions, history, current input, tools,
  adapter overhead, and output reserve once.
- `CompiledTurn` includes a content fingerprint and a content-free receipt
  naming retained/omitted event IDs and component usage.
- `ContextCompilationFailure` stops dispatch before a provider call. Prior
  history never silently collapses to an unmarked current-message-only call.
- `ModelAdapter` consumes only `CompiledTurn` and emits canonical events.

## Migration

1. Land and exercise the contracts without changing existing provider paths.
2. Compile the normal loop's already-sanitized request through this boundary.
3. Move Codex app-server behind the same compiler; serialize the compiled turn
   inside its adapter without independently selecting or budgeting history.
4. Add a native append-turn/session-event API and move Desktop to it.
5. Add optional continuation reuse only after restart/model-switch tests pass
   with provider state forcibly disabled.
6. Delete provider-specific history-preamble and client-side budget compilers.

## Compatibility and caching

The compiler is a projection, not a transcript rewrite. Canonical events remain
append-only, and derived checkpoints retain source-event lineage. Stable
prefixes remain byte-stable unless normal compression/checkpoint selection
changes them. Existing OpenAI-compatible endpoints may translate into the same
session command internally while external stateless behavior remains available.

## Deferred decisions

- persisted event-journal schema/version migration from the current message
  rows;
- checkpoint validation and rebuild policy;
- tokenizer plugin interface and estimator calibration;
- tool-catalog routing/lazy schema discovery policy;
- exact native session API transport and version negotiation.
