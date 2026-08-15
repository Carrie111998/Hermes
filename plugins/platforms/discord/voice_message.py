"""Discord native voice-message container validation (pure logic, no audio processing).

Discord native voice messages are OGG containers (Opus codec) uploaded with
content type ``audio/ogg`` or ``application/ogg``.  This module decides, from
metadata alone, whether an attachment may be treated as a native Discord voice
message or must fall back to a normal attachment.  It never inspects or
transcodes audio bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

# Content types Discord serves native voice messages with.
NATIVE_OGG_CONTENT_TYPES = frozenset({"audio/ogg", "application/ogg"})

# Discord's practical cap for voice-message uploads (25 MiB).
DEFAULT_MAX_BYTES = 25 * 1024 * 1024


class VoiceMessageError(ValueError):
    """Raised when an attachment cannot be handled as a native voice message."""


@dataclass(frozen=True)
class VoiceAttachment:
    """Metadata describing a single attachment (no binary payload)."""

    filename: str
    size_bytes: int | None = None
    content_type: str | None = None


def is_native_ogg(filename: str) -> bool:
    """Return True if *filename* ends in ``.ogg`` (case-insensitive)."""
    return filename.lower().endswith(".ogg")


def validate_native_voice_attachment(att: VoiceAttachment) -> str:
    """Return *att.filename* if the attachment is already a Discord-native OGG.

    A native voice message must have an ``.ogg`` extension and, when a content
    type is present, it must be ``audio/ogg`` or ``application/ogg``.  Any
    other combination (e.g. an MP3 or WAV renamed to ``.ogg``, or a plain
    MP3/WAV) raises :class:`VoiceMessageError`: arbitrary audio cannot be
    renamed and relabeled as a native voice message -- it must travel as a
    normal attachment instead.
    """
    if not is_native_ogg(att.filename):
        raise VoiceMessageError(
            f"attachment {att.filename!r} is not a native Discord voice message: "
            f"expected an .ogg container"
        )
    if (
        att.content_type is not None
        and att.content_type not in NATIVE_OGG_CONTENT_TYPES
    ):
        raise VoiceMessageError(
            f"attachment {att.filename!r} cannot be labeled as a native voice "
            f"message: content type {att.content_type!r} is not Discord-native OGG"
        )
    return att.filename


def voice_attachment_policy(
    att: VoiceAttachment, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Return the routing policy for *att*: ``'native'`` or ``'normal_attachment'``.

    - ``'native'``: the attachment is valid native OGG (extension + content
      type, when given) and its size is within *max_bytes*.
    - ``'normal_attachment'``: the attachment is not native OGG -- it falls
      back to a normal attachment (never a silent drop).
    - :class:`VoiceMessageError` is raised when the size exceeds *max_bytes*.
    """
    if att.size_bytes is not None and att.size_bytes > max_bytes:
        raise VoiceMessageError(
            f"attachment {att.filename!r} is {att.size_bytes} bytes, "
            f"exceeding the {max_bytes}-byte voice-message limit"
        )
    if is_native_ogg(att.filename) and (
        att.content_type is None or att.content_type in NATIVE_OGG_CONTENT_TYPES
    ):
        return "native"
    return "normal_attachment"


def enforce_single_audio(attachments: list) -> None:
    """Raise :class:`VoiceMessageError` if *attachments* holds >1 audio file.

    Audio attachments are identified by a content type starting with
    ``audio/``.  A single audio attachment (or none) is allowed.
    """
    audio = [
        a for a in attachments
        if a.content_type is not None and a.content_type.startswith("audio/")
    ]
    if len(audio) > 1:
        raise VoiceMessageError(
            f"expected at most one audio attachment per message, "
            f"got {len(audio)}"
        )
