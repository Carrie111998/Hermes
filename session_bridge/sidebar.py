from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata

from .context_pack import _redact
from .models import OriginKind, Provider, SessionProjection, canonical_session_id


ACK_OR_CONTROL_ONLY = frozenset({
    "ok",
    "okay",
    "yes",
    "y",
    "ready",
    "resume",
    "/resume",
    "clear",
    "/clear",
    "help",
    "/help",
    "quit",
    "/quit",
})

_TITLE_PREFIXES = {
    Provider.CLAUDE: "[Claude] ",
    Provider.HERMES: "[Hermes] ",
}
_MAX_TITLE_CHARS = 120
_UNICODE_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1"
_MARKER_FIELDS = frozenset({
    "bridge_id",
    "policy_generation",
    "source_session_id",
    "target_provider",
})
_BASE64URL_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_CHARACTERIZATION_TITLE_RE = re.compile(
    r"^\[Hermes Bridge Characterization\] "
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


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
class VerifiedSidebarThread:
    thread_id: str
    source_session_id: str
    bridge_id: str


def normalize_meaningful_user_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


def is_meaningful_user_text(value: object) -> bool:
    normalized = normalize_meaningful_user_text(value)
    if normalized is None or normalized.casefold() in ACK_OR_CONTROL_ONLY:
        return False
    return sum(character.isalnum() for character in normalized) >= 3


def is_sidebar_session_eligible(
    projection: SessionProjection,
    *,
    now: float,
    backfill_days: int = 30,
    automation_only: bool = False,
    subagent_only: bool = False,
) -> bool:
    _validate_finite_timestamp(now, "now")
    if (
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
    if projection.last_active < now - backfill_days * 86_400:
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
        if _is_exact_registration_block(message.content):
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


def build_registration_prompt(candidate: SidebarCandidate, marker: str) -> str:
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

    return "\n".join((
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
        "Before substantive work, call "
        f'session_continue(session_id={source}, target_provider="codex").',
        "Wait for the first substantive user message before doing anything else.",
    ))


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
    if len(lines) != 12 or lines[:2] != [
        "This is a Hermes Session Bridge placeholder registration.",
        "Do not perform project work during registration.",
    ]:
        return False
    if lines[11] != (
        "Wait for the first substantive user message before doing anything else."
    ):
        return False

    try:
        marker = _prompt_line_value(lines[2], "Signed marker: ")
        source = _decode_canonical_prompt_field(lines[3], "Source session ID: ")
        provider_value = _decode_canonical_prompt_field(
            lines[4], "Source provider: "
        )
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
