"""RCS channel for the Twilio plugin.

Sends through a Twilio Messaging Service (``MessagingServiceSid``) that
has an RCS Sender (approved by Google) attached — Twilio auto-selects RCS
for capable recipients and falls back to SMS/MMS otherwise.

Rich content (RCS cards, carousels) is sent by referencing a pre-created
Content API template through a ``CONTENT:<ContentSid>[:<json vars>]``
directive in the message text (mirrors the existing ``MEDIA:<path>``
convention used elsewhere in Hermes cross-platform messaging). Create
templates with ``scripts/manage_content.py``.

Self-contained: nothing outside this file needs to change to modify RCS
behavior, and this file never reaches into another channel's module.
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.helpers import strip_markdown

from ..core.credentials import get_scoped_secret
from .base import MessagingChannel

# Twilio's documented RCS text body limit — verify against current Twilio
# docs if recipients start seeing truncated messages.
MAX_RCS_LENGTH = 3072

# Mirrors tools/send_message_tool._E164_TARGET_RE — this platform isn't in
# core's hardcoded _PHONE_PLATFORMS set, so it must declare its own parser
# to accept bare E.164 numbers as targets.
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")

# 'CONTENT:<ContentSid>' or 'CONTENT:<ContentSid>:<json ContentVariables>' —
# references a Content API template created via scripts/manage_content.py.
_CONTENT_DIRECTIVE_RE = re.compile(
    r"^CONTENT:(?P<sid>HX[0-9a-fA-F]{32})(?::(?P<vars>.+))?$", re.DOTALL
)


def _parse_content_directive(message: str) -> Optional[Tuple[str, Optional[str]]]:
    """Return (content_sid, content_variables_json_or_None), or None if
    `message` isn't a CONTENT: directive. Raises ValueError if the
    variables aren't valid JSON."""
    match = _CONTENT_DIRECTIVE_RE.match(message.strip())
    if not match:
        return None
    raw_vars = match.group("vars")
    if raw_vars is not None:
        try:
            json.loads(raw_vars)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid ContentVariables JSON in CONTENT: directive: {e}")
    return match.group("sid"), raw_vars


class RcsChannel(MessagingChannel):
    name = "rcs"
    max_message_length = MAX_RCS_LENGTH
    required_env = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID"]
    cron_deliver_env_var = "TWILIO_RCS_HOME_CHANNEL"
    platform_hint = (
        "You are sending via Twilio RCS (with automatic SMS/MMS fallback). "
        "Plain text only — no markdown."
    )

    def check_requirements(self) -> bool:
        return bool(
            get_scoped_secret("TWILIO_ACCOUNT_SID")
            and get_scoped_secret("TWILIO_AUTH_TOKEN")
            and os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
        )

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        if not os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip():
            return False, (
                "TWILIO_MESSAGING_SERVICE_SID not set — cannot send. Attach an "
                "RCS Sender to a Messaging Service in the Twilio Console and "
                "set its SID here."
            )
        return True, None

    def is_connected(self) -> bool:
        return bool(os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()) and bool(
            (get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
        )

    def parse_target_ref(self, target_ref: str):
        match = _E164_TARGET_RE.fullmatch(target_ref)
        if match:
            return target_ref.strip(), None
        return None

    def validate_target_ref(self, chat_id: str):
        return True if _E164_TARGET_RE.fullmatch(chat_id) else "not a valid E.164 phone number"

    def format_message(self, content: str) -> str:
        """Strip markdown — the RCS/SMS Body field renders it as literal characters."""
        return strip_markdown(content)

    def build_send_requests(
        self, chat_id: str, content: str, messaging_service_sid: str
    ) -> List[Dict[str, str]]:
        directive = _parse_content_directive(content)
        if directive:
            content_sid, content_variables = directive
            fields = {
                "MessagingServiceSid": messaging_service_sid,
                "To": chat_id,
                "ContentSid": content_sid,
            }
            if content_variables:
                fields["ContentVariables"] = content_variables
            return [fields]

        formatted = self.format_message(content)
        chunks = BasePlatformAdapter.truncate_message(formatted, max_length=self.max_message_length)
        return [
            {"MessagingServiceSid": messaging_service_sid, "To": chat_id, "Body": chunk}
            for chunk in chunks
        ]
