# Governed Portfolio Rebalancing Engine

Sigil Step 21 converts an approved Step 20 target portfolio package and a
current portfolio snapshot into deterministic, bounded rebalance proposals.

## Capabilities

- Current-versus-target drift analysis
- Buy, sell, and no-action determination
- New-position and full-exit governance
- Drift tolerances
- Minimum trade-value controls
- Maximum single-trade controls
- Maximum turnover controls
- Cash-aware portfolio valuation
- Deterministic proposed values and quantities
- Immutable package identities and tamper verification
- Rebalance-package comparison
- Evidence references and source target-package linkage
- Explicit analytical-only operation

## Governance boundary

The engine does not submit orders, connect to brokers, mutate accounts, access
credentials, reserve capital, or assert execution authority. Every output is a
proposal requiring downstream validation and approval.

## Package location

`sigil.portfolio_rebalancing`

The isolated namespace prevents collisions with existing construction,
portfolio-risk, and execution packages.
