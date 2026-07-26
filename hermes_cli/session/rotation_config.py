"""Fixed bounds for automatic session rotation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RotationCaps:
    soft_limit_tokens: int = 100_000
    hard_limit_tokens: int = 160_000
    handoff_summary_max_chars: int = 8_000
    max_recent_verdicts: int = 5
    max_recent_dispatches: int = 3


ROTATION_CAPS = RotationCaps()


__all__ = ["ROTATION_CAPS", "RotationCaps"]
