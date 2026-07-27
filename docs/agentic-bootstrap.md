# Non-interactive agentic bootstrap

The supported headless bootstrap contract creates or resumes the initial
solo-founder organization, Founder/CEO mandate, treasury account, initial
capital entry, objective, cadence schedule, authority baseline, and recovery
posture. It is safe to rerun against the same mounted state directory.

From a source checkout or installed Charterforge environment:

```bash
export CHARTERFORGE_HOME=/srv/charterforge
charterforge business bootstrap \
  --charter-file examples/agentic-charter.json
charterforge business status
```

For a container, mount `/srv/charterforge` as the persistent data volume and
run the same bootstrap command once before starting the supervised gateway.
The command writes the charter only after the durable bootstrap succeeds; a
failed bootstrap cannot leave configuration claiming that the business is
ready.

The example deliberately starts with a USD 10.00 capital seed, a low-risk
market capability, and no payment-provider credentials. Replace the charter
with an advisor-reviewed file before enabling real external systems.

## Restart proof

The bootstrap command is resumable: a second invocation against the same state
directory returns the existing organization and objective rather than creating
duplicates. The persistence contract is covered by
`tests/hermes_cli/test_agentic_bootstrap.py` and the command contract by
`tests/hermes_cli/test_business_bootstrap_command.py`.

This is local bootstrap evidence, not proof of legal formation, production
provider readiness, container-registry publication, or high availability.
