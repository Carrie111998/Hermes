# One-Shot MCP Discovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure every Hermes one-shot session takes its tool snapshot only after the existing bounded MCP discovery gate.

**Architecture:** `_run_agent` will idempotently start background MCP discovery and wait using `hermes_cli.mcp_startup` immediately before constructing `AIAgent`. The existing shared startup helper remains the single owner of timeout and thread behavior.

**Tech Stack:** Python 3.12, pytest, Hermes MCP startup helpers, Home Lab Nomad live canaries.

---

### Task 1: Reproduce the startup-order defect in a unit test

**Files:**
- Modify: `tests/hermes_cli/test_tui_resume_flow.py`

**Step 1: Write the failing test**

Add a test that stubs the existing one-shot dependencies, records calls to
`start_background_mcp_discovery`, `wait_for_mcp_discovery`, and `AIAgent`, and
asserts the order is:

```python
["start", "wait", "agent"]
```

**Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/hermes_cli/test_tui_resume_flow.py::test_oneshot_waits_for_mcp_discovery_before_agent_snapshot -q
```

Expected: FAIL because `_run_agent` constructs `AIAgent` without calling either
MCP startup helper.

### Task 2: Add the bounded MCP readiness gate

**Files:**
- Modify: `hermes_cli/oneshot.py`
- Test: `tests/hermes_cli/test_tui_resume_flow.py`

**Step 1: Write the minimal implementation**

Inside `_run_agent`, before `AIAgent(...)`:

```python
from hermes_cli.mcp_startup import (
    start_background_mcp_discovery,
    wait_for_mcp_discovery,
)

start_background_mcp_discovery(
    logger=logging.getLogger(__name__),
    thread_name="oneshot-mcp-discovery",
)
wait_for_mcp_discovery()
```

**Step 2: Run the focused test**

Run:

```bash
uv run pytest tests/hermes_cli/test_tui_resume_flow.py::test_oneshot_waits_for_mcp_discovery_before_agent_snapshot -q
```

Expected: PASS.

**Step 3: Run the complete one-shot test module**

Run:

```bash
uv run pytest tests/hermes_cli/test_tui_resume_flow.py -q
```

Expected: all tests pass.

**Step 4: Commit**

```bash
git add hermes_cli/oneshot.py tests/hermes_cli/test_tui_resume_flow.py docs/plans/2026-07-27-oneshot-mcp-discovery*.md
git commit -m "fix(cli): wait for MCP discovery in oneshot mode"
```

### Task 3: Repair and verify the deployed profiles

**Files:**
- Profile runtime configuration under `/Users/luma/.hermes/`
- Home Lab profile configuration only if a source-controlled provisioning gap is confirmed

**Step 1: Preserve unrelated profile and repository state**

Compare provider authentication and runtime environment presence without
printing secret values. Apply only the smallest Co-Intelligence repair.

**Step 2: Deploy the tested Hermes change**

Apply the committed patch to the active Hermes checkout without overwriting its
unrelated modified files, then restart only the affected Nomad gateway tasks if
the long-lived runtime needs a reload.

**Step 3: Run profile canaries**

For each profile, invoke `hermes -z` without `-t` or `--toolsets`, export the
new session transcript, and assert:

```text
tool_call_count >= 1
assistant tool call name == mcp_browser_control_execute
tool result is successful
```

Profiles: default/personal, cointelligence, nl-internal, nl-acfoods, nl-arx.

**Step 4: Run configuration checks**

Run:

```bash
agents mcp test
agents mcp test --runtime
agents sync
agents sync --check
```

Expected: browser-control configuration and runtime checks pass; sync check
reports no new drift introduced by this fix.
