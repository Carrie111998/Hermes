# Frozen audited requirements subset: Cross-Harness Session Bridge

This is the frozen audited requirement subset, not the complete plan.

- Canonical external origin: `C:\Users\diego\.config\superpowers\worktrees\hermes\session-bridge\docs\superpowers\plans\2026-07-13-cross-harness-session-bridge.md`
- External repository revision: `91d5ebe8a081620c0eb854998e071eb1bb65c2cb`
- Full-source SHA-256: `8EBA29199E7D56CD762E499DF4BD6404397A844226A983539CC71F2F58778DDA`
- Extraction date: 2026-07-30
- Normalization: the excerpt blocks below are concatenated in displayed order,
  joined with LF, and terminated by one LF; UTF-8 bytes are SHA-256 hashed.
- Normalized excerpt SHA-256: `5bcf522f614d23a98466f72936fa69179f0a721f41c95f9c07b3a46470a61c31`

## CH-001, CH-006, CH-007, CH-008 — source lines 5-7

> **Goal:** Make every Claude Code, Codex, and Hermes session searchable in one local catalog, mirror eligible sessions into both native sidebars, and continue them safely through an authenticated snapshot-and-handoff workflow.
>
> **Architecture:** Extend the main Hermes `SessionDB` with additive bridge metadata, link, context-pack, and durable-job tables. Read Claude JSONL and Codex app-server data through provider adapters, project normalized messages into existing Hermes FTS5 tables, create target placeholders only through supported Claude CLI and Codex app-server calls, and expose the catalog/coordinator through one loopback FastMCP service on `127.0.0.1:7484`. Native provider transcripts remain read-only and divergence is recorded rather than merged.

## CH-001 through CH-005 — source lines 22-33

> Both repositories use branch `codex/session-bridge`. The Hermes root repository owns this plan and the guarded laptop installer. The `agent-src` repository owns the service, schema, adapters, MCP tools, web API, desktop UI, and tests.
>
> The implementation must preserve these invariants in every task:
>
> - Never append to or fabricate `~/.claude/projects/**/*.jsonl`.
> - Never append to or fabricate `~/.codex/sessions/**/rollout-*.jsonl`.
> - Claude target creation goes through `claude`; Codex target creation goes through app-server.
> - Automatic native mirror creation is disabled by default until both one-way canaries pass.
> - Catalog import and mirror creation have separate switches; catalog import can run while mirroring is off.
> - Every live characterization record has a unique marker and can affect only its own disposable native ID.
> - Every change to `~/.claude.json`, `~/.codex/config.toml`, `laptop-start.ps1`, or `laptop-monitor.ps1` is backed up and applied by an idempotent tracked installer.
> - MemPalace and GBrain remain independent services. Their failure may reduce enrichment but cannot block catalog search or continuation.

## CH-006 through CH-008 — source lines 1352-1357

> - [x] Every discoverable historical Claude, Codex, and Hermes session is in the unified catalog or has a documented exclusion.
> - [x] Only the last 30 days were natively backfilled; all new ordinary sessions mirror continuously.
> - [x] Native placeholders are created only through Claude CLI or Codex app-server.
> - [x] First substantive cross-harness continuation hydrates an immutable, explicit snapshot.
> - [x] Repeated scans, retries, restarts, and ambiguous timeouts create no duplicate placeholders.
> - [x] Divergence is visible and never auto-merged.
