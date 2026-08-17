# MCP Agent System - Example Implementation

## Agent Definition Parser Example

```python
# agent/mcp_agent_parser.py
import yaml
import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class AgentDefinition:
    name: str
    model: str
    mode: str  # mcp, subagent, chat
    tools: List[str]
    skills: List[str]
    prompt: str
    source_path: Path

def parse_agent_file(file_path: Path) -> Optional[AgentDefinition]:
    """Parse a markdown agent definition file."""
    try:
        content = file_path.read_text()
        
        # Split frontmatter and body
        match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
        if not match:
            return None
        
        frontmatter_str, body = match.groups()
        frontmatter = yaml.safe_load(frontmatter_str)
        
        # Extract fields with defaults
        name = frontmatter.get('name', file_path.stem)
        model = frontmatter.get('model', 'mimo-v2.5')
        mode = frontmatter.get('mode', 'mcp')
        tools = frontmatter.get('tools', [])
        skills = frontmatter.get('skills', [])
        
        return AgentDefinition(
            name=name,
            model=model,
            mode=mode,
            tools=tools,
            skills=skills,
            prompt=body.strip(),
            source_path=file_path
        )
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
        return None
```

## Agent Registry Example

```python
# agent/mcp_agent_registry.py
from pathlib import Path
from typing import Dict, List, Optional
import watchdog.observers
import watchdog.events

class MCPAgentRegistry:
    """Loads and manages agent definitions."""
    
    def __init__(self):
        self.agents: Dict[str, AgentDefinition] = {}
        self.observer = watchdog.observers.Observer()
    
    def load_agents(self, directories: List[Path]):
        """Load all agent definitions from directories."""
        for directory in directories:
            if not directory.exists():
                continue
            
            for file_path in directory.rglob('*.md'):
                agent = parse_agent_file(file_path)
                if agent:
                    # Project-level overrides global
                    if agent.name not in self.agents:
                        self.agents[agent.name] = agent
    
    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        """Get agent by name."""
        return self.agents.get(name)
    
    def list_agents(self) -> List[AgentDefinition]:
        """List all available agents."""
        return list(self.agents.values())
    
    def watch_changes(self, callback):
        """Watch for file changes and reload agents."""
        handler = AgentFileHandler(self, callback)
        for directory in self.directories:
            self.observer.schedule(handler, str(directory), recursive=True)
        self.observer.start()
```

## MCP Agent Server Example

```python
# agent/mcp_agent_server.py
from mcp.server.fastmcp import FastMCP
from typing import Dict, Any
import json

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
                "skills": self.agent_def.skills,
                "status": "ready"
            }
        
        @self.mcp.tool()
        def agent_list_tools() -> list:
            """List agent's available tools."""
            return self.agent_def.tools
    
    def _process_message(self, message: str) -> str:
        """Process a message using the agent's model and prompt."""
        # Build system prompt
        system_prompt = self.agent_def.prompt
        
        # Call model (simplified)
        # In reality, this would use the model API
        response = f"[{self.agent_def.name}] Processing: {message}"
        
        return response
    
    def run(self, transport: str = 'stdio'):
        """Run the MCP server."""
        self.mcp.run(transport=transport)
```

## MCP Agent Client Example

```python
# agent/mcp_agent_client.py
from mcp.client.stdio import stdio_client
from typing import Optional
import asyncio

class MCPAgentClient:
    """MCP client for connecting to an agent."""
    
    def __init__(self, agent_def: AgentDefinition):
        self.agent_def = agent_def
        self.client = None
        self.session = None
    
    async def connect(self):
        """Connect to agent's MCP server."""
        # Start agent process
        # Connect via stdio
        self.client, self.session = await stdio_client(
            command="python",
            args=["-m", "agent.mcp_agent_server", self.agent_def.name]
        )
        
        # Initialize session
        await self.session.initialize()
    
    async def chat(self, message: str) -> str:
        """Send message to agent and get response."""
        if not self.session:
            raise RuntimeError("Not connected to agent")
        
        result = await self.session.call_tool(
            "agent_chat",
            {"message": message}
        )
        
        return result.content[0].text if result.content else ""
    
    async def get_status(self) -> dict:
        """Get agent status."""
        if not self.session:
            raise RuntimeError("Not connected to agent")
        
        result = await self.session.call_tool("agent_status", {})
        return json.loads(result.content[0].text) if result.content else {}
    
    async def disconnect(self):
        """Disconnect from agent."""
        if self.session:
            await self.session.close()
            self.session = None
            self.client = None
```

## Message Router Example

```python
# agent/mcp_message_router.py
from typing import Dict, List
import asyncio

class MCPMessageRouter:
    """Routes messages between agents."""
    
    def __init__(self, registry: MCPAgentRegistry):
        self.registry = registry
        self.clients: Dict[str, MCPAgentClient] = {}
        self.conversation_history: List[Dict] = []
    
    async def connect_agent(self, agent_name: str):
        """Connect to an agent."""
        agent_def = self.registry.get_agent(agent_name)
        if not agent_def:
            raise ValueError(f"Agent {agent_name} not found")
        
        client = MCPAgentClient(agent_def)
        await client.connect()
        self.clients[agent_name] = client
    
    async def send_to_agent(self, agent_name: str, message: str) -> str:
        """Send message to specific agent."""
        if agent_name not in self.clients:
            await self.connect_agent(agent_name)
        
        client = self.clients[agent_name]
        response = await client.chat(message)
        
        # Record in conversation history
        self.conversation_history.append({
            "from": "user",
            "to": agent_name,
            "message": message,
            "response": response
        })
        
        return response
    
    async def agent_to_agent(self, from_agent: str, to_agent: str, message: str) -> str:
        """Direct agent-to-agent communication."""
        # Format message with sender info
        formatted_message = f"[{from_agent}]: {message}"
        
        response = await self.send_to_agent(to_agent, formatted_message)
        
        # Record in conversation history
        self.conversation_history.append({
            "from": from_agent,
            "to": to_agent,
            "message": message,
            "response": response
        })
        
        return response
    
    async def disconnect_agent(self, agent_name: str):
        """Disconnect from an agent."""
        if agent_name in self.clients:
            await self.clients[agent_name].disconnect()
            del self.clients[agent_name]
```

## Session Manager Example

```python
# agent/mcp_session_manager.py
from typing import List, Set
import asyncio

class MCPSessionManager:
    """Manages session with multiple agents."""
    
    def __init__(self):
        self.registry = MCPAgentRegistry()
        self.router = MCPMessageRouter(self.registry)
        self.active_agents: Set[str] = set()
        self.session_id: str = None
    
    async def start_session(self, agent_names: List[str]):
        """Start session with specified agents."""
        # Load agents from directories
        self.registry.load_agents([
            Path.home() / '.hermes' / 'agents',
            Path.cwd() / '.agents'
        ])
        
        # Connect to agents
        for name in agent_names:
            await self.router.connect_agent(name)
            self.active_agents.add(name)
        
        self.session_id = f"session-{len(agent_names)}-agents"
        print(f"Started session with agents: {', '.join(agent_names)}")
    
    async def user_message(self, message: str) -> str:
        """Handle user message in session."""
        # For now, send to first active agent
        # In future, could use routing logic
        if not self.active_agents:
            return "No active agents in session"
        
        first_agent = next(iter(self.active_agents))
        return await self.router.send_to_agent(first_agent, message)
    
    async def agent_to_agent(self, from_agent: str, to_agent: str, message: str):
        """Direct agent-to-agent communication."""
        if from_agent not in self.active_agents:
            raise ValueError(f"Agent {from_agent} not in session")
        if to_agent not in self.active_agents:
            raise ValueError(f"Agent {to_agent} not in session")
        
        return await self.router.agent_to_agent(from_agent, to_agent, message)
    
    async def end_session(self):
        """End session and disconnect all agents."""
        for name in self.active_agents:
            await self.router.disconnect_agent(name)
        self.active_agents.clear()
        print("Session ended")
```

## CLI Integration Example

```python
# hermes_cli/commands/agent.py
import click
import asyncio
from pathlib import Path

@click.group()
def agent():
    """Agent management commands."""
    pass

@agent.command()
@click.argument('agent_names', nargs=-1, required=True)
def start(agent_names):
    """Start session with agents."""
    session = MCPSessionManager()
    asyncio.run(session.start_session(list(agent_names)))
    
    # Interactive loop
    print("Agent session started. Type 'exit' to end.")
    while True:
        try:
            message = input("> ")
            if message.lower() in ['exit', 'quit']:
                break
            
            response = asyncio.run(session.user_message(message))
            print(response)
        except KeyboardInterrupt:
            break
    
    asyncio.run(session.end_session())

@agent.command()
@click.argument('agent_name')
@click.argument('message')
def chat(agent_name, message):
    """Chat with specific agent."""
    session = MCPSessionManager()
    asyncio.run(session.start_session([agent_name]))
    
    response = asyncio.run(session.user_message(message))
    print(response)
    
    asyncio.run(session.end_session())

@agent.command('list')
def list_agents():
    """List available agents."""
    registry = MCPAgentRegistry()
    registry.load_agents([
        Path.home() / '.hermes' / 'agents',
        Path.cwd() / '.agents'
    ])
    
    agents = registry.list_agents()
    if not agents:
        print("No agents found")
        return
    
    print("Available agents:")
    for agent in agents:
        print(f"  {agent.name} ({agent.mode}) - {agent.model}")
```
