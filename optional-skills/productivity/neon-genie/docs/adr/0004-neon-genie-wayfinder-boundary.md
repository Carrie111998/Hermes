# ADR 0004 — Neon Genie / Wayfinder boundary

## Status

Accepted

## Context

Product intent and engineering execution must not be owned by one skill.

## Decision

| Owner | Owns |
|-------|------|
| Neon Genie | what, why, user, boundary, proof |
| Wayfinder | decomposition, milestones, implementation status |

Handoffs set `product_intent_changes_require_neon_genie_review: true`.

## Consequences

- Wayfinder must not silently rewrite product intent.
- Run envelopes point Wayfinder at `run-envelope.json` as ingest entry.
