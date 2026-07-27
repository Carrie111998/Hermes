# Authority, Goal, Task, and Delegation Lifecycles

## Authority

Initial setup creates a standing charter. Deterministic policy checks exact
capability, target system/resource, risk, reversibility, spend, resource
limits, prohibited actions, and approval requirements. A permit binds the
approved payload and expires. The planner cannot expand its own authority.

Charter revisions append a new Founder/CEO mandate version and revoke stale
unconsumed permits, approvals, and task grants.

## Objective lifecycle

```text
proposed -> accepted -> planned -> authorized -> executing
                                      |             |
                                      v             v
                                   blocked       completed -> verified -> closed
```

Cancellation, expiry, abandonment, and supersession are explicit. Plans are
immutable versions. Objective completion requires registered success-criteria
verifiers; generated prose is not completion evidence.

Objectives created by the active Founder/CEO using the canonical
`employee:<active-ceo-id>` identity inside the organization's standing
authority scope transition from `proposed` to `accepted` during the
runtime cycle. That transition is recorded as durable lifecycle evidence and
does not require a routine advisor dispatch. Objectives originating outside
that CEO authority scope remain proposed and create an evidence-bearing
`objective_acceptance_required` advisor handoff.

Each objective carries a durable `reaffirmed_at` timestamp. When
`agentic.reaffirmation_ttl_seconds` elapses, the runtime blocks the objective
and opens an `objective_reaffirmation_required` advisor handoff. Execution
resumes only after evidence-bearing reaffirmation refreshes intent and emits a
new wake event.

## Task lifecycle

Kanban tasks move through ready, claimed/in-progress, completed, blocked, or
failed states. Claims and leases prevent concurrent execution. Retries use
idempotency keys and bounded failure counters. Recurring failures trip a
circuit breaker and create an intervention rather than retrying forever.

## Delegation

An employee task grant binds:

- organization, objective, candidate action, manager, employee, profile;
- exact mandate ID and version;
- title/body hashes;
- capabilities, systems, toolsets, skills, budget, and expiry.

At launch, the dispatcher loads toolsets and skills from the immutable grant,
not from a broader profile. The worker refuses mismatches. At handoff, current
employment, mandate, task binding, and result authority are checked again.
Comments, email, web content, and task prose cannot expand the grant.
