"""Deterministic egress checks for private Skill bodies."""

from __future__ import annotations

import hashlib
import unicodedata

_FINGERPRINT_CHARS = 64
_MIN_FINGERPRINT_CHARS = 16


def _normalize(text: str) -> str:
    """Ignore presentation-only differences while preserving content order."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _window_fingerprints(text: str, window_size: int) -> set[bytes]:
    return {
        hashlib.blake2s(
            text[index:index + window_size].encode("utf-8"),
            digest_size=16,
        ).digest()
        for index in range(len(text) - window_size + 1)
    }


class SkillEgressGuard:
    """Keep only hashed body windows and detect copied Skill fragments."""

    def __init__(self) -> None:
        self._fingerprints_by_size: dict[int, set[bytes]] = {}
        self._skill_names: set[str] = set()

    @property
    def active(self) -> bool:
        return bool(self._skill_names)

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skill_names))

    def add_skill(self, name: str, body: str) -> bool:
        normalized = _normalize(body)
        if not name or not normalized:
            return False
        self._skill_names.add(name)
        if len(normalized) < _MIN_FINGERPRINT_CHARS:
            return True
        window_size = min(_FINGERPRINT_CHARS, len(normalized))
        fingerprints = self._fingerprints_by_size.setdefault(window_size, set())
        fingerprints.update(_window_fingerprints(normalized, window_size))
        return True

    def matches(self, text: str) -> bool:
        if not self.active:
            return False
        normalized = _normalize(text)
        for window_size, protected in self._fingerprints_by_size.items():
            if len(normalized) < window_size:
                continue
            for fingerprint in _window_fingerprints(normalized, window_size):
                if fingerprint in protected:
                    return True
        return False
