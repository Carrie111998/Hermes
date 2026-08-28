# Project management: plans, gates, and what approval actually guarantees

Hermes has a plan-gate **kernel**: a representation for holding a task at a
human plan gate, the release path that would cross it, the refusals around it,
and the audit trail under it. It does not have a PM workflow you can run.
**No shipped CLI command or tool parks a task at a plan gate, and none releases
one.** This page is about what that gate is — and, more importantly, what it is
not.

The load-bearing architecture claims below are checked against the code by
`tests/hermes_cli/test_assurance_statement.py`,
`tests/agent/test_pm_plan_gate_guidance.py`, and
`tests/agent/test_pm_surface_contract.py`: the assurance statement's wording
and the locations that carry it, the gate's representation, the absence of an
approval surface, and the absence of a shipped surface that parks. The prose
around those claims is not individually pinned by a test. Where a thing is not
built, this page says so rather than describing the plan for it.

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
which acceptance locations are still deferred. The word-for-word equivalence
check in `tests/hermes_cli/test_assurance_statement.py` is written by hand
against the four implemented locations — one test per file, not generated from
a structured path manifest — so a fifth location added to the manifest needs
its own extraction written there too. The manifest test catches the mismatch;
it cannot write the extraction for you.

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

## Nothing shipped puts a task at a plan gate

`park_for_plan_approval()` in `hermes_cli/kanban_db.py` is the only writer of
`gate_state = 'plan'`, and it has **no production caller**. Outside that module
every non-test reference to it is a prose comment. The `hermes project` command
has no park verb, and `plan_submit` records a plan revision without binding a
task to anything.

So the plan gate is a kernel, not a workflow. Everything in the two sections
below describes what happens *once a task is gated* — which today only
same-user code calling the kernel directly can arrange. The tests that drive a
real gate transition, including the prompt byte-stability tests in
`tests/agent/test_pm_plan_gate_guidance.py`, park that way deliberately: they
are evidence about the kernel's behaviour, and they are not evidence that a
sanctioned path to reach it exists.

Building that path — a parking verb or tool — is not a documentation change.
It needs its own design and its own review, and it is not in this slice.

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

Authoring is the whole of the orchestrator's reach. It cannot park a task at a
gate and it cannot release one, because no tool on its surface does either.

Orchestrator profiles also receive `PM_PLAN_GATE_GUIDANCE` in their system
prompt, saying the same things this page says. Its wording is held to the
`plan_submit` schema and to the string that tool returns on success, because
those reach the same model in the same context: three surfaces that disagreed
about whether an approval path exists would be resolved by a capable model
going to look for it. Ordinary kanban workers receive none of it: they are
never spawned on a gated task, so they never meet a gate, and their prompt is
unchanged.

## Phases

`tasks.workflow_template_id` and `tasks.current_step_key` exist as columns and
can be filtered on, but **nothing writes them**. There is no workflow engine,
no phase transitions, and no `pm-v1` template in the codebase. Cards do not
carry phases today. Any description of a five-phase lifecycle is a plan, not a
feature.
