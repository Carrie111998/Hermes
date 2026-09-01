from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopGuard:
    """Bound automatic Developer→Tester/Reviewer correction loops."""

    max_correction_loops: int = 3
    attempts: int = 0

    def can_retry(self) -> bool:
        return self.attempts < self.max_correction_loops

    def next_attempt(self) -> int:
        if not self.can_retry():
            raise RuntimeError(
                f"Maximum correction loops reached ({self.max_correction_loops})."
            )
        self.attempts += 1
        return self.attempts

    def status(self) -> dict[str, int | bool]:
        return {
            "attempts": self.attempts,
            "max_correction_loops": self.max_correction_loops,
            "can_retry": self.can_retry(),
        }
