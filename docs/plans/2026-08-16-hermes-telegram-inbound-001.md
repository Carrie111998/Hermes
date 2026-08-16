---
title: "HERMES-TELEGRAM-INBOUND-001: restore and prove Telegram inbound polling readiness"
status: DESIGN_REVIEW_PASS
date: 2026-08-16
type: reliability
ticket: HERMES-TELEGRAM-INBOUND-001
target_repo: hermes-agent
---

# HERMES-TELEGRAM-INBOUND-001: restore and prove Telegram inbound polling readiness

## Status

IMPLEMENTATION_REVIEW_BLOCKED_CLAUDE_UNAVAILABLE

## Context

HERMES-TELEGRAM-TRANSPORT-001 is merged and deployed, and the gateway
process is healthy, but Telegram inbound polling still has no qualifying
`getUpdates` progress evidence. Outbound delivery is separately proven.
This ticket is the independent inbound-readiness gate; it must not be closed
from `systemctl is-active`, `start_polling()` returning, or outbound success.

Current verified boundary:

- DGX host: `55-0940189-03`
- active release: `v2026.8.16-hermes-ca-29d4663bb9`
- code merge: `29d4663bb94cf2d9603d2de9d437a431b5101f14`
- service: `hermes-gateway.service`, active/running, `NRestarts=0`
- outbound: prior verified `success=true`, `message_id=1967`, `mirrored=true`
- inbound: `DEGRADED/UNPROVEN`

## Goal

Restore and prove Telegram inbound polling readiness without weakening
authorization, TLS, update ordering, retry bounds, or the private-first
boundary.

## Scope

1. Perform read-only DGX diagnosis of the actual primary/fallback Telegram
   network path, polling owner, webhook/getUpdates state, and bounded logs.
2. Line-map the current failure or missing progress signal to the active code
   path before implementation.
3. Define non-colliding states for process/service health, inbound polling
   health, and outbound send health.
4. Define qualifying progress as an error-free `getUpdates` response
   (HTTP 200, empty or non-empty batch) or an accepted update callback.
   A returned `start_polling()`, retry timer, connection attempt, or menu
   registration is not progress.
5. If a code correction is confirmed, keep polling ownership single-generation,
   awaits bounded, request pools cleaned up, retry/backoff capped and jittered,
   and fatal escalation explicit.
6. Add hermetic regression coverage for the confirmed failure class and for
   inbound/outbound health separation.
7. Record CI, independent review, merge, deployment, rollback, inbound live
   evidence, and outbound live evidence as separate gates.

## Non-goals

- No Telegram credential, bot ownership, allowlist, webhook secret, or
  Telegram state changes.
- No TLS verification bypass.
- No unconditional watchdog or restart loop before root cause and recovery
  contract are reviewed.
- No ARCH-002 runtime-state work or HERMES-MONITORING-001 alerting here.
- No DGX restart or deployment during ticket design/review.

## Acceptance gates

- Read-only diagnosis identifies the exact active path and a reproducible or
  evidence-backed failure boundary.
- Ticket/design review reaches consensus from exactly one authenticated Claude
  reviewer and one authenticated AGY reviewer on the same bounded packet.
- Implementation is blocked until the design/review gate and test audit pass.
- Focused hermetic tests cover the confirmed failure, progress promotion,
  timeout/retry/cancellation/cleanup where applicable, and inbound/outbound
  separation.
- Relevant GitHub CI passes.
- Deployment uses an immutable merged SHA, effective `systemctl --user`
  evidence, bounded restart and health retry, preserved rollback release, and
  an explicit stop condition.
- Inbound readiness is PASS only with direct qualifying `getUpdates` or
  accepted-update evidence in the bounded post-deploy window. Service health
  and outbound delivery cannot substitute for it.

## Review questions

- What exact active code/network path prevents qualifying inbound progress?
- Is the degradation caused by network/API behavior, polling ownership/lifecycle,
  instrumentation, or a combination?
- Does the readiness contract avoid false healthy while preserving low-traffic
  empty polls?
- Are rollback triggers and evidence boundaries sufficient?

## Gate order

Ticket design/review -> diagnosis -> implementation -> focused tests -> CI ->
independent implementation review -> merge -> immutable deployment -> runtime
service health -> direct inbound polling evidence -> outbound delivery
evidence. A green process or CI result does not close inbound readiness.

## Ticket design review evidence

- Packet boundary: metadata-only; no source, credentials, secrets, absolute paths,
  or generated evidence text.
- Packet SHA-256: `64a33bd93a4df2fe7dbfb43bca658d48750c29260a20579c8ea511f45ba84870`.
- Authenticated Claude: DGX Spark `55-0940189-03`, Claude Code `2.1.229`,
  bounded print review, verdict `PASS`.
- Authenticated AGY: DGX Spark `55-0940189-03`, AGY `1.1.13`, bounded
  print review, verdict `PASS`.
- Consensus: `DESIGN_REVIEW_PASS`; the ticket design gate is complete.
  Implementation remains a separate, explicitly authorized gate.

## Diagnosis evidence

Read-only diagnosis completed on 2026-08-16 against the authenticated DGX
Spark runtime. No source, credential, Telegram state, systemd unit, or live
runtime was modified.

### Runtime identity and service boundary

- Host/user: `55-0940189-03` / `cwliao`.
- Service: `hermes-gateway.service`, `ActiveState=active`,
  `SubState=running`, `MainPID=27416`, `NRestarts=0`, `ExecMainStatus=0`.
- Active release: `v2026.8.16-hermes-ca-29d4663bb9`.
- Release marker: `29d4663bb94cf2d9603d2de9d437a431b5101f14`.
- The process command uses the shared Hermes venv with `PYTHONPATH` bound to
  the active immutable release; import verification resolved the active
  `adapter.py` and `telegram_network.py` from that release.

### Active code path

- `plugins/platforms/telegram/adapter.py:3277` `connect()` selects polling
  mode when `TELEGRAM_WEBHOOK_URL` is absent.
- `adapter.py:3426-3469` builds separate general and `getUpdates` request
  pools, using the fallback transport when fallback IPs are available, then
  instruments the dedicated polling request.
- `adapter.py:3505` bounds application initialization with the wall-clock
  `_await_with_thread_deadline()` helper; the default initialization timeout
  is 30 seconds.
- `adapter.py:3615` performs best-effort `deleteWebhook`, and
  `adapter.py:3647` enters `_start_polling_resilient()`.
- `adapter.py:1994-2005` promotes inbound polling health only when an
  error-free `getUpdates` response has an `ok=true` envelope and result;
  `start_polling()` return and `getMe()` do not promote that state.
- `adapter.py:2031-2059` observes the dedicated request without changing the
  PTB payload or parser; `adapter.py:2072-2124` schedules a bounded progress
  verifier after polling starts.
- `plugins/platforms/telegram/telegram_network.py` preserves the logical
  Telegram host while trying the primary path and DoH-discovered fallback IPs.

### Bounded live observations

- The gateway log recorded `Connected to Telegram (polling mode)` at
  `09:14:27`, followed by `gateway.run: Gateway running with 1 platform(s)`;
  command-menu registration completed afterward.
- The same bounded post-start log window contained no explicit
  `getUpdates` success/progress record, no accepted-update record, and no
  post-start Telegram timeout/reconnect error.
- The journal stderr slice ended at `Connecting to Telegram (attempt 1/8)`;
  the gateway log showed the later successful bootstrap, so the two streams
  are not interchangeable evidence of inbound readiness.
- An unauthenticated reachability probe to `https://api.telegram.org/`
  returned HTTP `302` with connection time `0.002875s` and first-byte time
  `0.879259s`; system DNS resolved `api.telegram.org` to
  `149.154.166.110`. This proves only general endpoint reachability, not Bot
  API authorization or `getUpdates` success.
- No token-protected `getUpdates` request was issued, and no inbound message
  or accepted-update callback was injected. Therefore inbound readiness
  remains `DEGRADED/UNPROVEN`.

### Diagnosis result and stop condition

The service/process and general Telegram bootstrap are currently healthy, and
the bounded window did not reproduce a live transport timeout. The confirmed
remaining boundary is that the current operational evidence does not expose a
qualifying `getUpdates` success or accepted-update event, even though the
active code has an internal promotion hook. This is not enough to claim a
transport root cause or authorize implementation yet.

Next gate: turn this evidence boundary into a narrowly scoped implementation
correction set (including safe, metadata-only observability and hermetic
coverage), then obtain the required Claude+AGY implementation review before
editing source. Keep inbound, service, and outbound gates separate.

## Implementation correction-set review evidence

- Packet boundary: identical metadata-only packet for both reviewer families;
  no source, credentials, secrets, absolute paths, or evidence text.
- Packet SHA-256: `ed8767916ce27d6a35045e5cf9f59f1c018c0d00be2ba01a9f4290f60325265c`.
- Proposed correction set: add rate-limited metadata-only records at the
  existing dedicated `getUpdates` promotion point; preserve independent
  inbound/service/outbound states; record bounded degraded transitions; and
  add hermetic promotion, timeout, fencing, and redaction tests. No network,
  credential, TLS, webhook, timeout-policy, or DGX-runtime changes.
- DGX Claude: host `55-0940189-03`, Claude Code `2.1.223`; authenticated SSH
  session reached the reviewer, but the bounded 90-second review timed out
  without a verdict.
- DGX AGY: host `55-0940189-03`; `agy` was not installed/available, so no
  verdict was obtained there.
- WSL Claude fallback: host `55-0940189-91`, Claude Code `2.1.233`; the
  bounded invocation returned `Execution error` without a verdict.
- WSL AGY fallback: host `55-0940189-91`, AGY `1.1.13`; returned
  `AUTHENTICATED_REVIEWER: yes` and `VERDICT: PASS` for the exact packet.
- Consensus: `BLOCKED`; the required Claude PASS is absent. The AGY PASS
  cannot substitute for the missing Claude verdict, and implementation is
  not authorized.

## Current next action

Obtain one authenticated Claude PASS on the exact packet, then reconcile it
with the recorded AGY PASS before any source edit. Do not modify code,
credentials, Telegram state, systemd units, or the live DGX runtime while the
implementation review gate is blocked.
