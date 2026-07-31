"""Antigravity CLI direct subprocess execution for AIAgent.

Executes `agy -p` directly and returns true, uncorrupted, complete responses.
"""

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AGY_BIN = "/home/admin/.local/bin/agy"


def run_antigravity_mcp_turn(
    agent: Any,
    user_message: Any,
    original_user_message: Any = None,
    messages: List[Dict[str, Any]] = None,
    effective_task_id: Optional[str] = None,
    should_review_memory: Any = None,
) -> Dict[str, Any]:
    """Execute a turn directly through Antigravity CLI process."""
    prompt_text = ""
    if isinstance(user_message, str):
        prompt_text = user_message
    elif isinstance(user_message, list):
        parts = []
        for part in user_message:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        prompt_text = "\n".join(parts)
    else:
        prompt_text = str(user_message or "")

    logger.info("Executing direct Antigravity CLI turn for: %s...", prompt_text[:60])

    cmd = [
        AGY_BIN,
        "-p", prompt_text,
        "--dangerously-skip-permissions",
    ]

    session_id = getattr(agent, "_antigravity_conversation_id", None)
    if session_id:
        cmd.extend(["--conversation", session_id])

    cwd = "/home/admin/antigravity-bot/workspace"

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        response_text = proc.stdout.strip()
        if not response_text and proc.stderr:
            logger.warning("Antigravity CLI stderr: %s", proc.stderr)
            response_text = proc.stderr.strip()
    except Exception as e:
        logger.error("Direct Antigravity CLI execution error: %s", e)
        response_text = f"Ошибка при вызове Antigravity CLI: {e}"

    if not response_text:
        response_text = "Antigravity CLI не вернул текста."

    if messages is None:
        messages = getattr(agent, "messages", [])

    messages.append({"role": "user", "content": prompt_text})
    messages.append({"role": "assistant", "content": response_text})

    return {
        "final_response": response_text,
        "messages": messages,
        "task_id": effective_task_id,
    }
