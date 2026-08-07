"""Send Kernel Approval Tool -- request operator approval for a pending memory-kernel proposal.

Sends a Telegram message to the operator's home channel with two inline buttons
(Approve / Reject). Tapping a button is handled by the LIVE gateway's Telegram
callback dispatcher (`gateway/platforms/telegram.py`, "kp:" prefix) — this tool only
SENDS the request; it never decides or writes to the kernel itself.

Field/new_value/reason are resolved SERVER-SIDE from the kernel's own ledger (never
trusted from the agent's own recollection), so the message is accurate even if the
agent's memory of what it just proposed is imprecise.
"""

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Absolute path to the memory-kernel repo this tool reads proposals from. Hermes and the
# memory-kernel are separate codebases (see the retirement/verified-commit/telegram-approve
# packets this session) — this is the one place that bridges them for a READ-ONLY lookup.
_MEMORY_KERNEL_SRC = "/Users/MoltbotAgent/moltbot/workspace-dev/memory-kernel/src"


def _kernel_lookup(project: str, proposal_id: str):
    """Read-only lookup of a pending proposal's field/new_value/reason. Returns dict or None."""
    if _MEMORY_KERNEL_SRC not in sys.path:
        sys.path.insert(0, _MEMORY_KERNEL_SRC)
    from memory_kernel.persistence import LedgerStore, default_database_path

    store = LedgerStore(default_database_path())
    try:
        proposal = store.get_proposal_by_id(project, proposal_id)
        return None if proposal is None else dict(proposal)
    finally:
        store.close()


SEND_KERNEL_APPROVAL_SCHEMA = {
    "name": "send_kernel_approval",
    "description": (
        "Request the operator's approval for a pending memory-kernel proposal, via Telegram "
        "inline buttons in this same conversation. Call this IMMEDIATELY after kernel_propose "
        "returns status='pending' — don't wait, don't batch, one call per proposal. "
        "The operator taps Approve/Reject; you never see or process the decision yourself — "
        "the kernel commits (or doesn't) independently of this conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "The kernel project_id the proposal belongs to (e.g. 'prj_nex_trends')."
            },
            "proposal_id": {
                "type": "string",
                "description": "The proposal_id returned by kernel_propose (e.g. 'prop_abc123...')."
            }
        },
        "required": ["project", "proposal_id"]
    }
}


def send_kernel_approval_tool(args, **kw):
    """Handle send_kernel_approval tool calls."""
    from tools.registry import tool_error

    project = args.get("project", "")
    proposal_id = args.get("proposal_id", "")
    if not project or not proposal_id:
        return tool_error("Both 'project' and 'proposal_id' are required")

    try:
        proposal = _kernel_lookup(project, proposal_id)
    except Exception as e:
        return json.dumps({"error": f"Failed to read proposal from memory-kernel: {e}"})
    if proposal is None:
        return json.dumps({"error": f"No such pending proposal: {proposal_id} in {project}"})

    field = proposal.get("field")
    new_value = json.loads(proposal.get("new_value_json", "null"))
    reason = proposal.get("reason") or ""

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps({"error": f"Failed to load gateway config: {e}"})

    pconfig = config.platforms.get(Platform.TELEGRAM)
    if not pconfig or not pconfig.enabled:
        return json.dumps({"error": "Telegram is not configured/enabled for this profile"})
    home = config.get_home_channel(Platform.TELEGRAM)
    if not home:
        return json.dumps({"error": "No Telegram home channel set for this profile"})

    try:
        from model_tools import _run_async
        result = _run_async(_send_approval_request(
            pconfig.token, home.chat_id, project, proposal_id, field, new_value, reason,
        ))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"Send failed: {e}"})


async def _send_approval_request(token, chat_id, project, proposal_id, field, new_value, reason):
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

    text = (
        f"🔔 *Kernel proposal pending approval*\n\n"
        f"*Project:* `{project}`\n"
        f"*Field:* `{field}`\n"
        f"*New value:* `{json.dumps(new_value)}`\n"
        f"*Reason:* {reason}\n\n"
        f"`{proposal_id}`"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"kp:approve:{proposal_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"kp:reject:{proposal_id}"),
    ]])

    bot = Bot(token=token)
    msg = await bot.send_message(
        chat_id=int(chat_id), text=text, parse_mode="Markdown", reply_markup=keyboard,
    )
    return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": str(msg.message_id)}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="send_kernel_approval",
    toolset="messaging",
    schema=SEND_KERNEL_APPROVAL_SCHEMA,
    handler=send_kernel_approval_tool,
    emoji="🔔",
)
