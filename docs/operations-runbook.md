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
- **Rate limit:** bounded exponential backoff; do not rewrite integrations.
- **Repeated task failure:** circuit breaker and advisor intervention.
- **State drift:** compare configuration, package lock, schema, profiles, and
  provider state to the recorded baseline.
- **Conflicting workers:** claims and resource leases select one writer.
- **Accounting interruption:** reconcile provider records before posting or
  reversing journal entries.

