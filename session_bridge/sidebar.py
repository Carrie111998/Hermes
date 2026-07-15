from __future__ import annotations

from dataclasses import dataclass
import hashlib
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

_MARKER_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)
_MARKER_FULL_RE = re.compile(
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)
_TITLE_PREFIXES = {
    Provider.CLAUDE: "[Claude] ",
    Provider.HERMES: "[Hermes] ",
}
_MAX_TITLE_CHARS = 120


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
    if (
        projection.provider not in (Provider.CLAUDE, Provider.HERMES)
        or projection.origin_kind is not OriginKind.NATIVE
        or automation_only
        or subagent_only
    ):
        return False
    if (
        not isinstance(backfill_days, int)
        or isinstance(backfill_days, bool)
        or backfill_days < 0
    ):
        raise ValueError("backfill days must be a non-negative integer")
    if (
        not isinstance(now, (int, float))
        or isinstance(now, bool)
        or not math.isfinite(now)
    ):
        raise ValueError("now must be a finite timestamp")
    if not math.isfinite(projection.last_active):
        return False
    if projection.last_active < now - backfill_days * 86_400:
        return False

    for message in projection.messages:
        if message.role != "user" or not isinstance(message.content, str):
            continue
        if _MARKER_CANDIDATE_RE.search(message.content):
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
    return prefix + compact[: _MAX_TITLE_CHARS - len(prefix)]


def build_registration_prompt(candidate: SidebarCandidate, marker: str) -> str:
    _validate_candidate(candidate)
    if not isinstance(marker, str) or _MARKER_FULL_RE.fullmatch(marker) is None:
        raise ValueError("registration marker is malformed")

    source = _redacted_metadata(candidate.source_session_id)
    provider = _redacted_metadata(candidate.provider.value)
    cwd = _redacted_metadata(candidate.cwd)
    git_root = _redacted_metadata(candidate.git_root)
    git_branch = _redacted_metadata(candidate.git_branch)
    git_head = _redacted_metadata(candidate.git_head)
    worktree_id = _redacted_metadata(candidate.worktree_id)

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


def _redacted_metadata(value: str | None) -> str:
    if value is None:
        return "(none)"
    return _redact(value)


def _compact_whitespace(value: str) -> str:
    return " ".join(value.split())


def _has_line_break(value: str) -> bool:
    return "\n" in value or "\r" in value
