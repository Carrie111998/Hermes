# Deterministic Session Sidebar Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the token-consuming conversational sidebar worker with a restart-safe local executor and close both Session Bridge acceptance specifications.

**Architecture:** A focused executor owns one durable job at a time and drives the existing store plus Codex app-server adapters through exact-ID reconciliation, create-once binding, indexing, rename, and atomic lineage commit. Claude-native delivery remains in its existing pseudoterminal registrar; final rollout gates validate both directions.

**Tech Stack:** Python 3.12, SQLite, Codex app-server JSON-RPC, FastMCP, pytest, PowerShell, React/Vitest.

---

### Task 1: Add the executor state machine

**Files:**
- Create: `session_bridge/sidebar_executor.py`
- Test: `tests/session_bridge/test_sidebar_executor.py`

- [ ] Write failing tests for recovered-ID commit, zero-candidate create/bind, create ambiguity, bind ambiguity, indexing timeout, rename failure, commit ambiguity, and strict single-job serialization.
- [ ] Run `uv run --no-sync pytest tests/session_bridge/test_sidebar_executor.py -q -p no:cacheprovider` and confirm failures are missing executor behavior.
- [ ] Implement `SidebarDeliveryExecutor.run_once()` with injected store, source adapter, native delivery port, clock, and sleep. Return a sanitized result containing only state and fixed error code.
- [ ] Run the focused tests and `uv run --no-sync ruff check session_bridge/sidebar_executor.py tests/session_bridge/test_sidebar_executor.py`.

### Task 2: Integrate the executor with service and CLI

**Files:**
- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/config.py`
- Test: `tests/session_bridge/test_cli.py`
- Test: `tests/session_bridge/test_coordinator.py`

- [ ] Write failing tests proving `sidebar-run-once` processes at most one job, continuous scans invoke one serialized cycle, unhealthy providers never lease, and disabled mode never mutates.
- [ ] Add the CLI command and production dependency construction using `CodexAppServerClient`; preserve the existing authenticated broker tools for diagnostics only.
- [ ] Add a process-wide async lock and ensure scan-triggered delivery cannot overlap CLI or service cycles.
- [ ] Run affected CLI/coordinator/config suites and Ruff.

### Task 3: Prove restart and ambiguity safety

**Files:**
- Modify: `tests/session_bridge/test_end_to_end.py`
- Modify: `tests/session_bridge/test_store.py`
- Modify: `tests/session_bridge/test_mcp_server.py`

- [ ] Add fault-injection tests for response loss after create, process death after bind, restart before rename, and commit response loss.
- [ ] Assert a known ID is always reconciled exactly and no path calls `thread/start` twice for the same durable job.
- [ ] Run the store/MCP/end-to-end suites and type checks.

### Task 4: Deploy and drain without conversational automation

**Files:**
- Modify: `scripts/install_session_bridge.ps1`
- Modify: `scripts/session_bridge_smoke.ps1`
- Test: `scripts/test_install_session_bridge.ps1`

- [ ] Extend the guarded installer and smoke test for the deterministic executor.
- [ ] Deploy through `C:\Users\diego\.hermes\session-bridge\launch-session-bridge.ps1`.
- [ ] Keep `session-sidebar-sync-worker` paused, run bounded `sidebar-run-once` cycles, and stop on failed/duplicate/conflict rows.
- [ ] Verify zero pending, leased, retry, and failed rows plus exact uniqueness.

### Task 5: Close Claude visibility and cross-harness acceptance

**Files:**
- Modify: `docs/superpowers/specs/2026-07-17-claude-native-session-visibility-design.md`
- Modify: `C:\Users\diego\.config\superpowers\worktrees\hermes\session-bridge\docs\superpowers\plans\2026-07-13-cross-harness-session-bridge.md`

- [ ] Run both 30-day dry-runs and review every exclusion.
- [ ] Verify Claude `/resume`, `/session-bridge`, Codex sidebar, unified search, immutable continuation both directions, and one continuous registration within one minute.
- [ ] Run full Python and desktop regressions, smoke all three MCPs, and complete a fresh 30-minute clean soak.
- [ ] Record exact acceptance evidence in both documents, write non-duplicate MemPalace/GBrain checkpoints, and delete the obsolete conversational automation.

### Task 6: Final review and ship

- [ ] Run `git diff --check`, Ruff, type checking, the complete Python suite, and the complete desktop suite from the ship worktree.
- [ ] Review the final diff against every acceptance bullet in both cited documents.
- [ ] Commit implementation and acceptance evidence; report shipped only with fresh zero-open-work and health outputs.
