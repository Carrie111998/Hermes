#!/usr/bin/env python3
"""One-time Telegram group setup for Hermes Event Bus notifications.

Usage:
    python scripts/hermes_telegram_setup.py --chat-id=-100XXXXXXXXXX

Requires:
    - @j4um_bot added to the group as admin
    - Group must have Topics/Forum mode enabled
    - TELEGRAM_BOT_TOKEN set in ~/.hermes/profiles/main/.env
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

logger = logging.getLogger(__name__)

TOPICS = [
    {"key": "alerts", "name": "Alerts & Actions", "icon_color": 0xFF0000},
    {"key": "scout", "name": "Scout / Discoveries", "icon_color": 0x0088FF},
    {"key": "matcher", "name": "Matcher / Scores", "icon_color": 0xFFCC00},
    {"key": "tailor_applier", "name": "Tailor & Applier", "icon_color": 0x00CC66},
    {"key": "tracker", "name": "Tracker / Pipeline", "icon_color": 0x9933FF},
    {"key": "digests", "name": "Digests & Summaries", "icon_color": 0xFFFFFF},
    {"key": "system", "name": "System Health", "icon_color": 0x999999},
    {"key": "agent_comms", "name": "Agent Comms", "icon_color": 0xFF8800},
]


def get_bot_token() -> str:
    """Load bot token from .env file."""
    env_path = Path.home() / ".hermes" / "profiles" / "main" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in .env or environment")
    return token


def telegram_api(token: str, method: str, **params) -> dict:
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = httpx.post(url, json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', data)}")
    return data["result"]


def main():
    parser = argparse.ArgumentParser(description="Set up Telegram forum topics for Hermes")
    parser.add_argument("--chat-id", required=True, help="Telegram group chat ID (e.g., -100XXXXXXXXXX)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    token = get_bot_token()
    chat_id = args.chat_id

    # Verify bot has access
    logger.info("Verifying bot access to group %s...", chat_id)
    try:
        chat = telegram_api(token, "getChat", chat_id=chat_id)
        logger.info("Group: %s (type: %s)", chat.get("title", "?"), chat.get("type", "?"))
    except RuntimeError as e:
        logger.error("Cannot access group: %s", e)
        logger.error("Make sure @j4um_bot is added as admin and Topics are enabled.")
        sys.exit(1)

    # Create forum topics
    topics_config = {"group_chat_id": chat_id, "topics": {}}

    for topic_def in TOPICS:
        logger.info("Creating topic: %s...", topic_def["name"])
        try:
            result = telegram_api(
                token, "createForumTopic",
                chat_id=chat_id,
                name=topic_def["name"],
                icon_color=topic_def["icon_color"],
            )
            thread_id = result["message_thread_id"]
            topics_config["topics"][topic_def["key"]] = {
                "thread_id": thread_id,
                "name": topic_def["name"],
            }
            logger.info("  Created: thread_id=%s", thread_id)
        except RuntimeError as e:
            logger.error("  Failed: %s", e)

    # Save topic registry
    from hermes_constants import get_hermes_home
    telegram_dir = get_hermes_home() / "telegram"
    telegram_dir.mkdir(parents=True, exist_ok=True)

    topics_path = telegram_dir / "topics.json"
    from datetime import datetime, timezone
    topics_config["created_at"] = datetime.now(timezone.utc).isoformat()
    topics_path.write_text(json.dumps(topics_config, indent=2), encoding="utf-8")
    logger.info("\nTopic registry saved to: %s", topics_path)

    # Create default verbosity config
    verbosity = {key: {"mode": "all"} for key in topics_config["topics"]}
    verbosity["system"] = {"mode": "digest_only"}
    verbosity["agent_comms"] = {"mode": "significant_only"}

    verbosity_path = telegram_dir / "verbosity.json"
    verbosity_path.write_text(json.dumps(verbosity, indent=2), encoding="utf-8")
    logger.info("Verbosity config saved to: %s", verbosity_path)

    # Create default quiet hours config
    notifications_dir = get_hermes_home() / "notifications"
    notifications_dir.mkdir(parents=True, exist_ok=True)
    quiet_config = {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
        "queue_file": str(notifications_dir / "quiet_queue.json"),
    }
    quiet_path = notifications_dir / "quiet_hours.json"
    quiet_path.write_text(json.dumps(quiet_config, indent=2), encoding="utf-8")
    logger.info("Quiet hours config saved to: %s", quiet_path)

    # Send test messages
    logger.info("\nSending test messages to each topic...")
    for key, topic in topics_config["topics"].items():
        try:
            telegram_api(
                token, "sendMessage",
                chat_id=chat_id,
                message_thread_id=topic["thread_id"],
                text=f"Hermes Event Bus connected. Topic: {topic['name']}",
            )
            logger.info("  %s: OK", topic["name"])
        except RuntimeError as e:
            logger.error("  %s: FAILED — %s", topic["name"], e)

    # Send + pin welcome message in the General topic (no message_thread_id
    # targets the forum's General topic, visible to everyone who joins).
    logger.info("\nPinning welcome message in General topic...")
    welcome_text = (
        "Hermes Event Bus\n\n"
        "This group is Hermes's control room. Each forum topic receives "
        "a class of events from the agent platform:\n\n"
        "- Alerts & Actions: blockers, interviews, offers, gateway health\n"
        "- Scout / Discoveries: new jobs found\n"
        "- Matcher / Scores: scoring output (highlights when >= 8.75)\n"
        "- Tailor & Applier: resume generation + submission lifecycle\n"
        "- Tracker / Pipeline: stage transitions, follow-ups\n"
        "- Digests & Summaries: 3x/day pipeline digests\n"
        "- System Health: cron lifecycle, agent errors, memory writes\n"
        "- Agent Comms: mirrored mailbox messages\n\n"
        "WhatsApp only receives action-required escalations. Details land here."
    )
    try:
        welcome = telegram_api(
            token, "sendMessage",
            chat_id=chat_id,
            text=welcome_text,
        )
        telegram_api(
            token, "pinChatMessage",
            chat_id=chat_id,
            message_id=welcome["message_id"],
            disable_notification=True,
        )
        logger.info("  Pinned welcome message (id=%s)", welcome["message_id"])
    except RuntimeError as e:
        logger.warning("  Could not pin welcome message: %s", e)

    # Update .env with home channel
    env_path = Path.home() / ".hermes" / "profiles" / "main" / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if "TELEGRAM_HOME_CHANNEL" not in content:
            content += f"\nTELEGRAM_HOME_CHANNEL={chat_id}\n"
            env_path.write_text(content)
            logger.info("\nAdded TELEGRAM_HOME_CHANNEL=%s to .env", chat_id)

    logger.info("\nSetup complete! Restart the gateway to activate notifications.")


if __name__ == "__main__":
    main()
