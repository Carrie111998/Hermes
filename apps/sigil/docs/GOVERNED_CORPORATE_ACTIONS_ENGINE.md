# Governed Corporate Actions Engine

Sigil Step 18 introduces immutable, deterministic, evidence-linked corporate-action normalization.

The engine consumes only explicit caller-supplied events. It does not browse, subscribe to market feeds, contact brokers, mutate positions, submit elections, authorize trades, allocate capital, or execute transactions.

## Guarantees

- Immutable events, policies, requests, provenance, packages, and adjustment instructions
- Deterministic canonical identities and input-order-independent package construction
- Explicit evidence and source provenance
- Governed support for dividends, splits, mergers, acquisitions, spin-offs, symbol changes, offers, rights, delistings, and liquidations
- Validation of required ratios, cash amounts, currencies, target instruments, and symbols
- Duplicate and conflicting-event detection
- Read-only package comparison and audit helpers
- Analytical adjustment instructions that always require human review
- No position mutation and no trading authorization

A verified corporate-action package remains informational. It is never permission to trade or modify an account.
