# Python sandbox verification

## Local (macOS)

- Focused battery: `17 passed, 5 skipped` (`local-focused-tests-final.txt`). The five skips are the real Linux/user-namespace jail cases; macOS has no `unshare`.
- Relevant Hermes suites: `90 passed, 5 skipped` (`local-relevant-tests-clean.txt`): sandbox tool, toolset exposure, toolset distributions, and registry.
- Static/deployment gates (`local-static-gates-clean.txt`): Ruff clean, plane lint strict clean with zero new findings, runtime slots regenerated, deployment spec valid, and `git diff --check` clean.
- Bundle assembly dry-run: success, tool included (`bundle-dry-run.txt`). This was read-only; no deployment was performed.

## Linux jailed E2E

The dedicated `Python sandbox` GitHub Actions job runs the complete focused file on Ubuntu after enabling unprivileged user namespaces. It covers the five cases skipped locally: 600-row reconciliation, empty network namespace, filesystem escape prevention, timeout/orphan cleanup, and distinct OOM handling. The Actions run URL and transcript are recorded after the branch push.

## Deployment boundary

No deployment or production mutation was performed. `tgg-app-1` was not changed. The driver-owned Sunday procedure is in `../deploy-runbook.md`.
