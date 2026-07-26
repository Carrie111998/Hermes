"""Microsoft 365 / Exchange Online adapter using Microsoft Graph."""
from __future__ import annotations

import httpx

from .base import OutgoingEmail, SendResult


class MicrosoftProvider:
    API = "https://graph.microsoft.com/v1.0/me"

    def __init__(self):
        self.credentials: dict = {}
        self.client = httpx.Client(timeout=30)

    def connect_account(self, credentials: dict) -> None:
        self.credentials = dict(credentials)
        if not self.credentials.get("access_token") and self.credentials.get("refresh_token"):
            self.refresh_token()
        if not self.credentials.get("access_token"):
            raise ValueError("Microsoft access_token or refresh_token is required")

    def refresh_token(self) -> None:
        required = ("refresh_token", "client_id", "client_secret")
        if any(not self.credentials.get(key) for key in required):
            raise ValueError("Microsoft refresh requires refresh_token, client_id, and client_secret")
        tenant = self.credentials.get("tenant_id", "common")
        response = self.client.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"grant_type": "refresh_token", "refresh_token": self.credentials["refresh_token"],
                  "client_id": self.credentials["client_id"], "client_secret": self.credentials["client_secret"],
                  "scope": "offline_access Mail.ReadWrite Mail.Send User.Read"},
        )
        response.raise_for_status()
        data = response.json()
        self.credentials["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            self.credentials["refresh_token"] = data["refresh_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.credentials['access_token']}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, **kwargs):
        response = self.client.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code == 401 and self.credentials.get("refresh_token"):
            self.refresh_token()
            response = self.client.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def _message(email: OutgoingEmail) -> dict:
        message = {
            "subject": email.subject,
            "body": {"contentType": "HTML" if "<" in email.body and ">" in email.body else "Text",
                     "content": email.body},
            "toRecipients": [{"emailAddress": {"address": email.to}}],
            "ccRecipients": [{"emailAddress": {"address": address}} for address in email.cc],
        }
        if email.reply_to:
            message["replyTo"] = [{"emailAddress": {"address": email.reply_to}}]
        if email.headers:
            # Graph only accepts custom headers prefixed x- or registered ones,
            # and only on a draft; List-Unsubscribe is accepted here.
            message["internetMessageHeaders"] = [
                {"name": name, "value": value} for name, value in email.headers.items()
            ]
        return message

    def create_draft(self, email: OutgoingEmail) -> SendResult:
        response = self._request("POST", f"{self.API}/messages", json=self._message(email))
        return SendResult(response.json()["id"], "draft")

    def send_email(self, email: OutgoingEmail) -> SendResult:
        draft = self.create_draft(email)
        return self.send_draft(draft.provider_message_id)

    def send_draft(self, draft_id: str) -> SendResult:
        self._request("POST", f"{self.API}/messages/{draft_id}/send")
        return SendResult(draft_id, "sent")

    def get_message_status(self, provider_message_id: str) -> str:
        response = self.client.get(f"{self.API}/messages/{provider_message_id}", headers=self._headers())
        if response.status_code == 404:
            return "accepted"
        response.raise_for_status()
        return "draft" if response.json().get("isDraft") else "sent"

    def list_recent_replies(self) -> list[dict]:
        response = self._request(
            "GET", f"{self.API}/mailFolders/inbox/messages",
            params={"$top": 100, "$select": "id,subject,from,receivedDateTime,internetMessageId"},
        )
        return list(response.json().get("value", []))

    def disconnect_account(self) -> None:
        self.credentials.clear()
        self.client.close()

