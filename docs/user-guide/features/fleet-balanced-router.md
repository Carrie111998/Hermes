# Fleet-balanced task router

`hermes fleet` is an opt-in CLI edge feature for routing a bounded task to one
qualified subscription-backed worker. It adds no model tool and does not change
the provider or model of an existing Hermes conversation.

## Safety contract

- Fleet is disabled by default under `fleet.enabled` in `config.yaml`.
- Credential-shaped fleet config is rejected. API keys, unknown auth sources,
  pay-as-you-go/overage, and incomplete qualification all make a lane
  ineligible.
- Stale or missing capacity is ineligible by default. The explicit
  `fleet.rotation_without_fresh_capacity` opt-in permits an otherwise fully
  qualified lane to enter only the deterministic rotation fallback pool.
  Untrusted percentages never affect the 20-point override or reserve
  arithmetic, and selection is audited as
  `ROTATION_WITHOUT_FRESH_CAPACITY`.
- Selection defaults to persisted cyclic rotation across the eligible pool.
  Fresh, high-confidence capacity may override the cyclic lane only at a
  difference of at least 20 percentage points. A stale fallback can rotate
  when explicitly enabled, but can never trigger or win that override.
- `status`, `doctor`, `plan`, and `audit` are read-only. In particular, `plan`
  does not create the fleet database, acquire a lease, reserve capacity, or
  advance rotation.
- `run` selects once for a new task ID, then pins that task to its lane.
  Reusing `--task-id` never migrates the task to another lane.
- The optional `C:/HermesBridge/usage-weekly.json` source is opened read-only.
  It is capacity evidence only, never authentication evidence.

## Commands

```text
hermes fleet status [--json]
hermes fleet doctor [--lane LANE] [--json]
hermes fleet plan --task-file PATH [--cwd PATH] [--json]
hermes fleet run --task-file PATH [--cwd PATH] [--task-id UUID] [--json]
hermes fleet audit [--task-id UUID] [--reason CODE] [--jsonl]
hermes fleet release TASK_ID [--outcome completed|failed|cancelled] [--json]
```

On native Windows, Antigravity qualification also checks
`%LOCALAPPDATA%/agy/bin/agy.exe` when `agy` is absent from `PATH`; the resolved
file is used for both qualification and execution.

JSON output includes reason codes and, when available, the lane, adapter kind,
model/effort, bridge SHA-256 identity, capture/read/expiry timestamps,
freshness, confidence, and effective remaining capacity.

## V1 limitation

The bundled CLI does not infer subscription qualification from the mere
presence of a credential or executable. Unless current, attributable auth,
billing, capability, and fast-off evidence has been supplied by a reviewed
integration, `doctor` and `plan` report the failed gates and `run` starts no
child process. Kimi remains explicitly deferred.
