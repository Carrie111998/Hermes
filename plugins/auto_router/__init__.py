"""Native slash command /auto for routing prompts using auto_model_router."""

import os
from typing import Optional

try:
    from .router import chat_auto
except ImportError as e:
    chat_auto = None
    _import_err = str(e)

def _handle_auto(raw_args: str) -> Optional[str]:
    prompt = raw_args.strip()
    if not prompt:
        return "Usage: /auto <prompt>"
    
    if chat_auto is None:
        return f"Error: router.py not found or failed to load. ({_import_err})"
        
    try:
        response = chat_auto(prompt)
        
        if isinstance(response, dict) and response.get("status") == "success":
            provider = response.get("provider", "unknown")
            model = response.get("model", "unknown")
            category = response.get("category", "unknown")
            content = response.get("reply", "")
            return f"**[Routed to {provider} / {model} for '{category}']**\n\n{content}"
        else:
            err = response.get('message', 'Unknown error') if isinstance(response, dict) else "Invalid response format"
            return f"Failed to get response: {err}"
    except Exception as e:
        return f"Error executing auto router: {e}"

def register(ctx) -> None:
    ctx.register_command(
        "auto",
        handler=_handle_auto,
        description="Auto-route a prompt to the best free model.",
        args_hint="<prompt>"
    )
