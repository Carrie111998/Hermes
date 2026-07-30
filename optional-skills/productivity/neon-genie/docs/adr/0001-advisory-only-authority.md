# ADR 0001 — Advisory-only authority

## Status

Accepted

## Context

Neon Genie influences product and commercial decisions. Agents that can spend,
publish, or mutate repositories create unacceptable risk.

## Decision

All Neon Genie outputs are **`authority: advisory_only`** with
**`grants_execution: false`**. The packaging CLI and envelopes enforce this.

## Consequences

- Downstream systems must obtain separate authorization for execution.
- CI and doctor reject envelopes that claim execution rights.
- Learning ledger entries cannot auto-apply to the corpus.
