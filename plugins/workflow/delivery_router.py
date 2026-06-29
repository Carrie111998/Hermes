"""Delivery router for workflow outputs.

The router persists workflow output to a local log file, the same way
cron jobs persist their output. This is the default behavior — every
workflow run writes to ``~/.hermes/workflow-logs/{date}/{run_id}.log``.

Additionally, if a cron was set up with a ``delivery`` target, the router
ALSO posts to that platform. This is an opt-in additional layer — only
cron workflows explicitly configured with ``delivery="discord:..."`` (or
similar) trigger platform posting.

Activation:
  - ALWAYS: writes to local log file (cron-style persistence)
  - IF delivery is set to a platform target: ALSO posts to that platform
  - IF delivery is empty/missing: just the log file

Delivery formats (the optional additional layer):
  - empty / not set / ``"local"``: log file only
  - ``"discord:CHANNEL_ID"``: log file + post to Discord channel
  - ``"discord:CHANNEL_ID:THREAD_ID"``: log file + post to Discord thread
  - ``"telegram:CHAT_ID"``: log file + post to Telegram chat
  - ``"telegram:CHAT_ID:THREAD_ID"``: log file + post to Telegram topic

Example cron setup with additional delivery:
  ```python
  cronjob(
      action="create",
      schedule="0 */4 * * *",
      prompt="Run workflow_start(workflow='fleet-health', context={...}, "
             "delivery='discord:123456789'). Report back only if issues.",
      name="fleet-health-cron"
  )
  ```

Without ``delivery``, the workflow just writes to the log file (same as
cron jobs do by default).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def _hermes_binary() -> str:
    """Resolve the ``hermes`` CLI binary from the venv.

    Uses the same resolution logic as the workflow engine to ensure
    consistency across invocations.
    """
    candidate = pathlib.Path(sys.executable).parent / "hermes"
    if candidate.is_file():
        return str(candidate)
    venv_candidate = pathlib.Path(sys.prefix) / "bin" / "hermes"
    if venv_candidate.is_file():
        return str(venv_candidate)
    project_venv = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / ".venv"
        / "bin"
        / "hermes"
    )
    if project_venv.is_file():
        return str(project_venv)
    return "hermes"


# ── Public API ───────────────────────────────────────────────────

def deliver(
    result: Dict[str, Any],
    delivery: Optional[str],
    run_id: str,
    workflow_name: str,
) -> Dict[str, Any]:
    """Route workflow result to the configured delivery target.

    ``delivery`` formats:
      - ``"local"``: write to log file, return
        ``{"delivered": "local", "path": "<file>"}``.
      - ``"discord:CHANNEL_ID"``: log file + post to Discord, return
        ``{"delivered": "discord", "channel_id": "...", "status": "sent"}``.
      - ``"discord:CHANNEL_ID:THREAD_ID"``: log file + post to Discord thread.
      - ``"telegram:CHAT_ID"``: log file + post to Telegram chat.
      - ``"telegram:CHAT_ID:THREAD_ID"``: log file + post to Telegram topic.
      - anything else: log a warning and fall back to local only.

    Always returns a dict with delivery metadata (includes log path).
    """
    # Always write to local log — the log file is the persistent record.
    log_result = _deliver_local(result, run_id, workflow_name)

    if not delivery or delivery.strip() in ("", "local"):
        return log_result

    target = delivery.strip()
    if target.startswith("discord:"):
        discord_result = _deliver_discord(result, target, run_id, workflow_name)
        return {**log_result, **discord_result}
    elif target.startswith("telegram:"):
        telegram_result = _deliver_telegram(result, target, run_id, workflow_name)
        return {**log_result, **telegram_result}
    else:
        log.warning(
            "Unknown delivery target: %s — defaulting to local log only",
            delivery,
        )
        return log_result


# ── Local delivery ───────────────────────────────────────────────

def _deliver_local(
    result: Dict[str, Any],
    run_id: str,
    workflow_name: str,
) -> Dict[str, Any]:
    """Write result to a local log file. Always runs.

    Log path: ``~/.hermes/workflow-logs/{YYYY-MM-DD}/{run_id}.log``
    """
    log_dir = (
        pathlib.Path.home()
        / ".hermes"
        / "workflow-logs"
        / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{run_id}.log"

    with open(log_file, "w") as f:
        f.write(f"# {workflow_name} — {run_id}\n")
        f.write(
            f"# Completed: {datetime.now(timezone.utc).isoformat()}\n\n"
        )
        f.write(json.dumps(result, indent=2, default=str))

    log.info("Workflow result written to %s", log_file)
    return {"delivered": "local", "path": str(log_file)}


# ── Discord delivery ─────────────────────────────────────────────

def _deliver_discord(
    result: Dict[str, Any],
    delivery: str,
    run_id: str,
    workflow_name: str,
) -> Dict[str, Any]:
    """Post result to a Discord channel via ``hermes send``.

    Format: ``discord:CHANNEL_ID`` or ``discord:CHANNEL_ID:THREAD_ID``
    """
    parts = delivery.split(":", 2)
    channel_id = parts[1]
    thread_id = parts[2] if len(parts) > 2 else None

    body = _format_message(result, workflow_name, run_id)

    send_target = f"discord:{channel_id}"
    if thread_id:
        send_target = f"discord:{channel_id}:{thread_id}"

    try:
        _hermes_send(send_target, body)
        log.info("Delivered to Discord channel %s", channel_id)
        return {
            "delivered": "discord",
            "channel_id": channel_id,
            "thread_id": thread_id,
            "status": "sent",
        }
    except Exception as exc:
        log.error(
            "Discord delivery failed for channel %s: %s — "
            "log file already written",
            channel_id,
            exc,
        )
        return {
            "delivered": "discord",
            "channel_id": channel_id,
            "thread_id": thread_id,
            "status": "failed",
            "error": str(exc),
        }


# ── Telegram delivery ────────────────────────────────────────────

def _deliver_telegram(
    result: Dict[str, Any],
    delivery: str,
    run_id: str,
    workflow_name: str,
) -> Dict[str, Any]:
    """Post result to a Telegram chat/topic via ``hermes send``.

    Format: ``telegram:CHAT_ID`` or ``telegram:CHAT_ID:THREAD_ID``
    """
    parts = delivery.split(":", 2)
    chat_id = parts[1]
    thread_id = parts[2] if len(parts) > 2 else None

    body = _format_message(result, workflow_name, run_id)

    send_target = f"telegram:{chat_id}"
    if thread_id:
        send_target = f"telegram:{chat_id}:{thread_id}"

    try:
        _hermes_send(send_target, body)
        log.info("Delivered to Telegram chat %s", chat_id)
        return {
            "delivered": "telegram",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "status": "sent",
        }
    except Exception as exc:
        log.error(
            "Telegram delivery failed for chat %s: %s — "
            "log file already written",
            chat_id,
            exc,
        )
        return {
            "delivered": "telegram",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "status": "failed",
            "error": str(exc),
        }


# ── Helpers ──────────────────────────────────────────────────────

def _hermes_send(target: str, text: str) -> None:
    """Send a message via the ``hermes send`` CLI command.

    Uses subprocess so the delivery router is decoupled from the
    gateway process. Works from any context that can run ``hermes``.
    Raises ``RuntimeError`` on non-zero exit.
    """
    cmd = [
        _hermes_binary(),
        "send",
        "--to", target,
        "--json",
    ]
    result = subprocess.run(
        cmd,
        input=text,
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hermes send failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _format_message(
    result: Dict[str, Any],
    workflow_name: str,
    run_id: str,
) -> str:
    """Format a workflow result into a concise platform message.

    Keeps the message short enough for a chat window while including
    enough context for operators to understand the run outcome.
    """
    completed = sum(1 for v in result.values() if v == "done")
    failed = sum(
        1
        for v in result.values()
        if v in ("failed", "timed_out", "blocked")
    )
    skipped = sum(1 for v in result.values() if v == "skipped")
    total = len(result)

    lines = [
        f"**Workflow: {workflow_name}**",
        f"Run: `{run_id}`",
        "",
        f"✅ {completed}/{total} done"
        + (f"  ❌ {failed} failed" if failed else "")
        + (f"  ⏭ {skipped} skipped" if skipped else ""),
        "",
    ]

    # Per-node status — only non-done nodes get details
    for node_id, status in sorted(result.items()):
        if status != "done":
            emoji = {
                "failed": "❌",
                "timed_out": "⏰",
                "blocked": "🚫",
                "skipped": "⏭",
            }.get(status, "❓")
            lines.append(f"  {emoji} `{node_id}`: {status}")

    return "\n".join(lines)
