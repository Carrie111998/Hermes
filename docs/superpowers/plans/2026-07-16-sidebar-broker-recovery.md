# Session Sidebar Broker Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete and stabilize native Claude/Hermes session delivery to the Codex sidebar without retry exhaustion or repeated heavyweight broker runtimes.

**Architecture:** Preserve Session Bridge as the source of truth. Change the Codex skill to preflight sanitized bridge status and native project lookup before leasing, then run it from a dedicated project with project-scoped minimal MCP/plugin configuration. Recover and drain the existing queue through signed-marker reconciliation, verify resource stability, and only then enable continuous registration.

**Tech Stack:** Python 3.11, pytest through `scripts/run_tests.sh`, FastMCP, TOML Codex project configuration, Codex native task and automation tools, PowerShell operational probes.

---

### Task 1: Make native health checks precede leasing

**Files:**
- Modify: `tests/session_bridge/test_sidebar_skill.py`
- Modify: `session_bridge/assets/session-sidebar-sync/SKILL.md`

- [ ] **Step 1: Write the failing ordering test**

Add this behavior test:

```python
def test_sidebar_skill_preflights_bridge_and_native_projects_before_leasing() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    assert skill.index("session_status") < skill.index("list_projects({})")
    assert skill.index("list_projects({})") < skill.index(
        "session_sidebar_pending(limit=1)"
    )
    assert "do not call `session_sidebar_pending`" in skill
    assert "no job attempt is consumed" in skill
```

Update the allowed session-tool assertion to include `session_status`.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py -k "preflights_bridge or names_only" -q
```

Expected: FAIL because the current skill leases before project lookup and does not
name `session_status`.

- [ ] **Step 3: Implement the minimal skill change**

Keep steps 3-9 and all marker, creation, rename, and settlement rules unchanged.
Rewrite only the first two procedure steps so they require:

```text
session_status once -> healthy/actionable check -> list_projects once ->
session_sidebar_pending(limit=1) once
```

If status has no pending/retry work, exit silently. If native project lookup fails,
exit without leasing and without calling `session_sidebar_fail`.

- [ ] **Step 4: Run focused and complete skill tests**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/session_bridge/test_sidebar_skill.py session_bridge/assets/session-sidebar-sync/SKILL.md
git commit -m "fix(session-bridge): preflight native sidebar before leasing"
```

### Task 2: Install and verify the corrected personal skill

**Files:**
- Runtime install: `C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md`

- [ ] **Step 1: Run the supported installer**

```powershell
hermes-session-bridge install-sidebar-skill
```

Expected: JSON with `status=installed` and the personal skill path.

- [ ] **Step 2: Verify the installed asset exactly matches source**

```powershell
Compare-Object (Get-Content session_bridge\assets\session-sidebar-sync\SKILL.md) (Get-Content C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md)
```

Expected: no output.

### Task 3: Create the dedicated minimal broker project and task

**Files:**
- Create: `C:\Users\diego\Developer\session-sidebar-broker\.codex\config.toml`
- Create: `C:\Users\diego\Developer\session-sidebar-broker\AGENTS.md`
- Create: `C:\Users\diego\Developer\session-sidebar-broker\README.md`

- [ ] **Step 1: Create the project configuration**

Write `.codex/config.toml` with project-local overrides:

```toml
[mcp_servers.codegraph]
enabled = false
[mcp_servers.context7]
enabled = false
[mcp_servers.github]
enabled = false
[mcp_servers.node_repl]
enabled = false
[mcp_servers.openaiDeveloperDocs]
enabled = false
[mcp_servers.gbrain]
enabled = true
[mcp_servers.mempalace]
enabled = true
[mcp_servers.session_bridge]
enabled = true

[plugins."browser@openai-bundled"]
enabled = false
[plugins."chrome@openai-bundled"]
enabled = false
[plugins."computer-use@openai-bundled"]
enabled = false
[plugins."documents@openai-primary-runtime"]
enabled = false
[plugins."gmail@openai-curated"]
enabled = false
[plugins."google-drive@openai-curated"]
enabled = false
[plugins."pdf@openai-primary-runtime"]
enabled = false
[plugins."presentations@openai-primary-runtime"]
enabled = false
[plugins."sites@openai-bundled"]
enabled = false
[plugins."spreadsheets@openai-primary-runtime"]
enabled = false
[plugins."template-creator@openai-primary-runtime"]
enabled = false
[plugins."visualize@openai-bundled"]
enabled = false
```

Write `AGENTS.md` to require one `$session-sidebar-sync` invocation, silence on empty
work, and no project edits or app-server fallback. Write `README.md` with start,
health, and rollback instructions.

- [ ] **Step 2: Validate TOML and initialize the tiny repository**

```powershell
python -c "import pathlib,tomllib; tomllib.loads(pathlib.Path('.codex/config.toml').read_text())"
git init
git add .codex/config.toml AGENTS.md README.md
git commit -m "chore: configure minimal session sidebar broker"
```

Expected: TOML parse exits 0 and the repository has a clean first commit.

- [ ] **Step 3: Add the folder as a Codex project and create one broker task**

Use the Codex project picker to add the exact folder. Create one local task with this
prompt:

```text
This is the dedicated Session Sidebar Sync broker. Do not perform project work. Wait for the one-minute heartbeat and invoke $session-sidebar-sync exactly once per wake.
```

Rename it `Session Sidebar Broker` and record its native thread ID.

- [ ] **Step 4: Verify effective runtime isolation**

Run one empty broker turn, then inspect the Codex process tree. Expected: no new
`codegraph`, `context7`, `github`, or `node_repl` process owned by the broker turn;
GBrain, MemPalace, and Session Bridge tools remain available.

### Task 4: Recover and drain the existing queue safely

**Files:**
- Runtime database: `C:\Users\diego\.hermes\state.db`

- [ ] **Step 1: Confirm the exact failed-row invariants**

Use `SessionBridgeStore.get_sidebar_job_for_source()` for
`claude:92cc4635-ef5b-446f-98b1-cd28b75a6b18`. Require state
`sidebar_failed`, error `project_lookup_failed`, and null task/completion/visible
fields.

- [ ] **Step 2: Use the existing operator recovery path once**

Call:

```python
store.retry_failed_sidebar_job(
    source_session_id="claude:92cc4635-ef5b-446f-98b1-cd28b75a6b18",
    expected_error_code="project_lookup_failed",
    now=time.time(),
)
```

Expected: the exact row returns to `sidebar_pending` with attempts 0 and no native
task identity.

- [ ] **Step 3: Drain one job at a time**

For each job, follow the installed skill verbatim: preflight, lease one, reconcile
the signed marker, create only when absence is proven, read until idle, rename, and
commit. After each commit, verify source, bridge, idempotency, and Codex thread IDs
remain unique.

- [ ] **Step 4: Complete the remaining bounded backfill**

Run a 30-day dry-run with limit 10. Review exclusions against deleted Claude-managed
worktrees and the documented Hermes missing-cwd set. Apply at most one reviewed batch
at a time until the dry-run reports zero candidates.

### Task 5: Cut over the automation and pass resource/continuity gates

**Files:**
- Runtime automations under `C:\Users\diego\.codex\automations`
- Runtime config: `C:\Users\diego\.hermes\config.yaml`

- [ ] **Step 1: Delete the historical rollout heartbeat**

Use the supported Codex automation API to delete
`session-sidebar-backfill-rollout` after the backfill reaches zero candidates.

- [ ] **Step 2: Retarget and enable the production heartbeat**

Update `session-sidebar-sync` to target the dedicated broker task, keep the one-minute
cadence, and use this prompt:

```text
Invoke $session-sidebar-sync exactly once. The skill must preflight Session Bridge status and native projects before leasing. End silently when no job is ready. Never perform project work, copy transcripts, create without a valid lease, or use external app-server creation.
```

- [ ] **Step 3: Run the empty-cycle resource gate**

Observe at least three empty wakes. Compare Codex-owned process count and working set
before and after. Expected: at most one stale broker bundle and no more than 150 MiB
growth. If this fails, pause the heartbeat and implement a project-local,
session-ID-scoped cleanup hook; do not use a global process reaper.

- [ ] **Step 4: Enable continuous registration and verify one real source**

Run the supported continuous-enable command. Create or use one new meaningful native
Claude or Hermes session, wait up to one minute, and verify exactly one native task
appears. Continue it and prove the exact source cwd/worktree is returned before any
project command.

- [ ] **Step 5: Run final verification**

```bash
bash scripts/run_tests.sh tests/session_bridge/ tests/hermes_state/test_session_bridge_schema.py -q
git status --short
```

Also verify Session Bridge status is healthy, sidebar pending/leased/retry/failed are
zero, every visible binding is unique, both GBrain and MemPalace answer health/search,
and only the production heartbeat remains.

- [ ] **Step 6: Record durable memory**

Search before writing, then add one MemPalace drawer in wing `hermes`, room
`codex-native-sidebar-rollout`, and update GBrain page
`systems/cross-harness-session-bridge` with final task ID, process-resource evidence,
rollback, and the verified continuous-registration result.

