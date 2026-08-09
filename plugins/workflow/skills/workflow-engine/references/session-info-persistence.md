# Session Info Persistence for Workflow Subscriptions

## Problem
The workflow engine needs to subscribe final-layer cards for notification. The subscription requires session context (platform, chat_id, thread_id, profile). But ContextVars from `gateway.session_context` are **lost across the tool-handler → engine.execute() boundary** — verified via debug logging.

## Root Cause
Gateway sets ContextVars (`HERMES_SESSION_PLATFORM`, etc.) before dispatching to the agent. The tool handler can read them. But `engine.execute()` runs in a context where ContextVars return empty. This is a ContextVar propagation issue across the call boundary.

## Solution: File-Based Session Bridge

### 1. Tool handler captures session info (ContextVars are live here)

```python
# In tools.py — called from the tool handler before engine.execute()
def _capture_session_for_engine() -> None:
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if platform and chat_id:
        data = {"platform": platform, "chat_id": chat_id, ...}
        with open("/tmp/wfe-session.json", "w") as f:
            json.dump(data, f)
```

### 2. Engine reads from temp file (ContextVars are NOT available here)

```python
# In engine.py — _get_session_info() step 2
def _get_session_info(self) -> dict:
    # 1. Try ContextVars (works in some contexts)
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        if platform and chat_id:
            return {...}
    except Exception:
        pass
    # 2. Check temp file written by tool handler
    try:
        with open("/tmp/wfe-session.json") as f:
            info = json.load(f)
        if info and info.get("platform"):
            return info
    except Exception:
        pass
    # 3. Fallback: os.environ
    ...
```

### 3. Tool handler passes to engine.execute()

```python
# In tools.py
_capture_session_for_engine()  # Writes to /tmp/wfe-session.json
result = engine.execute(
    session_info=_get_captured_session() or None,
    ...
)
```

### 4. `_save_state` preserves session_info across supervisor resume

When the supervisor subprocess resumes a looped workflow, it calls `_save_state()` without session_info. Fix: preserve from previously loaded state.

```python
def _save_state(self, ..., session_info=None):
    if session_info:
        state["session_info"] = session_info
    else:
        existing = self._load_state(workflow_name, run_id)
        if existing and existing.get("session_info"):
            state["session_info"] = existing["session_info"]
```

### 5. Hook reads from state file

```python
def _subscribe_final_layer(state, completed_layer_idx, layers):
    session_info = state.get("session_info", {})
    if not session_info.get("platform") or not session_info.get("chat_id"):
        return
    # Find card IDs for final layer, create subscriptions
```

## Why NOT module-level dict or direct import

- **Module-level dict:** `_SESSION_BRIDGE` in tools.py — import from engine.py fails silently (circular import or module loading order in gateway context)
- **Direct import:** `from plugins.workflow.tools import _get_captured_session` — circular import between engine.py ↔ tools.py
- **File-based bridge:** `/tmp/wfe-session.json` — reliable, no import needed, works across subprocess boundaries

## Why NOT just pass session_info as execute() parameter

The tool handler DOES pass it, but `_get_captured_session()` reads from the same file. The file-based approach is the canonical source of truth — the parameter is a convenience for testing.

## Verification

After running a workflow, check:
1. `/tmp/wfe-session.json` exists with platform and chat_id
2. State file (initial, from execute()) has `session_info` with platform and chat_id
3. State file (supervisor resume) ALSO has `session_info` (preserved from load)
4. `kanban_notify_subs` table has subscription rows for final-layer cards
5. When the final node completes, the notifier pushes to the calling session

## Supervisor Process Leak

Every `workflow_start` call with loop zones spawns a supervisor subprocess. Gateway restarts (USR1) do NOT kill old supervisors. After multiple test runs and restarts, orphaned supervisors accumulate. They poll the same state files and may interfere with the active run.

**Before starting a fresh test:** `pkill -f "workflow_engine start.*--resume"` to kill orphans.

**Root cause:** The supervisor is spawned with `start_new_session=True` and `stdout/stderr=DEVNULL`, making it invisible and detached from the gateway process tree. USR1 only reloads the gateway code — it doesn't traverse the process tree to kill children.
