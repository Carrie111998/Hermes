# Project management: plans, gates, and what approval actually guarantees

Hermes' PM workflow puts a human plan gate in the middle of an otherwise
autonomous board. This page is about what that gate is — and, more importantly,
what it is not.

Every factual claim below is checked against the code by
`tests/hermes_cli/test_assurance_statement.py` and
`tests/agent/test_pm_plan_gate_guidance.py`. Where a thing is not built, this
page says so rather than describing the plan for it.

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

This text is canonical. `approval_broker.ASSURANCE_STATEMENT` is the source,
and `approval_broker.ASSURANCE_LOCATIONS` lists where it is reproduced and
which acceptance locations are still deferred.

## How a plan gate is represented

A gated task is parked in the **existing** `scheduled` status — already
non-dispatchable — with `tasks.gate_state` set:

```text
status     = 'scheduled'
gate_state = 'plan'
```

No new task status was added. `VALID_STATUSES` is unchanged
(`triage, todo, scheduled, ready, running, blocked, review, done, archived`),
so every literal status guard in the dispatcher behaves exactly as it did
before. A `scheduled` task with `gate_state IS NULL` is an ordinary time-parked
task and is untouched by any of this.

`VALID_GATE_STATES` declares `{"plan", "deploy"}`, but **only `plan` has a
writer**. There is no deploy gate: nothing parks a task at one, and nothing
releases one. The constant reserves the name.

## What being gated does

For any actor following the sanctioned path:

- the task is not dispatched and not auto-promoted;
- it does not satisfy its children's dependencies;
- an approval cannot be replayed, re-targeted to another project or revision,
  or applied to a plan whose text changed after it was read — the binding is
  CAS on `(project_id, revision)` plus `UNIQUE(subject, binding_hash)`;
- every release and every refusal is audited in the same transaction as the
  state change, so the ledger and the state cannot disagree;
- gate notifications are passive. `WAKE_KINDS` is *derived* as
  `TERMINAL_KINDS − NEVER_WAKE_KINDS`, and the plan-gate kinds are in
  `NEVER_WAKE_KINDS`, so a gate can never wake an agent toward the gate it is
  forbidden to cross.

## What being gated does not do

**No approval surface ships.** `resolve_plan_approval_adapter()` returns
`None`, there is deliberately no configuration key that names an adapter, and
`for_plan_decision()` raises `NoApprovalSurfaceError` on every call before
doing anything else. The CLI displays the authoritative plan and refuses to
decide. `release_plan_gate` is the only way out of `gate_state='plan'`, and it
requires an `Attestation` that nothing shipped can mint.

`issue_attestation_for_adapter()` is a constructor, not an authentication
boundary. It authenticates nothing; calling it asserts that the caller has
already established human presence somewhere else.

**Same-user processes remain outside the boundary.** Same-user Python can call
the issuer directly or monkeypatch the adapter seam, and both produce an
attestation `release_plan_gate` accepts. More fundamentally,
`UPDATE tasks SET gate_state = NULL` needs no Hermes code at all. No
application-level change closes that, and none is attempted.

So: **cooperative workflow integrity, not a security boundary.** A real
boundary needs authoritative state and authentication outside the
agent-writable trust domain — a separately authenticated remote service, or a
separate OS identity owning the database. Neither is started. See
`planning/M3B-ARCHITECTURE-RECONCILIATION.md`, which supersedes the
architecture section of the master specification on exactly this point.

## Authoring a plan

`project_ensure` and `plan_submit` are orchestrator-scoped tools: available to
a profile with the `kanban` toolset that is **not** scoped to a single task.
A dispatcher-spawned worker does not get them, and a delegated child is refused
twice — by the `check_fn` and again at the handler.

Drafting is not approving. Neither tool changes a task status, clears
`gate_state`, or writes `pm_approvals`. Submitting again supersedes the
previous revision, which is why re-submitting to force a decision only creates
more for a human to read.

Orchestrator profiles also receive `PM_PLAN_GATE_GUIDANCE` in their system
prompt, saying the same things this page says. Ordinary kanban workers do not:
they are never spawned on a parked task, so they never meet a gate, and their
prompt is unchanged.

## Phases

`tasks.workflow_template_id` and `tasks.current_step_key` exist as columns and
can be filtered on, but **nothing writes them**. There is no workflow engine,
no phase transitions, and no `pm-v1` template in the codebase. Cards do not
carry phases today. Any description of a five-phase lifecycle is a plan, not a
feature.
