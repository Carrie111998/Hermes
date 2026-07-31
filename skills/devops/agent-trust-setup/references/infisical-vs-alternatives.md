# Infisical vs Alternatives — Secrets Tools for Agent Trust

## Criteria: "YOLO some things, gate others"

The core requirement is per-secret or per-environment granularity: some secrets are accessed freely by agents (dev keys, local sudo), while others require human approval before each access (production credentials, cloud provider keys).

## Comparison

| Tool | Granular gating | Self-hosted | Agent integration | Fit |
|---|---|---|---|---|
| **Infisical** | Per-folder/per-secret RBAC + approval workflows + JIT access with TTLs | Docker Compose (Postgres + Redis + server) or free cloud tier | `infisical run -- hermes` injects env vars into any process; Agent Vault proxy mode | Strong — mature, MIT, free cloud |
| **Gatehouse** | Per-secret `requires_approval=true` flag — exact match for YOLO-some pattern | Single Docker container (~50MB) | MCP-native (9 tools), explicit Hermes support, onboarding links auto-install skills | Best fit on paper — but brand new (0 stars, fork, Jun 2026). Worth revisiting when mature |
| **AgentPassVault** | Per-secret async approval flow — agent requests, human approves via web UI | CLI + web UI | Zero-knowledge, encrypted delivery via public key crypto | Good but newer project |
| **pass (unix)** | All-or-nothing — if GPG unlocked, everything accessible | Native, no deps | `pass show path` — simple but no approval gate | Too simple for per-secret gating |
| **Proton Pass CLI** | Per-vault scoping with audit logging, no per-secret approval | Cloud only | Agent tokens with `PROTON_PASS_AGENT_REASON` audit | Not granular enough |
| **HashiCorp Vault** | Deepest dynamic secrets + leasing model, per-path policies | Self-hosted (heavy) | Vault Agent daemon, API-first | Overkill for single-user; unseal ceremony |
| **1Password CLI (op)** | Per-vault scoping | Cloud only | `op read "op://vault/item/field"` | Good if already using 1Password; no per-secret approval |

## Why Infisical is the recommended choice

1. **Free cloud tier** — no need to self-host 3 containers for a single user
2. **`infisical run` injection** — dead simple: `infisical run --env=dev -- hermes desktop`
3. **Approval workflows** — built-in JIT access with TTLs and approval gates for prod env
4. **MIT licensed** — core is fully open source
5. **Mature** — 28k+ stars, active development, good documentation

## Why Gatehouse is worth watching

Gatehouse is the closest architectural match to the "treat machine as employee device" pattern. Its `requires_approval=true` per-secret flag is exactly "YOLO some, gate others" — you leave dev secrets on autopilot and gate production credentials individually. It also has a proxy mode where agents never see credentials at all (they say "call this API" and Gatehouse forwards). Single Docker container, AGPL-3.0. But as of July 2026 it has 0 stars and is a fork — too early to depend on.

## When to revisit

- **Gatehouse**: check back in 3-6 months. If it gains community traction, the per-secret approval flag + single-container deployment is superior for agent-specific use cases.
- **Infisical Agent Vault**: Infisical's own proxy layer (separate repo) is evolving. If it matures, it provides the same "agents never see secrets" pattern as Gatehouse but with Infisical's maturity behind it.
