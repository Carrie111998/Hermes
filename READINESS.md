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

After exact permit target-resource binding, the acceptance was rerun from
commit `b587ca533a9a5f0f8d1518d0d963395fbe108e9a` on 2026-07-27. It returned
`current-tree agentic acceptance: PASS`; interrupted-provider read-back again
succeeded with `duplicate_provider_calls: 0` and `duplicate_effects: 0`.
Image manifest: `sha256:4f6bfd947234c853b0f1bad3a1467318d58529f3ed15b2b1e4e77c4ce1e7260a`.

The complete acceptance was rerun against current `main` commit
`dce3949fea0f0ffe9edc4cbd9e956dae76a14694` after transitive delegation
enforcement. The exact command was:

```sh
scripts/run_agentic_acceptance.sh
```

It passed installation, bootstrap, blocked-to-ready readiness, bounded CEO
execution, scheduled replanning, process-separated delegation, uncertain
provider read-back, inbound tax-bearing settlement, durable restart recovery,
and master stop:

```text
{"phase": "prepare", "initial_readiness": "blocked"}
{"phase": "prepare", "ready": true, "runtime_active": false}
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1, "plan_versions": 2}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "ceo", "event": "kanban.task.done", "objective": "verified"}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The resulting local image manifest was
`sha256:b7b611baf4b9b7466bfde4b2e5a2d61b2672ef15a3d02e3ea63c43293ac5c499`.
This remains deterministic local-provider evidence and does not establish
production deployment, live payment credentials, or legal/compliance
readiness.

After exact capability/system launch-surface enforcement, the installed
acceptance was rerun from commit `a1251763116dd951e6f3ab2303032ce2956ccb90`
on 2026-07-27. It returned `current-tree agentic acceptance: PASS`; the
subordinate process validated its exact grant, completed the task, and woke
the CEO with durable evidence. Image manifest:
`sha256:367f6b56334dc5053cdcb6658dc9c9a68c00e6af6f67082cf5829d8d1a277e6c`.

After adding the readiness healthcheck contract, the installed acceptance was
rerun from commit `5dbdbf96ae036bb0e9d20a31782124a75760ae07` on 2026-07-27.
It returned `current-tree agentic acceptance: PASS`; image manifest:
`sha256:2b08831350ebd5c9a605f78d5049e1ad3d1a7caa261ff1606ae5385bdb905862`.

The governed runtime regression was also run from current `main` (`cd256040a36a281d61356b57f4c43ac4c4563bd2`) with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_objective_runtime.py \
  tests/hermes_cli/test_objective_service.py \
  tests/hermes_cli/test_objective_worker.py \
  tests/hermes_cli/test_objectives_db.py \
  tests/hermes_cli/test_workforce_delegation.py
```

Result: **79 passed, 0 failed**. This is focused regression evidence for
durable objective recovery, event claims, worker failure handling, permits,
and authority-monotone delegation; it does not expand the container acceptance
scope or prove production provider readiness.

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

Payment rail readiness signaling was validated on current `main` (`b363524267ceee902c58ef012b885431c163e80d`) with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_business_bootstrap_command.py \
  tests/hermes_cli/test_finance_and_payments.py
```

Result: **37 passed, 0 failed**. The new `business payment-rails --check`
contract returns non-zero for unavailable discovered rails while remaining
read-only; payment intent idempotency, provider read-back, and accounting
settlement remain covered by the same regression.

Event-driven CEO wakeups were validated on current `main` (`7fc5cae501718262b9c95eac67845e4bc3b6abd1`) with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_objective_triggers.py \
  tests/hermes_cli/test_objective_service.py \
  tests/hermes_cli/test_objective_runtime.py
```

Result: **55 passed, 0 failed**. This covers scheduled catch-up without event
storms, authenticated external-event routing, durable event claims, worker
recovery, and objective execution after a persistent wake.

Compliance admission and readiness interactions were validated on current
`main` (`09c92043f76e32f9cfea8a953a5049568ad049e9`) with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_regulatory_compliance.py \
  tests/hermes_cli/test_compliance_db.py \
  tests/hermes_cli/test_business_bootstrap_command.py
```

Result: **29 passed, 0 failed**. The evidence confirms immutable applicability,
obligation, control, and payment-provider records; explicit supersession
lineage; fail-closed action admission; and readiness payment-profile gates.

Abrupt worker-death and ingress restart recovery were validated on current
`main` (`bd0af992f11278450a431d7dafe9e46211224e1b`) with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_objective_worker.py \
  tests/hermes_cli/test_agentic_runtime_ingress_e2e.py
```

Result: **17 passed, 0 failed**. The tests cover stale heartbeat fencing,
supervisor stop/circuit behavior, authenticated event persistence across worker
restart, and verified objective completion after the restart.

The complete acceptance was rerun against current `main` commit
`435c540428e0c77eb553a06b1c288b1bb13c62a7` after immutable parent-grant
lineage was added. The exact command was:

```sh
scripts/run_agentic_acceptance.sh
```

It returned `current-tree agentic acceptance: PASS`. The critical results were:

```text
{"phase": "run", "objective": "verified", "effects": 1, "scheduled_events": 1, "plan_versions": 2}
{"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
{"phase": "ceo", "event": "kanban.task.done", "objective": "verified"}
{"phase": "interrupt", "intent": "uncertain", "provider_effect": 1}
{"phase": "recover", "readback": "succeeded", "duplicate_provider_calls": 0, "ledger_entries": 1, "inbound_received_minor": 530, "tax_minor": 30}
{"phase": "stop", "autonomy": "paused", "generation": 2, "duplicate_effects": 0}
current-tree agentic acceptance: PASS
```

The resulting local image manifest was
`sha256:75d8a8671a1512526574c2d924a4e6ce5f0d771e95dcbbd9cae682046141dd04`.
This remains deterministic local-provider evidence and does not establish
production deployment, live payment credentials, or legal/compliance
readiness.

Parent-grant revocation propagation was validated on current `main` commit
`e7901a02a6cc06cb11862be9e0aebebde192d1f0` with:

```sh
uv run --extra dev pytest -q tests/hermes_cli/test_workforce_delegation.py
```

Result: **14 passed, 0 failed**. The suite now proves that a revoked parent
grant fences descendant authorization, including worker/result authority
checks, and that malformed cyclic grant chains fail closed.

Parent-scope integrity verification was additionally validated on current
`main` commit `37ae658513c83bfaae0baf498425cbc374c99836` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_authority_integrity.py
```

Result: **20 passed, 0 failed**. The integrity path independently checks
parent-grant scope, budget, expiry, and revocation instead of trusting child
records alone.

The independent installer was validated against a freshly built wheel on
current `main` commit `8bbe0fe25ebe52e82f131761611147679e1583b9` with:

```sh
artifact_dir="$(mktemp -d)"
install_dir="$(mktemp -d)"
uv build --wheel --out-dir "$artifact_dir"
wheel_path="$(find "$artifact_dir" -maxdepth 1 -name '*.whl' -print -quit)"
CHARTERFORGE_SOURCE="$wheel_path" \
  CHARTERFORGE_INSTALL_DIR="$install_dir" \
  scripts/install-charterforge.sh
"$install_dir/bin/charterforge" --version
```

Result: **Charterforge v0.19.0 (2026.7.20)**. This proves local isolated
installation from the project artifact; package-index publication remains
separately authorized and unproven.

The current governed-runtime regression sweep was run on commit
`c88c88333d15120183efa7ce0d7cb04a35517bf7` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_objective_policy.py \
  tests/hermes_cli/test_objective_runtime.py \
  tests/hermes_cli/test_objective_service.py \
  tests/hermes_cli/test_objective_worker.py \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_finance_and_payments.py \
  tests/hermes_cli/test_business_bootstrap_command.py \
  tests/hermes_cli/test_agentic_business_e2e.py
```

Result: **121 passed, 0 failed**. This is focused regression evidence for the
durable objective loop, worker coordination, delegation authority, finance,
readiness, and Founder/CEO end-to-end surfaces; it does not establish the
external production gates listed below.

The integrated organizational decision path was validated on current `main`
commit `da756f5f5559cd86823f452fc4af62d73f354a9d` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_hiring_policy.py \
  tests/hermes_cli/test_procurement_policy.py \
  tests/hermes_cli/test_objective_adapters.py
```

Result: **39 passed, 0 failed**. This covers evidence-based contractor/FTE
selection, budget and headcount enforcement, hierarchical provisioning,
build/FOSS/buy ordering, procurement idempotency, and governed adapter
execution/read-back.

Compose deployment definitions were validated on current `main` commit
`a692fdc52d38d94e3e867c1b5bb703a701c1fa50` with:

```sh
docker compose config --quiet
docker compose -f docker-compose.windows.yml config --quiet
```

Both commands passed. This validates Compose configuration syntax and
interpolation only; it does not establish a production deployment or external
service availability.

Human intervention-boundary controls were validated on current `main` commit
`4d20cc99d4ea74bc68e65f610d5c0202511dcb79` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_operational_control.py \
  tests/hermes_cli/test_objective_runtime.py
```

Result: **37 passed, 0 failed**. Employee, worker, and runtime identities are
rejected when attempting to resolve an open intervention; explicitly
identified human advisor identities remain the resolution boundary.

Organization-scoped intervention isolation was additionally validated on
current `main` commit `bd1cbb3c406609504d281a94d61e21740bc56d46` with:

```sh
uv run --extra dev pytest -q tests/hermes_cli/test_operational_control.py
```

Result: **10 passed, 0 failed**. Scope-free resolution is rejected for
organization-bound interventions; only explicitly unscoped control records
retain that path.

The broader governance regression sweep was rerun on current `main` commit
`8044ca64f9b51b02178c0b917ec9435cbed9ef3d` with the objective, intervention,
approval, event, AgentMail, maintenance, and bootstrap suites. Result:
**99 passed, 0 failed**. This confirms production resolution paths provide
tenant scope while legacy objective-only stores remain non-crashing.

Master-stop delegation fencing was validated on current `main` commit
`13c80a421b062c15511784eeefeb8d17e63a7f25` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_operational_control.py
```

Result: **25 passed, 0 failed**. Pausing autonomy revokes active employee
grants and blocks delegated result handoff through the autonomy boundary.

The complete current-tree Founder/CEO acceptance scenario was rerun on commit
`5d8adc7a24f968c9c38ea4f597186e49bc522675` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. The scenario covered
install, bootstrap, readiness blocked then satisfied, bounded CEO execution,
process-separated delegation and evidence handoff, interrupted provider action
with read-back convergence, inbound settlement and tax recording, restart
recovery without duplicate effects, and master-stop fencing. The acceptance
image digest was `sha256:5f905c0b55bc7b3866e935f78d0d8c0cb138a2d04aaea1e1a4a9c2988c6c3e9e`.
This is controlled local-provider evidence, not proof of production provider
credentials, corporate readiness, or legal/compliance certification.

After the hierarchical delegation budget and objective-boundary hardening, the
same acceptance was rerun on current `main` commit
`41646c5a4f86df36cd86b98dde303ab7e2b09f2f` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. The bounded CEO loop,
process-separated subordinate handoff, uncertain provider read-back,
inbound tax-bearing settlement, durable restart recovery, and master-stop
fencing all passed with zero duplicate effects. The acceptance image digest
was `sha256:1e5c0a9bc9bfa110a8c98581f55c6f960f1c2cd3d5d5e4bdbe78792148cc2e16`.
This remains deterministic local-provider evidence; it does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After tightening organization actor identity resolution, the acceptance was
rerun on current `main` commit
`61c570d33c4571bce3efb8e04633922c12e603f9` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
blocked-to-ready readiness, bounded CEO execution, process-separated
delegation, uncertain provider read-back, inbound tax settlement, durable
restart recovery, and master-stop fencing passed. The acceptance image digest
was `sha256:87a21204e5e034b36587cb2e13441b4f081799e38b0b8696a9a486c869dc4adf`.
This is deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After binding organization-scoped permits to known employee executors, the
acceptance was rerun on current `main` commit
`c9bf5535a99383c16c34606c03425996fd78a2c8` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:096cbdcc6dc30f30b65193163073eeee9e443b065ca378949d0918875258f206`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After making CEO compatibility alias resolution fail closed on ambiguous
organization state, the acceptance was rerun on current `main` commit
`5314c7aa6b74d8db3d61137436a8d2d3419d9a07` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:ca1ddbd6ddd7620a44b0f57dbbe540ad121233882f8807d2b721329e9b927dd5`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After binding delegated worker launch and result handoff to independent
grant-chain integrity verification, the acceptance was rerun on current
`main` commit `838104e1bc0576495afed999584fa0e10867d92d` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:0946ab43495f376578fda4c25989de5a9af9e1c28668e6fe94d8ee6768375311`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After requiring an active delegator employee and mandate at grant issuance,
the acceptance was rerun on current `main` commit
`73bf0bb6813d189faa866569534a92bbd3851149` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:bb7baf8c797ea49eadc98876f3d940a938b10df0d2288608fedc9243915b79d9`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After binding result handoff to the exact Kanban task contract and explicit
board identity, the acceptance was rerun on current `main` commit
`9f765b604533db48854b63acc37e5900694bef02` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:69d612c451a8a5dae63305529e13d5cca022cdc5c805c50b187c0621c1126597`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After making governed completed task results append-only, the acceptance was
rerun on current `main` commit
`2079c12f5d05f33f427e86eb178efb365bd70223` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
uncertain provider read-back, inbound tax settlement, durable restart
recovery, and master-stop fencing passed. The acceptance image digest was
`sha256:8b667180d3ffbc3de7f98dd7942285130d424de42d413e019089628d306c6d46`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After reauthorizing governed task completion at the database write boundary,
the acceptance was rerun on current `main` commit
`acfa060a8a818ea7b05672261bfb994588eb5cd2` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
unauthorized governed-completion rejection, uncertain provider read-back,
inbound tax settlement, durable restart recovery, and master-stop fencing
passed. The acceptance image manifest digest was
`sha256:0134556b879712c94564b1117061c9d0d4c4c7dfaa91417e4b1a8c2ac29a9bd7`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After binding grants to explicit subordinate task contracts, the acceptance
was rerun on current `main` commit
`a55179e221096b886a7804c6b94332042e4bd184` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:247b018ab971d6df8bcac15df1457f9864f520814dfb56595d4d2b0c4f8d53fc`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After enforcing execution-time worker file capabilities, the acceptance was
rerun on current `main` commit
`0001068e678d64c0087367d65c2c851f5e41e778` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:95a72f48c3133be8bbf88012a10e24e91d470ad2d8fe3054c876d00643d945ea`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After separating worker resource scope from the Kanban coordination scope,
the acceptance was rerun on current `main` commit
`e9d97677ad82c86caf5abb2b976a987d750d410a` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:c8e79bc69ba59b5591f2147251592a27560f621bcf0b164e1d81e3fbd9778b9a`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After constraining governed terminal commands to exact command resources, the
acceptance was rerun on current `main` commit
`e9f420b75180c5fbce6942869e8ee62c882a5449` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:74651cd2e4259541d2e87b90af66c5427f0ea3b85bfe50faa2b0931626e7ce22`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After constraining governed web destinations to exact query and URL resources,
the acceptance was rerun on current `main` commit
`5b706d5757f376f2e446efe449a582b1e5e7c09f` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:8e595dc0862dfe55a581c2029921bca95ea88ebb749fe757266458900a6743e0`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After constraining governed browser navigation to exact URL resources, the
acceptance was rerun on current `main` commit
`70784b794028f37d6b1318f39049acae63d265eb` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:1a2c36c5f33f4fa41fde583e4c2bc3c53c15e72f8e8c41ba4a0d2343c754513e`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

After constraining governed outbound messaging and reactions to exact platform
and target resources, the acceptance was rerun on current `main` commit
`b74d06efaf2884148a8f20bed23c506d01b9cf64` with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. Install/bootstrap,
readiness gating, bounded CEO execution, process-separated delegation,
contract-bound worker authorization, uncertain provider read-back, inbound
tax settlement, durable restart recovery, and master-stop fencing passed. The
acceptance image manifest digest was
`sha256:7786d980dbfd7039ae868975d4203b56ccff35280990f93e9d8a63f31876e598`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

The exact delegated-worker resource boundary was additionally validated on
current `main` commit `21273d2407` with:

```sh
uv run --extra dev pytest -q tests/hermes_cli/test_workforce_delegation.py
```

Result: **17 passed, 0 failed**. The regression proves that a worker granted
one exact read resource cannot retarget the read or infer a write operation.
The full current-tree acceptance was also rerun with:

```sh
scripts/run_agentic_acceptance.sh
```

Result: **current-tree agentic acceptance: PASS**. The acceptance image
manifest digest was
`sha256:199840b4c6e4026bf4112cbe8de62d460d4512be47e8f36faca7d5f9f203878a`.
This remains deterministic local-provider evidence and does not establish
production provider credentials, corporate readiness, or legal/compliance
certification.

The exact local-file delegation example was rerun on current `main` commit
`eb56235ba1` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py
```

Result: **24 passed, 0 failed**. The worker is authorized for
`file.read` on `localhost:/home/mike/ceofile.txt` only; the sibling path and
`file.write` operation are rejected. This is deterministic authority-store
evidence and does not establish host filesystem access for production workers.

The file authorization proof was extended through the actual tool entry points
on current `main` commit `793c7ccc50` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/tools/test_file_tools.py
```

Result: **75 passed, 0 failed**. The allowed read passed authorization before
filesystem access; a retargeted read and a write attempt were rejected by the
same live worker contract. This remains deterministic local authority-store
evidence and does not establish production host-filesystem access.

The process-separated delegation acceptance was extended on current `main`
commit `c198a88f4315ed66dcf5e2ea699b546359fd33f3` with:

```sh
scripts/delegation_process_acceptance.py
scripts/run_agentic_acceptance.sh
```

The installed subordinate now invokes the real `read_file_tool` and
`write_file_tool` entry points under its exact grant, rejects a retargeted read
and write attempt, records `file_tool_boundary: pass`, and then completes the
Kanban task so a fresh CEO runtime can verify the parent objective. The full
current-tree acceptance result was **PASS**. Its image manifest digest was
`sha256:d1299bc9d6732c390d9b3d8c3f239991e01831d87777a286fc611e03be8ae700`.

The run also exposed and closed a durable grant-integrity defect: canonical
toolset ordering is now persisted before subprocess verification. This is
deterministic local-provider evidence and does not establish production
filesystem access, provider credentials, or legal/compliance certification.

The governed patch-write bypass closure was validated on current `main` commit
`a324305f32eaf7e6c26bfe77f25902a92c77b885` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/tools/test_file_tools.py
uv run --extra dev ruff check tools/file_tools.py scripts/delegation_process_acceptance.py
scripts/run_agentic_acceptance.sh
```

Results: **75 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The process-separated subordinate now rejects direct writes,
retargeted reads, and patch replacement under its read-only grant. The
acceptance image manifest digest was
`sha256:4b7ca9cc25747eaaf83245bdfaed5e6354cbce830aa28bd12ea1ac57758d837d`.
This remains deterministic local-provider evidence and does not establish
production filesystem access, provider credentials, or legal/compliance
certification.

The governed file-search boundary was validated on current `main` commit
`6485b3aaea03ea16e7ff5d7fbda2b4edbae795a2` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/tools/test_file_tools.py
uv run --extra dev ruff check tools/file_tools.py scripts/delegation_process_acceptance.py
scripts/run_agentic_acceptance.sh
```

Results: **75 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The process-separated subordinate now rejects unauthorized search in
addition to direct writes and patch replacement. The acceptance image manifest
digest was
`sha256:884f33fbe9fe0630dadff95e33eade4b0e281b50a3f4c82297f3e6a858018322`.
This remains deterministic local-provider evidence and does not establish
production filesystem access, provider credentials, or legal/compliance
certification.

The governed code-execution boundary was validated on current `main` commit
`1e7531b7d170547cc4cffe902ca15db361e4b4e9` with:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py \
  tests/tools/test_file_tools.py \
  tests/tools/test_code_execution.py
uv run --extra dev ruff check tools/code_execution_tool.py scripts/delegation_process_acceptance.py
scripts/run_agentic_acceptance.sh
```

Results: **151 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The subordinate's read-only grant rejected arbitrary Python before
the script could open `/etc/passwd`. The acceptance image manifest digest was
`sha256:5ac03bb27defaa3a648413dba1453c111a310a00f9f1b8341c86c78cf0eb6760`.
This remains deterministic local-provider evidence and does not establish
production code-execution isolation, provider credentials, or legal/compliance
certification.

The governed local-media boundary was validated on current `main` commit
`de4a266a7aee21ac7d9d34db903fc99ccd0f9831` with:

```sh
uv run --extra dev pytest -q \
  tests/tools/test_image_source.py \
  tests/tools/test_video_analyze.py \
  tests/tools/test_vision_tools.py \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_agentic_business_e2e.py
uv run --extra dev ruff check tools/image_source.py tools/vision_tools.py scripts/delegation_process_acceptance.py
scripts/run_agentic_acceptance.sh
```

Results: **159 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The subordinate's ordinary file grant rejected vision access before
media bytes were opened. The acceptance image manifest digest was
`sha256:24ec4d18e0ab5e8d4d63be32c76e60961df67b21e559a22176f6fc0ca6d5b571`.
This remains deterministic local-provider evidence and does not establish
production media isolation, provider credentials, or legal/compliance
certification.

## Delegator-authority monotonicity evidence (82e4d3b87e)

The current tree also rejects a non-root employee that attempts to create a
grant assigned to itself without an active parent grant. This closes the
self-dispatch route by which a subordinate could otherwise convert its
standing mandate into new delegation authority. The focused regression run
was:

```sh
uv run --extra dev pytest -q tests/hermes_cli/test_workforce_delegation.py
uv run --extra dev ruff check hermes_cli/workforce_delegation.py tests/hermes_cli/test_workforce_delegation.py
```

Results: **17 tests passed and ruff passed**. The full current-tree acceptance
also passed after this change with image manifest digest
`sha256:5461cb687ad66f361930c16165220a883c768dc9bb656a674b4fdf9c4da64597`.
This proves the local deterministic authority boundary; it does not establish
production deployment or external-provider authorization.

## Browser interaction authority evidence (dfba76b9a1)

The current tree now applies the same exact-operation contract to browser
interactions. A governed worker must hold the specific capability (for example
`browser.type` or `browser.evaluate`) and the exact
`browser-session:<session>` resource. Browser navigation remains separately
URL-scoped, so navigation cannot be used to infer interaction authority.

Validation:

```sh
uv run --extra dev ruff check tools/browser_tool.py tests/tools/test_browser_private_page_action_guard.py
uv run --extra dev pytest -q \
  tests/tools/test_browser_private_page_action_guard.py \
  tests/tools/test_browser_console.py \
  tests/tools/test_browser_get_images_ssrf.py \
  tests/tools/test_browser_snapshot_ssrf.py \
  tests/tools/test_browser_type_redaction.py
scripts/run_agentic_acceptance.sh
```

Results: **79 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:e8a1e5e4d1e375fe1172e92257126337d6748719a15936458370c3ccffec8050`.
This remains deterministic local-provider evidence and does not establish
production browser-provider isolation or external authorization.

## Raw CDP authority evidence (f07b40c700)

The raw `browser_cdp` escape hatch now requires `browser.cdp` plus an exact
resource of the form `browser-cdp:<session>:<method>`, with target and frame
identifiers included when supplied. This prevents a governed worker from
using direct CDP to bypass the operation-specific browser tools.

Validation:

```sh
uv run --extra dev ruff check tools/browser_cdp_tool.py tests/tools/test_browser_cdp_tool.py
uv run --extra dev pytest -q tests/tools/test_browser_cdp_tool.py
scripts/run_agentic_acceptance.sh
```

Results: **26 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:b50fade653fadc28c724dee30efb50a8c53b69ba3e89dbae0373092d983d0153`.
This remains deterministic local-provider evidence and does not establish
production CDP endpoint isolation or external authorization.

## Desktop computer-use authority evidence (5129992928)

The desktop `computer_use` tool now requires an exact capability such as
`computer.click`, `computer.type`, or `computer.capture` against
`desktop-session:<session>`. Governed workers use this control-plane permit as
their authority; interactive approval remains available only for ungoverned
interactive sessions. A requested follow-up capture also requires its own
capture capability.

Validation:

```sh
uv run --extra dev ruff check tools/computer_use/tool.py tests/tools/test_computer_use.py
uv run --extra dev pytest -q \
  tests/tools/test_computer_use.py \
  tests/tools/test_computer_use_delivery_ladder.py \
  tests/tools/test_computer_use_capture_routing.py
scripts/run_agentic_acceptance.sh
```

Results: **256 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:952b29d98e5189ff92e10891bc127f1e49258512cd281b7b4a143f1622941660`.
This remains deterministic local-provider evidence and does not establish
production desktop-driver isolation or external authorization.

## Dynamic MCP authority evidence (7ac13d16a3)

MCP tools are dynamically registered, so their handlers now enforce
`mcp.call` against an exact resource such as
`mcp-server:<server>:tool:<tool>`. Resource reads and prompt retrieval add the
requested URI or prompt name to the scope. The authorization check runs before
transport lookup or RPC dispatch, preventing an advertised MCP tool from
becoming an ungoverned side-effect route.

Validation:

```sh
uv run --extra dev ruff check tools/mcp_tool.py tests/tools/test_mcp_tool.py
uv run --extra dev pytest -q \
  tests/tools/test_mcp_tool.py \
  tests/tools/test_mcp_capability_gating.py \
  tests/tools/test_mcp_utility_capability_gating.py
scripts/run_agentic_acceptance.sh
```

Results: **253 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:8d6d1c193db2d8dc224fe9cd4a310006084eb94b8a9a2b5b29361fdcdef04ba8`.
This remains deterministic local-provider evidence and does not establish
production MCP-server trust or external authorization.

## Scheduler authority evidence (47c396c62b)

Cron mutations now require operation-specific capabilities (`cron.create`,
`cron.update`, `cron.pause`, `cron.resume`, `cron.remove`, or `cron.run`) and
an exact scheduler resource. Existing jobs use `cron-job:<id>`; creation uses
`cron-create:<name-or-schedule>`. Read-only listing remains available without
mutation authority, while immediate execution is governed as a run action.

Validation:

```sh
uv run --extra dev ruff check tools/cronjob_tools.py tests/tools/test_cronjob_tools.py
uv run --extra dev pytest -q \
  tests/tools/test_cronjob_tools.py \
  tests/tools/test_cronjob_run_immediate.py \
  tests/tools/test_cron_approval_mode.py \
  tests/tools/test_cron_prompt_injection.py
scripts/run_agentic_acceptance.sh
```

Results: **125 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:e57b0e27ae859805458206cad65384b7f7139a3694aeba91d581c125e59d9f3f`.
This remains deterministic local-provider evidence and does not establish
production scheduler deployment or external authorization.

## Governed fan-out delegation evidence (9f78b3544c)

The legacy `delegate_task` fan-out path now fails closed for governed workers
unless the control plane authorizes `work.delegate` against a resource derived
from the exact goal/context/tasks/role payload. This prevents a subordinate
from spawning an unrecorded child merely by inheriting a broad toolset. The
normal interactive path remains compatible because it has no execution
contract.

Validation:

```sh
uv run --extra dev ruff check tools/delegate_tool.py tests/tools/test_delegate.py
uv run --extra dev pytest -q tests/tools/test_delegate.py
scripts/run_agentic_acceptance.sh
```

Results: **160 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:017f64f455339e8e8ff0c190739650e4f0c7d7691f3be0f1e0b1963464d16274`.
This remains deterministic local-provider evidence and does not establish
production multi-agent deployment or external authorization.

## Discord action authority evidence (044a6bce9c)

Discord and Discord-admin handlers now authorize each action before REST
dispatch. The permit uses a `discord.<action>` capability and a hash of every
supplied guild, channel, user, role, message, query, pagination, and thread
parameter, preventing replay against a different external target.

Validation:

```sh
uv run --extra dev ruff check tools/discord_tool.py tests/tools/test_discord_tool.py
uv run --extra dev pytest -q tests/tools/test_discord_tool.py
scripts/run_agentic_acceptance.sh
```

Results: **101 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:2d56357d5ad2a5727ffd99e61e8a8bbf66eefce3408c0099d4d547590adcbaec`.
This remains deterministic local-provider evidence and does not establish
production Discord credentials, permissions, or external authorization.

## Home Assistant authority evidence (6b8849a5a0)

`ha_call_service` now authorizes `homeassistant.call_service` against a hash of
the exact Home Assistant URL, domain, service, entity, and service data before
the POST request. Existing blocked service domains remain fail-closed, and
read-only entity/state/service discovery is not conflated with device-control
authority.

Validation:

```sh
uv run --extra dev ruff check tools/homeassistant_tool.py tests/tools/test_homeassistant_tool.py
uv run --extra dev pytest -q tests/tools/test_homeassistant_tool.py
scripts/run_agentic_acceptance.sh
```

Results: **70 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:6f764b88cbc4742ec2f3134e2b724fd43d59c314de642aaf0ef8349b883834f7`.
This remains deterministic local-provider evidence and does not establish
production Home Assistant credentials, device safety, or external
authorization.

## Feishu comment authority evidence (33d8f5a97e)

Feishu document comment replies and additions now require separate
operation-specific permits before provider dispatch. Each target resource is
the SHA-256 digest of the exact operation, document token, comment ID when
applicable, file type, and content, so a permit cannot be replayed against a
different document or payload.

Validation:

```sh
uv run --extra dev ruff check tools/feishu_drive_tool.py tests/tools/test_feishu_tools.py
uv run --extra dev pytest -q tests/tools/test_feishu_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **7 tests passed, ruff passed, and current-tree agentic acceptance
passed**. The acceptance image manifest digest was
`sha256:5cf406577525cc58315fa713efc753a023e7829bb4a94dc2c285614db5c3187e`.
This remains deterministic local-provider evidence and does not establish
production Feishu credentials, permissions, or external authorization.

## Outbound messaging authority evidence (287a9c8db5)

`send_message` and reaction actions now authorize only after target resolution.
The permit resource is a SHA-256 digest of the exact operation, resolved
platform and chat, thread or message ID, message or emoji, media paths, and
delivery mode. This prevents a worker from changing content or retargeting an
alias while reusing a previously issued permit.

Validation:

```sh
uv run --extra dev ruff check tools/send_message_tool.py \
  tests/tools/test_send_message_react.py \
  tests/tools/test_send_message_target_parse.py
uv run --extra dev pytest -q \
  tests/tools/test_send_message_react.py \
  tests/tools/test_send_message_target_parse.py
uv run --extra dev pytest -q \
  tests/tools/test_send_message_tool.py \
  tests/tools/test_signal_media.py \
  tests/tools/test_slack_send_message_media.py
scripts/run_agentic_acceptance.sh
```

Results: **15 focused tests passed, 12 broader tests passed with 4 optional
skips, Ruff passed, and current-tree agentic acceptance passed**. The
acceptance image manifest digest was
`sha256:f99812aa2e3d68880263645f08b913b9f215a2a006ea0e1bc89a22128c14240f`.
This remains deterministic local-provider evidence and does not establish
production messaging credentials, permissions, or external authorization.

## Browser navigation authority evidence (8782d3a21d)

Browser navigation now requires `browser.navigate` against a resource derived
from the exact browser session and a SHA-256 digest of the normalized URL.
Navigation remains distinct from click, typing, evaluation, snapshot, and CDP
capabilities; a URL grant cannot be replayed in another worker session.

Validation:

```sh
uv run --extra dev ruff check tools/browser_tool.py
uv run --extra dev pytest -q \
  tests/tools/test_browser_camofox.py \
  tests/tools/test_browser_hardening.py \
  tests/tools/test_browser_secret_exfil.py \
  tests/tools/test_browser_ssrf_local.py
scripts/run_agentic_acceptance.sh
```

Results: **104 browser regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:08a98c235cb09005802fa7454ea5feaaec49c01059eb89b805b5d18b7d8a00b2`.
This remains deterministic local-provider evidence and does not establish
production browser credentials, permissions, or external authorization.

## Terminal execution authority evidence (b3be1f7625)

`terminal.exec` permits now use a SHA-256 resource derived from the complete
execution context: command, resolved working directory, backend, timeout, task
identity, background and PTY mode, and notification/watch settings. A
subordinate cannot reuse a command grant to change where or how it executes.

Validation:

```sh
uv run --extra dev ruff check tools/terminal_tool.py tests/tools/test_terminal_tool.py
uv run --extra dev pytest -q \
  tests/tools/test_terminal_tool.py \
  tests/tools/test_terminal_task_cwd.py \
  tests/tools/test_terminal_foreground_timeout_cap.py \
  tests/tools/test_terminal_none_command_guard.py \
  tests/tools/test_terminal_compound_background.py \
  tests/tools/test_terminal_exit_semantics.py
scripts/run_agentic_acceptance.sh
```

Results: **119 terminal regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:2e37ddea216ffa45eccd9dee8887ecf091049d6ad5d32053832935ed1ac0b2c5`.
This remains deterministic local-provider evidence and does not establish
production shell credentials, sandbox deployment, or external authorization.

## Web provider authority evidence (7fdf1f6eb9)

Web provider permits now use hashed resources for the complete request. Search
binds the query and result limit; extraction binds each normalized URL, output
format, and character limit. A subordinate cannot reuse a request grant to
increase provider usage or alter the returned representation.

Validation:

```sh
uv run --extra dev ruff check tools/web_tools.py
uv run --extra dev pytest -q \
  tests/tools/test_web_providers.py \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_extract_robustness.py \
  tests/tools/test_web_tools_dict_urls.py \
  tests/tools/test_web_tools_truncate.py
scripts/run_agentic_acceptance.sh
```

Results: **104 web regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:82af4423a67717d4cee7aa7cc38e5ff13144ee767a3a8c8b3d8c631f49c2c8ef`.
This remains deterministic local-provider evidence and does not establish
production search/extraction credentials, quotas, or external authorization.

## Remote media authority evidence (d2c510410c)

Remote image and video sources now require separate URL-bound permits before
network download. Local filesystem media permissions do not imply external
fetch authority; each remote source is represented by a SHA-256 resource
derived from its exact URL.

Validation:

```sh
uv run --extra dev ruff check tools/image_source.py tools/vision_tools.py
uv run --extra dev pytest -q tests/tools/test_image_source.py tests/tools/test_vision_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **106 media regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:8081a3ec740c3a271fe9ed82500f5f54e79a073f3951fd7699f842c44785ed76`.
This remains deterministic local-provider evidence and does not establish
production media-provider credentials, quotas, or external authorization.

## Code execution authority evidence (9cf2e75646)

`code.execute` permits now use a SHA-256 resource derived from the exact
script, task identity, effective sandbox tool allow-list, and terminal backend.
Changing the script or requesting broader RPC tools requires a new grant, and
the permit cannot be replayed against another execution environment.

Validation:

```sh
uv run --extra dev ruff check tools/code_execution_tool.py tests/tools/test_code_execution.py
uv run --extra dev pytest -q tests/tools/test_code_execution.py
scripts/run_agentic_acceptance.sh
```

Results: **77 code-execution regression tests passed, Ruff passed, and
current-tree agentic acceptance passed**. The acceptance image manifest digest
was `sha256:3660e2b4ba99b2cc0b6c19711543c97f187ca70503154739612524b7b1e68b43`.
This remains deterministic local-provider evidence and does not establish
production sandbox deployment or external authorization.

## Kanban linking authority evidence (7552bb0573)

`kanban.link` now requires a SHA-256 resource derived from the exact parent
task, child task, board, and operation before the dependency edge is written.
This prevents governed workers from creating arbitrary cross-task durable
relationships outside their delegated authority.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **120 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:14f5617350049b5a54123776f136f8c0255dde3d0323e4cc01ad2caebbf99a67`.
This remains deterministic local-provider evidence and does not establish
production multi-tenant board deployment or external authorization.

## Kanban creation authority evidence (279558018c)

`kanban.create` now requires a SHA-256 resource derived from the complete
requested task contract: title, body, assignee, parents, tenant, priority,
workspace/project, skills, model/provider, goal settings, status, session, and
board. This prevents task creation from bypassing the delegation boundary.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **121 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:c7cade3b2bc4d5f2f15a086970b5ff7fd78386765cc893685c0264fff0d0885b`.
This remains deterministic local-provider evidence and does not establish
production multi-tenant task dispatch or external authorization.

## Kanban comment authority evidence (464f01f0ea)

`kanban.comment` now requires a SHA-256 resource derived from the exact task,
board, runtime author, and redacted comment body. Durable handoff evidence
cannot be redirected or rewritten under a broader comment grant, and caller
supplied author identities remain ignored.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **122 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:6592d9e527e686808ed44081adfb16331e8a26d9fd825832a3f62ee5b83149ab`.
This remains deterministic local-provider evidence and does not establish
production multi-tenant evidence storage or external authorization.

## Kanban attachment authority evidence (4485007496)

Kanban attachment writes now require exact permits before durable effects. Inline
attachments bind task, board, filename, content type, size, and content hash;
URL attachments bind task, board, source URL, filename, and content type before
the remote fetch begins.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **124 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:ddf6fe1704ad13a0ff8e49a39dd2d2c955b68e888687bc12ce354119f6c8acd9`.
This remains deterministic local-provider evidence and does not establish
production artifact storage, remote-provider credentials, or external
authorization.

## Kanban heartbeat authority evidence (4caa839dce)

`kanban.heartbeat` now requires a SHA-256 resource derived from the exact task,
board, note, claim lock, and expected worker run identity before extending a
claim or recording a heartbeat event. This prevents lease extension or run
impersonation outside the delegated lifecycle scope.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **125 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:67783db32ac3ab37ab56c561ca0e53629683e868754a258a57f1c407a4ced640`.
This remains deterministic local-provider evidence and does not establish
production distributed lease infrastructure or external authorization.

## Kanban closure authority evidence (20f5bc014f)

Kanban completion and block transitions now require exact permits before task
state changes. Completion binds task, board, redacted summary/result/metadata,
created cards, artifacts, and expected run; block binds task, board, redacted
reason, kind, and expected run. Existing goal-judge, ownership, artifact, and
run-integrity checks remain authoritative.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **127 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:7f4b920906366658208ebb096dc76f53d49b74401dc7f4f826334f03d21b3c13`.
This remains deterministic local-provider evidence and does not establish
production distributed lifecycle infrastructure or external authorization.

## Kanban read authority evidence (00af90ce79)

`kanban.show` and `kanban.attachments` now require SHA-256 resources derived
from the exact task and board before reading durable state. This covers task
bodies, comments, runs, events, worker context, and attachment metadata;
cross-task reads are not implied by the Kanban toolset.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **129 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:4e9062977a6056e2ba24d1f3e8cff95f1003cb2a3bfe004c4c63adb4b238706a`.
This remains deterministic local-provider evidence and does not establish
production multi-tenant read isolation or external authorization.

## Kanban orchestrator authority evidence (dddf88097b)

Orchestrator-only Kanban actions now require exact permits. `kanban.list` binds
filters, tenant, archive mode, limit, and board; `kanban.unblock` binds the
exact task and board. A governed orchestrator cannot replay a grant with a
broader query or a different lifecycle target.

Validation:

```sh
uv run --extra dev ruff check tools/kanban_tools.py tests/tools/test_kanban_tools.py
uv run --extra dev pytest -q tests/tools/test_kanban_tools.py
scripts/run_agentic_acceptance.sh
```

Results: **129 Kanban regression tests passed, Ruff passed, and current-tree
agentic acceptance passed**. The acceptance image manifest digest was
`sha256:a46eeca5239c5426c1310ded5db9cd8d0f9ba4a56d2838c2da2220e761cfed6b`.
This remains deterministic local-provider evidence and does not establish
production multi-tenant orchestration or external authorization.

## Release gates that remain open

- Corporate formation, legal personhood, banking, and human legal-principal
  actions remain outside the software's authority.
- Production deployment, high availability, disaster-recovery drills, and
  external payment-provider credentials are not established by this checkout.
- PCI DSS, SOC 2, SOX, GDPR, EU AI Act, CASL, CAN-SPAM, and jurisdiction-specific
  tax or payments applicability remain deployment and legal work.
- SQLite is the implemented authority store; Postgres and an external broker
  remain deployment work.
- Supervised worker crash recovery invariants are proven via fault-injection
  tests (tests/hermes_cli/test_worker_fault_injection.py); production
  worker topology with remote authority-store remains deployment work.

See [implementation status](docs/implementation-status.md) for the complete
capability and limitation inventory, and the release boundary in
[CHANGELOG.md](CHANGELOG.md).

## Exact delegated-authority and finance audit (2026-07-27)

The current tree keeps delegation authority monotone: a child grant must be a
subset of the delegator's active capabilities, systems, toolsets, skills,
resource scope, budget, objective, and expiry. Worker calls are checked again
at execution time against the live grant, revocation chain, mandate version,
and exact `system` plus `target_resource`. Consequently, a grant such as
`file.read` for `localhost:/home/mike/ceofile.txt` cannot be retargeted to
`/home/mike/notceofile.txt` or inferred as `file.write`.

The business-finance path separately requires organization/objective lineage,
exact idempotency payloads, budget reservations, spend controls, provider
assessments, and provider read-back before settlement. These controls are
projection-independent from readiness; readiness consumes their persisted
results rather than reimplementing their policy.

Exact validation command:

```sh
uv run --extra dev pytest -q \
  tests/hermes_cli/test_workforce_delegation.py \
  tests/hermes_cli/test_finance_and_payments.py \
  tests/hermes_cli/test_procurement_policy.py
```

Result on commit `2e17387bc4e8a9914edad237ec7b0ec6bd30465e`: **47 passed**.
This is deterministic authority-store and test-provider evidence; it does
not establish production banking, payment-provider credentials, or external
legal compliance.
