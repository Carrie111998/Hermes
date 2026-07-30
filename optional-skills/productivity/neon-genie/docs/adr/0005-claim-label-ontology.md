# ADR 0005 — Claim label ontology

## Status

Accepted

## Context

Agents blur evidence and invention. Operators need fail-closed promotion.

## Decision

Material claims use exactly:

- `OBSERVED` — sourced
- `INFERRED` — derived from observed
- `SPECULATIVE` — model prior / untested
- `NOT_COMPUTABLE` — missing after find/request

Protocol: public → research; private → `DataRequest`; then `NOT_COMPUTABLE`.

## Consequences

- Model prior is never `OBSERVED`.
- Behavioral suite and transcripts enforce labels.
- Promotion cannot override mandatory gate failures.
