from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import cast

from .context_pack import _redact
from .models import (
    InvalidBridgeMarker,
    OriginKind,
    HydrationMarkerPayload,
    Provider,
    SessionPreview,
    SessionProjection,
    canonical_session_id,
    decode_bridge_marker,
)
from .sidebar_placement import filesystem_path_identity


ACK_OR_CONTROL_ONLY = frozenset({
    "ok",
    "okay",
    "yes",
    "y",
    "ready",
    "try again",
    "resume",
    "/resume",
    "clear",
    "/clear",
    "help",
    "/help",
    "quit",
    "/quit",
})
_INTERNAL_USER_EVENT_PREFIXES = (
    "[System: The active model for this chat has changed to ",
    "[IMPORTANT: Background process ",
)

_TITLE_PREFIXES = {
    Provider.CLAUDE: "[Claude] ",
    Provider.HERMES: "[Hermes] ",
}
_MAX_TITLE_CHARS = 120
_UNICODE_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1"
_HYDRATION_MARKER_PREFIX = "HERMES_SESSION_HYDRATION_V1"
_MARKER_FIELDS = frozenset({
    "bridge_id",
    "policy_generation",
    "source_session_id",
    "target_provider",
})
_HYDRATION_MARKER_FIELDS = frozenset({
    "bridge_id",
    "codex_thread_id",
    "preview_digest",
    "preview_version",
    "source_cursor",
    "source_hash",
    "source_session_id",
})
_BASE64URL_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_CHARACTERIZATION_TITLE_RE = re.compile(
    r"^\[Hermes Bridge Characterization\] "
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PREVIEW_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_READABLE_REGISTRATION_DELIMITER = "\n\n## Bridge Registration\n"


@dataclass(frozen=True)
class SidebarCandidate:
    source_session_id: str
    provider: Provider
    bridge_id: str
    title: str
    cwd: str
    git_root: str | None
    git_branch: str | None
    git_head: str | None
    worktree_id: str | None
    eligible_at: float


@dataclass(frozen=True)
class SidebarRegistrationIdentity:
    source_session_id: str
    source_cwd: str
    bridge_id: str


@dataclass(frozen=True)
class VerifiedSidebarThread:
    thread_id: str
    source_session_id: str
    bridge_id: str
    projection: SessionProjection | None = field(
        default=None,
        compare=False,
        repr=False,
    )


class SidebarInitialPromptKind(StrEnum):
    LEGACY_PLACEHOLDER = "legacy_placeholder"
    READABLE_REGISTRATION = "readable_registration"
    UNRELATED = "unrelated"


def normalize_meaningful_user_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


def is_meaningful_user_text(value: object) -> bool:
    if isinstance(value, str) and value.startswith(_INTERNAL_USER_EVENT_PREFIXES):
        return False
    normalized = normalize_meaningful_user_text(value)
    if normalized is None or normalized.casefold() in ACK_OR_CONTROL_ONLY:
        return False
    return sum(character.isalnum() for character in normalized) >= 3


def is_sidebar_session_eligible(
    projection: SessionProjection,
    *,
    now: float,
    backfill_days: int | None = 30,
    automation_only: bool = False,
    subagent_only: bool = False,
) -> bool:
    _validate_finite_timestamp(now, "now")
    if backfill_days is not None and (
        not isinstance(backfill_days, int)
        or isinstance(backfill_days, bool)
        or backfill_days < 0
    ):
        raise ValueError("backfill days must be a non-negative integer")
    _validate_finite_timestamp(projection.last_active, "projection last_active")
    if (
        projection.provider not in (Provider.CLAUDE, Provider.HERMES)
        or projection.origin_kind is not OriginKind.NATIVE
        or automation_only
        or subagent_only
    ):
        return False
    if (
        backfill_days is not None
        and projection.last_active < now - backfill_days * 86_400
    ):
        return False
    if (
        projection.provider is Provider.CLAUDE
        and isinstance(projection.title, str)
        and _CHARACTERIZATION_TITLE_RE.fullmatch(projection.title) is not None
    ):
        return False

    for message in projection.messages:
        if message.role != "user" or not isinstance(message.content, str):
            continue
        if is_registration_prompt(message.content):
            continue
        if is_meaningful_user_text(message.content):
            return True
    return False


def sidebar_idempotency_key(source_session_id: str) -> str:
    source = _validated_source_session_id(source_session_id)
    return f"codex-sidebar:{source}:v1"


def sidebar_bridge_id(source_session_id: str) -> str:
    key = sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"sidebar:{digest}"


def sidebar_create_recovery_key(marker: str, marker_secret: bytes) -> str:
    """Derive the durable native-create reservation key for one signed marker."""

    if (
        type(marker) is not str
        or not marker
        or marker != marker.strip()
        or "\n" in marker
        or "\r" in marker
    ):
        raise ValueError("sidebar marker must be canonical")
    if type(marker_secret) is not bytes or not marker_secret:
        raise ValueError("sidebar marker secret must be non-empty bytes")
    digest = hmac.new(
        marker_secret,
        b"sidebar-create-v1\0" + marker.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hermes-session-bridge-create-v1:{digest}"


def validate_sidebar_create_reservation(
    reservation: object,
    *,
    job_id: str,
    source_session_id: str,
    bridge_id: str,
    expected_recovery_key: str,
) -> str:
    """Validate the exact durable reservation record returned by the store."""

    base_fields = {
        "version",
        "job_id",
        "source_session_id",
        "bridge_id",
        "recovery_key",
        "reserved_at",
    }
    if not isinstance(reservation, Mapping):
        raise ValueError("sidebar create reservation is malformed")
    reservation_map = cast(Mapping[str, object], reservation)
    version = reservation_map.get("version")
    expected_fields = (
        base_fields
        if version == 1
        else base_fields
        | {"reconciliation_proof_digest", "reconciliation_generation"}
        if version == 2
        else set()
    )
    if type(version) is not int or set(reservation_map) != expected_fields:
        raise ValueError("sidebar create reservation is malformed")
    if (
        reservation_map.get("job_id") != job_id
        or reservation_map.get("source_session_id") != source_session_id
        or reservation_map.get("bridge_id") != bridge_id
    ):
        raise ValueError("sidebar create reservation identity mismatch")
    recovery_key = reservation_map.get("recovery_key")
    if (
        type(recovery_key) is not str
        or not recovery_key
        or recovery_key != recovery_key.strip()
        or "\n" in recovery_key
        or "\r" in recovery_key
    ):
        raise ValueError("sidebar create reservation is malformed")
    reserved_at = reservation_map.get("reserved_at")
    if (
        isinstance(reserved_at, bool)
        or not isinstance(reserved_at, (int, float))
        or not math.isfinite(float(reserved_at))
    ):
        raise ValueError("sidebar create reservation is malformed")
    if not hmac.compare_digest(recovery_key, expected_recovery_key):
        raise ValueError("sidebar create reservation key mismatch")
    if version == 2:
        proof_digest = reservation_map.get("reconciliation_proof_digest")
        generation = reservation_map.get("reconciliation_generation")
        if (
            type(proof_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", proof_digest) is None
            or type(generation) is not str
            or not generation
            or generation != generation.strip()
            or "\n" in generation
            or "\r" in generation
        ):
            raise ValueError("sidebar create reservation is malformed")
    return recovery_key


def sidebar_title(provider: Provider, title: str | None, first_request: str) -> str:
    if not isinstance(provider, Provider) or provider not in _TITLE_PREFIXES:
        raise ValueError("sidebar title provider must be Claude or Hermes")
    if title is not None and not isinstance(title, str):
        raise ValueError("sidebar title must be a string or None")

    source = normalize_meaningful_user_text(title)
    if source is None:
        source = normalize_meaningful_user_text(first_request)
    if source is None:
        raise ValueError("sidebar title source must not be empty")

    compact = _compact_whitespace(_redact(source))
    if not compact:
        raise ValueError("sidebar title source must not be empty")
    prefix = _TITLE_PREFIXES[provider]
    return prefix + compact[: _MAX_TITLE_CHARS - len(prefix)].rstrip()


def encode_hydration_marker(
    payload: HydrationMarkerPayload,
    secret: bytes,
) -> str:
    normalized = _validated_hydration_payload(payload)
    secret_bytes = _validated_marker_secret(secret)
    body = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    encoded_body = base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        secret_bytes,
        encoded_body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = (
        base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )
    return f"{_HYDRATION_MARKER_PREFIX}:{encoded_body}.{encoded_signature}"


def decode_hydration_marker(
    marker: str,
    secret: bytes,
) -> HydrationMarkerPayload:
    secret_bytes = _validated_marker_secret(secret)
    try:
        if not isinstance(marker, str) or marker.count(":") != 1:
            raise ValueError
        prefix, encoded_and_signature = marker.split(":", 1)
        if (
            prefix != _HYDRATION_MARKER_PREFIX
            or encoded_and_signature.count(".") != 1
        ):
            raise ValueError
        encoded_body, encoded_signature = encoded_and_signature.split(".", 1)
        body = _decode_canonical_base64url(encoded_body)
        signature = _decode_canonical_base64url(encoded_signature)
        if len(signature) != hashlib.sha256().digest_size:
            raise ValueError
        expected = hmac.new(
            secret_bytes,
            encoded_body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != _HYDRATION_MARKER_FIELDS:
            raise ValueError
        canonical = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if canonical != body:
            raise ValueError
        normalized = _validated_hydration_payload(
            HydrationMarkerPayload(**decoded)
        )
        return HydrationMarkerPayload(**normalized)
    except (
        binascii.Error,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError("hydration marker is malformed or unauthenticated") from exc


def _validated_hydration_payload(
    payload: HydrationMarkerPayload,
) -> dict[str, object]:
    if not isinstance(payload, HydrationMarkerPayload):
        raise ValueError("hydration marker payload is malformed")
    source_id = _validated_source_session_id(payload.source_session_id)
    if payload.bridge_id != sidebar_bridge_id(source_id):
        raise ValueError("hydration marker bridge identity mismatch")
    values = {
        "bridge_id": payload.bridge_id,
        "codex_thread_id": payload.codex_thread_id,
        "preview_digest": payload.preview_digest,
        "preview_version": payload.preview_version,
        "source_cursor": payload.source_cursor,
        "source_hash": payload.source_hash,
        "source_session_id": source_id,
    }
    for field in (
        "bridge_id",
        "codex_thread_id",
        "source_cursor",
        "source_hash",
    ):
        value = values[field]
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or _has_line_break(value)
        ):
            raise ValueError(f"hydration marker {field} is malformed")
    if payload.preview_version != 1:
        raise ValueError("hydration marker preview version must be 1")
    if _PREVIEW_DIGEST_RE.fullmatch(payload.preview_digest) is None:
        raise ValueError(
            "hydration marker preview digest must be lowercase SHA-256"
        )
    return values


def _validated_marker_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("hydration marker secret must be nonempty bytes")
    return secret


def build_registration_prompt(
    candidate: SidebarCandidate,
    marker: str,
    *,
    preview: SessionPreview | None = None,
) -> str:
    _validate_candidate(candidate)
    _validate_registration_marker(candidate, marker)

    if _redact(candidate.source_session_id) != candidate.source_session_id:
        raise ValueError("source session ID cannot be represented safely")
    source = json.dumps(
        candidate.source_session_id,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    provider = _serialized_metadata(candidate.provider.value)
    cwd = _serialized_metadata(candidate.cwd)
    git_root = _serialized_metadata(candidate.git_root)
    git_branch = _serialized_metadata(candidate.git_branch)
    git_head = _serialized_metadata(candidate.git_head)
    worktree_id = _serialized_metadata(candidate.worktree_id)

    legacy_prompt = "\n".join((
        "This is a Hermes Session Bridge placeholder registration.",
        "Do not perform project work during registration.",
        f"Signed marker: {marker}",
        f"Source session ID: {source}",
        f"Source provider: {provider}",
        f"Source cwd: {cwd}",
        f"Git root: {git_root}",
        f"Git branch: {git_branch}",
        f"Git HEAD: {git_head}",
        f"Worktree ID: {worktree_id}",
        "Do not call session_continue during this registration turn.",
        "On the first later substantive user message, call "
        f'session_continue(session_id={source}, target_provider="codex").',
        "Until that later user message, reply with only: REGISTERED",
    ))
    if preview is None:
        return legacy_prompt

    _validate_preview(candidate, preview)
    preview_cursor = _serialized_preview_identity(
        preview.source_cursor,
        "preview source cursor",
    )
    preview_hash = _serialized_preview_identity(
        preview.source_hash,
        "preview source hash",
    )
    bridge_block = "\n".join((
        f"Preview version: {preview.version}",
        f"Preview source cursor: {preview_cursor}",
        f"Preview source hash: {preview_hash}",
        f"Preview digest: {preview.digest}",
        legacy_prompt,
    ))
    return (
        preview.rendered.rstrip("\n")
        + "\n\n## Bridge Registration\n"
        + bridge_block
    )


def is_registration_prompt(value: object) -> bool:
    """Recognize exact legacy or digest-bound readable registration prompts."""
    if _is_exact_registration_block(value):
        return True
    if not isinstance(value, str) or value.count(_READABLE_REGISTRATION_DELIMITER) != 1:
        return False
    rendered_without_newline, bridge_block = value.split(
        _READABLE_REGISTRATION_DELIMITER,
        1,
    )
    bridge_lines = bridge_block.split("\n")
    if len(bridge_lines) != 17:
        return False
    try:
        version = _prompt_line_value(bridge_lines[0], "Preview version: ")
        if version != "1":
            return False
        cursor = _decode_canonical_prompt_field(
            bridge_lines[1],
            "Preview source cursor: ",
        )
        source_hash = _decode_canonical_prompt_field(
            bridge_lines[2],
            "Preview source hash: ",
        )
        digest = _prompt_line_value(bridge_lines[3], "Preview digest: ")
        if (
            not isinstance(cursor, str)
            or not cursor
            or not isinstance(source_hash, str)
            or not source_hash
            or _PREVIEW_DIGEST_RE.fullmatch(digest) is None
        ):
            return False
        rendered = rendered_without_newline + "\n"
        if not rendered.startswith("# Imported "):
            return False
        if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != digest:
            return False
        return _is_exact_registration_block("\n".join(bridge_lines[4:]))
    except (TypeError, ValueError):
        return False


def classify_sidebar_initial_prompt(
    value: object,
    marker_secret: bytes,
) -> SidebarInitialPromptKind:
    """Classify only structurally exact registrations with an authentic marker."""

    if type(marker_secret) is not bytes or not marker_secret:
        raise ValueError("sidebar marker secret must be non-empty bytes")
    exact = _exact_registration_and_kind(value)
    if exact is None:
        return SidebarInitialPromptKind.UNRELATED
    try:
        decode_sidebar_registration_identity(value, marker_secret)
    except ValueError:
        return SidebarInitialPromptKind.UNRELATED
    return exact[1]


def decode_sidebar_registration_identity(
    prompt: object,
    marker_secret: bytes,
) -> SidebarRegistrationIdentity:
    """Decode exact registration metadata only after marker authentication."""

    try:
        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError
        exact = _exact_registration_and_kind(prompt)
        if exact is None:
            raise ValueError
        registration, _kind = exact
        lines = registration.split("\n")
        marker = _prompt_line_value(lines[2], "Signed marker: ")
        source = _decode_canonical_prompt_field(lines[3], "Source session ID: ")
        source_cwd = _decode_canonical_prompt_field(lines[5], "Source cwd: ")
        payload = decode_bridge_marker(marker, marker_secret)
        if (
            not isinstance(source, str)
            or not isinstance(source_cwd, str)
            or (
                filesystem_path_identity(source_cwd, platform="windows") is None
                and filesystem_path_identity(source_cwd, platform="posix") is None
            )
            or payload.source_session_id != source
            or payload.bridge_id != sidebar_bridge_id(source)
            or payload.target_provider is not Provider.CODEX
            or payload.policy_generation != 1
        ):
            raise ValueError
        return SidebarRegistrationIdentity(
            source_session_id=source,
            source_cwd=source_cwd,
            bridge_id=payload.bridge_id,
        )
    except (InvalidBridgeMarker, TypeError, ValueError) as exc:
        raise ValueError(
            "sidebar registration identity is malformed or unauthenticated"
        ) from exc


def build_hydration_message(
    *,
    preview_rendered: str | None,
    source_session_id: str,
    hydration_marker: str,
    send_reserved: bool,
) -> str:
    source_id = _validated_source_session_id(source_session_id)
    marker = _exact_single_line_text(hydration_marker, "hydration marker")
    if type(send_reserved) is not bool:
        raise ValueError("hydration send reservation flag is malformed")
    if preview_rendered is None:
        if not send_reserved:
            raise ValueError("unreserved hydration requires a readable preview")
        readable = (
            "# Session Bridge Hydration Reconciliation\n\n"
            "The readable hydration was already reserved for this exact task. "
            "Reconcile the authenticated marker; do not send it again."
        )
    else:
        if (
            not isinstance(preview_rendered, str)
            or not preview_rendered.startswith("# Imported ")
            or not preview_rendered.endswith("\n")
        ):
            raise ValueError("hydration preview is malformed")
        readable = preview_rendered.rstrip("\n")
    return "\n".join((
        readable,
        "",
        "## In-place Session Bridge Hydration",
        "",
        "This is an authenticated in-place Session Bridge hydration.",
        "Do not perform project work during this maintenance turn.",
        "Do not call session_continue during this maintenance turn.",
        f"Hydration marker: {marker}",
        "After the marker is recorded, reply only: HYDRATED",
    ))


def _exact_single_line_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _has_line_break(value)
    ):
        raise ValueError(f"{label} must be canonical")
    return value


def _validate_preview(candidate: SidebarCandidate, preview: SessionPreview) -> None:
    if not isinstance(preview, SessionPreview):
        raise ValueError("session preview is malformed")
    if (
        preview.version != 1
        or preview.source_session_id != candidate.source_session_id
        or not isinstance(preview.source_cursor, str)
        or not preview.source_cursor.strip()
        or not isinstance(preview.source_hash, str)
        or not preview.source_hash.strip()
        or preview.source_cursor != preview.source_cursor.strip()
        or preview.source_hash != preview.source_hash.strip()
        or _redact(preview.source_cursor) != preview.source_cursor
        or _redact(preview.source_hash) != preview.source_hash
        or not isinstance(preview.rendered, str)
        or not preview.rendered.endswith("\n")
        or _READABLE_REGISTRATION_DELIMITER in preview.rendered
        or len(preview.rendered) > preview.budget_chars
        or len(preview.recent_messages) > 5
        or _PREVIEW_DIGEST_RE.fullmatch(preview.digest) is None
        or hashlib.sha256(preview.rendered.encode("utf-8")).hexdigest()
        != preview.digest
    ):
        raise ValueError("session preview is malformed or mismatched")
    expected_header = (
        "# Imported Claude Code Session"
        if candidate.provider is Provider.CLAUDE
        else "# Imported Hermes Session"
    )
    if not preview.rendered.startswith(expected_header):
        raise ValueError("session preview is malformed or mismatched")


def _serialized_preview_identity(value: str, label: str) -> str:
    if not value or value != value.strip() or _has_line_break(value):
        raise ValueError(f"{label} must be canonical")
    if _redact(value) != value:
        raise ValueError(f"{label} cannot be represented safely")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _validated_source_session_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source session ID must not be empty")
    if value != value.strip() or _has_line_break(value):
        raise ValueError("source session ID must be canonical")
    if value.startswith(f"{Provider.CLAUDE.value}:"):
        provider = Provider.CLAUDE
        native_id = value.removeprefix(f"{Provider.CLAUDE.value}:")
    else:
        provider = Provider.HERMES
        native_id = value
    try:
        canonical = canonical_session_id(provider, native_id)
    except ValueError as exc:
        raise ValueError(
            "source session ID must identify native Claude or Hermes"
        ) from exc
    if canonical != value:
        raise ValueError("source session ID must be canonical")
    return value


def _validate_candidate(candidate: SidebarCandidate) -> None:
    if not isinstance(candidate, SidebarCandidate):
        raise ValueError("sidebar candidate is malformed")
    if (
        not isinstance(candidate.provider, Provider)
        or candidate.provider not in _TITLE_PREFIXES
    ):
        raise ValueError("sidebar candidate provider must be Claude or Hermes")

    source = _validated_source_session_id(candidate.source_session_id)
    if not _source_matches_provider(source, candidate.provider):
        raise ValueError("source session ID does not match provider")
    if candidate.bridge_id != sidebar_bridge_id(source):
        raise ValueError("candidate bridge ID must match source session ID")
    _validate_required_metadata(candidate.cwd, "sidebar candidate cwd")
    for value, label in (
        (candidate.git_root, "sidebar candidate git root"),
        (candidate.git_branch, "sidebar candidate git branch"),
        (candidate.git_head, "sidebar candidate git HEAD"),
        (candidate.worktree_id, "sidebar candidate worktree ID"),
    ):
        _validate_optional_metadata(value, label)
    if (
        not isinstance(candidate.eligible_at, (int, float))
        or isinstance(candidate.eligible_at, bool)
        or not math.isfinite(candidate.eligible_at)
    ):
        raise ValueError("sidebar candidate eligible_at must be finite")


def _source_matches_provider(source: str, provider: Provider) -> bool:
    if provider is Provider.CLAUDE:
        prefix = f"{Provider.CLAUDE.value}:"
        if not source.startswith(prefix):
            return False
        native_id = source[len(prefix) :]
        try:
            return canonical_session_id(provider, native_id) == source
        except ValueError:
            return False
    try:
        return canonical_session_id(provider, source) == source
    except ValueError:
        return False


def _validate_required_metadata(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if _has_line_break(value):
        raise ValueError(f"{label} must be a single line")


def _validate_optional_metadata(value: object, label: str) -> None:
    if value is None:
        return
    _validate_required_metadata(value, label)


def _serialized_metadata(value: str | None) -> str:
    redacted = None if value is None else _redact(value)
    return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))


def _compact_whitespace(value: str) -> str:
    return " ".join(value.split())


def _has_line_break(value: str) -> bool:
    return any(character in _UNICODE_LINE_BREAKS for character in value)


def _validate_finite_timestamp(value: object, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite timestamp")


def _validate_registration_marker(candidate: SidebarCandidate, marker: object) -> None:
    try:
        if not isinstance(marker, str) or marker.count(":") != 1:
            raise ValueError
        prefix, encoded_and_signature = marker.split(":", 1)
        if prefix != _MARKER_PREFIX or encoded_and_signature.count(".") != 1:
            raise ValueError
        encoded_body, encoded_signature = encoded_and_signature.split(".", 1)
        body = _decode_canonical_base64url(encoded_body)
        signature = _decode_canonical_base64url(encoded_signature)
        if len(signature) != 32:
            raise ValueError

        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != _MARKER_FIELDS:
            raise ValueError
        expected = {
            "bridge_id": candidate.bridge_id,
            "policy_generation": 1,
            "source_session_id": candidate.source_session_id,
            "target_provider": Provider.CODEX.value,
        }
        canonical_body = json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if decoded != expected or body != canonical_body:
            raise ValueError
    except (binascii.Error, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("registration marker is malformed or mismatched") from exc


def _decode_canonical_base64url(value: object) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or any(character not in _BASE64URL_CHARACTERS for character in value)
    ):
        raise ValueError
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        padded.encode("ascii"),
        altchars=b"-_",
        validate=True,
    )
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise ValueError
    return decoded


def _is_exact_registration_block(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lines = value.split("\n")
    if len(lines) != 13 or lines[:2] != [
        "This is a Hermes Session Bridge placeholder registration.",
        "Do not perform project work during registration.",
    ]:
        return False
    if lines[10] != "Do not call session_continue during this registration turn.":
        return False
    if lines[12] != ("Until that later user message, reply with only: REGISTERED"):
        return False

    try:
        marker = _prompt_line_value(lines[2], "Signed marker: ")
        source = _decode_canonical_prompt_field(lines[3], "Source session ID: ")
        provider_value = _decode_canonical_prompt_field(lines[4], "Source provider: ")
        cwd = _decode_canonical_prompt_field(lines[5], "Source cwd: ")
        git_root = _decode_canonical_prompt_field(lines[6], "Git root: ")
        git_branch = _decode_canonical_prompt_field(lines[7], "Git branch: ")
        git_head = _decode_canonical_prompt_field(lines[8], "Git HEAD: ")
        worktree_id = _decode_canonical_prompt_field(lines[9], "Worktree ID: ")
        if not isinstance(source, str) or not isinstance(provider_value, str):
            return False
        if not isinstance(cwd, str):
            return False
        if git_root is not None and not isinstance(git_root, str):
            return False
        if git_branch is not None and not isinstance(git_branch, str):
            return False
        if git_head is not None and not isinstance(git_head, str):
            return False
        if worktree_id is not None and not isinstance(worktree_id, str):
            return False
        provider = Provider(provider_value)
        if provider not in (Provider.CLAUDE, Provider.HERMES):
            return False
        candidate = SidebarCandidate(
            source_session_id=source,
            provider=provider,
            bridge_id=sidebar_bridge_id(source),
            title="",
            cwd=cwd,
            git_root=git_root,
            git_branch=git_branch,
            git_head=git_head,
            worktree_id=worktree_id,
            eligible_at=0.0,
        )
        return build_registration_prompt(candidate, marker) == value
    except (TypeError, ValueError):
        return False


def _exact_registration_and_kind(
    value: object,
) -> tuple[str, SidebarInitialPromptKind] | None:
    if not isinstance(value, str):
        return None
    if _is_exact_registration_block(value):
        return value, SidebarInitialPromptKind.LEGACY_PLACEHOLDER
    if (
        value.count(_READABLE_REGISTRATION_DELIMITER) != 1
        or not is_registration_prompt(value)
    ):
        return None
    _rendered, bridge_block = value.split(
        _READABLE_REGISTRATION_DELIMITER,
        1,
    )
    bridge_lines = bridge_block.split("\n")
    return (
        "\n".join(bridge_lines[4:]),
        SidebarInitialPromptKind.READABLE_REGISTRATION,
    )


def _prompt_line_value(line: str, prefix: str) -> str:
    if not line.startswith(prefix):
        raise ValueError
    value = line[len(prefix) :]
    if not value:
        raise ValueError
    return value


def _decode_canonical_prompt_field(line: str, prefix: str) -> object:
    encoded = _prompt_line_value(line, prefix)
    decoded = json.loads(encoded)
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if encoded != canonical:
        raise ValueError
    return decoded
