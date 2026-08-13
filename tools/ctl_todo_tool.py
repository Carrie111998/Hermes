#!/usr/bin/env python3
"""
Persistent Todo Tool Module - CTL Database Integration

Provides persistent todo management backed by CTL's Postgres database.
Unlike the session-scoped todo() tool, these todos survive across sessions
and Hermes restarts. Accessible from both CLI (hermes todo) and in-session
via the ctl_todo() tool.
"""

import json
import os
from typing import Optional
import requests


def _get_ctl_api_url() -> str:
    """Get CTL API URL from env or use default."""
    return os.getenv("CTL_API_URL", "http://10.2.0.102:8001")


def _call_ctl_api(method: str, endpoint: str, data: Optional[dict] = None) -> dict:
    """Make API call to CTL backend."""
    url = f"{_get_ctl_api_url()}{endpoint}"
    try:
        if method == "GET":
            response = requests.get(url, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=data, timeout=10)
        elif method == "PATCH":
            response = requests.patch(url, json=data, timeout=10)
        elif method == "DELETE":
            response = requests.delete(url, timeout=10)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        
        # DELETE returns 204 No Content
        if response.status_code == 204:
            return {"success": True}
        
        return response.json()
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "error": f"Cannot connect to CTL at {_get_ctl_api_url()}. Make sure CTL is running."
        }
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}


def ctl_todo(
    action: str,
    todo_id: Optional[int] = None,
    content: Optional[str] = None,
    show_completed: bool = False,
    task_id: Optional[str] = None
) -> str:
    """
    Manage persistent todos stored in CTL database.
    
    Args:
        action: Operation to perform: "list", "add", "complete", "update", "delete", "toggle"
        todo_id: Todo ID (required for complete, update, delete, toggle)
        content: Todo content (required for add, update)
        show_completed: Include completed todos in list (only for list action)
        task_id: Internal task tracking ID
    
    Returns:
        JSON string with operation result
    """
    try:
        if action == "list":
            todos = _call_ctl_api("GET", "/api/todos/")
            
            if "error" in todos:
                return json.dumps({"success": False, "error": todos["error"]})
            
            # Filter completed if needed
            if not show_completed:
                todos = [t for t in todos if not t.get("completed", False)]
            
            return json.dumps({
                "success": True,
                "action": "list",
                "todos": todos,
                "count": len(todos)
            })
        
        elif action == "add":
            if not content:
                return json.dumps({
                    "success": False,
                    "error": "Content is required for add action"
                })
            
            result = _call_ctl_api("POST", "/api/todos/", {"content": content})
            
            if "error" in result:
                return json.dumps({"success": False, "error": result["error"]})
            
            return json.dumps({
                "success": True,
                "action": "add",
                "todo": result
            })
        
        elif action == "complete":
            if todo_id is None:
                return json.dumps({
                    "success": False,
                    "error": "todo_id is required for complete action"
                })
            
            result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}", {"completed": True})
            
            if "error" in result:
                return json.dumps({"success": False, "error": result["error"]})
            
            return json.dumps({
                "success": True,
                "action": "complete",
                "todo": result
            })
        
        elif action == "update":
            if todo_id is None or not content:
                return json.dumps({
                    "success": False,
                    "error": "todo_id and content are required for update action"
                })
            
            result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}", {"content": content})
            
            if "error" in result:
                return json.dumps({"success": False, "error": result["error"]})
            
            return json.dumps({
                "success": True,
                "action": "update",
                "todo": result
            })
        
        elif action == "delete":
            if todo_id is None:
                return json.dumps({
                    "success": False,
                    "error": "todo_id is required for delete action"
                })
            
            result = _call_ctl_api("DELETE", f"/api/todos/{todo_id}")
            
            if "error" in result:
                return json.dumps({"success": False, "error": result["error"]})
            
            return json.dumps({
                "success": True,
                "action": "delete",
                "todo_id": todo_id
            })
        
        elif action == "toggle":
            if todo_id is None:
                return json.dumps({
                    "success": False,
                    "error": "todo_id is required for toggle action"
                })
            
            result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}/toggle", None)
            
            if "error" in result:
                return json.dumps({"success": False, "error": result["error"]})
            
            return json.dumps({
                "success": True,
                "action": "toggle",
                "todo": result
            })
        
        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown action: {action}. Valid actions: list, add, complete, update, delete, toggle"
            })
    
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# Register the tool
from tools.registry import registry

registry.register(
    name="ctl_todo",
    toolset="todo",
    schema={
        "name": "ctl_todo",
        "description": (
            "Manage persistent todos stored in CTL database. These todos survive across "
            "Hermes sessions and restarts. Use this for long-term task tracking. For "
            "session-scoped planning, use the regular todo() tool instead.\n\n"
            "Actions:\n"
            "- list: Get all todos (set show_completed=true to include completed ones)\n"
            "- add: Create a new todo (requires content)\n"
            "- complete: Mark a todo as done (requires todo_id)\n"
            "- update: Change todo content (requires todo_id and content)\n"
            "- delete: Remove a todo (requires todo_id)\n"
            "- toggle: Toggle completion status (requires todo_id)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation to perform",
                    "enum": ["list", "add", "complete", "update", "delete", "toggle"]
                },
                "todo_id": {
                    "type": "integer",
                    "description": "Todo ID (required for complete, update, delete, toggle)"
                },
                "content": {
                    "type": "string",
                    "description": "Todo content (required for add, update)"
                },
                "show_completed": {
                    "type": "boolean",
                    "description": "Include completed todos in list (only for list action)",
                    "default": False
                }
            },
            "required": ["action"]
        }
    },
    handler=lambda args, **kwargs: ctl_todo(
        action=args.get("action"),
        todo_id=args.get("todo_id"),
        content=args.get("content"),
        show_completed=args.get("show_completed", False),
        task_id=kwargs.get("task_id")
    ),
    check_fn=lambda: True  # Always available (graceful error if CTL is down)
)
