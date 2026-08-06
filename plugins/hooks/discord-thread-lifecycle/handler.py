"""Discord thread lifecycle hook.

The Gateway supplies the in-process platform adapter in ``context["adapter"]``.
The hook intentionally awaits the adapter operation so completion updates are
not lost when the agent turn returns.
"""

from typing import Any, Dict


async def handle(event_type: str, context: Dict[str, Any]) -> None:
    if context.get("platform") != "discord":
        return

    thread_id = str(context.get("thread_id") or "").strip()
    adapter = context.get("adapter")
    rename_thread = getattr(adapter, "rename_thread", None)
    if not thread_id or not callable(rename_thread):
        return

    emoji = "⏳" if event_type == "agent:start" else ("❌" if context.get("failed") else "✅")
    await rename_thread(thread_id, "", lifecycle_emoji=emoji)