"""RCS channel: Twilio Messaging Service (``MessagingServiceSid``) with an
RCS Sender, falling back to SMS/MMS automatically.

Rich content (cards, carousels) via a pre-created Content API template,
referenced with a ``CONTENT:<ContentSid>[:<json vars>]`` directive (see
``scripts/manage_content.py``) — mirrors the ``MEDIA:<path>`` convention
used elsewhere in Hermes.
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.helpers import strip_markdown

from ..core.credentials import get_scoped_secret
from .base import MessagingChannel

# Twilio's documented RCS text limit — re-verify if messages get truncated.
MAX_RCS_LENGTH = 3072

# Not in core's hardcoded _PHONE_PLATFORMS, so this channel parses its own
# E.164 targets (mirrors tools/send_message_tool._E164_TARGET_RE).
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")

# 'CONTENT:<ContentSid>' or 'CONTENT:<ContentSid>:<json ContentVariables>'.
_CONTENT_DIRECTIVE_RE = re.compile(
    r"^CONTENT:(?P<sid>HX[0-9a-fA-F]{32})(?::(?P<vars>.+))?$", re.DOTALL
)


def _parse_content_directive(message: str) -> Optional[Tuple[str, Optional[str]]]:
    """(content_sid, variables_json_or_None), or None if not a CONTENT:
    directive. Raises ValueError on invalid JSON."""
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
