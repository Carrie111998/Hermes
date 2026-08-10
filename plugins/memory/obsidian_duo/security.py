"""Deterministic credential detection at the Memory Duo persistence edge."""

from __future__ import annotations

import re
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
    matches = scan_for_secrets(result).matches
    for match in reversed(matches):
        result = result[:match.start] + f"<REDACTED:{match.category}>" + result[match.end:]
    return result


def assert_safe_to_persist(text: str) -> None:
    result = scan_for_secrets(text)
    if result.matches:
        categories = ", ".join(sorted({match.category for match in result.matches}))
        raise ValueError(f"secret credentials detected ({categories}); refusing persistence")
