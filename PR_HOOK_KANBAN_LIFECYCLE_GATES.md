# Upstream PR Description: Add Neutral Policy Hooks for Kanban Task Claim & Complete Gating

**Branch**: `contrib/hook-kanban-lifecycle-gates`  
**Base**: `main` (`b39d76d902b0891457bc73a6eb43aa136f26d7a8`)  
**Type**: Feature / Extension Point

---

## Summary
Hermes currently provides observer hooks (`kanban_task_claimed`, `kanban_task_completed`, `on_kanban_task_updated`) that notify plugins *after* durable database state has transitioned. However, plugins and governance engines that need to validate prerequisites (e.g. CI checks, evidence bundles, verification ledgers, contract conformance) or reject premature completion have no neutral mechanism to veto state transitions before they are committed to SQLite.

This PR adds two minimal, neutral policy hooks:
1. `pre_kanban_task_complete`: Fired inside `complete_task(...)` before the final transaction commits `status = 'done'`. Allows plugins to return `{"allow": False, "reason": "..."}` or raise to reject completion with an auditable reason.
2. `pre_kanban_task_claim`: Fired inside `claim_task(...)` before transitioning `ready -> running`. Allows plugins to return `{"allow": False, "reason": "..."}` to defer or prevent claim.

Both hooks are registered in `VALID_HOOKS` and `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS` in `hermes_cli/plugins.py`.

## Tests Added
- `tests/hermes_cli/test_kanban_lifecycle_gates_hooks.py`: Verifies that hooks can synchronously reject completion and claim, preserving the prior task status.
