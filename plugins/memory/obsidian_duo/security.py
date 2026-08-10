"""Deterministic credential detection at the Memory Duo persistence edge."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretMatch:
    category: str
    start: int
    end: int


@dataclass(frozen=True)
class SecretScanResult:
    matches: tuple[SecretMatch, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.matches


_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [^-\n]+-----[\s\S]*?-----END [^-\n]+-----")),
    ("authorization_header", re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*bearer\s+\S+")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("known_api_key", re.compile(r"(?i)\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-|AKIA[0-9A-Z]{8})[A-Za-z0-9_./+=-]{12,}")),
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|password|passwd|client[_-]?secret|github[_-]?token)\s*[:=]\s*[^\s,;]+"
    )),
    ("credential_entropy", re.compile(
        r"(?i)\b(?:token|secret|password|credential|api[_-]?key)\b\s*[:=]\s*[A-Za-z0-9_./+=-]{24,}"
    )),
)


def scan_for_secrets(text: str) -> SecretScanResult:
    matches = []
    occupied = []
    for category, pattern in _PATTERNS:
        for found in pattern.finditer(text or ""):
            if any(found.start() < end and found.end() > start for start, end in occupied):
                continue
            match = SecretMatch(category, found.start(), found.end())
            matches.append(match)
            occupied.append((match.start, match.end))
    return SecretScanResult(tuple(sorted(matches, key=lambda item: item.start)))


def redact_secrets(text: str) -> str:
    result = text or ""
    for _ in range(4):
        matches = scan_for_secrets(result).matches
        if not matches:
            break
        for match in reversed(matches):
            replacement = "<REDACTED>"
            if match.category in {"credential_assignment", "credential_entropy"}:
                original = result[match.start:match.end]
                label = re.split(r"\s*[:=]\s*", original, maxsplit=1)[0]
                replacement = f"{label} <REDACTED>"
            result = result[:match.start] + replacement + result[match.end:]
    return result


def assert_safe_to_persist(text: str) -> None:
    result = scan_for_secrets(text)
    if result.matches:
        categories = ", ".join(sorted({match.category for match in result.matches}))
        raise ValueError(f"secret credentials detected ({categories}); refusing persistence")


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _walk_strings(item)


def assert_safe_value(value) -> None:
    for text in _walk_strings(value):
        assert_safe_to_persist(text)


def redact_value(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {redact_value(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def assert_candidate_safe_to_persist(candidate) -> None:
    assert_safe_to_persist(candidate.content)
    for evidence in candidate.evidence:
        assert_safe_to_persist(evidence.content)
        assert_safe_value((evidence.evidence_id, evidence.kind, evidence.source, evidence.session_id))
    assert_safe_value(candidate.metadata)
