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
`9e93cc1edfcc7cb7ec3ac45e3527e0f91a362cf7` (the payment-rail discovery fix).
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
- Expanded current-main validation command: **95 passed, 0 failed** across 10
  files, including the independent installer contract, security-readiness
  escalation, and credential-safe payment-rail discovery regression.
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
