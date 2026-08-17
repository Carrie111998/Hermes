# Agent Definition System - PRD (Redesigned)

## Problem
Hermes agents work in isolation. Users want multiple agents to collaborate with full configuration (reasoning, temperature, top_p) and persona in a single file, without complex profile setup.

## Solution
Redesigned `delegate_task` with agent definitions:
1. Agent config + persona in one `.md` file
2. Global config references agent files
3. Skills stay in `.hermes/skills/` (referenced by name)
4. Background=True always (no parameter)
5. Each delegate = new session (session tracking)
6. Multi-agent spawn support (orchestrator can spawn multiple agents)

## Architecture

```
User → Hermes (main session)
  → delegate_task(agent="debugger", goal="fix bug")
    → Load debugger.md config
    → Spawn session: delegate-debugger-uuid
    → Background dispatch (always)
    → Notify when done

  → delegate_task(agent="reviewer", goal="review code")
    → Load reviewer.md config
    → Spawn session: delegate-reviewer-uuid
    → Background dispatch (always)
    → Notify when done
```

## Agent Definition Format

**Location:** `~/.hermes/agents/` (global) or `.agents/` (per-project)

**Format:**
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
max_tokens: 4096          # max output tokens
context_length: 0         # 0 = inherit from model
compression_threshold: 0.0   # 0 = inherit from config
compression_target_ratio: 0.0  # 0 = inherit from config
tools: [read_file, search_files, terminal]
skills: [caveman]
max_depth: 3
timeout: 300
---
You are a debugging agent. Find and fix bugs quickly.
```

## Global Config

**config.yaml:**
```yaml
delegate:
  debugger: ~/.hermes/agents/debugger.md
  reviewer: ~/.hermes/agents/reviewer.md
  designer: ~/.hermes/agents/designer.md
```

## delegate_task API (Redesigned)

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

**Removed Parameters:**
- ❌ `background` (always True)
- ❌ `notify` (always True)

**Default Behavior:**
- `agent` = required
- `goal` = optional (default from agent.prompt)
- `background` = True (always)
- `notify` = True (always)
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

## Multi-Agent Spawn

```python
# Orchestrator can spawn multiple agents
delegate_task(agent="debugger", goal="fix auth")
delegate_task(agent="reviewer", goal="review code")
delegate_task(agent="designer", goal="design UI")
# All run in background, results come back when done
```

## Key Features

### 1. Background Always
```python
delegate_task(agent="debugger", goal="fix bug")
# → background=True (always)
# → Chat lanjut, result masuk pas selesai
```

### 2. Session Per Delegate
```python
# Each delegate gets a new session
session_id = f"delegate-{agent_name}-{uuid}"
delegate_sessions[delegation_id] = session_id
```

### 3. Multi-Agent Spawn
```python
# Orchestrator can spawn multiple agents
delegate_task(agent="debugger", goal="fix auth")
delegate_task(agent="reviewer", goal="review code")
# Both run in background, results come back when done
```

### 4. Agent Config Injection
```python
# Agent config overrides parent config
child = AIAgent(
    model=agent_def.model,
    reasoning=agent_def.reasoning,
    temperature=agent_def.temperature,
    ephemeral_system_prompt=agent_def.prompt,
    # ... other config from .md
)
```

## Implementation

### Phase 1: Core (Week 1)
1. Agent Definition Parser ✅
2. Agent Registry ✅
3. Config integration (delegate section) ✅

### Phase 2: delegate_task Redesign (Week 2)
1. Remove `background` parameter (always True)
2. Session tracking per delegate
3. Agent config injection ✅

### Phase 3: Multi-Agent (Week 3)
1. Multi-agent spawn support
2. Session management
3. Result consolidation

### Phase 4: Polish (Week 4)
1. CLI commands ✅
2. Error handling
3. Documentation
4. Testing

## Usage Examples

### Single Agent
```bash
hermes agent test debugger "fix auth bug"
```

### Multi-Agent
```python
# Orchestrator spawns multiple agents
delegate_task(agent="debugger", goal="fix auth bug")
delegate_task(agent="reviewer", goal="review code")
delegate_task(agent="designer", goal="design UI")
# All run in background, results come back when done
```

### Agent-to-Agent
```python
# Debugger can delegate to reviewer
delegate_task(agent="reviewer", goal="review my fix")
```

## Benefits
- ✅ Agent persona, config, skills per agent
- ✅ Background always (no blocking)
- ✅ Session per delegate (clean isolation)
- ✅ Multi-agent support (parallel work)
- ✅ Simple API (agent required, goal optional)
- ✅ Backward compatible (existing features preserved)
