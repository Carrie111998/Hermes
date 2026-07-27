# Non-interactive agentic bootstrap

The supported headless bootstrap contract creates or resumes the initial
solo-founder organization, Founder/CEO mandate, treasury account, initial
capital entry, objective, cadence schedule, authority baseline, and recovery
posture. It is safe to rerun against the same mounted state directory.

Before initialization, `charterforge business status` is read-only and returns
a structured `next_step` handoff. It points the advisor to the example charter
and reports `autonomy_started: false`; status never bootstraps or enables
unattended operation implicitly.

From a source checkout or installed Charterforge environment:

```bash
export CHARTERFORGE_HOME=/srv/charterforge
charterforge business bootstrap \
  --charter-file examples/agentic-charter.json
charterforge business status
charterforge business readiness
```

`business readiness` is a read-only deterministic projection. It reports
`ready: false` with an exact blocker list until bootstrap, autonomy mode,
security readiness, drift gates, open advisor
interventions, and any payment rails declared by the charter all permit
unattended operation. If the
charter grants `payments.receive` or `payments.send`, a credential-ready rail,
a current screened provider assessment, and a non-custodial compliance profile
are required for the corresponding direction. It never enables autonomy or
attempts a provider action. The projection also reports `runtime_active`; it is
false until a supervised CEO worker is started, but worker liveness is
intentionally separate from the control-plane readiness decision.

The checked-in example charter admits both supervised gateway and standalone
worker hosts (`runtime_host: "either"`). To exercise the standalone process
without a second terminal, run one governed tick after bootstrap:

```bash
HERMES_HOME="$CHARTERFORGE_HOME" charterforge objectives worker --once
HERMES_HOME="$CHARTERFORGE_HOME" charterforge objectives worker-status
```

With no configured model/provider evidence, the worker is expected to record a
durable `security_blocked` cycle and stop; it must not invent an external
result or continue retrying without authority.

For a container, mount `/srv/charterforge` as the persistent data volume and
run the same bootstrap command once before starting the supervised gateway.
The command writes the charter only after the durable bootstrap succeeds; a
failed bootstrap cannot leave configuration claiming that the business is
ready.

The example deliberately starts with a USD 10.00 capital seed, a low-risk
market capability, and no payment-provider credentials. Replace the charter
with an advisor-reviewed file before enabling real external systems.

## Compose deployment

The repository's `docker-compose.yml` includes an opt-in `agentic` profile for
the standalone Founder/CEO supervisor. Set the charter's `runtime_host` to
`standalone`, bootstrap the mounted `~/.charterforge` state, and then start the
profile:

```bash
CHARTERFORGE_UID=$(id -u) CHARTERFORGE_GID=$(id -g) \
  docker compose --profile agentic up -d --build
docker compose --profile agentic ps
docker compose --profile agentic logs --tail=100 ceo-worker
```

The worker and gateway share the same durable state volume. Runtime leases and
host policy prevent a gateway worker from competing with this supervisor; do
not run the profile with a charter that selects `runtime_host: "gateway"`.
The worker uses bounded `on-failure:5` restart semantics: controlled governed
stops return successfully and remain stopped, while unexpected process crashes
receive at most five automatic retries before requiring advisor intervention.
This is a deployment contract, not proof of provider credentials, isolation
readiness, or production availability.

The current-tree container smoke also exercised the worker boundary with image
`charterforge:agentic-current` (local image ID
`sha256:83bb9a85e36ecc948c67a1051445566a7e6a6f3d00f63e7c5756b962da346db4`).
Bootstrap, `objectives worker --once`, and `objectives worker-status` ran in
separate containers against one temporary mounted directory. The worker
persisted `last_cycle_status: security_blocked`,
`stop_reason: runtime_blocked:security_blocked`, and the exact isolation/secret
readiness reason; it performed no external action.

## Container smoke contract

The image build and supervised entrypoint were exercised locally on 2026-07-27
with Docker using image digest
`sha256:5448ca6ad296a7bf1678f5ae5c8c9d8b14d84592d2c7828c7fd050769b1ff1dd`.
The build passed, all supervised services started and stopped cleanly, and two
bootstrap invocations against the same mounted `/opt/data` volume returned the
same organization and objective IDs. This is local image/startup/persistence
proof, not registry publication, provider readiness, or production availability
evidence.

The executed smoke command was:

```bash
docker build --tag charterforge:agentic-smoke .
state_dir=$(mktemp -d)
docker run --rm -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:agentic-smoke \
  business bootstrap --charter-file /opt/hermes/examples/agentic-charter.json
docker run --rm -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:agentic-smoke business status
docker run --rm -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:agentic-smoke \
  business bootstrap --charter-file /opt/hermes/examples/agentic-charter.json
docker run --rm -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:agentic-smoke business status
```

## Restart proof

The bootstrap command is resumable: a second invocation against the same state
directory returns the existing organization and objective rather than creating
duplicates. The persistence contract is covered by
`tests/hermes_cli/test_agentic_bootstrap.py` and the command contract by
`tests/hermes_cli/test_business_bootstrap_command.py`.

This is local bootstrap evidence, not proof of legal formation, production
provider readiness, container-registry publication, or high availability.

## Current-tree Founder/CEO acceptance

The process-separated delegation gate is also covered by:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/hermes_cli/test_workforce_delegation.py
```

It launches the subordinate as a separate interpreter with only its exact
grant-bound environment, persists evidence in the Kanban task-run record, then
lets a fresh CEO runtime consume `kanban.task.done` and verify the parent
objective. The grant is specific to the employee profile, mandate version,
capabilities, systems, toolsets, skills, budget, and expiry; it is not a
standing account-wide authorization.

The single current-tree acceptance scenario is the decisive bounded runtime
proof. It builds and installs the wheel, starts a fresh container, demonstrates
blocked readiness before controls are satisfied, applies the test charter and
required local controls, starts the CEO worker, executes one bounded objective,
restarts the container, and verifies the durable objective state without a
duplicate provider effect:

```bash
scripts/run_agentic_acceptance.sh
```

Run on 2026-07-27 at commit `c2e463abea` (the current-tree acceptance harness
commit). Result:

```text
Charterforge v0.19.0 (2026.7.20)
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The image was built locally as `charterforge:agentic-acceptance` with image
ID `sha256:312d4ca8f665a0f482aedb7c2e02b7fee007ff5dd328f4639a133a0726982607`.
The provider is a deterministic file-backed test adapter mounted in the
temporary state volume; no Stripe, AgentMail, network endpoint, or production
credential is used. The container uses its real image entrypoint and
supervised startup path. This proves the controlled restart/idempotency
contract, not production deployment or live payment capability.

## Interrupted provider-action acceptance

The harder financial recovery case is covered separately. The deterministic
rail durably records a successful provider effect, then raises an uncertain
response before local settlement. A new process and restarted container use
only provider read-back to converge the intent, reservation, spend hold, and
ledger, and retrying the same idempotency key does not call the provider again:

```bash
scripts/run_provider_recovery_acceptance.sh
```

Run on 2026-07-27 at commit `8abc8dde89`. Result:

```text
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1}
provider recovery acceptance: PASS
```

Image ID: `sha256:ee59306e2257eb3e2a4267d5146dfa996bba75d36a78d4a51a7b580d4875d278`.
This is a deterministic local provider boundary, not live payment-provider
evidence.

## Latest acceptance record

The complete current-tree acceptance was rerun from commit
`d1cd322ffa985582aa48ec7cd844b77bbe3d4659` on 2026-07-27 with:

```bash
scripts/run_agentic_acceptance.sh
```

It passed install, blocked-to-ready bootstrap, scheduled CEO progress,
replanning, normal restart recovery, uncertain outbound provider read-back,
inbound receivable settlement with tax liability, and durable master stop:

```text
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1, "plan_versions": 2}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The local image manifest was
`sha256:2c0a6169eade11f87005dd89e4482dfa77e213ba16366c04a167b5c6d0a8f73d`.
The providers remain deterministic local test adapters; this is not live
payment, email, or production deployment evidence.
