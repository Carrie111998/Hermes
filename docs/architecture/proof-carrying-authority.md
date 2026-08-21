# Proof-carrying authority continuity

Tracked by [#90866](https://github.com/NousResearch/hermes-agent/issues/90866) and
the exact-object completion doctrine in
[#91230](https://github.com/NousResearch/hermes-agent/issues/91230).

## Invariant

A proof object must survive from admission to the actual effect and typed
settlement. Coordinates, configured intent, predecessor evidence, and surviving
output shape cannot be re-derived as authority downstream.

The repository-wide execution shape is:

```text
admit exact identity, scope, generation, and epistemic state
  -> carry immutable authority and provenance
  -> consume it at the real effect
  -> settle a typed outcome and retry authority
  -> publish evidence bound to the exact object
```

This is a composition contract, not a request for one universal runtime type.
Each subsystem may use its own physical representation, but it must preserve
the proof dimensions required by its consequential consumer.

The machine-readable contract is
[`authority-continuity.json`](authority-continuity.json). Its conformance test
prevents the active delivery graph from drifting back into duplicate authority,
inherited evidence, or child-count retirement.

## Global laws

1. Machine-readable repository state outranks narrative status prose.
2. A parent retires only after every surviving semantic invariant has a typed
   destination; child count alone is insufficient.
3. CI, review, and publication receipts never transfer across heads, sibling
   branches, parent objects, or rebuilt bytes.
4. Publication consumes runtime evidence. Runtime must not depend on campaign
   ledgers to authorize effects.
5. A non-injective transformation carries both survivor identity and proof of
   the input state it consumed.

## Active interlocks

### Webhook authority chain

```text
#85002 effective configuration
  -> #90995 canonical route/provider/verifier/delivery spine
  -> #90236 strict HTTP, raw-body, rate, and idempotency mechanics
     + #85318 explicit cryptographic verifier authority
  -> #90304 session and unattended-interaction admission
  -> #85644 per-target effect settlement and terminal partial truth
  -> #85638 mechanically derived documentation
  -> #85640 sole final assembler and real-listener proof
```

[#90989](https://github.com/NousResearch/hermes-agent/issues/90989) remains the
subsystem architecture owner. Provider, verifier, event, delivery identity, and
raw-body provenance are bound once. Downstream stages consume the immutable
envelope rather than rereading headers or re-deriving authority. Only the
composed head can receive integration evidence.

### MCP OAuth split

[#84963](https://github.com/NousResearch/hermes-agent/pull/84963) remains open
until two credited children exist and an exhaustive coverage map assigns every
surviving invariant to one of four dispositions:

- [#90888](https://github.com/NousResearch/hermes-agent/pull/90888), the OAuth
  lifecycle and callback-ownership child;
- the still-owed MCP 2.0 transport/control-plane child;
- behavior already present on current main; or
- explicit obsolescence under the current negotiation architecture.

Protocol-era discovery and OAuth metadata discovery remain orthogonal. The
split must not create a second era selector, transport factory, provider cache,
or credential authority.

### Durable compression projection

[#88551](https://github.com/NousResearch/hermes-agent/pull/88551) is the single
implementation owner. [#88740](https://github.com/NousResearch/hermes-agent/issues/88740)
and [#88758](https://github.com/NousResearch/hermes-agent/issues/88758) are
interlocked specification nodes, not independent rebase lanes.

A transformed durable projection needs two facts:

```text
_row_id
    exact durable identity represented by the surviving message

_row_id_watermark
    greatest raw durable row consumed by the full projection,
    including rows merged, stripped, or dropped
```

Caller stamping without projection-consumption proof can turn a safe fail-closed
restore into false-positive re-adoption.

### Publication contracts and channel ledgers

[#90307](https://github.com/NousResearch/hermes-agent/pull/90307) precedes the
Slack ledger in [#91036](https://github.com/NousResearch/hermes-agent/pull/91036)
and the Discord ledger in
[#90321](https://github.com/NousResearch/hermes-agent/pull/90321).

Human-readable status counts must be generated from, or independently checked
against, committed ledgers. A PR description is not a second status database.
The publication layer validates runtime evidence; it does not become runtime
authority.

### Documentation and terminal assembly

[#85638](https://github.com/NousResearch/hermes-agent/pull/85638) is a
projection of delivered behavior. It does not define that behavior.
[#85640](https://github.com/NousResearch/hermes-agent/pull/85640) remains the
sole terminal assembler, but its branch must be rebuilt from current main after
the behavior owners publish exact candidates.

### Hermes Tag vertical consumer

[#91111](https://github.com/NousResearch/hermes-agent/pull/91111) is a
governance kernel, not delivered runtime authority. The next implementation
must carry one immutable admission decision through a real
`pre_gateway_dispatch` consumer into a real `pre_tool_call` effect consumer and
typed receipt. Shadow evaluation is evidence; it is not effect enforcement.

### Discord voice V6

[#78180](https://github.com/NousResearch/hermes-agent/pull/78180) and
[#78196](https://github.com/NousResearch/hermes-agent/pull/78196) share one
voice-turn admission contract:

```text
captured utterance
  -> bounded transcription settlement
  -> transcript destination routing
  -> agent-turn admission
  -> optional TTS/media delivery
  -> terminal per-stage receipt
```

STT fallback cannot bypass transcribe-only mode. File-only routing cannot leak
the transcript into Discord. Disabled agent turns cannot suppress transcript
persistence.

### Windows updater bootstrap purity

[#60233](https://github.com/NousResearch/hermes-agent/pull/60233) is retained
for its invariant, not revived as a July implementation. The modern owner chain
is [#91316](https://github.com/NousResearch/hermes-agent/pull/91316) admission
followed by
[#91895](https://github.com/NousResearch/hermes-agent/pull/91895) durable
mutation authority.

The actor mutating a deployment generation must not execute from mutable or
native artifacts owned by that target generation. Settlement must observe the
admitted target generation at the admitted deployment root.

### Applied-secret provider egress

[#77162](https://github.com/NousResearch/hermes-agent/issues/77162) remains open.
[#77198](https://github.com/NousResearch/hermes-agent/pull/77198) is the active
implementation candidate, not current-main closure evidence. Closure requires a
provider-wire witness on the exact current-main candidate proving that an
externally applied secret under an arbitrary name is absent from the outbound
provider message.

### God-file campaign topology

[#78647](https://github.com/NousResearch/hermes-agent/issues/78647) owns the
campaign graph. The large `#77xxx`-`#79xxx` population is provenance inventory,
not an automatic rebase queue. KILL LOCK plus current-main truth selects the
surviving semantic slices. Only selected slices receive active delivery
authority, while authorship and supersession remain explicit.

## What this contract does not claim

This contract does not merge, release, or operationally verify any referenced
implementation. It records current architecture ownership, missing consumers,
retirement gates, and exact-object evidence rules so subsequent implementation
cannot silently recreate sibling authority.
