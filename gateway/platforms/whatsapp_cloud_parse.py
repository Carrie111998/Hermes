"""Pure parse core for WhatsApp Cloud API inbound messages.

Extracted from ``WhatsAppCloudAdapter._build_message_event_from_cloud``
(gateway/platforms/whatsapp_cloud.py) so the field-derivation logic — type
mapping, body extraction, sender/chat resolution, reply context, the
group-shape refusal — is importable WITHOUT adapter state. The adapter
delegates here (single source of truth); the ingress conformance vector
generator (scripts/generate_ingress_vectors.py) renders synthetic Cloud
payloads through this SAME code, making it the executable spec for the
connector's WhatsApp normalizer.

Deliberately excluded (adapter-side, effectful): interactive-reply dispatch,
allow-list/broadcast gating (_should_process_message), media download +
text-document injection, rich_sent_store lookups (quoted TEXT resolution),
and the last-wamid typing cache. The core derives every field that exists
in the payload alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gateway.platforms.base import MessageType

#: Cloud message types that carry a downloadable media object.
CLOUD_MEDIA_TYPES = frozenset(
    {"image", "video", "audio", "voice", "document", "sticker"}
)

#: Cloud ``type`` → Hermes MessageType (verbatim from the adapter).
CLOUD_MESSAGE_TYPE_MAP: Dict[str, MessageType] = {
    "text": MessageType.TEXT,
    "image": MessageType.PHOTO,
    "video": MessageType.VIDEO,
    "audio": MessageType.VOICE,
    "voice": MessageType.VOICE,
    "document": MessageType.DOCUMENT,
    "sticker": MessageType.PHOTO,
    "button": MessageType.TEXT,
    "interactive": MessageType.TEXT,
    "location": MessageType.TEXT,
    "contacts": MessageType.TEXT,
}


@dataclass
class CloudParsedMessage:
    """Payload-derivable fields of one Cloud inbound message."""

    msg_type_str: str
    message_type: MessageType
    body: str
    sender_id: str
    sender_name: str
    chat_id: str
    wamid: Optional[str]
    reply_to_id: Optional[str]
    reply_to_is_own: bool
    #: Media object ``id`` + ``mime_type`` when present (download is the
    #: adapter's job; the core only derives WHAT to download).
    media_id: Optional[str] = None
    media_mime: Optional[str] = None
    document_filename: Optional[str] = None
    #: True when the payload is group-shaped (``chat`` field present) —
    #: the Cloud adapter REFUSES these (Baileys handles groups).
    group_shaped: bool = False
    contacts_by_waid: Dict[str, str] = field(default_factory=dict)


def parse_cloud_message(
    raw_message: Dict[str, Any],
    contacts_by_waid: Dict[str, str],
    metadata: Dict[str, Any],
) -> CloudParsedMessage:
    """Derive every payload-only field of a Cloud inbound message.

    Pure: no I/O, no adapter state. Mirrors the extraction order of
    ``_build_message_event_from_cloud`` exactly; behavior changes here MUST
    be reflected in the committed ingress conformance vectors (regenerate)
    and reviewed against the connector's normalizer.
    """
    msg_type_str = str(raw_message.get("type") or "text").lower()

    body = ""
    if msg_type_str == "text":
        text = raw_message.get("text") or {}
        body = str(text.get("body") or "")
    elif msg_type_str in {"button", "interactive"}:
        if msg_type_str == "button":
            body = str((raw_message.get("button") or {}).get("text") or "")
        else:
            inter = raw_message.get("interactive") or {}
            inner = inter.get("button_reply") or inter.get("list_reply") or {}
            body = str(inner.get("title") or "")
    elif msg_type_str in CLOUD_MEDIA_TYPES:
        inner = raw_message.get(msg_type_str) or {}
        body = str(inner.get("caption") or "")

    message_type = CLOUD_MESSAGE_TYPE_MAP.get(msg_type_str, MessageType.TEXT)

    sender_id = str(raw_message.get("from") or "").strip()
    sender_name = contacts_by_waid.get(sender_id, "")
    chat_id = sender_id

    group_shaped = bool(raw_message.get("chat"))

    media_id: Optional[str] = None
    media_mime: Optional[str] = None
    document_filename: Optional[str] = None
    if msg_type_str in CLOUD_MEDIA_TYPES:
        inner = raw_message.get(msg_type_str) or {}
        media_id = str(inner.get("id") or "").strip() or None
        media_mime = str(inner.get("mime_type") or "").strip() or None
        if msg_type_str == "document":
            document_filename = str(inner.get("filename") or "").strip() or None

    context = raw_message.get("context") or {}
    reply_to_id = str(context.get("id") or "").strip() or None
    reply_to_is_own = False
    if reply_to_id:
        quoted_from = str(context.get("from") or "").strip()
        our_number = str(metadata.get("display_phone_number") or "").strip()
        if quoted_from and our_number:
            reply_to_is_own = quoted_from == our_number

    wamid = str(raw_message.get("id") or "") or None

    return CloudParsedMessage(
        msg_type_str=msg_type_str,
        message_type=message_type,
        body=body,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        wamid=wamid,
        reply_to_id=reply_to_id,
        reply_to_is_own=reply_to_is_own,
        media_id=media_id,
        media_mime=media_mime,
        document_filename=document_filename,
        group_shaped=group_shaped,
        contacts_by_waid=dict(contacts_by_waid),
    )


def document_fallback_body(parsed: CloudParsedMessage) -> str:
    """The ``[Document: name]`` placeholder used when a document has no
    caption — same rule as the adapter (applied only after a successful
    media download, which is adapter-side)."""
    if (
        parsed.msg_type_str == "document"
        and not parsed.body
        and parsed.document_filename
    ):
        return f"[Document: {parsed.document_filename}]"
    return parsed.body
