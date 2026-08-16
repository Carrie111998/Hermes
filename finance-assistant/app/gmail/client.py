from __future__ import annotations

import base64
import time
from typing import Any

from .models import GmailAttachment, GmailMessage


class GmailApiError(RuntimeError):
    pass


class GmailClient:
    def __init__(self, service: Any, *, max_retries: int = 3, sleep=time.sleep) -> None:
        self.service = service
        self.max_retries = max_retries
        self.sleep = sleep

    @classmethod
    def from_credentials(cls, credentials: Any) -> "GmailClient":
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailApiError("Gmail API dependencies are not installed") from exc
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    def list_message_ids(self, query: str) -> list[str]:
        messages: list[str] = []
        token = None
        while True:
            kwargs = {"userId": "me", "q": query, "maxResults": 100}
            if token:
                kwargs["pageToken"] = token
            response = self._execute(self.service.users().messages().list(**kwargs))
            messages.extend(item["id"] for item in response.get("messages", []))
            token = response.get("nextPageToken")
            if not token:
                return messages

    def get_message(self, message_id: str) -> GmailMessage:
        response = self._execute(self.service.users().messages().get(userId="me", id=message_id, format="full"))
        attachments: list[GmailAttachment] = []
        index = 0

        def walk(part: dict[str, Any]) -> None:
            nonlocal index
            filename = str(part.get("filename") or "")
            body = part.get("body") or {}
            attachment_id = str(body.get("attachmentId") or "")
            mime_type = str(part.get("mimeType") or "")
            if filename and attachment_id and (mime_type.casefold() == "application/pdf" or filename.casefold().endswith(".pdf")):
                attachments.append(GmailAttachment(attachment_id, filename, mime_type, index))
                index += 1
            for child in part.get("parts") or []:
                walk(child)

        walk(response.get("payload") or {})
        return GmailMessage(str(response["id"]), response.get("internalDate"), tuple(attachments))

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._execute(
            self.service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            )
        )
        try:
            return base64.urlsafe_b64decode(response["data"] + "===")
        except (KeyError, ValueError) as exc:
            raise GmailApiError("Gmail attachment response was invalid") from exc

    def _execute(self, request: Any) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                return request.execute()
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                retryable = status == 429 or (isinstance(status, int) and 500 <= status < 600)
                if not retryable or attempt >= self.max_retries:
                    raise GmailApiError("Gmail API request failed") from exc
                self.sleep(2**attempt)
        raise GmailApiError("Gmail API request failed")
