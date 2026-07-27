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
```

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
