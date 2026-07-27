# Direct Product Owner Work Inbox Implementation Plan

> **For Hermes:** Execute this plan with `superpowers:executing-plans`, one test-first task at a time. Do not mutate any v2 board while implementing or verifying it.

**Goal:** Replace auxiliary-model qualification of new Work Inbox product work with a claimed, auditable primary `productowner` profile run, while preserving the existing auxiliary path for requalification and authenticated override.

**Architecture:** Extend the existing Kanban database with an intake-run lease and append-only events, then have the watcher launch a detached Hermes CLI process using the configured `productowner` profile. The process receives only intake-scoped MCP tools, and Hermes—not model prose—validates, signs, and atomically materializes accepted work at Architecture. Existing Kanban contracts, card creation, and dependency recomputation remain the system of record.

**Tech stack:** Python, SQLite, Hermes CLI profiles, Hermes MCP tool server, FastAPI dashboard API, pytest through `scripts/run_tests.sh`.

**Scope constraints:** No new database, queue, provider abstraction, UI redesign, v2 lifecycle mutation, or migration of requalification. No provider fallback is permitted within the direct PO run. Product cards may be claimed at Architecture despite open parents, but entry to Development remains dependency-gated.

---

## Task 1: Persist idempotent intake runs and events

**Files:**
- Modify: `hermes_cli/kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_intake_db.py`

1. Add failing behavior tests for:
   - duplicate new-work submissions returning one intake ID;
   - the expanded states `running`, `needs_clarification`, and `attention_required`;
   - one active leased run per intake with exact run/lock ownership;
   - heartbeat renewal, terminal completion, append-only events, and explicit retry;
   - migration of an existing database without losing intake or decision rows.
2. Run:
   `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`
   Confirm the new tests fail for missing schema/API behavior.
3. Add the smallest idempotent migration:
   - nullable `idempotency_digest`, `current_run_id`, `claim_lock`, and `claim_expires`;
   - `qualification_intake_runs`;
   - `qualification_intake_events`;
   - a partial unique digest index;
   - a rebuilt intake status constraint and terminal-only decision trigger.
4. Add transaction-safe functions to create/deduplicate, claim, heartbeat, complete, append/list events, respond to clarification, and explicitly retry attention-required intake.
5. Re-run the focused test file and commit.

## Task 2: Launch the configured Product Owner as the primary intake worker

**Files:**
- Create: `hermes_cli/kanban_po_intake.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `gateway/kanban_watchers.py`
- Test: `tests/hermes_cli/test_kanban_po_intake.py`
- Test: `tests/gateway/test_kanban_qualification_watcher.py`

1. Add failing tests proving:
   - `task_create` intake is claimed and launched under the `productowner` profile;
   - provider, model, and effort come from that profile's effective config;
   - the process is detached and the watcher does not wait for inference;
   - only internal intake identity, board context, and the Kanban toolset are passed;
   - requalification still invokes `kanban_qualifier`;
   - dead/expired attempts return safely to pending until the bounded failure limit, then become `attention_required`.
2. Run both focused files and confirm failure.
3. Extract a reusable effective-profile runtime identity resolver from the existing task worker path.
4. Implement a narrow PO intake dispatcher that claims, records runtime identity, spawns `hermes -p productowner --cli --accept-hooks --toolsets kanban chat -q ...`, records the PID, and returns immediately.
5. Route only new-work intake through it; retain auxiliary qualifier routing for requalification.
6. Re-run focused tests and commit.

## Task 3: Enforce intake-only authority and no fallback

**Files:**
- Modify: `tools/kanban_tools.py`
- Modify: `tools/model_tools.py`
- Modify: `agent/transports/hermes_tools_mcp_server.py`
- Modify: `agent/local_agent_provider.py`
- Modify: `agent/system_prompt.py`
- Modify: `hermes_cli/cli_agent_setup_mixin.py`
- Modify: `agent/agent_init.py`
- Test: `tests/agent/transports/test_hermes_tools_mcp_server.py`
- Test: `tests/agent/test_local_agent_provider.py`
- Test: `tests/run_agent/test_provider_fallback.py`

1. Add failing tests proving:
   - intake runs expose only `work_inbox_show`, `work_inbox_decide`, `work_inbox_heartbeat`, and intake-scoped Agent Memory recall/write;
   - ordinary card mutation/completion tools are absent;
   - Claude MCP selects a dedicated `product-owner-intake` capability;
   - cached tool definitions cannot leak between task and intake scopes;
   - the internal direct-primary flag prevents both initialization-time auth fallback and in-session fallback activation.
2. Run the focused files and confirm failure.
3. Add the fail-closed MCP capability and toolset scoping. Generalize the local-Claude governed authority selector without weakening existing task authority.
4. Add intake-specific system instructions for every primary provider.
5. Pass a private run-scoped `HERMES_DISABLE_PROVIDER_FALLBACK=1`; honor it by suppressing configured fallback at both CLI credential resolution and agent construction/activation.
6. Re-run focused tests and commit.

## Task 4: Implement authoritative PO show, heartbeat, and decision tools

**Files:**
- Modify: `hermes_cli/kanban_po_intake.py`
- Modify: `tools/kanban_tools.py`
- Modify: `hermes_cli/kanban_qualifier.py`
- Test: `tests/hermes_cli/test_kanban_po_intake.py`
- Test: `tests/hermes_cli/test_kanban_qualifier.py`

1. Add failing tests proving exact run/lock validation, sanitized intake/context retrieval, claim heartbeats, every disposition, and two-strike invalid-output escalation.
2. Add provenance tests requiring:
   - `qualification_path: po`;
   - `po_evidence.surface: work_inbox_intake`;
   - issuer surface/profile/provider/model/effort/run ID matching the recorded intake run.
3. Run focused tests and confirm failure.
4. Implement:
   - `work_inbox_show` using trusted database context;
   - `work_inbox_heartbeat`;
   - `work_inbox_decide` for `accepted`, `needs_clarification`, and `rejected`;
   - validation-attempt tracking with `attention_required` after the second malformed semantic decision.
5. Generate all runtime provenance from the claimed run rather than accepting it from model output. Preserve legacy task-run PO evidence and auxiliary requalification validation.
6. Re-run focused tests and commit.

## Task 5: Materialize accepted work atomically at Architecture

**Files:**
- Modify: `hermes_cli/kanban_intake.py`
- Modify: `hermes_cli/kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_po_intake.py`
- Test: `tests/hermes_cli/test_kanban_db.py`

1. Add failing tests proving:
   - accepted decisions atomically store contract, decision, cards, terminal run, and cleared lease;
   - injected failure leaves no partial contract/card/terminal decision;
   - PO routing is forced to Architecture/architect;
   - dependent cards begin claimable at Architecture;
   - Architecture-to-Development handoff waits in `todo` while parents remain open;
   - dependency completion promotes the waiting Development card;
   - later phase dependency behavior remains unchanged.
2. Run focused tests and confirm failure.
3. Extend materialization to accept a matching active intake run and use its outer transaction.
4. Add the two narrow product-workflow dependency exceptions: bypass the parent claim gate only at Architecture, and reapply it when handing off to Development.
5. Re-run focused tests and commit.

## Task 6: Complete clarification and authenticated API flow

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Test: `tests/hermes_cli/test_work_inbox_auth_security.py`
- Test: `tests/plugins/dashboard_auth/test_work_inbox_provider.py`

1. Add failing tests proving:
   - API version 2 accepts a `clarification_response` only from the original authenticated source;
   - response text/attachments are appended as events without mutating the original intake;
   - the intake returns to `pending`;
   - status exposes the current sanitized clarification question;
   - an authenticated narrow operator retry can return `attention_required` to `pending`;
   - assigned-delivery and existing new-work behavior remain compatible.
2. Run focused tests and confirm failure.
3. Add the versioned request type, source check, status response, and retry endpoint with no dashboard redesign.
4. Re-run focused tests and commit.

## Task 7: Verify the complete slice

1. Run all directly affected tests:
   `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py tests/hermes_cli/test_kanban_po_intake.py tests/gateway/test_kanban_qualification_watcher.py tests/agent/transports/test_hermes_tools_mcp_server.py tests/agent/test_local_agent_provider.py tests/run_agent/test_provider_fallback.py tests/hermes_cli/test_kanban_qualifier.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_work_inbox_auth_security.py tests/plugins/dashboard_auth/test_work_inbox_provider.py -q`
2. Run broader Kanban, MCP, provider, gateway, and dashboard regression groups through `scripts/run_tests.sh`.
3. Run repository lint/type checks that cover changed files, if configured.
4. Inspect `git diff --check`, `git status --short`, and the exact base-to-head diff.

## Task 8: Independent Claude review, integration, and restart

1. Invoke the locally authenticated Claude Code CLI with Claude Opus 5 at high effort in read-only review mode against the exact base-to-head diff and approved design.
2. Save its review under the session `outputs/` directory. For each actionable finding, reproduce it with a failing test before changing production code.
3. Re-run the complete affected verification and obtain a clean second review if changes were material.
4. Commit the final verified branch, integrate it onto local `main` without rewriting unrelated work, and verify `main` contains the exact commits.
5. Restart only the affected Hermes gateway/dashboard processes, then verify health and a non-mutating Work Inbox canary.
6. Report commit IDs, verification evidence, review result, restart status, and stop.
