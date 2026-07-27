"""Normalize authenticated AgentMail webhook events for objective routing."""

from __future__ import annotations

from typing import Any, Mapping

from hermes_cli import objective_triggers


class AgentMailEventError(ValueError):
    pass


RECEIVED_EVENTS = frozenset(
    {
        "message.received",
        "message.received.spam",
        "message.received.blocked",
        "message.received.unauthenticated",
    }
)


def route_authenticated_event(
    conn,
    *,
    organization_id: str,
    expected_inbox_id: str,
    payload: Mapping[str, Any],
    svix_id: str,
    svix_timestamp: str,
) -> list[str]:
    """Route only a validated, tenant-bound AgentMail event into objectives."""
    event_type = str(payload.get("event_type") or "")
    event_id = str(payload.get("event_id") or "")
    if event_type not in RECEIVED_EVENTS:
        raise AgentMailEventError(f"unsupported AgentMail event: {event_type}")
    if not event_id or not svix_id or not svix_timestamp:
        raise AgentMailEventError("AgentMail event identity evidence is incomplete")
    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise AgentMailEventError("AgentMail received event omitted message")
    inbox_id = str(message.get("inbox_id") or "")
    if inbox_id != expected_inbox_id:
        raise AgentMailEventError("AgentMail event inbox does not match route tenant")
    message_id = str(message.get("message_id") or "")
    if not message_id:
        raise AgentMailEventError("AgentMail event omitted message_id")
    sender = str(message.get("from") or "")
    recipients = message.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    metadata = {
        "provider_event_id": event_id,
        "delivery_id": svix_id,
        "inbox_id": inbox_id,
        "message_id": message_id,
        "thread_id": message.get("thread_id"),
        "sender": sender,
        "recipients": [str(item) for item in recipients],
        "subject": str(message.get("subject") or ""),
        "timestamp": message.get("timestamp"),
        "labels": list(message.get("labels") or []),
        "size": message.get("size"),
        "attachment_metadata": [
            {
                "attachment_id": item.get("attachment_id"),
                "filename": item.get("filename"),
                "content_type": item.get("content_type"),
                "size": item.get("size"),
            }
            for item in (message.get("attachments") or [])
            if isinstance(item, Mapping)
        ],
        "content_omitted": not bool(
            message.get("extracted_text") or message.get("text") or message.get("html")
        ),
        "sender_authentication": (
            "unauthenticated"
            if event_type == "message.received.unauthenticated"
            else "provider_accepted"
        ),
        "classification": event_type.removeprefix("message.received").lstrip(".")
        or "normal",
    }
    content = (
        message.get("extracted_text")
        or message.get("text")
        or message.get("html")
    )
    try:
        return objective_triggers.route_external_event(
            conn,
            organization_id=organization_id,
            source_type="agentmail",
            event_type=event_type,
            source_reference=event_id,
            payload=metadata,
            authentication_evidence={
                "scheme": "svix_hmac_sha256",
                "signature_validated": True,
                "delivery_id": svix_id,
                "signed_timestamp": svix_timestamp,
                "provider_event_id": event_id,
            },
            content=str(content) if content is not None else None,
        )
    except objective_triggers.TriggerError as exc:
        raise AgentMailEventError(str(exc)) from exc
