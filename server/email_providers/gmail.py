"""Google Workspace/Gmail API adapter."""
from __future__ import annotations

import base64
from email.message import EmailMessage

import httpx

from .base import OutgoingEmail, SendResult


class GmailProvider:
    API = "https://gmail.googleapis.com/gmail/v1/users/me"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self):
        self.credentials: dict = {}
        self.client = httpx.Client(timeout=30)

    def connect_account(self, credentials: dict) -> None:
        self.credentials = dict(credentials)
        if not self.credentials.get("access_token") and self.credentials.get("refresh_token"):
            self.refresh_token()
        if not self.credentials.get("access_token"):
            raise ValueError("Gmail access_token or refresh_token is required")

    def refresh_token(self) -> None:
        required = ("refresh_token", "client_id", "client_secret")
        if any(not self.credentials.get(key) for key in required):
            raise ValueError("Gmail refresh requires refresh_token, client_id, and client_secret")
        response = self.client.post(self.TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": self.credentials["refresh_token"],
            "client_id": self.credentials["client_id"], "client_secret": self.credentials["client_secret"],
        })
        response.raise_for_status()
        self.credentials["access_token"] = response.json()["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.credentials['access_token']}"}

    def _request(self, method: str, url: str, **kwargs):
        response = self.client.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code == 401 and self.credentials.get("refresh_token"):
            self.refresh_token()
            response = self.client.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def _raw(email: OutgoingEmail) -> str:
        message = EmailMessage()
        message["To"] = email.to
        if email.cc:
            message["Cc"] = ", ".join(email.cc)
        if email.reply_to:
            message["Reply-To"] = email.reply_to
        message["Subject"] = email.subject
        for name, value in email.headers.items():
            message[name] = value
        subtype = "html" if "<" in email.body and ">" in email.body else "plain"
        message.set_content(email.body, subtype=subtype)
        return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")

    def create_draft(self, email: OutgoingEmail) -> SendResult:
        response = self._request("POST", f"{self.API}/drafts", json={"message": {"raw": self._raw(email)}})
        return SendResult(response.json()["id"], "draft")

    def send_email(self, email: OutgoingEmail) -> SendResult:
        response = self._request("POST", f"{self.API}/messages/send", json={"raw": self._raw(email)})
        return SendResult(response.json()["id"], "sent")

    def send_draft(self, draft_id: str) -> SendResult:
        response = self._request("POST", f"{self.API}/drafts/send", json={"id": draft_id})
        return SendResult(response.json()["id"], "sent")

    def get_message_status(self, provider_message_id: str) -> str:
        response = self._request("GET", f"{self.API}/messages/{provider_message_id}",
                                 params={"format": "minimal"})
        labels = set(response.json().get("labelIds", []))
        return "sent" if "SENT" in labels else "delivered" if "INBOX" in labels else "accepted"

    def list_recent_replies(self) -> list[dict]:
        response = self._request("GET", f"{self.API}/messages", params={"q": "in:inbox newer_than:30d", "maxResults": 100})
        messages = []
        for item in response.json().get("messages", [])[:50]:
            detail = self._request(
                "GET", f"{self.API}/messages/{item['id']}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Message-ID"]},
            ).json()
            headers = {header["name"].lower(): header["value"]
                       for header in detail.get("payload", {}).get("headers", [])}
            messages.append({"id": item["id"], "thread_id": item.get("threadId"),
                             "from": headers.get("from", ""), "subject": headers.get("subject", ""),
                             "internet_message_id": headers.get("message-id")})
        return messages

    def disconnect_account(self) -> None:
        self.credentials.clear()
        self.client.close()
