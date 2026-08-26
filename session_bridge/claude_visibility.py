from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import re
import uuid
from typing import Any, Literal

from .claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
)
from .models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    SessionProjection,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
)
from .context_pack import redact_sensitive_text
from .sidebar import (
    is_meaningful_user_text,
    normalize_meaningful_user_text,
    sidebar_title,
)


CLAUDE_VISIBILITY_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://hermes-agent/session-bridge/claude-visibility/v1",
)
CLAUDE_VISIBILITY_EXCLUSION_CODES = frozenset({
    "source_claude",
    "unsupported_provider",
    "bridge_placeholder",
    "bridge_continuation",
    "automation_only",
    "subagent_only",
    "acknowledgement_only",
    "control_only",
    "no_meaningful_request",
    "unstable_identity",
    "source_cwd_missing",
})
_ACKNOWLEDGEMENTS = frozenset({"ok", "okay", "yes", "y", "ready", "try again"})
_CONTROLS = frozenset({
    "resume",
    "/resume",
    "clear",
    "/clear",
    "help",
    "/help",
    "quit",
    "/quit",
})
_MAX_PROMPT_CHARS = 8192
_MAX_METADATA_CHARS = 4096
_CODEX_INJECTED_USER_PREFIXES = (
    "<recommended_plugins>\n",
    "# AGENTS.md instructions for ",
    "<skill>\n",
)
_CURRENT_CODEX_REGISTRATION_PREAMBLE = "\n".join((
    "This is a Hermes Session Bridge Claude visibility registration.",
    "Do not perform project work or use tools.",
    "Signed marker: HERMES_SESSION_BRIDGE_V1:",
))
_CODEX_REGISTRATION_PREFIXES = (
    _CURRENT_CODEX_REGISTRATION_PREAMBLE,
    # Same preamble with single-space joins -- some launch wrappers collapse
    # newlines, and those variants slipped past this exclusion and became
    # visible "[Codex] This is a Hermes..." sidebar records.
    "This is a Hermes Session Bridge Claude visibility registration. "
    "Do not perform project work or use tools. "
    "Signed marker: HERMES_SESSION_BRIDGE_V1:",
    (
        "Hermes Session Bridge registration only. "
        "Hermes Session Bridge placeholder.\n"
        "Signed marker: HERMES_SESSION_BRIDGE_V1:"
    ),
    (
        "Hermes Session Bridge registration only. Signed marker: "
        "HERMES_SESSION_BRIDGE_V1:"
    ),
    (
        "Hermes registration diagnostic. Hermes Session Bridge diagnostic "
        "placeholder.\nSigned marker: HERMES_SESSION_BRIDGE_V1:"
    ),
)


@dataclass(frozen=True)
class ClaudeVisibilityCandidate:
    source_session_id: str
    source_provider: Provider
    native_name: str
    source_cwd: str
    git_root: str | None
    git_branch: str | None
    git_head: str | None
    worktree_id: str | None
    eligible_at: float


@dataclass(frozen=True)
class ClaudeVisibilityIdentity:
    job_id: str
    bridge_id: str
    idempotency_key: str
    claude_uuid: str
    signed_marker: str


@dataclass(frozen=True, kw_only=True)
class ClaudeVisibilityClaim:
    status: str
    lease_kind: Literal["launch", "reconciliation"] | None = None
    job_id: str | None = None
    source_session_id: str | None = None
    source_provider: Provider | None = None
    reserved_claude_uuid: str | None = None
    native_name: str | None = None
    source_cwd: str | None = None
    git_root: str | None = None
    git_branch: str | None = None
    git_head: str | None = None
    worktree_id: str | None = None
    signed_marker: str | None = None
    lease_digest: str | None = None
    attempt_ordinal: int | None = None
    prior_error_code: str | None = None
    requires_exact_id_reconciliation: bool = False
    registration_reserved: bool = False
    launch_permitted: bool = False

    @property
    def claimed(self) -> bool:
        return self.status == "claimed"


def evaluate_claude_visibility(
    projection: SessionProjection,
    *,
    automation_only: bool = False,
    subagent_only: bool = False,
) -> str:
    if projection.provider is Provider.CLAUDE:
        return "source_claude"
    if projection.provider not in (Provider.CODEX, Provider.HERMES):
        return "unsupported_provider"
    if projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER:
        return "bridge_placeholder"
    if projection.origin_kind is OriginKind.BRIDGE_CONTINUATION:
        return "bridge_continuation"
    if projection.origin_kind is not OriginKind.NATIVE or projection.origin_bridge_id:
        return "bridge_continuation"
    if automation_only:
        return "automation_only"
    if subagent_only:
        return "subagent_only"
    try:
        canonical_session_id(projection.provider, projection.native_id)
    except ValueError:
        return "unstable_identity"
    user_contents = tuple(
        message.content for message in projection.messages if message.role == "user"
    )
    if projection.provider is Provider.CODEX:
        if any(_is_codex_registration(content) for content in user_contents):
            return "bridge_placeholder"
        if any(_is_codex_automation_envelope(content) for content in user_contents):
            return "automation_only"
        user_contents = tuple(
            content
            for content in user_contents
            if not _is_codex_injected_context(content)
        )
    normalized_user_texts = tuple(
        normalized
        for content in user_contents
        if (normalized := normalize_meaningful_user_text(content)) is not None
    )
    if any(is_meaningful_user_text(content) for content in user_contents):
        try:
            _required_metadata(projection.cwd, "source cwd")
        except ValueError:
            return "source_cwd_missing"
        return "eligible"
    folded = {value.casefold() for value in normalized_user_texts}
    if folded and folded <= _ACKNOWLEDGEMENTS:
        return "acknowledgement_only"
    if folded and folded <= _CONTROLS:
        return "control_only"
    return "no_meaningful_request"


def build_claude_visibility_candidate(
    projection: SessionProjection,
    *,
    eligible_at: float,
    git_root: str | None = None,
    git_head: str | None = None,
    worktree_id: str | None = None,
    automation_only: bool = False,
    subagent_only: bool = False,
) -> ClaudeVisibilityCandidate:
    reason = evaluate_claude_visibility(
        projection,
        automation_only=automation_only,
        subagent_only=subagent_only,
    )
    if reason != "eligible":
        raise ValueError(f"Claude visibility candidate is excluded: {reason}")
    timestamp = _finite_float(eligible_at, "eligible_at")
    source_cwd = _required_metadata(projection.cwd, "source cwd")
    first_request = next(
        content
        for content in _visibility_user_contents(projection)
        if is_meaningful_user_text(content)
    )
    if not isinstance(first_request, str):
        raise ValueError("Claude visibility request text must be a string")
    title_provider = (
        Provider.CLAUDE if projection.provider is Provider.CODEX else Provider.HERMES
    )
    sanitized = sidebar_title(title_provider, None, first_request)
    if projection.provider is Provider.CODEX:
        sanitized = "[Codex] " + sanitized.removeprefix("[Claude] ")
    return ClaudeVisibilityCandidate(
        source_session_id=canonical_session_id(
            projection.provider, projection.native_id
        ),
        source_provider=projection.provider,
        native_name=sanitized,
        source_cwd=source_cwd,
        git_root=_optional_metadata(git_root, "git root"),
        git_branch=_optional_metadata(projection.git_branch, "git branch"),
        git_head=_optional_metadata(git_head, "git head"),
        worktree_id=_optional_metadata(worktree_id, "worktree id"),
        eligible_at=timestamp,
    )


def _visibility_user_contents(projection: SessionProjection) -> tuple[str, ...]:
    contents = tuple(
        message.content
        for message in projection.messages
        if message.role == "user" and isinstance(message.content, str)
    )
    if projection.provider is not Provider.CODEX:
        return contents
    return tuple(
        content for content in contents if not _is_codex_injected_context(content)
    )


def _is_codex_injected_context(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_CODEX_INJECTED_USER_PREFIXES)


def _is_codex_automation_envelope(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith("<heartbeat>\n"):
        return (
            "<automation_id>" in value
            and "<instructions>" in value
            and value.endswith("</heartbeat>")
        )
    return (
        value.startswith("Automation: ")
        and "\nAutomation ID: " in value
        and "\nAutomation memory: " in value
        and "\nLast run: " in value
    )


def _is_codex_registration(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_CODEX_REGISTRATION_PREFIXES)


def derive_claude_visibility_identity(
    candidate: ClaudeVisibilityCandidate,
    marker_secret: bytes,
) -> ClaudeVisibilityIdentity:
    _validate_candidate(candidate)
    job_id, bridge_id, idempotency_key, claude_uuid = _identity_values(candidate)
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=candidate.source_session_id,
            target_provider=Provider.CLAUDE,
            policy_generation=1,
        ),
        marker_secret,
    )
    return ClaudeVisibilityIdentity(
        job_id=job_id,
        bridge_id=bridge_id,
        idempotency_key=idempotency_key,
        claude_uuid=claude_uuid,
        signed_marker=marker,
    )


def _identity_values(
    candidate: ClaudeVisibilityCandidate,
) -> tuple[str, str, str, str]:
    source_identity = (
        f"v1:{candidate.source_provider.value}:{candidate.source_session_id}"
    )
    identity_digest = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
    bridge_id = f"claude-visibility:{identity_digest}"
    idempotency_key = f"claude-visibility:{identity_digest}:v1"
    claude_uuid = str(uuid.uuid5(CLAUDE_VISIBILITY_UUID_NAMESPACE, bridge_id))
    job_id = (
        f"claude-visibility-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
    )
    return job_id, bridge_id, idempotency_key, claude_uuid


def build_claude_registration_prompt(
    candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity,
    marker_secret: bytes,
) -> str:
    validate_claude_visibility_identity_binding(candidate, identity, marker_secret)
    if (
        redact_sensitive_text(candidate.source_session_id)
        != candidate.source_session_id
    ):
        raise ValueError("source session ID cannot be represented safely")
    metadata = {
        "bridge_id": identity.bridge_id,
        "git_branch": _safe_prompt_metadata(candidate.git_branch),
        "git_head": _safe_prompt_metadata(candidate.git_head),
        "git_root": _safe_prompt_metadata(candidate.git_root),
        "source_cwd": _safe_prompt_metadata(candidate.source_cwd),
        "source_provider": candidate.source_provider.value,
        "source_session_id": candidate.source_session_id,
        "worktree_id": _safe_prompt_metadata(candidate.worktree_id),
    }
    serialized = json.dumps(
        metadata,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    marker_suffix = identity.signed_marker.removeprefix(
        "HERMES_SESSION_BRIDGE_V1:"
    )
    prompt = "\n".join((
        f"{_CURRENT_CODEX_REGISTRATION_PREAMBLE}{marker_suffix}",
        f"Bounded metadata: {serialized}",
        "You must reply exactly REGISTERED.",
        "After the first subsequent substantive user request, call session_continue ",
        "with the canonical source session identity before performing project work.",
    ))
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError("Claude registration prompt exceeds its fixed bound")
    return prompt


def normalized_claude_visibility_error(error_code: object) -> tuple[str, bool]:
    if not isinstance(error_code, str):
        return "unknown_error_code", False
    if error_code in CLAUDE_VISIBILITY_RETRY_CODES:
        return str(error_code), True
    if error_code in CLAUDE_VISIBILITY_FATAL_CODES:
        return str(error_code), False
    return "unknown_error_code", False


# One renderer for the operator's only escape hatch, shared by every surface
# that reports an abandoned repair lease. A second copy would reintroduce the
# exact drift this reporting change exists to remove.
CLAUDE_VISIBILITY_REPAIR_IDENTIFIER = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
)
_CLAUDE_VISIBILITY_REPAIR_ROW_KEYS = frozenset(
    {"job_id", "reserved_claude_uuid", "error_code"}
)


def claude_visibility_repair_command(
    job_id: object, reserved_claude_uuid: object, error_code: object
) -> str | None:
    """Render the guarded repair invocation, or nothing at all.

    An abandoned repair lease is excluded from every automatic reclaim path on
    purpose, so this operator invocation IS the recovery path -- worth nothing
    if the reader has to reconstruct it. It is assembled from stored
    identifiers, so anything that is not plainly an identifier yields no command
    rather than a line that could read as one thing and mean another.
    """

    if error_code != "bridge_conflict":
        return None
    for value in (job_id, reserved_claude_uuid):
        if not isinstance(
            value, str
        ) or not CLAUDE_VISIBILITY_REPAIR_IDENTIFIER.match(value):
            return None
    return (
        "hermes-session-bridge claude-visibility-repair-failed "
        f"--job-id {job_id} "
        f"--reserved-claude-uuid {reserved_claude_uuid} "
        f"--error-code {error_code} "
        "--apply --confirm-exact-terminal-repair"
    )


def normalized_claude_visibility_repair_rows(
    rows: object,
) -> tuple[list[dict[str, Any]], bool]:
    """Shape the store's repair_required rows; report whether any was malformed.

    A row of the WRONG SHAPE is dropped and reported -- that is bad evidence.
    A correctly shaped row whose identifiers will not render is KEPT with
    command=None: the reader still needs to know the job exists.  Callers decide
    what to do with the malformed flag; the strict public surfaces degrade on it.
    """

    if not isinstance(rows, list):
        return [], rows is not None
    shaped: list[dict[str, Any]] = []
    malformed = False
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != _CLAUDE_VISIBILITY_REPAIR_ROW_KEYS
        ):
            malformed = True
            continue
        job_id = row["job_id"]
        reserved = row["reserved_claude_uuid"]
        error_code = row["error_code"]
        if not all(
            isinstance(value, str)
            for value in (job_id, reserved, error_code)
        ):
            malformed = True
            continue
        shaped.append(
            {
                "job_id": job_id,
                "reserved_claude_uuid": reserved,
                "error_code": error_code,
                "command": claude_visibility_repair_command(
                    job_id, reserved, error_code
                ),
            }
        )
    return shaped, malformed


def validate_claude_visibility_identity_binding(
    candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity,
    marker_secret: bytes,
) -> None:
    _validate_candidate(candidate)
    if not isinstance(identity, ClaudeVisibilityIdentity):
        raise ValueError("Claude visibility identity is malformed")
    if (
        identity.job_id,
        identity.bridge_id,
        identity.idempotency_key,
        identity.claude_uuid,
    ) != _identity_values(candidate):
        raise ValueError("Claude visibility identity does not match candidate")
    try:
        payload = decode_bridge_marker(identity.signed_marker, marker_secret)
    except ValueError as exc:
        raise ValueError("Claude visibility signed marker is malformed") from exc
    if payload != BridgeMarkerPayload(
        bridge_id=identity.bridge_id,
        policy_generation=1,
        source_session_id=candidate.source_session_id,
        target_provider=Provider.CLAUDE,
    ):
        raise ValueError("Claude visibility signed marker does not match candidate")


def _safe_prompt_metadata(value: str | None) -> str | None:
    """Redact untrusted metadata before ASCII-only canonical JSON serialization."""

    return None if value is None else redact_sensitive_text(value)


def _validate_candidate(candidate: ClaudeVisibilityCandidate) -> None:
    if not isinstance(candidate, ClaudeVisibilityCandidate):
        raise ValueError("Claude visibility candidate is malformed")
    if candidate.source_provider not in (Provider.CODEX, Provider.HERMES):
        raise ValueError("Claude visibility source provider must be Codex or Hermes")
    canonical = canonical_session_id(
        candidate.source_provider,
        candidate.source_session_id.removeprefix(f"{candidate.source_provider.value}:"),
    )
    if canonical != candidate.source_session_id:
        raise ValueError("Claude visibility source identity is not canonical")
    if (
        not candidate.native_name.startswith(
            "[Codex] " if candidate.source_provider is Provider.CODEX else "[Hermes] "
        )
        or len(candidate.native_name) > 120
    ):
        raise ValueError("Claude visibility native name is invalid")
    _required_metadata(candidate.source_cwd, "source cwd")
    for value, label in (
        (candidate.git_root, "git root"),
        (candidate.git_branch, "git branch"),
        (candidate.git_head, "git head"),
        (candidate.worktree_id, "worktree id"),
    ):
        _optional_metadata(value, label)
    _finite_float(candidate.eligible_at, "eligible_at")


def _required_metadata(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be canonical nonempty text")
    if len(value) > _MAX_METADATA_CHARS or any(char in value for char in "\r\n\x00"):
        raise ValueError(f"{label} exceeds its safe bound")
    return value


def _optional_metadata(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_metadata(value, label)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite number")
    return normalized


def usd_microdollars(value: object, label: str) -> int:
    try:
        normalized = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be a decimal amount") from exc
    if not normalized.is_finite() or normalized <= 0:
        raise ValueError(f"{label} must be a positive decimal amount")
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{label} must be a finite decimal amount")
    if exponent < -6:
        raise ValueError(f"{label} supports at most 6 decimal places")
    if normalized > Decimal("1000000"):
        raise ValueError(f"{label} cannot exceed 1000000 USD")
    sign, digits, exponent = normalized.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"{label} must be a finite decimal amount")
    if sign:
        raise ValueError(f"{label} must be a positive decimal amount")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return coefficient * (10 ** (exponent + 6))


def canonical_usd(microdollars: int) -> str:
    if (
        not isinstance(microdollars, int)
        or isinstance(microdollars, bool)
        or microdollars < 0
    ):
        raise ValueError("microdollars must be a non-negative integer")
    whole, fraction = divmod(microdollars, 1_000_000)
    return f"{whole}.{fraction:06d}"
