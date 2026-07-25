# Governed Risk Engine

Sigil Step 19 introduces deterministic, evidence-backed portfolio-risk analysis.

The engine evaluates explicit caller-supplied portfolio positions and policy limits. It does not fetch market data, place orders, resize positions, allocate capital, contact brokers, or mutate portfolio state.

## Capabilities

- Gross, net, long, and short exposure
- Deterministic leverage proxy
- Position, issuer, and sector concentration
- Liquidity estimates using supplied average daily volume value
- Weighted volatility and drawdown inputs
- Evidence requirements and provenance
- Deterministic risk scoring
- Review requirements and readiness blockers
- Immutable package identities
- Before/after package comparison
- Read-only audit helpers

## Governance Guarantees

Every package is analytical only. It does not authorize trading, capital allocation, order submission, or position mutation.
