# Hermes Kanban + OpenSpec Enforcement

## Overview
Hermes Kanban has been extended to strictly enforce OpenSpec change compliance at the state transition layer. All tasks that declare an `openspec_change` MUST satisfy OpenSpec artifact verification before their status can transition from `ready` to `running`, or from `running` to `review` / `done`. 

This prevents phantom implementation without an underlying specification, protecting the core principle of spec-first development.

## Enforcement Mechanism

### The SQLite Triggers
The enforcement is embedded directly in the Kanban SQLite database schema (`kanban.sql`), making it impossible to bypass via API, CLI, or script wrapper.

- `BEFORE UPDATE` Trigger: Fires when `status` is updated.
- `BEFORE DELETE` Trigger: Fires if a deletion is attempted (preventing removal of OpenSpec tasks without explicit archiving).

### Transition Rules for `openspec_change` tasks
1. **To `running`**: The declared change MUST exist as a directory in `openspec/changes/<change-name>`.
2. **To `review` / `done`**: The change MUST have valid OpenSpec artifacts:
   - `proposal.md`
   - `specs/<capability>/spec.md` (at least one)
   - `design.md`
   - `tasks.md`

### Hard Rejection
If a transition violates the OpenSpec rules, the SQLite trigger aborts the transaction with a clear string error:
- `OpenSpec violation: Cannot start task. Change directory 'openspec/changes/<change-name>' does not exist.`
- `OpenSpec violation: Cannot complete task. Missing required artifacts for change '<change-name>'.`

## Operating & Monitoring

### Activation
The triggers are created automatically upon database initialization or migration:
```bash
hermes kanban migrate
```

### Monitoring & Validation
Any client interacting with the Kanban DB (Dashboard, API, CLI) must capture `sqlite3.IntegrityError` (or language equivalent) during UPDATE.
Dashboard APIs catch this and return HTTP 400 with `error_type: "openspec_enforcement"`.

### Remediation Workflow
If an agent or human gets blocked by an OpenSpec violation:
1. **Cannot start (`ready` -> `running`)**: Create the change directory and begin the exploration/proposal phase. Do not hack the task's `openspec_change` string to empty.
2. **Cannot complete (`running` -> `done`)**: You skipped generating the artifacts. You must write `proposal.md`, a `spec.md`, `design.md`, and `tasks.md`.

### Rollback Procedure
If the strict enforcement must be temporarily lifted (e.g. for emergency migration or hotfix out-of-band), drop the triggers via native sqlite3:

```sql
DROP TRIGGER IF EXISTS trg_kanban_tasks_openspec_enforcement;
DROP TRIGGER IF EXISTS trg_kanban_tasks_openspec_delete_lock;
```
*Note: This violates governance. Only use during critical incidents.*

## Traceability to Proposal
This enforcement was defined and authorized by the **`kanban-openspec-enforcement` OpenSpec Change**.
- Proposal: `openspec/changes/kanban-openspec-enforcement/proposal.md`
- Spec: `openspec/changes/kanban-openspec-enforcement/specs/kanban-openspec-enforcement/spec.md`

All features, including unit and integration test assertions (`tests/test_kanban_schema.py` and `tests/test_kanban_integration.py`), map directly to the requirements in `spec.md`.