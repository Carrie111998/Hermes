---
title: "HERMES-TELEGRAM-INBOUND-001: restore and prove Telegram inbound polling readiness"
status: PLAN_ONLY
date: 2026-08-16
type: reliability
ticket: HERMES-TELEGRAM-INBOUND-001
target_repo: hermes-agent
---

# HERMES-TELEGRAM-INBOUND-001: restore and prove Telegram inbound polling readiness

## Status

PLAN_ONLY / READY_FOR_REVIEW

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

## Current next action

Perform bounded, read-only DGX diagnosis and prepare a metadata-only review
packet. Do not modify code, credentials, Telegram state, systemd units, or
the live DGX runtime until this plan reaches review consensus.
