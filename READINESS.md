# Charterforge Readiness Determination

## Determination

This document is the top-level release evidence boundary for the independent
Charterforge fork.

| Readiness claim | Determination |
| --- | --- |
| Controlled Founder/CEO agentic-runtime acceptance | **PASS**, subject to the exact commands below |
| Production autonomous business operation | **NOT READY** |
| Universal legal, tax, payment, or compliance operation | **NOT PROVEN** |

The passing claim means that the durable local runtime demonstrates a bounded
Founder/CEO operating loop: a bootstrapped solo CEO creates and advances work,
executes an admissible external-action contract through the deterministic local
`ExternalMarket` test-provider boundary, independently verifies the result,
replans from the resulting event, and completes without a human turn. The test
provider records a provider-like reference and read-back; it does not call
Stripe, AgentMail, a network endpoint, or any live third-party system. It does
not mean the software is a formed legal entity, a certified control system, or
ready to hold or move funds without deployment-specific provider and legal
review.

## Evidence identity

The acceptance test and the release-evidence files are committed before the
commands are run. The exact evidence commit SHA is recorded here after that
baseline commit and is immutable for this determination.

- Evidence commit SHA: **4f7d585b8d0eda6f0d7646c843f9ddd43c4d7afe**
- Evidence tag: **v0.19.0-agentic-foundation** (annotated tag at that SHA)
- Branch: `main`
- Date: 2026-07-27
- Repository: independent Charterforge fork; no upstream submission is implied

## Exact validation commands

Run from the repository root with the committed evidence SHA checked out:

```sh
scripts/run_tests.sh -j 4 tests/hermes_cli/test_agentic_business_e2e.py
scripts/run_tests.sh -j 4 \
  tests/hermes_cli/test_objective_service.py \
  tests/hermes_cli/test_objective_runtime.py \
  tests/hermes_cli/test_objective_worker.py
scripts/run_tests.sh -j 4 \
  tests/hermes_cli/test_finance_and_payments.py \
  tests/hermes_cli/test_outcome_attribution.py
python3 -m py_compile \
  hermes_cli/objective_service.py \
  hermes_cli/objective_runtime.py \
  hermes_cli/objective_worker.py \
  hermes_cli/payment_controls.py \
  hermes_cli/payments.py \
  hermes_cli/usage_billing.py \
  hermes_cli/outcome_attribution.py
git diff --check
```

The recorded result must include the test-file counts, zero failures, a
successful compilation, and a clean diff check. A green result does not erase
the explicit **NOT READY** and **NOT PROVEN** boundaries above.

## Recorded result at the evidence commit

The commands above were run from the repository root at commit
`4f7d585b8d0eda6f0d7646c843f9ddd43c4d7afe`:

- `test_agentic_business_e2e.py`: **6 passed, 0 failed**.
- Objective service/runtime/worker sweep: **41 passed, 0 failed** across 3
  files.
- Finance and outcome-attribution sweep: **18 passed, 0 failed** across 2
  files.
- `python3 -m py_compile ...`: completed successfully with no output.
- `git diff --check`: completed successfully with no output.
- Final observed baseline: `git status --short --branch` reported no working
  tree changes; `git rev-parse HEAD` returned the evidence SHA above.

The current `main` branch is intentionally ahead of this tagged evidence
commit. Its additional commits are Unreleased and do not inherit this PASS
determination until a newer evidence commit is recorded.

## Post-boundary evidence on current main

The exact acceptance command set plus the installer contract was rerun against
current `main` at baseline commit
`56e2c1a1ccf2a4a5c5409b9d7187816e2ecf7b98` (the payment compliance-readiness
surface).
This is a separate,
post-boundary evidence run; it does not move or rewrite the immutable release
tag above.

- Founder/CEO E2E: **6 passed, 0 failed**.
- Objective service/runtime/worker sweep: **48 passed, 0 failed** across 3
  files.
- Finance and outcome-attribution sweep: **21 passed, 0 failed** across 2
  files.
- Accounting replay/idempotency regression: **7 passed, 0 failed**.
- Procurement decision replay regression: **8 passed, 0 failed**.
- Metered usage and billing regression: **8 passed, 0 failed**.
- Objective portfolio relationship regression: **7 passed, 0 failed**.
- Hiring policy regression: **13 passed, 0 failed**.
- Finance reservation regression: **21 passed, 0 failed**.
- Intervention-control regression: **10 passed, 0 failed**.
- Approval-artifact regression: **9 passed, 0 failed**.
- Company-email regression: **4 passed, 0 failed**.
- Compute-reconciliation regression: **9 passed, 0 failed**.
- Payment readback regression: **21 passed, 0 failed** within the finance suite.
- Circuit-breaker recovery-probe regression: **3 passed, 0 failed**.
- Compliance schema transaction regression: **7 passed, 0 failed**.
- Company-email schema transaction regression: **5 passed, 0 failed**.
- Compliance supersession lineage regression: **9 passed, 0 failed**.
- Packaging artifact regression: **2 passed, 0 failed**.
- Combined governed-runtime and packaging command: **84 passed, 0 failed**.
- Bootstrap/operator regression additions: **6 passed, 0 failed**.
- Expanded current-main validation command: **98 passed, 0 failed** across 10
  files, including the independent installer contract, security-readiness
  escalation, credential-safe payment-rail discovery, and deterministic
  readiness-blocker regressions.
- Current-tree container worker smoke passed with image ID
  `sha256:83bb9a85e36ecc948c67a1051445566a7e6a6f3d00f63e7c5756b962da346db4`:
  separate bootstrap, `objectives worker --once`, and `worker-status`
  containers shared one temporary state directory. The read-back worker record
  contained `security_blocked`, `runtime_blocked:security_blocked`, and the
  exact isolation/secret readiness reason; no external action was attempted.
- Authority restore smoke passed from a temporary state root: bootstrap created
  a verified snapshot, `recovery-verify` returned `valid: true`, restore returned
  `restored: true` with autonomy paused, and read-back status showed
  `reconciliation_required: true` plus one durable post-restore advisor
  intervention. This is restore-smoke evidence, not HA or full DR evidence.
- Non-interactive bootstrap smoke: two consecutive bootstrap invocations
  returned identical organization/objective IDs, and `business status` reported
  configured state from the persistent directory.
- Container image/startup persistence smoke: `docker build --tag
  charterforge:agentic-smoke .` passed; supervised `/init` startup and shutdown
  passed across four `docker run --rm` invocations using a mounted state
  directory, with stable organization/objective IDs. Image digest:
  `sha256:5448ca6ad296a7bf1678f5ae5c8c9d8b14d84592d2c7828c7fd050769b1ff1dd`.
- Container restart regression: `HERMES_TEST_IMAGE=charterforge:agentic-smoke
  uv run pytest -q tests/docker/test_container_restart.py` — **4 passed, 0
  failed** in 72.18 seconds, covering stopped-state preservation, stale PID
  cleanup, profile reconciliation, and live gateway auto-start after restart.
- Docker sandbox-provider integration: `uv run pytest -q -m integration
  tests/integration/test_vision_docker_resolve.py` — **3 passed, 0 failed** in
  6.13 seconds, covering container-only reads and host-secret non-exfiltration.
- Standalone worker launch smoke: after a clean charter bootstrap,
  `charterforge objectives worker --once` registered a durable
  `objective-runtime` worker, recorded `security_blocked`, and stopped cleanly;
  `objectives worker-status` read the stopped evidence and exact readiness
  violations back. No provider or external outcome was fabricated.
- Selected runtime compilation: completed successfully.
- `git diff --check`: completed successfully.

This current-main run supports the same **controlled Founder/CEO runtime
acceptance PASS** for the bounded tested surface, while production autonomous
business operation remains **NOT READY**. The exact commands are the same as
the command block above; the baseline SHA and results are the authoritative
post-boundary record.

Authenticated external-ingress freshness was additionally validated with:

```sh
scripts/run_tests.sh -q -j 4 \
  tests/hermes_cli/test_objective_triggers.py \
  tests/hermes_cli/test_agentmail_events.py \
  tests/hermes_cli/test_agentic_runtime_ingress_e2e.py
```

Result: **23 passed, 0 failed**. This focused ingress regression is supporting
evidence, not an expansion of the release-gate capability inventory.

LLM/provider rate-limit recovery was separately validated on current `main`
with:

```sh
python3 -m pytest tests/hermes_cli/test_objective_runtime.py -q
```

Result: **27 passed, 0 failed**. The regression raises a deterministic 429-like
planner failure, records `rate_limited:` in the durable inbox error, retries
after the persisted backoff, closes and reopens the authority store, and
verifies exactly one execution result. The same suite also validates numeric
`Retry-After` and rate-limit reset header parsing. This is local deterministic
provider-boundary evidence; it does not establish live LLM credentials, vendor
rate-limit behavior, or production availability.

Model-backed planner budget recovery was separately validated on current
`main` with:

```sh
python3 -m pytest tests/hermes_cli/test_objective_adapters.py -q
```

Result: **18 passed, 0 failed**. A simulated LLM rate-limit exception now
produces an immutable `released` compute reconciliation with zero billable
cost, leaving no unreconciled reservation for a later retry. This is local
failure-path evidence; it does not establish live provider billing accuracy.

Interrupted-action restart recovery was separately validated on current `main`
with:

```sh
python3 -m pytest tests/hermes_cli/test_objective_runtime.py -q
```

Result: **27 passed, 0 failed**. The uncertain-provider-effect regression closes
the authority store after the first worker failure, opens a fresh connection,
and verifies that the restarted runtime reconciles the exact idempotent action
without replanning or duplicating the provider effect.

## Current-tree install-to-restart acceptance

The current main branch has a separate, broader acceptance scenario. It is not
retroactively attributed to the tagged release boundary. At commit
`c2e463abea`, from the repository root, run:

```bash
scripts/run_agentic_acceptance.sh
```

This single command builds and installs the wheel, builds a Docker image,
bootstraps a fresh state volume, records blocked readiness, satisfies the
required local controls, starts the bounded CEO worker, executes one objective,
restarts the container, and verifies durable recovery with idempotent replay.
The exact result was:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The image ID was
`sha256:312d4ca8f665a0f482aedb7c2e02b7fee007ff5dd328f4639a133a0726982607`.
The provider boundary is a deterministic file-backed test adapter; this is
controlled runtime evidence and not proof of live provider credentials,
production deployment, or legal/tax readiness.

At current-main commit `0ed1118bec`, the same command was extended to include
the ambiguous provider-effect recovery in one acceptance run. The exact output
was:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1}
current-tree agentic acceptance: PASS
```

The resulting image manifest was
`sha256:249224289f6e7db05a8c29b5362ede6ce99e0b61fb5b30705b085214c456fb03`.
This is the current-main acceptance boundary; it remains deterministic
provider-adapter evidence, not live payment-provider or production deployment
proof.

At current-main commit `d2fc0e1e40`, the acceptance also dispatched a due
durable schedule inside the same worker run. The exact output was:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1}
current-tree agentic acceptance: PASS
```

The resulting image manifest was
`sha256:fda78f344ed7ca45cfce1860402607006c467f02d25c93825b5f925f302803c6`.
`scheduled_events: 1` is evidence of a durable autonomous wake-up, not proof
of an always-on production scheduler or external event-provider availability.

At current-main commit `2941d5083f`, the initial acceptance objective was also
admitted through the authenticated external-event boundary rather than a
direct inbox insert. The same run passed with the following event and recovery
output:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1}
current-tree agentic acceptance: PASS
```

The resulting image manifest was
`sha256:c001af7f15f7550276281d4faf8bfb8e63d228fc487a69883288b05952ea4004`.
This proves the local authenticated-ingress contract; it does not establish a
live webhook provider, production signature key management, or public network
availability.

At current-main commit `281fcab7a6`, the acceptance added an explicit durable
replanning assertion. Its run reported:

```text
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1, "plan_versions": 2}
```

The `plan_versions: 2` result demonstrates that the scheduled event generated
a new persisted plan version before verification; it is not merely a second
call against an in-memory plan.

At current-main commit `34458a3942`, the unified acceptance also completed the
master stop phase:

```text
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

This proves durable autonomy revocation and worker fail-closed behavior in the
controlled runtime. It does not claim that external provider credentials or
third-party workers can be revoked outside the configured control plane.

Current-main workforce coordination evidence at commit `d44d0ad47f` was run
with:

```sh
python3 -m pytest tests/hermes_cli/test_agentic_business_e2e.py \
  tests/hermes_cli/test_workforce_delegation.py -q
```

Result: **17 passed, 0 failed**. This covers CEO hiring and hierarchical
mandate provisioning, exact employee-worker launch validation, subordinate
task completion, and the durable task-result event accepted back into CEO
planning. It is local coordination evidence, not proof of a production worker
fleet or external workforce platform.

## Interrupted provider-action evidence

The current tree also proves the ambiguous mid-flight payment case. At commit
`8abc8dde89`, run:

```bash
scripts/run_provider_recovery_acceptance.sh
```

The provider effect is recorded before the response is lost; the local intent
is durably marked `uncertain`; after container restart, provider read-back
settles the payment exactly once. The recorded result was:

```text
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1}
provider recovery acceptance: PASS
```

At current-main commit `b62b11b4f6`, the provider recovery acceptance also
proved the inbound rail contract in the same restarted process boundary:

```text
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 500}
provider recovery acceptance: PASS
```

This demonstrates deterministic inbound receivable creation, read-back
settlement, accounting balance increase, and idempotent retry. It remains local
non-custodial provider-adapter evidence, not live Stripe, bank, card, or
stablecoin settlement proof.

At current-main commit `7a8cccc640`, the same run included verified tax
handling:

```text
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
provider recovery acceptance: PASS
```

The deterministic accounting assertion recorded 500 minor units as revenue and
30 as tax liability under the configured US-PA rule. This is accounting-path
evidence, not a jurisdiction-wide tax determination or filing authorization.

Image ID: `sha256:ee59306e2257eb3e2a4267d5146dfa996bba75d36a78d4a51a7b580d4875d278`.

## Latest current-tree acceptance record

The process-separated delegation acceptance gate was validated on the current
tree with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/hermes_cli/test_workforce_delegation.py
```

Result: **18 passed**. The process-separated test proves that the CEO-issued grant
is bound to one subordinate profile, mandate, system, toolset, skill, budget,
and expiry; a separate process performs the task and records evidence; and a
fresh CEO runtime receives the completion event, performs authoritative
read-back, and advances the parent objective to `verified`. This is local
deterministic acceptance evidence, not proof of production worker deployment.
The same gate attempts an unauthorized capability and confirms that delegation
is rejected because subordinate authority may never exceed the delegator's
current mandate.

The complete acceptance command was rerun from commit `d1cd322ffa` on
2026-07-27 with:

```sh
scripts/run_agentic_acceptance.sh
```

Result:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1, "plan_versions": 2}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The resulting local image manifest was
`sha256:2c0a6169eade11f87005dd89e4482dfa77e213ba16366c04a167b5c6d0a8f73d`.
This is deterministic local-provider evidence; production providers,
high-availability deployment, and legal/compliance readiness remain open.

The committed container acceptance was rerun from `9a5370b45c4c3378d97415a76d480f99461134c4` on 2026-07-27. In addition to the existing install, restart, uncertain-provider, accounting, and stop phases, it proved the installed process-separated delegation phase:

```text
{"phase": "ceo", "grant": "taskgrant_dcd0e0e81ca944138ac4c999cce90ac2", "task": "t_a354cfc6"}
{"phase": "subordinate", "grant_id": "taskgrant_dcd0e0e81ca944138ac4c999cce90ac2", "task_id": "t_a354cfc6", "evidence_recorded": true}
{"phase": "ceo", "event": "kanban.task.done", "objective": "verified"}
```

Image manifest: `sha256:92e65135599ec03c747539b51bc4b5ca9bf1f406c50a97faf758366e46cf33dc`. The provider boundaries remain deterministic local adapters.

The current-tree acceptance was rerun after exact-resource grant enforcement at
commit `fc69220e5c78f7c892f049749c37319283c4a18c` on 2026-07-27. The run used
`scripts/run_agentic_acceptance.sh` and passed installation/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain-provider read-back, durable recovery, and master stop without
duplicate effects:

```text
{"phase": "ceo", "event": "kanban.task.done", "objective": "verified"}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

Image manifest: `sha256:b081b0a9de63aadbcbb4392d6453616dfa8870c3cdc1402b20e06dffa9d40d3f`.
This is still deterministic local-provider evidence, not production deployment
or live payment-provider evidence.

After cumulative delegator-budget enforcement, the same command was rerun from
commit `8144594bf130dad4f25f18a4e194b8fa4f82072a` on 2026-07-27. It returned
`current-tree agentic acceptance: PASS`, with interrupted-provider read-back
succeeding, `duplicate_provider_calls: 0`, and `duplicate_effects: 0`.
Image manifest: `sha256:43ba8bfdacd7b6313e92c72da1e23c0cbe7fb80cd5982bb4dfb1587ab680b051`.

Compliance lineage was separately validated with:

```sh
python3 -m pytest tests/hermes_cli/test_regulatory_compliance.py -q
```

Result: **10 passed, 0 failed**. Applicability and control evidence remain
immutable, supersession must target the current leaf with an explicit reason,
and ambiguous branching records fail closed rather than being silently
selected as authoritative.

Payment-provider authority was additionally validated with:

```sh
python3 -m pytest tests/hermes_cli/test_compliance_db.py tests/hermes_cli/test_business_bootstrap_command.py -q
```

Result: **13 passed, 0 failed**. Provider assessments now support immutable
supersession, reject branch-from-old-record attempts, and stop authorization
when the current replacement no longer satisfies screening controls.
The supported `business provider-verify` command also accepts the exact
superseded assessment ID and required replacement reason.

Company-email readiness was validated with:

```sh
python3 -m pytest tests/hermes_cli/test_business_bootstrap_command.py tests/hermes_cli/test_company_email.py tests/hermes_cli/test_agentmail_events.py -q
```

Result: **20 passed, 0 failed**. A charter granting `email.send` now remains
blocked until AgentMail configuration is present, while outbound read-back and
authenticated inbound routing remain covered at the provider boundary.

Interrupted objective-cycle recovery was validated with:

```sh
python3 -m pytest tests/hermes_cli/test_objective_maintenance.py::test_housekeeping_requeues_interrupted_executing_objective tests/hermes_cli/test_objective_maintenance.py::test_housekeeping_requeues_active_objective_without_a_wakeup -q
```

Result: **2 passed, 0 failed**. Maintenance now creates a durable
`objective.executing.reconcile` wake when a crash leaves an executing objective
without a pending or processing event.

Tax-rule authority was validated with:

```sh
python3 -m pytest tests/hermes_cli/test_accounting_db.py tests/hermes_cli/test_finance_and_payments.py -q
```

Result: **31 passed, 0 failed**. Tax calculations use only the current
supersession leaf, and amended-rate lineage remains immutable.

## Release gates that remain open

- Corporate formation, legal personhood, banking, and human legal-principal
  actions remain outside the software's authority.
- Production deployment, high availability, disaster-recovery drills, and
  external payment-provider credentials are not established by this checkout.
- PCI DSS, SOC 2, SOX, GDPR, EU AI Act, CASL, CAN-SPAM, and jurisdiction-specific
  tax or payments applicability remain deployment and legal work.
- SQLite is the implemented authority store; Postgres and an external broker
  remain deployment work.

See [implementation status](docs/implementation-status.md) for the complete
capability and limitation inventory, and the release boundary in
[CHANGELOG.md](CHANGELOG.md).
