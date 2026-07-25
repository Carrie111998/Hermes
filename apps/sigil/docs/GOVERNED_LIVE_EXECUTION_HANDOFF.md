# Governed Live Execution Handoff

Sigil Step 32 converts a successfully admitted live order into a deterministic,
immutable execution envelope for a separately governed live broker adapter.

## Guarantees

- Only Step 31 `ADMITTED` orders can become execution-ready.
- Duplicate admission IDs are rejected by default.
- Stale and future-dated admissions are blocked.
- Handoff authorization must match admission authorization.
- Evidence is normalized and preserved.
- Failed decisions never contain an execution envelope.
- Handoff and envelope identifiers are deterministic.
- Step 32 does not contact a broker or submit an order.

## States

- `REJECTED`: a governance prerequisite failed.
- `DUPLICATE`: the admission was already handed off.
- `EXPIRED`: the admission exceeded its freshness window.
- `READY`: the immutable envelope may enter the next governed stage.

`READY` does not mean submitted, acknowledged, filled, reconciled, or profitable.
