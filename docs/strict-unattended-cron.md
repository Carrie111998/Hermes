# Strict unattended cron policy

Hermes can create a scheduled agent under the registered
`strict-unattended-v1` policy. This policy is intended for jobs that consume
untrusted evidence without giving that evidence access to the Hermes control
plane.

## Security contract

A policy job must persist all of these controls:

- `created_paused=true`: its first jobs-store record was disabled and had no
  next trigger;
- `strict_toolsets=true`: `enabled_toolsets` is the complete tool allowlist;
- `no_mcp=true`: the job does not initiate MCP discovery, previously
  registered `mcp__*` schemas are removed from its tool surface, automatic
  between-turn MCP refresh is disabled, and any defensive refresh,
  progressive-disclosure scope rebuild, generic bridge catalog rebuild, recursive
  bridge call, or direct dispatcher call preserves the MCP exclusion;
- `no_fallback=true`: provider-resolution failover and the agent model fallback
  chain are disabled;
- persistent memory/profile loading is disabled for the run; every named
  toolset is resolved fail-closed (including aggregate/alias and registered
  plugin toolsets), and any resolved toolset containing the `memory` tool is
  forbidden rather than allowed to override that isolation;
- explicit `provider` and `model` pins.

The policy ID and capability/runtime pins are immutable. Hermes validates the
policy at create, update, resume, due-scan, fire-claim, and immediately before
agent dispatch. Unknown or malformed persisted policies fail closed and are
durably disabled with a diagnostic pause reason.

Policy creation, activation, update, manual run, re-arm, and removal require the
direct operator CLI on the supported model-facing and application ingress paths.
Core `cron.jobs` mutation functions reject callers that do not carry the
in-process capability, so REST, dashboard, console, model-tool, and ordinary
wrapper calls fail closed; REST returns `403` for a protected mutation. The
model-facing `cronjob` schema cannot serialize the capability. A policy job may
pause itself as a containment action, but it cannot resume, mutate, remove,
re-arm, or manually run itself. `force=True` requires the operator capability
even when the job is already scheduled, and it cannot resurrect a paused policy
job.

The capability singleton is **not** an authority boundary against imported
Python code. A same-process plugin is part of Hermes's trusted computing base
and can import the singleton or mutate agent state. Likewise, a same-UID process
can edit the job store. Do not describe this control as plugin isolation or an
OS security boundary.

Invalid-policy quarantine also asks the active scheduler provider to reconcile
its registrations after the durable pause, preventing an external scheduler
from remaining armed against a locally quarantined record.

## Create paused

Use an exact, minimal allowlist. Do not include `terminal`, file-write,
process-control, cron-control, or external mutation toolsets in an evidence
reviewer.

```bash
hermes cron create '0 7 * * *' \
  'Review bounded evidence using only the fixed reader tool.' \
  --name 'Restricted evidence review' \
  --model 'provider/model' \
  --provider 'provider' \
  --policy-id strict-unattended-v1 \
  --enabled-toolset fixed-reader \
  --strict-toolsets \
  --no-mcp \
  --no-fallback \
  --start-paused
```

Read the created job back and verify:

- `enabled=false`;
- `state="paused"`;
- `next_run_at=null`;
- all policy fields and pins match the intended values.

Only then may an operator run `hermes cron resume <job-id>`.

## Design boundary

This policy is a model-capability boundary, not an operating-system sandbox.
Processes running as the same OS user can still modify Hermes files directly.
It also does not prove that model output is semantically correct.

A truly unattended state-changing workflow must therefore keep commit/finalize
authority out of the reviewing agent's process and tool surface. Put publication
behind a separate trusted service or process with distinct OS credentials and
authenticated, narrowly typed IPC; use fixed trusted code there to validate
bounded, hash-bound output and apply an exact state transition. Fail closed on
malformed evidence, incomplete coverage, policy drift, publisher failure, or
readback mismatch. This policy alone is insufficient to authorize unattended
publication.

Legacy jobs without `policy_id` keep their existing behavior.
