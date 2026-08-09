# Board Name Pattern

## The Rule

Board names are project-specific and MUST NOT be hardcoded in shared code (hooks, engine defaults, etc.). The board name flows through the system as follows:

1. **Workflow YAML** specifies `kanban_board: <name>` (optional)
2. **Engine** defaults to `"fleet-workflow"` if not specified in YAML
3. **Engine** saves `kanban_board` to the state file in `_save_state()`
4. **Hooks** read `kanban_board` from the state file
5. **Missing board** → hooks log an error and return (no silent fallback)

## Why

Board names like `"adventours"` or `"fleet-workflow"` are project-specific. Hardcoding them in hooks means:
- The hook connects to the wrong board for other projects
- Silent failures when the board doesn't exist
- Confusion about which board the workflow is actually using

## Code Pattern

### In hooks (`__init__.py`):
```python
board = state.get("kanban_board")
if not board:
    logger.error("kanban_board missing from state file — cannot process hook event")
    return
conn = kb.connect(board=board)
```

### In engine (`_save_state`):
```python
state = {
    "workflow_name": workflow_name,
    "kanban_board": self.kanban_board,  # always saved
    "current_layer": current_layer,
    ...
}
```

### In engine constructor:
```python
self.kanban_board = "fleet-workflow"  # engine default, used when YAML doesn't specify
```

## Common Mistake

```python
# WRONG — hardcoded board name in hook
board = state.get("kanban_board", "adventours")

# WRONG — hardcoded board name in hook
board = state.get("kanban_board", "fleet-workflow")

# CORRECT — read from state, fail loudly if missing
board = state.get("kanban_board")
if not board:
    logger.error("kanban_board missing from state file")
    return
```
