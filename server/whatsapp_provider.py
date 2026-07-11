"""WhatsApp Business Platform / Cloud API adapter."""
from __future__ import annotations

import httpx


class WhatsAppCloudProvider:
    def __init__(self, credentials: dict):
        self.credentials = credentials
        self.graph_version = credentials.get("graph_version", "v23.0")
        self.client = httpx.Client(timeout=45)

    @property
    def base(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}/{self.credentials['phone_number_id']}"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.credentials['access_token']}",
                "Content-Type": "application/json"}

    def test(self) -> dict:
        response = self.client.get(self.base, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def send_template(self, to: str, template_name: str, language: str,
                      components: list[dict] | None = None) -> dict:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": template_name, "language": {"code": language}}}
        if components:
            payload["template"]["components"] = components
        response = self.client.post(f"{self.base}/messages", headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    def send_text(self, to: str, body: str) -> dict:
        response = self.client.post(
            f"{self.base}/messages", headers=self.headers,
            json={"messaging_product": "whatsapp", "to": to, "type": "text",
                  "text": {"preview_url": False, "body": body}},
        )
        response.raise_for_status()
        return response.json()

