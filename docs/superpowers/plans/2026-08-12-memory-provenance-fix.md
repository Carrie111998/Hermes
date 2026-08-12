# Memory Duo Production Provenance Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry trusted direct-user memory intent from Hermes turn setup through the generic built-in memory bridge so Memory Duo can safely promote explicit add/update/forget operations.

**Architecture:** Add a deterministic host-only classifier in `agent/`, stamp its result on the active `AIAgent` turn, and extend the existing metadata callback without changing the model-facing memory schema. Obsidian Duo consumes only the new host-confirmed metadata and delegates exact-match replace/remove handling to broker/store contracts, preserving append-first history and fail-closed staging.

**Tech Stack:** Python 3, pytest, SQLite, YAML frontmatter, existing Hermes `MemoryManager`, `AIAgent`, `ObsidianDuoMemoryProvider`, `EmbeddedMemoryBroker`, and `ObsidianVault`.

## Global Constraints

- Do not begin Autopilot Orchestrator work.
- Do not merge or tag Memory Duo v1.
- Do not touch `C:\Users\curti\.hermes\hermes-agent` or any other protected dirty checkout.
- Do not expose credentials or add a provider-specific API key/model.
- Do not accept provenance fields from model-facing memory-tool arguments.
- Preserve `write_origin: assistant_tool` as the mechanical origin.
- Uncertain provenance fails closed to agent/unverified staging.
- Do not intentionally incur a paid API call.

---

### Task 1: Host provenance classifier and turn binding

**Files:**
- Create: `agent/memory_provenance.py`
- Modify: `agent/turn_context.py`
- Modify: `agent/agent_init.py`
- Test: `tests/agent/test_memory_provenance.py`
- Test: `tests/agent/test_turn_context.py` or a focused bridge fixture when needed

**Interfaces:**
- `classify_user_memory_intent(content: Any, *, synthetic: bool = False) -> str` returns one of `explicit_remember`, `explicit_update`, `explicit_forget`, `none`.
- `is_host_confirmed_user_memory(intent: str, *, write_origin: str, execution_context: str, synthetic: bool) -> bool` returns the fail-closed boolean.
- `build_turn_context()` sets `agent._memory_user_intent` and `agent._memory_user_turn_synthetic` before tool execution.

- [ ] **Step 1: Write failing classifier tests** for positive add/update/forget, quoted/explanatory text, external/prompt-injection wording, negated remember, skill scaffolding, and synthetic input.
- [ ] **Step 2: Run the focused tests** with `pytest -q tests/agent/test_memory_provenance.py`; confirm the expected failures because the helper does not exist.
- [ ] **Step 3: Implement the minimal deterministic classifier** using only clean current-turn text and the existing skill-instruction extractor.
- [ ] **Step 4: Stamp the current-turn result** from `build_turn_context()` and initialize/reset the agent fields in `agent_init.py`.
- [ ] **Step 5: Run classifier and turn-context tests** and confirm ordinary turns produce `none`.
- [ ] **Step 6: Commit** with `git add agent/memory_provenance.py agent/turn_context.py agent/agent_init.py tests/agent/test_memory_provenance.py tests/agent/test_turn_context.py && git commit -m "fix(memory): add host-owned user memory provenance"`.

### Task 2: Metadata seam and authority gate

**Files:**
- Modify: `agent/background_review.py:627-650`
- Modify: `plugins/memory/obsidian_duo/__init__.py:130-175`
- Modify: `agent/memory_provider.py` documentation for generic metadata
- Test: `tests/agent/test_memory_write_bridge.py`
- Test: `tests/plugins/memory/test_obsidian_duo_provider.py`

**Interfaces:**
- `build_memory_write_metadata()` adds `user_memory_intent` and `host_confirmed_user_memory` from host-owned agent state.
- `ObsidianDuoMemoryProvider.on_memory_write()` requires the trusted boolean and an allowed intent before assigning user authority.

- [ ] **Step 1: Add failing metadata/authority tests** for explicit direct user, autonomous assistant, background review, bare legacy `write_origin=user`, and model-tool-argument spoof fields.
- [ ] **Step 2: Run the focused bridge/provider tests** and confirm they fail on missing metadata/incorrect legacy authority.
- [ ] **Step 3: Add host-owned metadata** while leaving `write_origin` unchanged.
- [ ] **Step 4: Gate Obsidian Duo authority** on the trusted metadata only; leave model-facing `memory_duo propose` proposal-only.
- [ ] **Step 5: Run focused tests** and confirm explicit user promotes while all other origins stage.
- [ ] **Step 6: Commit** with `git add agent/background_review.py agent/memory_provider.py plugins/memory/obsidian_duo/__init__.py tests/agent/test_memory_write_bridge.py tests/plugins/memory/test_obsidian_duo_provider.py && git commit -m "fix(memory-duo): require trusted host confirmation"`.

### Task 3: Exact-match replace/remove broker behavior

**Files:**
- Modify: `plugins/memory/obsidian_duo/store.py`
- Modify: `plugins/memory/obsidian_duo/policy.py`
- Modify: `plugins/memory/obsidian_duo/broker.py`
- Modify: `plugins/memory/obsidian_duo/__init__.py`
- Test: `tests/plugins/memory/test_obsidian_duo_broker.py`
- Test: `tests/plugins/memory/test_obsidian_duo_provider.py`

**Interfaces:**
- Store exposes an exact active-content lookup scoped to the target memory type.
- Broker exposes a safe archive operation and keeps ambiguous correction requests staged/auditable.
- Provider maps built-in `add`, `replace`, and `remove` actions to promotion, supersession, or archive.

- [ ] **Step 1: Write failing add/replace/remove integration tests** that assert Markdown, SQLite status/authority/verification, version history, retrieval exclusion, and ambiguous fail-closed behavior.
- [ ] **Step 2: Run the focused broker/provider tests** and confirm replace/remove behavior is absent or incorrect.
- [ ] **Step 3: Implement exact lookup and `contradicts`/`user_correction` supersession** without fuzzy deletion.
- [ ] **Step 4: Implement archive-with-history for remove** without writing an empty fact.
- [ ] **Step 5: Run the focused suite** and confirm all mutation tests pass.
- [ ] **Step 6: Commit** with `git add plugins/memory/obsidian_duo/store.py plugins/memory/obsidian_duo/policy.py plugins/memory/obsidian_duo/broker.py plugins/memory/obsidian_duo/__init__.py tests/plugins/memory/test_obsidian_duo_broker.py tests/plugins/memory/test_obsidian_duo_provider.py && git commit -m "fix(memory-duo): preserve user replace and forget semantics"`.

### Task 4: Real bridge integration and regression suite

**Files:**
- Modify: `tests/agent/test_memory_write_bridge.py`
- Modify: `tests/plugins/memory/test_obsidian_duo_e2e.py` only if the existing real-path fixture needs coverage

- [ ] **Step 1: Add a failing integration-style test** that invokes the real manager notification with successful built-in result, host metadata, Obsidian Duo provider, broker, SQLite, and managed Markdown.
- [ ] **Step 2: Run the integration test** and verify it fails before the complete implementation.
- [ ] **Step 3: Make the minimal fixture adjustments** so the test uses a temporary HERMES_HOME/vault and no credentials/network.
- [ ] **Step 4: Run the integration test and all affected suites:** `tests/agent/test_memory_write_bridge.py`, `tests/agent/test_memory_provenance.py`, `tests/plugins/memory/test_obsidian_duo_*.py`, memory provider/tool tests, background-review tests, turn-context tests, and routing tests if changed.
- [ ] **Step 5: Run compilation and static checks:** `python -m compileall agent plugins/memory/obsidian_duo tests/agent tests/plugins/memory` and `git diff --check upstream/main...HEAD`.
- [ ] **Step 6: Commit** with `git add tests/agent/test_memory_write_bridge.py tests/plugins/memory/test_obsidian_duo_e2e.py && git commit -m "test(memory-duo): cover the production bridge end to end"`.

### Task 5: Publication and PR update

**Files:**
- No source files beyond the committed implementation/test changes.

- [ ] **Step 1: Verify source state** with `git status --short`, exact HEAD, and diff check; confirm protected checkout remains unchanged.
- [ ] **Step 2: Push only `feature/memory-duo-v1-clean`** to the existing fork remote.
- [ ] **Step 3: Inspect PR #83310** through the GitHub connector and update its description with the production blocker, host provenance, spoof protections, add/replace/remove verification, new HEAD, and test counts.
- [ ] **Step 4: Leave the PR draft/ready state unchanged unless the existing state requires reporting; do not merge.**

### Task 6: Controlled redeployment and blocked-gate re-smoke

**Files:**
- Existing live deployment files only after tests pass; preserve rollback bundle.

- [ ] **Step 1: Record the new exact HEAD and verify the deployment checkout is clean at that SHA.**
- [ ] **Step 2: Create/verify a new pre-change rollback snapshot before changing the running launcher/config.**
- [ ] **Step 3: Deploy that exact SHA through the existing default HERMES_HOME path and restart only the normal backend/proxy.**
- [ ] **Step 4: Run status/doctor before the write test.**
- [ ] **Step 5: Through normal Hermes Desktop, remember the new phrase `violet-pine-964`; verify host metadata, Memory Duo memory ID, SQLite record, and managed note.**
- [ ] **Step 6: Use a fresh session to prove deep recall from both SQLite and Markdown.**
- [ ] **Step 7: Edit the managed note manually in Obsidian to `golden-cedar-275`; fresh-retrieve and verify user authority, stale-value exclusion, and no agent overwrite.**
- [ ] **Step 8: Run optional explicit forget only if the add/replace gates pass and it is safe; verify archive/history semantics.**
- [ ] **Step 9: Restore or report the running deployment state and return the complete provenance-fix report.**
