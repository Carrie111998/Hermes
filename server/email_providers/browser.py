"""Agent-driven webmail adapter for ANY email service without an API.

The Hermes agent opens the tenant's webmail URL in a browser, signs in with
the stored username/password, and drives compose/send (or save-as-draft)
through the provider's own UI — Yandex, Zoho, cPanel/Roundcube, GMX,
anything with a login page. Credentials are encrypted at rest by
``server.crypto.CredentialCipher`` and only decrypted at delivery time.

Approval gating is unchanged: ``server.outreach_service`` only calls this
adapter for an approved, hash-matched revision. The agent decides HOW to
navigate, never WHAT to send.
"""
from __future__ import annotations

import json
import subprocess

from .base import OutgoingEmail, SendResult

# ponytail: one blocking hermes subprocess per delivery (minutes, not ms).
# Move to AgentRunService-managed async runs if webmail volume matters.
TIMEOUT = 900


class BrowserWebmailProvider:
    REQUIRED = ("webmail_url", "username", "password")

    def __init__(self):
        self.credentials: dict = {}

    def connect_account(self, credentials: dict) -> None:
        self.credentials = dict(credentials)
        missing = [key for key in self.REQUIRED if not str(self.credentials.get(key) or "").strip()]
        if missing:
            raise ValueError(f"Browser webmail credentials missing: {', '.join(missing)}")

    def refresh_token(self) -> None:
        return None  # password auth has no token to refresh

    def _run(self, task: dict) -> dict:
        prompt = (
            "You are operating a webmail account through its browser UI on behalf of its owner.\n"
            f"Webmail URL: {self.credentials['webmail_url']}\n"
            f"Username: {self.credentials['username']}\n"
            f"Password: {self.credentials['password']}\n"
            f"Provider hint: {self.credentials.get('provider_hint') or 'unknown — detect from the login page'}\n\n"
            "Follow the webmail-send skill. Sign in, perform exactly this task, sign out:\n"
            f"{json.dumps(task, ensure_ascii=False, indent=2)}\n\n"
            "Send/draft the message VERBATIM — never rewrite subject or body. "
            "If login or the task fails, output JSON {\"error\": \"<reason>\"}. "
            "On success output ONLY the JSON object the skill specifies."
        )
        process = subprocess.run(
            ["hermes", "-z", prompt, "--skills", "webmail-send", "--yolo"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"webmail agent exited with status {process.returncode}: "
                f"{(process.stderr or process.stdout or '').strip()[:2000]}"
            )
        from ..agent_service import extract_json  # lazy: keeps module import light
        output = extract_json(process.stdout)
        if output.get("error"):
            raise RuntimeError(f"webmail agent failed: {str(output['error'])[:2000]}")
        return output

    @staticmethod
    def _task(action: str, email: OutgoingEmail) -> dict:
        return {
            "action": action, "to": email.to, "cc": list(email.cc),
            "subject": email.subject, "body": email.body,
            "reply_to": email.reply_to,
        }

    def send_email(self, email: OutgoingEmail) -> SendResult:
        output = self._run(self._task("send", email))
        return SendResult(str(output.get("provider_message_id") or "webmail-sent"), "sent")

    def create_draft(self, email: OutgoingEmail) -> SendResult:
        output = self._run(self._task("draft", email))
        return SendResult(str(output.get("provider_message_id") or "webmail-draft"), "draft")

    def send_draft(self, draft_id: str) -> SendResult:
        output = self._run({"action": "send_draft", "draft_id": draft_id})
        return SendResult(str(output.get("provider_message_id") or draft_id), "sent")

    def get_message_status(self, provider_message_id: str) -> str:
        return "accepted"  # no cheap post-send check; replies/bounces come from polling

    def list_recent_replies(self) -> list[dict]:
        output = self._run({"action": "list_replies", "days": 30, "max_results": 50})
        replies = output.get("replies")
        return replies if isinstance(replies, list) else []

    def disconnect_account(self) -> None:
        self.credentials.clear()
