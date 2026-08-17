"""MCP agent tools — expose agent definitions as callable MCP tools.

Each registered agent becomes a tool the main agent can call:
  - agent_<name>(message) → send message to agent, get response
  - agent_list() → list available agents
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Agent registry reference (loaded lazily)
_registry = None


def _get_registry():
    """Get or load the agent registry."""
    global _registry
    if _registry is None:
        from agent.agent_registry import get_agent_registry
        _registry = get_agent_registry()
        
        # Load from config if not loaded yet
        if not _registry._loaded:
            try:
                from hermes_cli.config import load_config
                config = load_config()
                _registry.load_from_config(config)
            except Exception:
                pass
            
            # Also scan default directories
            from pathlib import Path
            for d in (Path.home() / ".hermes" / "agents", Path.cwd() / ".agents"):
                if d.exists():
                    _registry.load_from_directory(d)
    
    return _registry


def get_agent_tools() -> List[Dict[str, Any]]:
    """Get MCP tool definitions for all registered agents.
    
    Returns:
        List of tool definitions in MCP format
    """
    registry = _get_registry()
    tools = []
    
    for agent_def in registry.list_agents():
        tool = {
            "name": f"agent_{agent_def.name}",
            "description": (
                f"Delegate a task to the '{agent_def.name}' agent. "
                f"Model: {agent_def.model}, Reasoning: {agent_def.reasoning}. "
                f"{agent_def.prompt[:100]}..."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Task or message to send to the agent",
                    },
                },
                "required": ["message"],
            },
        }
        tools.append(tool)
    
    # Add agent_list tool
    tools.append({
        "name": "agent_list",
        "description": "List all available agent definitions",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    })
    
    return tools


def handle_agent_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Handle an agent tool call.
    
    Args:
        tool_name: Name of the tool (e.g., "agent_coder", "agent_list")
        arguments: Tool arguments
        
    Returns:
        Tool result as string
    """
    registry = _get_registry()
    
    # Handle agent_list
    if tool_name == "agent_list":
        agents = registry.list_agents()
        if not agents:
            return "No agents available."
        
        result = []
        for a in agents:
            result.append(
                f"- {a.name}: model={a.model}, reasoning={a.reasoning}, "
                f"temp={a.temperature}, tools={len(a.tools)}, skills={len(a.skills)}"
            )
        return "\n".join(result)
    
    # Handle agent_<name>
    if tool_name.startswith("agent_"):
        agent_name = tool_name[6:]  # Remove "agent_" prefix
        agent_def = registry.get_agent(agent_name)
        
        if not agent_def:
            return f"Error: Agent '{agent_name}' not found."
        
        message = arguments.get("message", "")
        if not message:
            return "Error: No message provided."
        
        return _delegate_to_agent(agent_def, message)
    
    return f"Error: Unknown agent tool '{tool_name}'."


def _delegate_to_agent(agent_def, message: str) -> str:
    """Delegate a task to an agent using its definition.
    
    Args:
        agent_def: AgentDefinition to use
        message: Task message
        
    Returns:
        Agent's response
    """
    try:
        # Import delegate_task functionality
        from model_tools import handle_function_call
        
        # Build delegate_task args with agent config
        args = {
            "task": message,
            "agent": agent_def.name,
        }
        
        # Call delegate_task with agent name
        result = handle_function_call("delegate_task", args)
        
        if isinstance(result, str):
            return result
        return json.dumps(result)
        
    except Exception as e:
        logger.exception("Failed to delegate to agent %s", agent_def.name)
        return f"Error delegating to agent '{agent_def.name}': {e}"


def register_agent_tools(registry) -> List[Dict[str, Any]]:
    """Register agent tools with the given registry.
    
    Args:
        registry: Tool registry to register with
        
    Returns:
        List of registered tool definitions
    """
    tools = get_agent_tools()
    
    for tool in tools:
        try:
            registry.register_tool(
                name=tool["name"],
                description=tool["description"],
                input_schema=tool["inputSchema"],
                handler=lambda args, _tool=tool: handle_agent_tool(_tool["name"], args),
            )
        except Exception as e:
            logger.warning("Failed to register agent tool %s: %s", tool["name"], e)
    
    return tools
