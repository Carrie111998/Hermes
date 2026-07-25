# Governed Portfolio Construction Engine

Sigil Step 20 converts approved analytical inputs into deterministic portfolio
proposals while preserving strict governance boundaries.

## Capabilities

- Equal-weight and score-weighted portfolio proposals
- Constrained score-weighted allocation
- Position, issuer, sector, gross-exposure, and position-count controls
- Liquidity, volatility, approval, and evidence gating
- Cash-reserve preservation
- Explainable inclusions and exclusions
- Deterministic target values and estimated share quantities
- Immutable package identities and tamper verification
- Package-to-package comparison
- Explicit analytical-only operation

## Governance boundary

The engine has no broker integration, order-routing capability, credential
handling, account mutation, or execution authority. Target values, weights,
and estimated shares are analytical proposals only.

## Package location

`sigil.portfolio_construction`

This isolated namespace avoids collisions with existing portfolio and risk
packages.
