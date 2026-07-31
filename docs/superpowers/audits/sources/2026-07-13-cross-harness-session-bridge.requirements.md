# Frozen audited requirements subset: Cross-Harness Session Bridge

This is the frozen audited requirement subset, not the complete plan.

- Canonical external origin: `C:\Users\diego\.config\superpowers\worktrees\hermes\session-bridge\docs\superpowers\plans\2026-07-13-cross-harness-session-bridge.md`
- External repository revision: `91d5ebe8a081620c0eb854998e071eb1bb65c2cb`
- Full-source SHA-256: `8EBA29199E7D56CD762E499DF4BD6404397A844226A983539CC71F2F58778DDA`
- Extraction date: 2026-07-30
- Normalization: the excerpt blocks below are concatenated in displayed order,
  joined with LF, and terminated by one LF; UTF-8 bytes are SHA-256 hashed.
- Normalized excerpt SHA-256: `654980aa770a959f8f617490032b12ba881944bac1eb4eae5f23716dc5af8755`

## CH-001, CH-006, CH-007, CH-008 — source lines 5-7

> **Goal:** Make every Claude Code, Codex, and Hermes session searchable in one local catalog, mirror eligible sessions into both native sidebars, and continue them safely through an authenticated snapshot-and-handoff workflow.
>
> **Architecture:** Extend the main Hermes `SessionDB` with additive bridge metadata, link, context-pack, and durable-job tables. Read Claude JSONL and Codex app-server data through provider adapters, project normalized messages into existing Hermes FTS5 tables, create target placeholders only through supported Claude CLI and Codex app-server calls, and expose the catalog/coordinator through one loopback FastMCP service on `127.0.0.1:7484`. Native provider transcripts remain read-only and divergence is recorded rather than merged.

## CH-001 through CH-005 — source lines 22-33

> Both repositories use branch `codex/session-bridge`. The Hermes root repository owns this plan and the guarded laptop installer. The `agent-src` repository owns the service, schema, adapters, MCP tools, web API, desktop UI, and tests. Keep commits repository-local and use the commit messages specified below.
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

## CH-003 and CH-007 — source lines 89-100, 222, 748, and 837

> - [ ] Write the failing model tests first. Cover canonical IDs, enum validation, deterministic message identity, signed marker round-trip, wrong-key rejection, and payload tamper rejection.
>
> ```python
> def test_bridge_marker_round_trip_and_tamper_rejection() -> None:
>     payload = BridgeMarkerPayload(
>         bridge_id="bridge-123",
>         source_session_id="claude:11111111-1111-4111-8111-111111111111",
>         target_provider=Provider.CODEX,
>         policy_generation=1,
>     )
>     marker = encode_bridge_marker(payload, b"local-test-key")
>

> Implement `canonical_session_id(provider, native_id)`, `stable_message_key(message)`, `encode_bridge_marker(payload, secret)`, and `decode_bridge_marker(marker, secret)`. Use prefix `HERMES_SESSION_BRIDGE_V1`, compact sorted JSON, URL-safe base64 without padding, HMAC-SHA256, and `hmac.compare_digest`. Reject missing/empty secrets and any provider other than Claude or Codex in marker payloads.

> The registration prompt is a single line and contains the signed marker plus canonical source ID. It explicitly labels the registration message as non-substantive metadata, instructs Claude not to call tools during registration, requires the exact reply `REGISTERED`, and instructs Claude to call `session_continue` with the bridge ID before answering the first subsequent substantive user message. These safeguards are required because the Windows `.CMD` shim truncated multiline prompts and an unconstrained registration prompt entered a repeated tool-call loop during live characterization. Use the configured Claude default model; do not pass `--model`. After process success, poll `ClaudeSourceAdapter.find_native_session(native_id)` within a bounded timeout. A timeout is ambiguous and must reconcile by exact native UUID and the complete signed marker before any retry.

> The first automatic scan for each provider is a restart-safe `INITIAL_BACKFILL` over `backfill_days`; persist provider completion only after its pending queue drains, then use the startup watermark for `CONTINUOUS` eligibility. Promote genuinely new Codex inventory identities ahead of durable backlog while retaining seen-state so unchanged inventory cannot starve older work. Breaker progress is bounded by explicit batches: a successful batch at the attempt cap resets atomically for the next batch, while a threshold-breaching batch remains halted. Rate reservations, due-job claims, and breaker progress changes must be globally atomic in the store so two coordinator instances cannot exceed creation limits or lose failures.

## CH-004 — source lines 648, 675-682, and 1230

> - [ ] Write failing tests for the 30-day last-activity boundary, newest-first backfill, continuous discovery after a durable watermark, meaningful-first-message debounce, provider inversion, bridge-origin suppression, exact-ID mapping suppression, job idempotency, deterministic exponential backoff, maximum attempts, bounded concurrency, rate limiting, and batch error-threshold stop.

> mirror_idempotency_key(source_session_id: str, target: Provider, generation: int) -> str
> retry_delay_seconds(idempotency_key: str, attempts: int) -> float
> classify_mirror_eligibility(projection: SessionProjection, context: EligibilityContext) -> Eligibility
> ```
>
> Persist `continuous_watermark` in `session_bridge_state`; never reinterpret pre-watermark catalog rows as newly created after service restart. A bridge-origin session may be indexed and continued, but automatic policy must reject mirroring it back to the provider that owns its bridge group.
>
> `EligibilityContext` carries `now`, `discovery_mode` (`initial_backfill` or `continuous`), `continuous_watermark`, and existing target mappings. `Eligibility` carries a boolean `eligible`, target provider, and one machine-readable reason from `eligible`, `too_old`, `before_watermark`, `empty`, `unstable_identity`, `bridge_origin`, or `already_mapped`.

> - [ ] Enable `mirrors.automatic_creation = true` only after both one-way canaries and at least one backfill batch pass. Observe both harnesses open for at least 30 minutes and verify the service targets: catalog updates within 5 seconds, native placeholders within 60 seconds, no reverse loops, stable memory, and healthy laptop-monitor probes.

## CH-006 through CH-008 — source lines 1352-1357

> - [x] Every discoverable historical Claude, Codex, and Hermes session is in the unified catalog or has a documented exclusion.
> - [x] Only the last 30 days were natively backfilled; all new ordinary sessions mirror continuously.
> - [x] Native placeholders are created only through Claude CLI or Codex app-server.
> - [x] First substantive cross-harness continuation hydrates an immutable, explicit snapshot.
> - [x] Repeated scans, retries, restarts, and ambiguous timeouts create no duplicate placeholders.
> - [x] Divergence is visible and never auto-merged.

## Snapshot-only hash verification

Run from the repository root. The command reads only this snapshot, removes one
block-quote prefix from every displayed source line, joins the resulting lines
with LF, appends one LF, and hashes the UTF-8 bytes.

```powershell
$snapshot = 'docs/superpowers/audits/sources/2026-07-13-cross-harness-session-bridge.requirements.md'
$displayed = [regex]::Matches((Get-Content -Raw $snapshot), '(?m)^> ?.*$') |
  ForEach-Object { $_.Value -replace '^> ?', '' }
$normalized = (($displayed -join "`n") + "`n")
$hasher = [System.Security.Cryptography.SHA256]::Create()
try { $sha256 = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)) } finally { $hasher.Dispose() }
([BitConverter]::ToString($sha256) -replace '-', '').ToLowerInvariant()
```
