# MCP Agent System - Technical Design

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Hermes Hub (MCP Server)               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Session Mgr │  │ Message     │  │ Agent       │     │
│  │             │  │ Router      │  │ Registry    │     │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘     │
│         │                │                │             │
│  ┌──────▼────────────────▼────────────────▼──────┐     │
│  │              MCP Protocol Layer                │     │
│  └───────────────────┬───────────────────────────┘     │
└──────────────────────┼──────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
┌───────▼───────┐ ┌────▼────┐ ┌───────▼───────┐
│  Agent A      │ │ Agent B │ │  Agent C      │
│  (MCP Client) │ │(MCP     │ │  (MCP Client) │
│  coder.md     │ │ Client) │ │  designer.md  │
└───────────────┘ │reviewer │ └───────────────┘
                  └─────────┘
```

## Component Design

### 1. Agent Definition Format

**Location:** `~/.hermes/agents/` (global) or `.agents/` (per-project)

**Format:**
```markdown
---
name: coder
model: mimo-v2.5
mode: mcp  # mcp | subagent | chat
tools: [read_file, search_files, terminal, patch]
skills: [caveman, ponytail]
---
You are a coding agent. Write clean, efficient code.
```

**Fields:**
- `name`: Agent identifier (required)
- `model`: Model to use (optional, defaults to hub model)
- `mode`: Spawn mode (mcp, subagent, chat)
- `tools`: Allowed tools (optional, defaults to all)
- `skills`: Skills to load (optional)
- Body: System prompt (required)

### 2. Agent Registry

**File:** `agent/mcp_agent_registry.py`

```python
class MCPAgentRegistry:
    """Loads and manages agent definitions from .md files."""
    
    def __init__(self):
        self.agents: Dict[str, AgentDefinition] = {}
        self.watchers: List[FileSystemWatcher] = []
    
    def load_agents(self, directories: List[Path]):
        """Load all agent definitions from directories."""
        pass
    
    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        """Get agent by name."""
        pass
    
    def list_agents(self) -> List[AgentDefinition]:
        """List all available agents."""
        pass
    
    def watch_changes(self, callback: Callable):
        """Watch for file changes and reload agents."""
        pass
```

### 3. MCP Agent Server

**File:** `agent/mcp_agent_server.py`

Each agent runs as an MCP server exposing:
- `agent_chat` tool: Send message to agent, get response
- `agent_status` tool: Get agent status
- `agent_list_tools` tool: List agent's available tools

```python
class MCPAgentServer:
    """MCP server for a single agent."""
    
    def __init__(self, agent_def: AgentDefinition):
        self.agent_def = agent_def
        self.mcp = FastMCP(f"agent-{agent_def.name}")
        self._setup_tools()
    
    def _setup_tools(self):
        """Register agent-specific tools."""
        @self.mcp.tool()
        def agent_chat(message: str) -> str:
            """Send a message to this agent and get a response."""
            return self._process_message(message)
        
        @self.mcp.tool()
        def agent_status() -> dict:
            """Get agent status and capabilities."""
            return {
                "name": self.agent_def.name,
                "model": self.agent_def.model,
                "tools": self.agent_def.tools,
                "status": "ready"
            }
    
    def _process_message(self, message: str) -> str:
        """Process a message using the agent's model and prompt."""
        # Build system prompt from agent definition
        # Call model with message
        # Return response
        pass
```

### 4. MCP Agent Client

**File:** `agent/mcp_agent_client.py`

Hermes Hub connects to agents as MCP clients.

```python
class MCPAgentClient:
    """MCP client for connecting to an agent."""
    
    def __init__(self, agent_def: AgentDefinition):
        self.agent_def = agent_def
        self.client = None
    
    async def connect(self):
        """Connect to agent's MCP server."""
        # Start agent process if needed
        # Connect via stdio or HTTP
        pass
    
    async def chat(self, message: str) -> str:
        """Send message to agent and get response."""
        result = await self.client.call_tool("agent_chat", {"message": message})
        return result
    
    async def get_status(self) -> dict:
        """Get agent status."""
        return await self.client.call_tool("agent_status", {})
    
    async def disconnect(self):
        """Disconnect from agent."""
        pass
```

### 5. Message Router

**File:** `agent/mcp_message_router.py`

Routes messages between agents and manages conversation flow.

```python
class MCPMessageRouter:
    """Routes messages between agents."""
    
    def __init__(self, registry: MCPAgentRegistry):
        self.registry = registry
        self.clients: Dict[str, MCPAgentClient] = {}
        self.conversation_history: List[Message] = []
    
    async def send_to_agent(self, agent_name: str, message: str) -> str:
        """Send message to specific agent."""
        client = self._get_or_create_client(agent_name)
        response = await client.chat(message)
        self._record_message(agent_name, message, response)
        return response
    
    async def broadcast(self, message: str) -> Dict[str, str]:
        """Send message to all agents and collect responses."""
        responses = {}
        for agent_name in self.clients:
            responses[agent_name] = await self.send_to_agent(agent_name, message)
        return responses
    
    async def route_conversation(self, message: str, target_agents: List[str]) -> str:
        """Route conversation between multiple agents."""
        # Send to first agent
        # Get response
        # Send response to next agent if needed
        # Continue until conversation complete
        pass
```

### 6. Session Manager

**File:** `agent/mcp_session_manager.py`

Manages single session with multiple agents.

```python
class MCPSessionManager:
    """Manages session with multiple agents."""
    
    def __init__(self):
        self.registry = MCPAgentRegistry()
        self.router = MCPMessageRouter(self.registry)
        self.active_agents: Set[str] = set()
        self.session_id: str = None
    
    async def start_session(self, agent_names: List[str]):
        """Start session with specified agents."""
        for name in agent_names:
            await self.router.connect_agent(name)
            self.active_agents.add(name)
    
    async def user_message(self, message: str) -> str:
        """Handle user message in session."""
        # Determine which agent(s) should respond
        # Route message appropriately
        # Return combined response
        pass
    
    async def agent_to_agent(self, from_agent: str, to_agent: str, message: str):
        """Direct agent-to-agent communication."""
        return await self.router.send_to_agent(to_agent, message)
    
    async def end_session(self):
        """End session and disconnect all agents."""
        for name in self.active_agents:
            await self.router.disconnect_agent(name)
        self.active_agents.clear()
```

## Integration Points

### With Existing Hermes Infrastructure

1. **CLI Integration:**
   ```bash
   hermes agent start coder designer  # Start session with agents
   hermes agent chat coder "fix bug"  # Direct agent chat
   hermes agent list                  # List available agents
   ```

2. **TUI Integration:**
   - Agent panel showing active agents
   - Agent switching via hotkey
   - Agent-to-agent chat visualization

3. **A2A Integration:**
   - Agents can be spawned as A2A processes
   - MCP protocol over A2A transport
   - Backward compatibility with existing A2A

4. **delegate_task Integration:**
   - Agents can be spawned as subagents
   - MCP protocol for subagent communication
   - Result reporting back to parent

### With Existing MCP Infrastructure

1. **hermes_tools_mcp_server.py:**
   - Reuse tool discovery and registration
   - Extend EXPOSED_TOOLS for agent-specific tools
   - Share MCP protocol implementation

2. **tools/mcp_tool.py:**
   - Reuse MCP client implementation
   - Extend for agent-specific connections
   - Share connection management

## Implementation Plan

### Phase 1: Core (Week 1-2)
1. Agent Definition Parser (Task 001)
2. Agent Registry (Task 002)
3. Basic MCP Agent Server
4. Basic MCP Agent Client

### Phase 2: Integration (Week 3-4)
1. Message Router
2. Session Manager
3. CLI Integration
4. TUI Integration

### Phase 3: Advanced (Week 5-6)
1. Agent-to-agent communication
2. Conversation flow management
3. Tool sharing between agents
4. Performance optimization

### Phase 4: Polish (Week 7-8)
1. Error handling and recovery
2. Documentation and examples
3. Testing and validation
4. Migration guide

## Example Usage

### Agent Definitions

**~/.hermes/agents/coder.md:**
```markdown
---
name: coder
model: mimo-v2.5
mode: mcp
tools: [read_file, search_files, terminal, patch]
skills: [caveman, ponytail]
---
You are a coding agent. Write clean, efficient code.
```

**~/.hermes/agents/reviewer.md:**
```markdown
---
name: reviewer
model: mimo-v2.5
mode: mcp
tools: [read_file, search_files]
skills: [caveman-review]
---
You are a code review agent. Review code for quality.
```

### Starting a Session

```bash
# Start session with multiple agents
hermes agent start coder reviewer

# Chat with specific agent
hermes agent chat coder "fix the authentication bug"

# Agent-to-agent communication
hermes agent route coder reviewer "review this code"
```

### TUI Interface

```
┌─────────────────────────────────────────────────┐
│ Hermes Agent Session                            │
├─────────────────────────────────────────────────┤
│ Active Agents: coder, reviewer                  │
├─────────────────────────────────────────────────┤
│ User: Fix the authentication bug                │
│                                                 │
│ coder: I'll fix the auth bug in auth.py...      │
│                                                 │
│ reviewer: The fix looks good, but consider...   │
│                                                 │
│ coder: Good point, I'll add that error handling │
└─────────────────────────────────────────────────┘
```

## Benefits

1. **Simple Setup:** One .md file = one agent
2. **Single Session:** All agents share context
3. **Flexible Communication:** Agent-to-agent, user-to-agent
4. **Tool Sharing:** Agents can use each other's tools
5. **Easy Management:** Add/remove agents without restart
6. **Backward Compatible:** Works with existing A2A/delegate_task
