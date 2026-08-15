---
title: "HERMES-TELEGRAM-TRANSPORT-001: restore Telegram polling health"
status: MERGED_DEPLOYED_RUNTIME_DEGRADED
date: 2026-08-15
type: reliability
ticket: HERMES-TELEGRAM-TRANSPORT-001
target_repo: hermes-agent
---

# HERMES-TELEGRAM-TRANSPORT-001: restore Telegram polling health

## Problem

The Hermes gateway process is running on DGX, but process health is not
equivalent to Telegram inbound readiness. The latest bounded DGX observation
showed `hermes-gateway.service` active with MainPID `3161529`, exit status `0`,
and `NRestarts=0`, while recent gateway logs reported:

- Telegram polling `TimedOut` during reconnect, with retry attempt `5/10`;
- repeated polling reconnect warnings;
- outbound delivery evidence exists, but inbound polling is not proven healthy.

This is a transport failure class, not an ARCH-002 runtime-state-only fix. The
repository already contains timeout and reconnect regression tests, so this
ticket must first reconcile the observed live behavior with the current code
and deployed release. It must not blindly duplicate an earlier fix.

## Goal

Restore and prove Telegram inbound polling health after transient or sustained
network/API timeout without reporting a merely running gateway process as
healthy. Preserve outbound send behavior, authorization, update ordering, and
the existing fail-closed safety boundaries.

## Scope

- Reproduce the observed polling timeout/reconnect path using bounded,
  network-free tests and read-only DGX evidence. Identify the exact current
  code path in `plugins/platforms/telegram/adapter.py` and any shared Telegram
  transport helper before changing behavior.
- Define distinct states for service/process health, inbound polling health,
  and outbound send-path health. A systemd `active` result alone must not clear
  inbound degradation.
- Define polling progress concretely: a successful, error-free `getUpdates`
  response, including HTTP 200 with an empty update batch, or an accepted
  update callback may advance readiness. `start_polling()` returning or a
  retry timer firing alone may not. No other signal qualifies unless added to
  this correction set and independently re-reviewed.
- Fix only the confirmed root cause in the Telegram polling transport. The
  correction must keep polling start/reconnect awaits bounded, prevent
  overlapping polling generations, preserve request-pool cleanup, and retain
  bounded retry/backoff/fatal escalation semantics. Retry backoff must have a
  bounded ceiling and jitter so sustained failures do not create a reconnect
  storm.
- Ensure a successful reconnect is promoted to healthy only after observable
  polling progress, not merely after `start_polling()` returns.
- Add regression coverage for the exact failure class, including hung polling
  start, `TimedOut`/network error, reconnect retry scheduling, progress-based
  recovery, fatal escalation, cancellation/cleanup, and separation of inbound
  polling from outbound send health.
- Treat the existing Telegram timeout/reconnect tests as baseline coverage, not
  proof that the deployed path is healthy. Extend or refactor them only where
  they do not assert this ticket's new readiness and ownership contract, then
  add focused hermetic cases for empty successful polls, retry exhaustion,
  request-pool cleanup across multiple retry cycles, and cancellation.
- Before implementation approval, audit the existing Telegram timeout/reconnect
  tests and record which are baseline, which need extension, and which new
  contract cases are missing. This audit is a review gate, not deployed-path
  evidence.
- The pre-implementation deployment/rollback checklist below is a required
  deliverable: immutable release identity, effective user-unit verification,
  bounded restart procedure, live inbound/outbound evidence requirements, and
  the exact rollback trigger and prior release restoration steps. This remains
  documentation and a gate, not authorization to execute it.
- Record CI, independent review, deployment, rollback, and live Telegram
  evidence as separate gates.

## Non-goals

- Do not change Telegram credentials, bot ownership, allowlists, or webhook
  secrets.
- Do not disable TLS, bypass Telegram API errors, suppress timeout warnings,
  or claim readiness from `systemctl is-active` alone.
- Do not add an unconditional service restart/watchdog before the root cause
  and safe recovery contract are reviewed.
- Do not modify `/home/cwliao/.hermes`, the live DGX checkout, systemd units,
  release snapshots, or Telegram state during planning or review.
- Do not fold ARCH-002 runtime-state schema work or HERMES-MONITORING-001
  alerting into this ticket. Monitoring may consume the health states after
  this transport contract is accepted.

## Acceptance criteria

1. The current degradation is reproduced or otherwise line-mapped to an exact
   code path with evidence; an existing regression test is not treated as
   proof that the deployed path is healthy.
2. Inbound polling, outbound send, and process/service health have explicit,
   non-colliding states and transitions. A timeout/reconnect loop is visibly
   degraded and a successful recovery requires bounded progress evidence: a
   successful empty or non-empty `getUpdates` response or an accepted update
   callback. A returned `start_polling()` with no such signal does not clear
   degradation.
3. Reconnect behavior is bounded and single-owner: no forever-hung await, no
   overlapping polling generations, no request-pool leak across retries, and
   no silent death after a transient failure. Backoff has bounded maximum
   delay plus jitter, and retry exhaustion reaches an explicit fatal state.
4. Focused hermetic tests cover timeout, retry scheduling, bounded/jittered
   backoff, empty successful poll progress, non-empty update progress,
   cancellation, request-pool cleanup across repeated retry cycles, fatal
   max-retry escalation, and inbound/outbound health separation without real
   Telegram calls or credentials. The pool-cleanup assertion must verify that
   a failed generation's pending `getUpdates` request does not leak into the
   next generation's request context or timeout budget. Existing regression
   tests are identified as baseline or explicitly extended; neither is treated
   as deployed-path proof.
5. Relevant GitHub CI checks pass, and exactly two independent authenticated
   CLI-agent review sessions independently review the same packet/correction
   set and return a traceable final `PASS`: one Claude session using the
   Claude Code remote binary at `/home/cwliao/.claude/remote/ccd-cli/2.1.229`,
   and one AGY session using the Antigravity CLI at
   `/home/cwliao/.local/bin/agy`. Reviewer means the independently invoked
   authenticated agent session, not an additional human or an unverified
   process. Qualification requires a real response from the named binary in
   the named host/OS session, with authentication proven by that response; a
   config directory may be absent when the existing authenticated session is
   supplied by its runtime. Record only host, cwd, binary/version, non-secret
   auth/session preflight result, packet hash, bounded command mode, final
   output, and verdict. Binary versions are recorded per run, not product
   dependencies; a changed path or version requires fresh qualification. No
   additional human or third reviewer is required for this ticket's review
   gate unless separately authorized. The packet-only review may not invoke
   tools or inspect other files; no implementation tools specifically means
   no edit/write/patch, test or lint execution, deploy, restart, credential
   access, or network mutation. Findings are reconciled before implementation
   or deployment.
6. A deployment/rollback checklist is present before implementation approval,
   covering immutable release identity, effective `systemctl --user` unit,
   bounded restart, live inbound and outbound evidence, rollback trigger, and
   prior-release restoration. The checklist must be documented and reviewed
   before implementation approval; its execution remains separately authorized
   after implementation, CI, and deployment approval. Deployment, service
   restart, live inbound Telegram verification, outbound verification, and
   rollback remain separately authorized and recorded. A green unit/CI result
   is not live health evidence.
7. Gate order is explicit: dual reviewer `PASS` on this plan permits
   implementation to begin; implementation then requires the pre-
   implementation test audit, focused hermetic tests, and relevant CI; only
   after those evidence gates and a separate deployment authorization may the
   deployment/rollback checklist be executed or live Telegram evidence be
   collected. No implementation or live action is implied by reviewer `PASS`.

## Review questions

- Is this ticket correctly separated from ARCH-002 and HERMES-MONITORING-001?
- Does the scope target the observed Telegram polling degradation without
  assuming that an existing test already proves the deployed release?
- Are the proposed health boundaries and acceptance tests sufficient to avoid
  false `healthy` status while preserving safe recovery?
- Is any part of the scope speculative or missing a required failure path?

## Pre-implementation deployment/rollback checklist

- Record the immutable implementation/release SHA and changed-file manifest.
- Verify the effective `systemctl --user` unit, drop-ins, release path, and
  `HERMES_RELEASE_SHA` before any restart; capture `MainPID`, `NRestarts`,
  exit status, and bounded logs showing no hung polling await or restart loop.
- Use a bounded, operator-visible restart procedure with a captured prior
  release and a stop condition for unhealthy polling.
- Require separate live evidence for inbound polling progress and outbound
  send success; process `active` alone is insufficient.
- Roll back when the bounded post-restart health window shows no qualifying
  `getUpdates` success/update callback, two fatal escalations within ten
  minutes, no qualifying progress within 90 seconds after a bounded reconnect
  attempt, or a regression in outbound delivery; restore the captured prior
  release and re-check service/process state.
- Do not execute any checklist step during planning or review without a
  separate deployment authorization.

## Re-review correction set

The independent review rounds identified clarifications rather than an
implementation defect. The plan now defines low-traffic empty-poll progress,
bounded jittered backoff, explicit pool-cleanup assertions, baseline versus
new contract tests, quantitative rollback triggers, authenticated Claude/AGY
reviewer qualification and traceability, pre-review deployment documentation,
and the implementation/test/deployment gate order.

## Implementation evidence

- Implementation commit: `683a1e122` (`fix: bound Telegram polling reconnect recovery`).
- Changed files: `plugins/platforms/telegram/adapter.py` and
  `tests/gateway/test_telegram_network_reconnect.py`.
- Implemented bounded polling request shutdown/initialization, jittered capped
  reconnect backoff, stale-generation progress protection, and regression
  coverage for jitter, pool-operation timeout, empty successful polling
  progress, and stale generation isolation.
- `py -m py_compile` passed for both changed Python files.
- `git diff --check` passed.
- Focused pytest initially could not collect because the available system
  Python lacked `httpx`; a disposable `.review-venv` was then created with
  the pinned test dependencies needed for this ticket. The focused command
  `pytest -p no:cacheprovider tests/gateway/test_telegram_network_reconnect.py
  tests/gateway/test_telegram_start_polling_timeout.py -q` passed: `51 passed
  in 16.26s`, including the cross-generation pool-drain regression.
- No DGX deployment, service restart, live inbound verification, outbound
  verification, or rollback execution had been performed at implementation
  review time; deployment evidence is recorded below.

## Post-implementation review evidence

- Review packet SHA-256: `1dd4ffbd7aa4316f508a5dc4d2416faecb9eb87943f32ecb883e335fc4666d5e`.
- DGX preflight: `/home/cwliao/.hermes/hermes-agent`, authenticated host
  `55-0940189-03`, packet hash matched.
- Authenticated DGX Claude `PASS` and authenticated DGX AGY `PASS` were
  independently returned for the same packet and correction set.
- Review found no remaining implementation defect. CI, merge, and deployment
  evidence are recorded separately below; runtime Telegram delivery remains
  unproven.

## Merge and deployment evidence

- PR #19 merged with squash commit
  `77bcb5d0717ed4b31daec8a9ef701057528e08ae`.
- GitHub Actions CI run `31883895988` required checks passed after rerunning
  the unrelated `tests/gateway/test_stream_consumer_fresh_final.py` slice.
- DGX host preflight matched `55-0940189-03`; the prior release was
  `/home/cwliao/.hermes/releases/v2026.8.15-hermes-calendar-guard-1b3d444955`.
- New immutable release:
  `/home/cwliao/.hermes/releases/v2026.8.15-hermes-telegram-transport-77bcb5d0717e`.
  `.hermes-release-sha` and `gateway_boot_fingerprint` both identify the
  merged SHA. The Telegram adapter compiled before restart.
- Rollback metadata is preserved under
  `/home/cwliao/.hermes/deploy-backups/hermes-telegram-transport-77bcb5d0717e`.
- `hermes-gateway.service` restarted through `systemctl --user`; post-deploy
  state is active/running, MainPID `3504674`, `NRestarts=0`, and
  `ExecMainStatus=0`, with the new release as `WorkingDirectory`.
- Runtime boundary: no qualifying `getUpdates` progress or Telegram inbound
  delivery was observed during the bounded post-deploy window; logs remained at
  the initial Telegram connection attempt. Service health is therefore active
  but Telegram polling is `DEGRADED`, not healthy. No outbound delivery test or
  rollback was executed.

## Current implementation gate

`MERGED_DEPLOYED_RUNTIME_DEGRADED`: implementation review, CI, merge, and
immutable deployment passed their respective gates. Telegram inbound readiness
remains unproven/degraded; investigate the network/initial-connect path or use
the documented rollback trigger before claiming runtime health.

## Current gate

`MERGED_DEPLOYED_RUNTIME_DEGRADED`. No Telegram inbound/outbound delivery
claim is authorized by this plan without direct evidence.


## Post-deploy E2E verification (2026-08-16)

- The separately authorized deployment selected immutable release
  `/home/cwliao/.hermes/releases/v2026.8.16-hermes-update-001-0fe3773ccf`
  with marker SHA `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b`.
- Effective `hermes-gateway.service` evidence after restart: `active/running`,
  MainPID `3992364`, `ExecMainStatus=0`, `NRestarts=0`, and WorkingDirectory
  and PYTHONPATH matched the new release. The calendar-guard timer was
  `active/waiting` and its unit/wrapper were updated to the same release.
- Outbound E2E through the configured Hermes path
  `hermes send --to telegram:SPARK --json` returned
  `success=true`, `message_id=1967`, and `mirrored=true`.
- Inbound E2E remains unproven/degraded. After restart at approximately
  `06:31:43` CST, logs showed Telegram connection and command-menu
  registration through approximately `06:31:52`, but no qualifying successful
  `getUpdates` progress was observed through `06:37:25` CST. A connected
  polling mode and an active systemd service do not clear this gate.
- Result: service/process health PASS; outbound delivery PASS; inbound polling
  DEGRADED/UNPROVEN; overall Telegram E2E is PARTIAL.
- Next action: diagnose the DGX primary/fallback Telegram network path and the
  exact polling-progress ownership before opening a correction set. Keep
  ARCH-002 and monitoring work separate.
