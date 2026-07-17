# Claude Native Session Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before any success claim.

**Goal:** Make meaningful native Codex and Hermes sessions appear as genuine, resumable entries in Claude Code's native `/resume` picker while preserving Session Bridge as the authoritative unified catalog and continuation system.

**Architecture:** Add an independent Claude-visibility queue to Session Bridge. A single local registrar reserves a deterministic Claude UUID, leases one job, launches real interactive Claude Code through ConPTY in the source cwd, verifies the resulting native transcript by exact UUID and signed marker, and commits visibility without ever editing Claude JSONL directly. A personal `/session-bridge` Claude Code skill exposes the unified catalog and continuation path through the existing MCP.

**Tech Stack:** Python 3.11+, SQLite, pywinpty/ConPTY, pytest, Ty, Ruff, Claude Code 2.1.110+, Session Bridge MCP, MemPalace, GBrain

---

## Safety invariants

- Never synthesize or modify Claude transcript JSONL.
- Never launch Claude without one valid lease and one persisted reserved UUID.
- Never create a replacement UUID after a launch timeout, commit ambiguity, or delayed indexing.
- Never use `claude --print` or the Agent SDK for native registration.
- Never hydrate context, invoke tools, or touch project files during registration.
- Never silently broaden or substitute the source cwd/worktree or Git identity.
- Never permit native Claude mirrors, bridge placeholders, or continuations to re-enter source eligibility.
- Never exceed 25 attempted registrations per local day or the independent emergency dollar threshold.
- Never queue another backfill batch while pending, leased, retry, or failed work exists.
- Keep creation internal to the single registrar; MCP may report status but must not expose a native-create operation.

## File responsibility map

- Modify `hermes_state.py`: additive SQLite schema and migrations.
- Modify `session_bridge/config.py`: disabled-by-default Claude visibility configuration.
- Create `session_bridge/claude_visibility.py`: eligibility, deterministic identity, names, markers, and retry codes.
- Modify `session_bridge/store.py`: queue, lease, usage reservation, reconciliation, commit, retry, and status transactions.
- Create `session_bridge/claude_registrar.py`: injected PTY protocol and the single interactive registrar.
- Modify `session_bridge/coordinator.py`: discovery, bounded enqueueing, and one-job delivery orchestration.
- Modify `session_bridge/cli.py`: dry-run, apply, status, continuous, run-once, and characterization commands.
- Modify `session_bridge/mcp_server.py`: read-only Claude visibility health/status tool.
- Create `session_bridge/asset_installer.py`: shared secure atomic asset installation.
- Modify `session_bridge/sidebar_skill.py`: use the shared installer without behavior changes.
- Create `session_bridge/claude_skill.py`: personal Claude Code skill installer.
- Create `session_bridge/assets/claude-session-bridge/SKILL.md`: `/session-bridge` catalog workflow.
- Modify `session_bridge/characterize.py`: disposable native Claude characterization and verified cleanup.
- Add focused tests under `tests/session_bridge/` for each component.

## Task 1: Add disabled configuration and additive state

**Files:**

- Modify: `session_bridge/config.py`
- Modify: `hermes_state.py`
- Test: `tests/session_bridge/test_config_safety.py`
- Test: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing configuration tests**

Add tests proving that omitted configuration is safe and disabled:

```python
def test_claude_visibility_defaults_disabled() -> None:
    config = BridgeConfig.from_mapping({})

    assert config.claude_visibility.enabled is False
    assert config.claude_visibility.continuous is False
    assert config.claude_visibility.backfill_days == 30
    assert config.claude_visibility.daily_registration_limit == 25
    assert config.claude_visibility.emergency_daily_cost_usd == Decimal("0.50")
```

Also test rejection of zero/negative limits, retry counts, lease durations, process timeouts, discovery timeouts, and estimated attempt costs.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_config_safety.py -q
```

Expected: failure because `BridgeConfig` has no `claude_visibility` field.

- [ ] **Step 3: Implement the configuration**

Add immutable `ClaudeVisibilityConfig` with these defaults:

```python
enabled = False
continuous = False
backfill_days = 30
continuous_batch_limit = 1
manual_batch_limit = 10
lease_seconds = 300
max_attempts = 5
daily_registration_limit = 25
reserved_cost_per_attempt_usd = Decimal("0.02")
emergency_daily_cost_usd = Decimal("0.50")
process_timeout_seconds = 120
discovery_timeout_seconds = 30
```

Parse it as an optional `claude_visibility` section in `BridgeConfig`. Validate all fields before the service starts.

- [ ] **Step 4: Write failing migration tests**

Test migration from an existing database and a fresh database. Assert the exact columns and uniqueness constraints for:

```text
session_claude_visibility_jobs
session_claude_registration_usage
```

The job table must persist job ID, source session ID, bridge ID, idempotency key, reserved Claude UUID, native name, source provider, source cwd, Git identity snapshot, signed marker, state, attempt count, next attempt, lease digest/expiry, error code/detail, completion digest, and timestamps. States are `claude_pending`, `claude_leased`, `claude_retry`, `claude_visible`, and `claude_failed`.

The usage table must persist local day, job ID, attempt ordinal, reserved estimated cost, and reservation timestamp. Enforce one usage reservation per job attempt.

- [ ] **Step 5: Run migration tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_store.py -k "claude_visibility_schema or claude_registration_usage" -q
```

Expected: failure because the tables do not exist.

- [ ] **Step 6: Implement additive schema migration**

Create the tables and indexes without changing `session_sidebar_jobs`. Independently enforce uniqueness for source session ID, bridge ID, idempotency key, and reserved Claude UUID. Ensure rerunning initialization is idempotent.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_config_safety.py tests/session_bridge/test_store.py -q
git add session_bridge/config.py hermes_state.py tests/session_bridge/test_config_safety.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): add Claude visibility state"
```

Expected: all selected tests pass and the commit succeeds.

## Task 2: Implement eligibility, identity, and transactional queue semantics

**Files:**

- Create: `session_bridge/claude_visibility.py`
- Modify: `session_bridge/store.py`
- Create: `tests/session_bridge/test_claude_visibility.py`
- Modify: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing policy and identity tests**

Cover these cases:

- native Codex and native Hermes sessions with at least one meaningful user request are eligible;
- Claude sources, bridge placeholders, bridge continuations, automation-only, subagent-only, acknowledgement-only, and control-only sessions are excluded with fixed reason codes;
- deterministic UUID, bridge ID, idempotency key, name, and marker are stable across retries and service restarts;
- names have the `[Codex] ` or `[Hermes] ` prefix followed by the bounded original request, and reuse existing normalization, redaction, whitespace compaction, and length limits;
- changing source provider or source session identity changes the derived UUID;
- the fixed registration prompt contains bounded signed metadata but no source transcript body.

- [ ] **Step 2: Run policy tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility.py -q
```

Expected: import failure because `session_bridge.claude_visibility` does not exist.

- [ ] **Step 3: Implement pure policy and identity types**

Create frozen dataclasses `ClaudeVisibilityCandidate`, `ClaudeVisibilityIdentity`, and `ClaudeVisibilityClaim`. Define a versioned UUID5 namespace constant. Centralize fixed exclusion and error codes. Reuse the existing meaningful-session classifier and title sanitizer instead of duplicating their logic.

- [ ] **Step 4: Write failing store transition tests**

Test the complete state machine:

```text
enqueue -> pending -> leased -> visible
                        |-> retry -> leased
                        |-> failed
```

Prove:

- enqueue is idempotent and rejects every independent identity collision;
- claim returns at most one due job and a signed lease digest;
- the deterministic UUID is persisted before claim and returned on every retry;
- exact-ID reconciliation is required before launch on retry;
- stale lease recovery preserves the UUID;
- commit and fail require the exact active lease;
- a visible job cannot be reopened;
- unknown error codes and fatal conflicts become failed status;
- no replacement UUID is generated after an ambiguous launch;
- status reports exact counts by state and retry code.

- [ ] **Step 5: Add daily count and cost-gate tests**

Use an injected clock and local timezone. Prove that the claim transaction atomically reserves one attempt and estimated cost, that the 26th attempt is not leased, that the emergency cost threshold independently stops leasing, and that read-only reconciliation consumes no slot.

- [ ] **Step 6: Implement store operations**

Add explicit methods:

```python
enqueue_claude_visibility_job(candidate, identity)
claim_claude_visibility_job(now, lease_seconds, daily_limit, cost_limit, reserved_cost)
retry_claude_visibility_job(job_id, lease_digest, error_code, next_attempt_at, detail)
commit_claude_visibility_job(job_id, lease_digest, transcript_digest, visible_at)
fail_claude_visibility_job(job_id, lease_digest, error_code, detail)
claude_visibility_status(now)
```

Perform usage reservation in the same `BEGIN IMMEDIATE` transaction as the lease transition. Return a typed cost-gate result rather than spinning.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility.py tests/session_bridge/test_store.py -q
git add session_bridge/claude_visibility.py session_bridge/store.py tests/session_bridge/test_claude_visibility.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): add Claude visibility queue"
```

## Task 3: Build the single interactive ConPTY registrar

**Files:**

- Create: `session_bridge/claude_registrar.py`
- Create: `tests/session_bridge/test_claude_registrar.py`
- Add fixture executable: `tests/session_bridge/fixtures/fake_interactive_claude.py`

- [ ] **Step 1: Write the fake interactive Claude fixture**

The fixture must record argv, cwd, stdin frames, and exit sequence; render deterministic prompts; emit `REGISTERED`; accept `/exit`; simulate delayed transcript indexing, authentication failure, timeout after native creation, malformed response, and nonzero exit. It must never call Anthropic.

- [ ] **Step 2: Write failing registrar contract tests**

Use an injected `InteractivePty` protocol. Assert:

- exact cwd is passed without normalization to a parent directory;
- argv contains `--session-id` followed by the claim's persisted `reserved_claude_uuid`, `--name` followed by its deterministic `native_name`, `--model haiku`, `--tools ""`, and `--permission-mode dontAsk`;
- argv never contains `--print` or `-p`;
- one fixed registration prompt is sent;
- the registrar accepts only the exact bounded `REGISTERED` response;
- `/exit` is sent and a clean exit is bounded by timeout;
- no tool call or source transcript content is present;
- timeout after launch returns `creation_ambiguous`, preserving the exact UUID;
- retry reconciles the exact UUID and signed marker before deciding whether launch is needed;
- a zero-result search after ambiguity never authorizes replacement creation.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_registrar.py -q
```

Expected: import failure because the registrar does not exist.

- [ ] **Step 4: Implement the PTY abstraction and Windows backend**

Define a narrow protocol for spawn, read-until, write, wait, terminate, and close. The production factory must use `winpty.PtyProcess.spawn`. If ConPTY/pywinpty is unavailable, return fixed retry code `pty_unavailable`; do not fall back to pipes.

- [ ] **Step 5: Implement exact native reconciliation**

Before every launch, query `ClaudeSourceAdapter` by the reserved UUID. If a transcript exists, parse it and verify UUID, signed marker, source/provider/bridge identities, exact cwd/project placement, name, and registration response. Return visible only on a complete match. Return a fatal conflict on any mismatch. Return absent only for a never-launched job; ambiguous or previously launched jobs remain retry-only when not indexed.

- [ ] **Step 6: Implement registration and cleanup behavior**

Launch one interactive process, send the fixed prompt, require `REGISTERED`, send `/exit`, await clean exit, then reread the transcript until the bounded discovery timeout. Always close the PTY. Convert only the documented transient conditions to bounded retry codes; preserve unknown results as fatal.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_registrar.py tests/session_bridge/test_claude_adapter.py -q
git add session_bridge/claude_registrar.py tests/session_bridge/test_claude_registrar.py tests/session_bridge/fixtures/fake_interactive_claude.py
git commit -m "feat(session-bridge): register native Claude sessions"
```

## Task 4: Integrate discovery, delivery, and operator CLI

**Files:**

- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/cli.py`
- Create: `tests/session_bridge/test_claude_visibility_coordinator.py`
- Modify: `tests/session_bridge/test_cli.py`

- [ ] **Step 1: Write failing coordinator tests**

Prove that discovery:

- selects only eligible native Codex/Hermes sources from the requested 30-day window;
- emits candidates and explicit exclusions in stable order;
- enqueues nothing in dry-run mode;
- refuses to enqueue when any Claude visibility job is pending, leased, retry, or failed;
- applies at most the requested limit and never more than 10 per manual batch;
- continuous mode considers only newly eligible sources and enqueues at most one per cycle;
- disabled configuration performs no discovery or delivery;
- one run processes at most one leased job through the registrar.

- [ ] **Step 2: Write failing CLI tests**

Add exact parsing and JSON-output tests for:

```text
claude-visibility-status --json
claude-visibility-backfill --days 30 --limit 10 --dry-run
claude-visibility-backfill --days 30 --limit 10 --apply
claude-visibility-continuous --enable
claude-visibility-continuous --disable
claude-visibility-run-once
```

Assert that `--apply` requires an explicit flag, limits above 10 fail, and commands never bypass open-job or cost gates.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility_coordinator.py tests/session_bridge/test_cli.py -q
```

- [ ] **Step 4: Implement coordinator methods**

Add pure candidate discovery, reviewed enqueue, continuous discovery, and `run_one_claude_visibility_job`. Inject clock, store, source adapters, and registrar. Keep the registrar as the sole delivery path and preserve stable result objects for CLI/MCP serialization.

- [ ] **Step 5: Implement CLI commands**

Return machine-readable counts, candidates, exclusions, open states, retry codes, daily usage, cost usage, continuous state, and last empty cycle. Use nonzero exit codes for unsafe apply, fatal health, unknown exclusions, or failed jobs.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility_coordinator.py tests/session_bridge/test_cli.py -q
git add session_bridge/coordinator.py session_bridge/cli.py tests/session_bridge/test_claude_visibility_coordinator.py tests/session_bridge/test_cli.py
git commit -m "feat(session-bridge): orchestrate Claude visibility"
```

## Task 5: Add read-only MCP health and the personal `/session-bridge` skill

**Files:**

- Modify: `session_bridge/mcp_server.py`
- Create: `session_bridge/asset_installer.py`
- Modify: `session_bridge/sidebar_skill.py`
- Create: `session_bridge/claude_skill.py`
- Create: `session_bridge/assets/claude-session-bridge/SKILL.md`
- Modify: `tests/session_bridge/test_mcp_server.py`
- Create: `tests/session_bridge/test_claude_skill.py`
- Modify: `tests/session_bridge/test_sidebar_skill.py`

- [ ] **Step 1: Write failing MCP status tests**

Add `session_claude_visibility_status` and prove it exposes counts, cost gates, continuous state, registrar heartbeat, and fixed degraded reasons. Assert that no MCP tool can claim, create, bind, or commit a Claude-native job.

- [ ] **Step 2: Write failing shared-installer tests**

Extract the existing path validation, symlink defense, temporary-directory staging, digest comparison, and atomic replacement behavior into `asset_installer.py`. First lock existing sidebar-skill behavior with tests, then add tests proving both installers reject path traversal, symlink destinations, and partial replacement.

- [ ] **Step 3: Write failing Claude skill tests**

Assert installation to `~/.claude/skills/session-bridge/SKILL.md`, exact frontmatter, asset digest, idempotent update, and safe preservation of unrelated user files.

- [ ] **Step 4: Create the Claude Code skill**

Use this frontmatter:

```yaml
---
name: session-bridge
description: Browse and continue the unified Claude, Codex, and Hermes session catalog.
user-invocable: true
disable-model-invocation: true
---
```

The workflow must use existing authenticated MCP tools to search/list sessions, show provider/title/cwd/activity/mirror state/preview, inspect a chosen relationship, and call `session_continue` only after explicit selection. It must explain that `/resume` plus `Ctrl+A` is the native picker and `/session-bridge` is the global catalog. Do not add a native-create tool.

- [ ] **Step 5: Implement installers and MCP status**

Refactor `sidebar_skill.py` to use the shared installer with no output or path change. Implement the Claude skill installer and a CLI install subcommand if the existing installer convention requires one.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_mcp_server.py tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_claude_skill.py -q
git add session_bridge/mcp_server.py session_bridge/asset_installer.py session_bridge/sidebar_skill.py session_bridge/claude_skill.py session_bridge/assets/claude-session-bridge/SKILL.md tests/session_bridge/test_mcp_server.py tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_claude_skill.py
git commit -m "feat(session-bridge): add Claude unified catalog skill"
```

## Task 6: Add real characterization and fault-injection coverage

**Files:**

- Modify: `session_bridge/characterize.py`
- Modify: `session_bridge/cli.py`
- Create: `tests/session_bridge/test_claude_visibility_characterization.py`
- Modify: `tests/session_bridge/test_fault_injection.py`
- Modify: `tests/session_bridge/test_live_characterization.py`

- [ ] **Step 1: Write failing characterization tests**

Add `characterize-claude-visibility --json`. Unit tests must prove it creates a disposable exact-cwd source, reserves one UUID, invokes the registrar once, verifies restart/exact-ID resume metadata, prints operator checks for `/resume` and `Ctrl+A`, and removes only the exact characterized session after verifying UUID, marker, and path.

- [ ] **Step 2: Add failure-matrix tests**

Cover process crash before transcript creation, timeout after transcript creation, delayed indexing, database busy at claim, database busy at commit, service restart while leased, stale lease, auth loss, missing executable, PTY failure, malformed marker, wrong cwd, wrong name, duplicate UUID, duplicate idempotency key, cost-cap rollover, and unknown retry code. Every ambiguous case must prove that launch count remains one and reserved UUID remains unchanged.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility_characterization.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_live_characterization.py -q
```

- [ ] **Step 4: Implement characterization**

Gate the live path behind the existing live-characterization environment switch. Detect installed Claude version and authentication without spending a registration slot. Produce explicit manual verification instructions because Claude exposes no supported `/resume` picker API. Cleanup must abort on any identity mismatch.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
uv run pytest tests/session_bridge/test_claude_visibility_characterization.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_live_characterization.py -q
git add session_bridge/characterize.py session_bridge/cli.py tests/session_bridge/test_claude_visibility_characterization.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_live_characterization.py
git commit -m "test(session-bridge): characterize Claude native visibility"
```

## Task 7: Run full verification and deploy disabled

**Files:**

- Modify only files required by failures discovered in this task.

- [ ] **Step 1: Run static checks**

```powershell
uv run ruff check session_bridge hermes_state.py tests/session_bridge
uv run ruff format --check session_bridge hermes_state.py tests/session_bridge
uv run ty check session_bridge hermes_state.py
```

Expected: zero errors. Fix root causes and rerun the complete commands.

- [ ] **Step 2: Run focused Session Bridge tests**

```powershell
uv run pytest tests/session_bridge -q
```

Expected: all Session Bridge tests pass.

- [ ] **Step 3: Run the full regression suite**

```powershell
uv run pytest -q
```

Expected: all tests pass. Record duration and counts for the rollout log.

- [ ] **Step 4: Install and validate the personal skill**

Run the implemented installer, then verify:

```powershell
Get-Content -LiteralPath "$HOME\.claude\skills\session-bridge\SKILL.md" -TotalCount 12
claude --version
```

Expected: installed frontmatter is exact and Claude Code reports version 2.1.110 or a characterized compatible version.

- [ ] **Step 5: Deploy through the canonical launcher with the feature disabled**

Use the existing canonical Session Bridge install/restart command documented by the repository launcher. Do not start a second service instance. Verify canonical smoke reports all providers healthy and `session_claude_visibility_status` reports disabled, zero open work, zero duplicates, and no registrar worker.

- [ ] **Step 6: Commit deployment fixes**

```powershell
git status --short
git diff --name-only
```

Review that list, stage each intended file by its literal path, then run
`git commit -m "fix(session-bridge): harden Claude visibility deployment"`.
Skip the commit only when `git status --short` is empty.

## Task 8: Perform guarded canary, backfill, continuous registration, and ship gate

**Files:**

- No source edits unless a gate fails; any fix requires a regression test and a return to Task 7.

- [ ] **Step 1: Run one disposable real characterization**

Run `characterize-claude-visibility --json`. Confirm the entry appears in Claude `/resume`; press `Ctrl+A` and confirm cross-project visibility; restart Claude and confirm the deterministic name and exact UUID persist; resume by exact UUID. Let verified cleanup delete only the characterization session.

- [ ] **Step 2: Review the 30-day dry-run**

```powershell
session-bridge claude-visibility-backfill --days 30 --limit 10 --dry-run
```

Review every candidate and exclusion. Accept only the meaningful-session exclusions defined by policy and already documented deleted Claude-managed worktrees or legacy missing-cwd records. Any unknown exclusion or identity mismatch is a hard stop.

- [ ] **Step 3: Enable delivery and register one Hermes canary**

Enable the feature but keep continuous discovery disabled. Apply only the selected Hermes canary, run the sole registrar until clean, and verify exact uniqueness of source session ID, bridge ID, idempotency key, and Claude UUID. Confirm name, cwd, marker, native `/resume` visibility, unified catalog relation, and exact continuation.

- [ ] **Step 4: Register one Codex canary**

Repeat the same checks for one Codex source. Confirm neither canary re-enters eligibility as a Claude or bridge source.

- [ ] **Step 5: Backfill in bounded batches**

At each gate:

1. require pending/leased/retry/failed counts all zero;
2. verify provider health and exact uniqueness;
3. run a fresh dry-run with limit 10;
4. review every exclusion;
5. apply at most one batch;
6. let the sole registrar drain it;
7. stop for the local-day count or emergency dollar threshold.

Never bypass the 25-attempt daily limit. Continue on later local days until the reviewed dry-run returns zero candidates.

- [ ] **Step 6: Enable continuous registration**

Enable continuous discovery. Verify one healthy empty registrar cycle. Create one new meaningful Hermes or Codex source request and prove that one—and only one—native Claude mirror appears within one minute with the correct cwd, name, UUID, marker, and catalog relation.

- [ ] **Step 7: Validate both discovery and continuation surfaces**

In Claude Code:

- use `/resume` in the source project;
- use `Ctrl+A` to find the mirror across all projects;
- use `/session-bridge` to search the unified Claude/Codex/Hermes catalog;
- continue one mirrored source and verify the immutable context pack and lineage;
- verify the reverse catalog path from Hermes and Codex without creating loops or duplicate native sessions.

- [ ] **Step 8: Run the final 30-minute clean soak**

For at least 30 uninterrupted minutes verify canonical smoke health, zero open or failed Claude jobs, exact uniqueness across all four identities, a healthy empty registrar heartbeat, zero 30-day candidates, no unexpected Anthropic spend, and healthy Session Bridge, MemPalace, and GBrain MCP endpoints.

- [ ] **Step 9: Capture durable checkpoints and ship**

Search before writing. Add a verbatim MemPalace record in the Session Bridge/Hermes wing with implementation commits, test evidence, rollout counts, canary UUIDs, health evidence, and operator paths. Update the matching GBrain page and timeline entry. If GBrain remains unavailable, report that explicitly and do not claim the checkpoint succeeded.

Delete the temporary rollout automation only after every gate passes. Report the shipped user experience:

```text
Claude native mirrors: /resume, then Ctrl+A for every project
Unified catalog: /session-bridge
Codex/Hermes catalog: existing Session Bridge surfaces
```

Run one final `git status --short` and retain no unintended changes.

---

## Completion evidence checklist

- [ ] Focused and full test suites pass from fresh commands.
- [ ] Static checks pass.
- [ ] Deployed service is the canonical single instance.
- [ ] One Hermes and one Codex canary are natively visible and resumable.
- [ ] Backfill has zero reviewed candidates remaining.
- [ ] Continuous registration creates exactly one mirror within one minute.
- [ ] All visible identity columns are exactly unique.
- [ ] `/resume`, `Ctrl+A`, and `/session-bridge` are validated.
- [ ] Bidirectional continuation and immutable context hydration are validated.
- [ ] Session Bridge, MemPalace, and GBrain health are green or any external blocker is explicitly reported.
- [ ] The 30-minute soak is clean.
- [ ] Final memory checkpoints exist and the rollout automation is deleted.
