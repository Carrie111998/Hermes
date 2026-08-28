# Project management: plans, gates, and what approval actually guarantees

Hermes' PM workflow puts two human gates in the middle of an otherwise
autonomous board. This page is about what those gates are for, and — more
importantly — what they are not.

## The assurance statement

> Hermes approval gates are an integrity control, not a security boundary
> against arbitrary code execution as your own user account.
>
> Hermes workers run as the same OS user as you, with terminal and filesystem
> access. Any process running as that user can read the credentials on disk.
> Approval gates therefore provide strong, tested protection against:
>
> - a worker or agent approving its own plan (accidental self-approval),
> - approval reached through ordinary tool invocation, on any surface,
> - prompt injection that emits an approval command,
> - approval of a stale plan whose text has since changed,
> - replay of a previous approval,
> - confused-deputy approval of a different project or board.
>
> They do not provide a cryptographic boundary against a deliberately
> adversarial process running as your user. Production deployment additionally
> requires an authenticated surface whose credentials are not deliberately
> exposed to workers, which raises the cost of bypass without eliminating it
> under full local compromise.
>
> A genuine adversarial-agent boundary requires an independent security domain:
> separate OS identities, an external approval service the agent cannot reach,
> or hardware-backed confirmation. None of these is in Phase 1.

This text is canonical. It is reproduced in
`hermes_cli/approval_broker.py`, the `pm_approvals` schema comment, this page,
and `tests/hermes_cli/test_approval_broker.py`, and
`tests/hermes_cli/test_assurance_statement.py` fails if any copy drifts.

## The two gates

| Gate | Status | Who crosses it |
|---|---|---|
| 1 | `awaiting_approval` | a human, through an authenticated surface |
| 2 | `ready_to_deploy` | a human, at strength `strong` or above |

Both are **dispatcher-inert**: no `task_runs` row is ever created for a task in
a gate status, because `SPAWNABLE_STATUSES` is derived
(`VALID_STATUSES − GATE − TERMINAL`) rather than listed, so a status added later
cannot be forgotten into spawnability.

Gate events are **passive notifications**. They are excluded from `_WAKE_KINDS`
by derivation, so a gate can never wake an agent toward the gate it is
forbidden to cross.

## Phases

A card under the `pm-v1` workflow template carries a phase in
`current_step_key`: `planning`, `research`, `building`, `qa`, `deploy`. A worker
finishes its own phase and hands off; it does not carry a card across a phase
boundary it was not assigned.

## There is no local approval authority

`approval_broker.for_plan_decision` fails closed. Nothing in this slice can
mint an approval locally — not the CLI, not a tool, not a worker, not cron.
An earlier revision confirmed human presence by reading a fixed phrase from
`/dev/tty`; that design was defeated twice and withdrawn, and the reasoning is
recorded in the module docstring. A future authenticated adapter mints an
`Attestation` through `issue_attestation_for_adapter`, and the consuming
machinery — subject, binding hash over the exact plan bytes, single-use nonce,
TTL, and the one-transaction release — accepts it unchanged.
