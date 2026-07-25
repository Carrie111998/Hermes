# Governed Paper Trading Session Lifecycle

Sigil Step 25 introduces an explicit, auditable lifecycle for bounded paper
trading sessions.

## Lifecycle

A session progresses through governed states:

1. `prepared`
2. `active`
3. `paused` and optionally `active` again
4. `closing`
5. `certified` or `failed`

Invalid transitions are rejected. Certified and failed sessions are terminal.

## Safety boundaries

- Sessions are restricted to `provider="paper"`.
- Sessions always use the `PAPER` execution environment.
- Maximum order count and gross notional are policy bounded.
- Activity can only be recorded while a session is active.
- Certification can require zero open orders.
- Certification can require every submitted order to be reconciled.
- Session and event identifiers are deterministic.
- Lifecycle evidence is immutable and deduplicated.
- No live brokerage credentials, network submission, or real-money execution
  are introduced by this milestone.

## Recorded session evidence

The lifecycle records:

- operator identity
- account identity
- preparation, start, pause, resume, closing, and terminal timestamps
- submitted, reconciled, and open order counts
- gross notional
- realized paper profit and loss
- simulated fees
- failure reason
- deterministic lifecycle events
- supporting evidence references
