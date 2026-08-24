# Mission Control Operating Layer v1

Status: authoritative for the Forge/Atlas SaaS bundle. Approved by RJ on
2026-08-24. This document supersedes conflicting Forge/Atlas ownership text in
the legacy shared governance files; historical text remains retained there.

## Ownership

| System | Authority | Scope |
|---|---|---|
| Paco | Home Team / Parent IP Mission Control | Internal work, parent IP, Home Team agents |
| Forge | SaaS business control plane | Tenant-safe business state, approvals, governed execution decisions, sanitized usage/outcomes |
| Atlas | SaaS infrastructure Mission Control | VPS/process/runtime health, recovery, telemetry, sanitized incidents |
| Forge + Atlas | Divestable SaaS Operations bundle | Normal SaaS operation without Paco |

Paco is not a required dependency for SaaS availability. Forge and Atlas must
not exchange tenant records, billing authority, wallet/ledger state,
credentials, prompts, memory, or raw diagnostics through this layer.

## Contract and adapters

The `mission_control_operating_layer` projection is additive and read-only. It
uses explicit allowlists, emits `healthy`, `degraded`, or `unavailable`, and
fails workloads safe when Atlas is unavailable while preserving customer
state. Atlas continues infrastructure monitoring/protection when Forge is
degraded. A disconnected Paco is represented as an optional dependency, not a
reason to block Forge or Atlas.

The contract is not a replacement for entitlement, billing, wallet, ledger,
customer-data, approval, or runtime authorities. Mission Control consumes the
projection; it does not become a second source of truth.

## Independence acceptance

1. Paco disconnected: Forge and Atlas remain healthy in the adapter contract.
2. Forge degraded: Atlas retains monitoring and recovery-protection capability.
3. Atlas degraded: Forge reports `fail_safe` and preserves customer state.

Live deployment is a separate gate. This candidate does not restart services,
alter customer data, or activate a new production route.
