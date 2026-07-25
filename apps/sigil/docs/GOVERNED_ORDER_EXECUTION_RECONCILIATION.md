# Governed Order Execution Reconciliation

## Purpose

Sigil Step 23 introduces a governed execution and reconciliation layer between
approved order intent and broker-side execution evidence.

This layer does not create autonomous trading authority. It preserves the
approval/execution boundary, validates execution admission, creates
deterministic submission requests, records provider acknowledgements, and
reconciles broker fills against approved order intent.

## Core capabilities

- Validate approved order-intent packages before execution submission.
- Enforce provider, account, environment, quantity, notional, turnover,
  evidence, and approval policies.
- Preserve human approval and explicit live-execution boundaries.
- Generate deterministic request and client-order identifiers.
- Prevent submission when admission is blocked.
- Detect execution-adapter provider mismatches.
- Represent successful, failed, and uncertain submission outcomes.
- Reconcile acknowledgements, broker snapshots, and fills.
- Calculate:
  - filled and remaining quantity
  - weighted-average fill price
  - gross executed notional
  - fees
  - net cash effect
  - price slippage
  - slippage in basis points
- Detect partial fills, overfills, duplicate evidence, foreign fills,
  quantity mismatches, excessive fees, and excessive slippage.
- Produce immutable, evidence-backed audit events.
- Compare governed execution packages deterministically.

## Governance model

Execution remains gated by the approved order-intent package and its associated
approval request and approval record.

An order is not admitted unless:

- the package is ready for approval
- the approval record contains an approve decision
- the approval record identifies the required human approver
- approval and execution remain separate actions
- the provider, account, account class, and environment are permitted
- execution limits are satisfied
- required evidence is present
- the order remains analytical-only before governed submission
- no execution authority is embedded in the source intent

Live execution is disabled by default and requires both policy permission and
an explicit live-execution request.

## Submission lifecycle

The governed submission engine supports these outcomes:

- `not_submitted`
- `failed`
- `uncertain`
- `awaiting_reconciliation`

A provider timeout or ambiguous transport result is represented as uncertain
rather than being silently retried. This protects against duplicate orders.

## Reconciliation behavior

Broker acknowledgements, order snapshots, and fills are reconciled against the
approved submission request.

The reconciliation result records:

- approved quantity
- filled quantity
- remaining quantity
- weighted-average execution price
- gross executed notional
- total fees
- net cash effect
- slippage
- discrepancies
- blockers
- warnings
- evidence references

Overfills and other material discrepancies are blocking. Partial fills may be
accepted or blocked according to policy.

## Determinism and auditability

Request identifiers, client-order identifiers, audit identifiers, comparison
results, policy snapshots, blockers, warnings, and evidence references are
normalized and deterministically ordered.

All core execution models are immutable dataclasses.

## Safety boundaries

This step does not:

- choose investments
- generate trading intent
- approve trades
- bypass human approval
- grant autonomous spending authority
- conceal uncertain provider outcomes
- silently retry ambiguous submissions
- permit unrestricted live execution

## Test coverage

The Step 23 test suite covers:

- public API exports
- deterministic identifiers
- canonical JSON
- deterministic policy snapshots
- policy limit validation
- immutable audit events
- execution enum stability
- full-fill reconciliation
- partial-fill reconciliation
- overfill blocking
- deterministic submission request creation
- blocked admission
- adapter-provider mismatch
- successful submission acknowledgement
- adapter submission failure
- uncertain submission outcome
