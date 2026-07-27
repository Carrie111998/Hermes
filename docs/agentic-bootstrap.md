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
runtime-worker health, security readiness, drift gates, open advisor
interventions, and any payment rails declared by the charter all permit
unattended operation. If the
charter grants `payments.receive` or `payments.send`, a credential-ready rail
is required for the corresponding direction. It never enables autonomy or
attempts a provider action.

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
