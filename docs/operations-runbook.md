# Operations, Backup, and Failure Recovery

## Start and inspect

```bash
uv run charterforge doctor
uv run charterforge business status
uv run charterforge gateway --help
uv run charterforge logs
```

Confirm the charter, capital, deadlines, interventions, provider assessments,
and stop conditions before enabling unattended execution.

For automation and container health checks, use the explicit gate form:

```bash
uv run charterforge business readiness --check
```

It emits the same JSON projection and exits `0` only when readiness is true;
blocked or unconfigured state exits `1` without mutating authority state.

## Normal escalation

An advisor intervention should include the objective, exact blocker, current
evidence, proposed choices, authority needed, expiry, and affected permits.
Do not answer by editing databases or broadening a credential. Change policy or
issue a narrowly bound approval through the supported control surface.

## Emergency stop

1. Stop the gateway and standalone runtime processes.
2. Stop external schedulers and the Kanban dispatcher.
3. Revoke payment, email, deployment, and infrastructure credentials at their
   providers if compromise is suspected.
4. Preserve databases and logs before repair.
5. Record the incident and exact last known remote commit.
6. Resume only after integrity, authority, and provider read-back checks pass.

When authority mode becomes `paused` or `manual`, the governed worker records
`autonomy_paused` and exits its execution loop. A process supervisor may
restart it only after the operator explicitly resumes autonomous mode.

## Backup

Use the implemented backup command:

```bash
uv run charterforge backup --help
```

Before relying on a backup, inspect the command options for the installed
version. Back up the entire canonical state root, including authority and
Kanban databases, config, profiles, audit artifacts, and required secrets via
the organization’s separate secret-management procedure. Do not commit any of
these artifacts.

## Restore

1. Stop all writers.
2. Preserve the damaged state separately.
3. Restore into an empty, access-controlled state root.
4. Run database integrity and authority-integrity checks.
5. Verify current provider state for payments, messages, deployments, and
   commitments before allowing retries.
6. Treat claimed/in-progress external actions as indeterminate until read-back
   proves whether they occurred.
7. Reissue only expired or revoked authority; never rewrite old evidence.

## Failure classes

- **Process crash:** lease expiry permits safe reclaim; external actions still
  require idempotent read-back.
- **Lost lifecycle wake:** housekeeping requeues accepted/planned objectives
  that have no pending or processing event, fenced by objective version; blocked
  objectives remain stopped until an advisor resolves their intervention.
- **Rate limit:** bounded exponential backoff; do not rewrite integrations.
- **Repeated task failure:** circuit breaker and advisor intervention.
- **State drift:** compare configuration, package lock, schema, profiles, and
  provider state to the recorded baseline.
- **Runtime baseline drift:** when enabled, `charterforge business runtime-drift`
  reports the exact baseline mismatch and autonomy pauses. Inspect the host and
  provider state, then use `charterforge business runtime-rebaseline` with a
  human reason only after accepting the change; resume autonomy separately.
- **Stale intent:** when an objective's reaffirmation window expires, the
  runtime blocks it and opens an `objective_reaffirmation_required`
  intervention. Verify that the mission and constraints still apply before
  resolving with `reaffirm`; do not extend the window by editing the database
  directly.
- **Conflicting workers:** claims and resource leases select one writer.
- **Accounting interruption:** reconcile provider records before posting or
  reversing journal entries.
- **Recovery unavailable:** the supervised objective worker stops on a
  `recovery_blocked` result; repair or replace the known-good authority
  snapshot/storage and resolve the recovery intervention before resuming
  autonomy.
- **Security readiness blocked:** the worker records `security_blocked`, opens
  one organization-scoped `security_readiness_blocked` advisor intervention
  containing the exact violations, and records that no external action was
  attempted. Resolve the underlying isolation/secret-manager policy through
  supported setup; never dismiss the intervention by editing the database.

## Container restart evidence

With Docker image `charterforge:agentic-smoke` built from current main, the
complete restart regression was executed with:

```bash
HERMES_TEST_IMAGE=charterforge:agentic-smoke uv run pytest -q \
  tests/docker/test_container_restart.py
```

Result: **4 passed, 0 failed** in 72.18 seconds. The suite exercises named
profile registration, intentional stopped-state preservation, stale PID cleanup,
and live gateway auto-start after a real Docker restart. This is local restart
evidence, not a disaster-recovery or high-availability drill.

## Authority snapshot restore smoke

The implemented authority snapshot path was exercised on 2026-07-27 with a
temporary state root. The sequence was:

```bash
state_dir=$(mktemp -d)
export HERMES_HOME="$state_dir" CHARTERFORGE_HOME="$state_dir"
uv run charterforge business bootstrap \
  --charter-file examples/agentic-charter.json
snapshot=$(find "$state_dir/business-recovery" -maxdepth 3 \
  -name '*.json' -print -quit)
uv run charterforge business recovery-verify --snapshot "$snapshot"
uv run charterforge business --db "$state_dir/objectives.db" autonomy paused \
  --reason "recovery smoke pause"
uv run charterforge business --db "$state_dir/objectives.db" recovery-restore \
  --snapshot "$snapshot" --actor human:smoke \
  --reason "recovery smoke restore" \
  --evidence '{"source_integrity_failed":false,"workers_stopped":true,"smoke":true}'
uv run charterforge business status
```

Observed result: manifest verification was `valid: true`; restore returned
`restored: true`, `autonomy: paused`, and `reconciliation_required: true`.
The post-restore status preserved the organization, CEO, balanced USD 10.00
opening ledger, and snapshot integrity evidence, while opening one durable
`post_restore_reconciliation` advisor intervention. This proves local snapshot
restore semantics only; it does not prove high availability, multi-host
replication, or a complete disaster-recovery exercise.

## Sandbox provider validation

The Docker terminal provider was exercised against a live container with:

```bash
uv run pytest -q -m integration \
  tests/integration/test_vision_docker_resolve.py
```

Result: **3 passed, 0 failed** in 6.13 seconds. The checks cover container-only
workspace reads, root-owned mode-600 reads performed inside the container, and
refusal to read a same-path host secret through the sandbox boundary. Modal,
Daytona, SSH, and Singularity providers still require separate environment
validation before they can be treated as production evidence.
