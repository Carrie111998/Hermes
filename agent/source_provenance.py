"""Trusted, request-scoped provenance for exact local file slices.

This module deliberately has no provider-facing API.  It records only grants
created by the two trusted file-read surfaces; the firewall is the later
consumer that decides whether a grant is eligible for a particular egress.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from threading import RLock
from typing import Iterator

from agent.file_safety import get_read_block_error
from agent.llm_egress_firewall import SourceGrant
from agent.redact import redact_sensitive_text


MAX_SOURCE_SLICE_LINES = 2_000
MAX_SOURCE_SLICE_BYTES = 262_144


class SourceProvenanceError(ValueError):
    """A trusted producer could not establish file-slice provenance."""


@dataclass(frozen=True, slots=True)
class SourceProvenanceContext:
    """Per-call identity available only while a trusted file read executes."""

    registry: "SourceProvenanceRegistry"
    session_id: str
    turn_id: str
    request_id: str
    policy_digest: str


_active_context: ContextVar[SourceProvenanceContext | None] = ContextVar(
    "source_provenance_context",
    default=None,
)


def active_source_provenance() -> SourceProvenanceContext | None:
    """Return the trusted context for the current tool dispatch, if any."""

    return _active_context.get()


@contextmanager
def activate_source_provenance(
    registry: "SourceProvenanceRegistry",
    *,
    session_id: str,
    turn_id: str,
    request_id: str,
    policy_digest: str,
) -> Iterator[SourceProvenanceContext]:
    """Temporarily make an authenticated request identity available to reads."""

    values = (session_id, turn_id, request_id, policy_digest)
    if not isinstance(registry, SourceProvenanceRegistry) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise SourceProvenanceError("missing_identity")
    context = SourceProvenanceContext(registry, *values)
    token = _active_context.set(context)
    try:
        yield context
    finally:
        _active_context.reset(token)


class SourceProvenanceRegistry:
    """In-memory grants keyed solely by their bound request identity."""

    def __init__(self) -> None:
        self._grants: dict[str, list[SourceGrant]] = {}
        self._lock = RLock()

    def issue_file_slice(
        self,
        *,
        path: Path,
        line_start: int,
        line_end: int,
        content: bytes,
        session_id: str,
        turn_id: str,
        request_id: str,
        policy_digest: str,
    ) -> SourceGrant:
        """Grant exactly the current canonical bytes, or reject the producer.

        The caller supplies the bytes it just read, but they are never trusted
        by themselves: this method re-reads the requested bounded source slice
        from the resolved regular file, checks the sensitive-path policy and
        forced redaction, then compares the two byte strings.
        """

        if not isinstance(path, Path):
            path = Path(path)
        if path.is_symlink():
            raise SourceProvenanceError("symlink_path")
        if not isinstance(content, bytes):
            raise SourceProvenanceError("invalid_content")
        if (
            not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or line_start < 1
            or line_end < line_start
            or line_end - line_start + 1 > MAX_SOURCE_SLICE_LINES
        ):
            raise SourceProvenanceError("invalid_line_range")
        if len(content) > MAX_SOURCE_SLICE_BYTES:
            raise SourceProvenanceError("slice_too_large")
        identities = (session_id, turn_id, request_id, policy_digest)
        if not all(isinstance(value, str) and value for value in identities):
            raise SourceProvenanceError("missing_identity")

        try:
            canonical = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise SourceProvenanceError("canonical_path_unavailable") from exc
        if not canonical.is_file():
            raise SourceProvenanceError("not_regular_file")
        try:
            if get_read_block_error(str(canonical)) is not None:
                raise SourceProvenanceError("sensitive_path")
        except SourceProvenanceError:
            raise
        except Exception as exc:
            raise SourceProvenanceError("read_policy_unavailable") from exc

        approved = _read_bounded_slice(canonical, line_start, line_end)
        if not compare_digest(sha256(content).digest(), sha256(approved).digest()):
            raise SourceProvenanceError("content_mismatch")
        try:
            source_text = approved.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceProvenanceError("non_text_source") from exc
        try:
            if redact_sensitive_text(
                source_text,
                force=True,
                file_read=True,
                redact_url_credentials=True,
            ) != source_text:
                raise SourceProvenanceError("redaction_changed_content")
        except SourceProvenanceError:
            raise
        except Exception as exc:
            raise SourceProvenanceError("redaction_unavailable") from exc

        grant = SourceGrant(
            canonical_path=canonical,
            display_path=_safe_display_path(canonical),
            line_start=line_start,
            line_end=line_end,
            content_sha256=sha256(approved).hexdigest(),
            byte_count=len(approved),
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            policy_digest=policy_digest,
        )
        with self._lock:
            self._grants.setdefault(request_id, []).append(grant)
        return grant

    def grants_for_request(self, request_id: str) -> tuple[SourceGrant, ...]:
        """Return an immutable snapshot of grants bound to ``request_id``."""

        if not isinstance(request_id, str) or not request_id:
            return ()
        with self._lock:
            return tuple(self._grants.get(request_id, ()))

    def clear_request(self, request_id: str) -> None:
        """Discard grants at the end of a request without touching other turns."""

        if not isinstance(request_id, str) or not request_id:
            return
        with self._lock:
            self._grants.pop(request_id, None)


def _read_bounded_slice(path: Path, line_start: int, line_end: int) -> bytes:
    """Read the inclusive raw-byte line interval without materializing a file."""

    selected: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number < line_start:
                    continue
                if line_number > line_end:
                    break
                total += len(line)
                if total > MAX_SOURCE_SLICE_BYTES:
                    raise SourceProvenanceError("slice_too_large")
                selected.append(line)
    except SourceProvenanceError:
        raise
    except OSError as exc:
        raise SourceProvenanceError("canonical_read_failed") from exc
    if len(selected) != line_end - line_start + 1:
        raise SourceProvenanceError("line_range_unavailable")
    return b"".join(selected)


def _safe_display_path(path: Path) -> str:
    """Keep grant metadata portable and free of an absolute home path."""

    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name
