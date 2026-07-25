# Governed Market Data Engine

Sigil Step 17 introduces an immutable, deterministic, evidence-linked market-data
normalization engine.

The engine consumes only explicit caller-supplied observations. It does not browse,
open network connections, subscribe to feeds, infer missing prices, approve orders,
allocate capital, or execute transactions.

## Guarantees

- Immutable observations, requests, policies, provenance, and packages
- Canonical identities for audit and replay
- Explicit source and evidence references
- Deterministic observation ordering
- Policy-controlled sources, data kinds, evidence, and freshness thresholds
- Required-field completeness checks
- Fresh, stale, and expired classifications
- Read-only audit and package comparison helpers
- Analytical-only output with no trading authorization

A package marked fresh or verified is data-quality information only. It is never
permission to trade.
