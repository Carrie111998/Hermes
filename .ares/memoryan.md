# Project Memory

## Context
Fork: `are-es/hermes-agent`. Upstream: `NousResearch/hermes-agent`.
Feature branch: `feat/native-plan-mode`.
Goal: upstream-quality native PLAN/BUILD TUI mode.

## Decisions
- BUILD default.
- Shift+Tab toggles PLAN/BUILD, replacing yolo shortcut.
- `/yolo` remains available.
- PLAN is session-scoped and backend-enforced.
- No global config write and no new dependency.

## New Feature: Agent Definition System (Redesigned)
- Simple markdown-based agent definitions (config + persona in one file)
- Agent config: model, base_url, provider, api_mode, api_key, reasoning, temperature, top_p, max_tokens, context_length, compression_*, tools, skills
- Global config: `delegate:` section with agent name → file path mapping
- Skills stay in `.hermes/skills/` (referenced by name)
- `delegate_task` redesigned:
  - `agent` parameter required
  - `background` removed (always True)
  - `notify` removed (always True)
  - Each delegate = new session (session tracking)
  - Multi-agent spawn support
- CLI commands: new, list, show, validate, test, add, remove

## Architecture
```
User → Hermes (main session)
  → delegate_task(agent="debugger", goal="fix bug")
    → Load debugger.md config
    → Spawn session: delegate-debugger-uuid
    → Background dispatch (always)
    → Notify when done
```

## Agent Definition Format
```markdown
---
name: debugger
model: mimo-v2.5
base_url:                # empty = inherit from config
provider:                # empty = inherit from config
api_mode:                # empty = inherit from config
api_key:                 # empty = inherit from config
reasoning: max
temperature: 0.2
top_p: 0.95
max_tokens: 4096
context_length: 0
compression_threshold: 0.0
compression_target_ratio: 0.0
tools: [read_file, search_files, terminal]
skills: [caveman]
max_depth: 3
timeout: 300
---
You are a debugging agent. Find and fix bugs quickly.
```

## Config Structure
```yaml
delegate:
  debugger: ~/.hermes/agents/debugger.md
  reviewer: ~/.hermes/agents/reviewer.md
  designer: ~/.hermes/agents/designer.md
```

## Files Modified
- `agent/agent_definition.py` — Parser + validation
- `agent/agent_registry.py` — Registry (load/manage)
- `agent/mcp_agent_tools.py` — MCP tools
- `hermes_cli/agent_cmd.py` — CLI commands
- `hermes_cli/config_defaults.py` — Config defaults
- `hermes_cli/config_migrations.py` — Migration v39
- `hermes_cli/main.py` — CLI registration
- `tools/delegate_tool.py` — delegate_task redesign
- `run_agent.py` — _dispatch_delegate_task fix

## Error Log
- Automated clone/fetch timed out; user completed clone manually.
- A2A timed out twice after partially writing code. ARES audited the partial diff, removed duplicate gates/tests, fixed agent hydration and TUI session hydration, then verified focused suites.

## Verification
- Python: 20 focused tests passed.
- TypeScript: typecheck passed; 121 focused tests passed.
- Ruff and git diff checks passed.
