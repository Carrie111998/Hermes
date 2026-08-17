# Technical Design - Agent Definition System

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Hermes (Main Session)                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Session Mgr │  │ Agent       │  │ delegate_   │     │
│  │             │  │ Registry    │  │ task()      │     │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘     │
│         │                │                │             │
│  ┌──────▼────────────────▼────────────────▼──────┐     │
│  │              Background Dispatch               │     │
│  └───────────────────┬───────────────────────────┘     │
└──────────────────────┼──────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
┌───────▼───────┐ ┌────▼────┐ ┌───────▼───────┐
│  Debugger     │ │Reviewer │ │  Designer     │
│  Session      │ │Session  │ │  Session      │
│  (new)        │ │(new)    │ │  (new)        │
└───────────────┘ └─────────┘ └───────────────┘
```

## delegate_task API

```python
def delegate_task(
    agent: str,                    # required: agent name
    goal: str = None,              # optional: default from agent.prompt
    context: str = None,           # optional: background info
    role: str = "leaf",            # optional: "leaf" or "orchestrator"
    tasks: list = None,            # optional: batch mode
    output_schema: dict = None,    # optional: validation schema
    action: str = "spawn",         # optional: "spawn", "list", "steer", "stop"
    subagent_id: str = None,       # optional: for steer/stop
    message: str = None,           # optional: for steer
    parent_agent=None,             # internal: parent agent context
) -> str:
```

**Key Changes:**
- `agent` = required (was optional)
- `background` = removed (always True)
- `notify` = removed (always True)
- Session = new per delegate

## Session Management

```python
# Each delegate gets a new session
session_id = f"delegate-{agent_name}-{uuid4()}"

# Track in registry
delegate_sessions = {
    delegation_id: {
        "session_id": session_id,
        "agent": agent_name,
        "goal": goal,
        "status": "running" | "completed" | "failed",
        "started_at": timestamp,
        "completed_at": timestamp,
    }
}
```

## Agent Config Injection

```python
# Load agent definition
agent_def = registry.get_agent(agent)

# Apply config to child
child = AIAgent(
    model=agent_def.model or parent.model,
    reasoning=agent_def.reasoning or parent.reasoning,
    temperature=agent_def.temperature or parent.temperature,
    ephemeral_system_prompt=agent_def.prompt,
    # ... other config from .md
)

# Background dispatch
dispatch_async(child, session_id)
```

## Multi-Agent Spawn

```python
# Orchestrator can spawn multiple agents
deleg1 = delegate_task(agent="debugger", goal="fix auth")
deleg2 = delegate_task(agent="reviewer", goal="review code")
deleg3 = delegate_task(agent="designer", goal="design UI")

# All run in background
# Results come back when each finishes
```

## Implementation

### Phase 1: Core (Week 1)
1. Agent Definition Parser
2. Agent Registry
3. Config integration (delegate section)

### Phase 2: delegate_task Redesign (Week 2)
1. Remove `background` and `notify` parameters
2. Always background=True, notify=True
3. Session tracking per delegate
4. Agent config injection

### Phase 3: Multi-Agent (Week 3)
1. Multi-agent spawn support
2. Session management
3. Result consolidation

### Phase 4: Polish (Week 4)
1. CLI commands
2. Error handling
3. Documentation
4. Testing
