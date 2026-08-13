"""
Todo subcommand for hermes CLI.

Handles persistent todo management via CTL database.
"""

import json
import sys
from pathlib import Path
from typing import Optional
import requests

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color


def _get_ctl_api_url() -> str:
    """Get CTL API URL from env or use default."""
    import os
    # Check if we're on the CTL host
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
        print(color(f"✗ Cannot connect to CTL at {_get_ctl_api_url()}", Colors.RED))
        print(color("  Make sure CTL is running on Friday's desktop (10.2.0.102:8001)", Colors.DIM))
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(color(f"✗ API error: {e}", Colors.RED))
        sys.exit(1)


def todo_list(show_completed: bool = False):
    """List all todos."""
    todos = _call_ctl_api("GET", "/api/todos/")
    
    if not todos:
        print(color("No todos found.", Colors.DIM))
        print(color("Add one with 'hermes todo add <text>' or the todo() tool in chat.", Colors.DIM))
        return
    
    # Filter completed if needed
    if not show_completed:
        todos = [t for t in todos if not t.get("completed", False)]
    
    if not todos:
        print(color("No pending todos. Great job! 🎉", Colors.GREEN))
        return
    
    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                              Todo List                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()
    
    # Separate incomplete and completed
    incomplete = [t for t in todos if not t.get("completed", False)]
    completed = [t for t in todos if t.get("completed", False)]
    
    if incomplete:
        for todo in incomplete:
            todo_id = todo.get("id")
            content = todo.get("content", "")
            print(f"  {color('☐', Colors.CYAN)} {color(f'[{todo_id}]', Colors.YELLOW)} {content}")
        print()
    
    if show_completed and completed:
        print(color("  Completed:", Colors.DIM))
        for todo in completed:
            todo_id = todo.get("id")
            content = todo.get("content", "")
            print(f"  {color('☑', Colors.GREEN)} {color(f'[{todo_id}]', Colors.DIM)} {color(content, Colors.DIM)}")
        print()


def todo_add(content: str):
    """Add a new todo."""
    if not content or not content.strip():
        print(color("✗ Todo content cannot be empty", Colors.RED))
        sys.exit(1)
    
    data = {"content": content.strip()}
    result = _call_ctl_api("POST", "/api/todos/", data)
    
    todo_id = result.get("id")
    print(color(f"✓ Added todo [{todo_id}]: {content}", Colors.GREEN))


def todo_complete(todo_id: int):
    """Mark a todo as complete."""
    data = {"completed": True}
    result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}", data)
    
    content = result.get("content", "")
    print(color(f"✓ Completed todo [{todo_id}]: {content}", Colors.GREEN))


def todo_update(todo_id: int, new_content: str):
    """Update todo content."""
    if not new_content or not new_content.strip():
        print(color("✗ Todo content cannot be empty", Colors.RED))
        sys.exit(1)
    
    data = {"content": new_content.strip()}
    result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}", data)
    
    print(color(f"✓ Updated todo [{todo_id}]: {new_content}", Colors.GREEN))


def todo_delete(todo_id: int):
    """Delete a todo."""
    _call_ctl_api("DELETE", f"/api/todos/{todo_id}")
    print(color(f"✓ Deleted todo [{todo_id}]", Colors.GREEN))


def todo_toggle(todo_id: int):
    """Toggle todo completion status."""
    result = _call_ctl_api("PATCH", f"/api/todos/{todo_id}/toggle", None)
    
    content = result.get("content", "")
    completed = result.get("completed", False)
    status = "completed" if completed else "reopened"
    print(color(f"✓ {status.capitalize()} todo [{todo_id}]: {content}", Colors.GREEN))


def todo_command(args):
    """Main entry point for todo subcommand."""
    action = getattr(args, "todo_action", None)
    
    if action == "list" or action == "ls" or action is None:
        show_all = getattr(args, "all", False)
        todo_list(show_completed=show_all)
    
    elif action == "add":
        content = getattr(args, "content", None)
        if not content:
            print(color("✗ Usage: hermes todo add <content>", Colors.RED))
            sys.exit(1)
        # content is a list from nargs="*"
        if isinstance(content, list):
            content = " ".join(content)
        todo_add(content)
    
    elif action == "complete" or action == "done":
        todo_id = getattr(args, "todo_id", None)
        if todo_id is None:
            print(color("✗ Usage: hermes todo complete <id>", Colors.RED))
            sys.exit(1)
        todo_complete(todo_id)
    
    elif action == "update" or action == "edit":
        todo_id = getattr(args, "todo_id", None)
        content = getattr(args, "content", None)
        if todo_id is None or not content:
            print(color("✗ Usage: hermes todo update <id> <new content>", Colors.RED))
            sys.exit(1)
        # content is a list from nargs="*"
        if isinstance(content, list):
            content = " ".join(content)
        todo_update(todo_id, content)
    
    elif action == "delete" or action == "rm":
        todo_id = getattr(args, "todo_id", None)
        if todo_id is None:
            print(color("✗ Usage: hermes todo delete <id>", Colors.RED))
            sys.exit(1)
        todo_delete(todo_id)
    
    elif action == "toggle":
        todo_id = getattr(args, "todo_id", None)
        if todo_id is None:
            print(color("✗ Usage: hermes todo toggle <id>", Colors.RED))
            sys.exit(1)
        todo_toggle(todo_id)
    
    else:
        print(color(f"✗ Unknown action: {action}", Colors.RED))
        print()
        print("Available commands:")
        print("  hermes todo list [--all]          List todos (use --all to show completed)")
        print("  hermes todo add <content>         Add a new todo")
        print("  hermes todo complete <id>         Mark todo as complete")
        print("  hermes todo update <id> <text>    Update todo content")
        print("  hermes todo delete <id>           Delete a todo")
        print("  hermes todo toggle <id>           Toggle completion status")
        sys.exit(1)
