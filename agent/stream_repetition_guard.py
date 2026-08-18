"""
Streaming repetition guard — detects degenerate LLM output IN THE STREAM
(before finish_reason=length), unlike repetition_guard.py which only fires
POST-truncation.

Sandbox prototype: this code is designed to be inserted into
chat_completion_helpers.py at the streaming chunk loop (line ~4143, where
content_parts.append(delta.content) happens).

Design:
- Lightweight rolling window check on accumulated text
- Fires every _CHECK_INTERVAL chars (cheap: O(n) per check, not O(n²))
- When tripped → raises StreamingRepetitionError → stream is torn down
  immediately instead of continuing to waste tokens
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# ── Tunable parameters (mirrors repetition_guard.py conventions) ──────────

# Minimum accumulated text before the guard starts checking at all.
# Below this, even repetitive text is legitimately starting a response.
_MIN_ACCUMULATED = 500

# Check the rolling buffer every N chars of new content.
# 200 chars = ~50 tokens — cheap enough for the hot path.
_CHECK_INTERVAL = 200

# Rolling window size: how many chars of recent text to keep.
# Must be >= _REPEAT_WINDOW for the sliding check to work.
_ROLLING_BUFFER = 2000

# Length of the exact-repeat window (same as repetition_guard.py).
_REPEAT_WINDOW = 60

# A window that repeats at least this many times → degenerate.
_MIN_REPEAT_COUNT = 5

# If repeated windows cover >= this fraction of the rolling buffer → degenerate.
_DOMINANCE_RATIO = 0.5


class StreamingRepetitionError(RuntimeError):
    """Raised when live stream output is dominated by repeated text.

    The stream is torn down immediately. The caller (chat_completion_helpers
    _call_chat_completions) catches this and aborts the turn with a clear
    user-facing error, mirroring the post-truncation repetition_guard path.
    """
    pass


@dataclass
class StreamingRepetitionGuard:
    """Stateful guard that checks accumulated streaming text on the fly.

    Feed it text via .append() as chunks arrive. It returns True when
    degenerate repetition is detected. The check runs every _CHECK_INTERVAL
    chars — NOT on every chunk — to keep the streaming hot path cheap.

    Usage in the streaming loop::

        guard = StreamingRepetitionGuard()
        for chunk in stream:
            if delta and delta.content:
                if guard.append(delta.content):
                    raise StreamingRepetitionError("degenerate repetition")
    """

    _accumulated: str = field(default="", init=False)
    _since_last_check: int = field(default=0, init=False)
    _checked: bool = field(default=False, init=False)

    def append(self, text: str) -> bool:
        """Add text, return True if degenerate repetition detected."""
        if not text:
            return False
        self._accumulated += text
        self._since_last_check += len(text)

        if len(self._accumulated) < _MIN_ACCUMULATED:
            return False

        if self._since_last_check < _CHECK_INTERVAL:
            return False

        self._since_last_check = 0
        self._checked = True
        return self._is_degenerate()

    def _is_degenerate(self) -> bool:
        """Check the rolling tail of accumulated text for repetition."""
        buf = self._accumulated[-_ROLLING_BUFFER:]
        n = len(buf)
        if n < _MIN_ACCUMULATED:
            return False

        # Fast path: line-based repetition (most common echo shape).
        if self._line_repetition_dominated(buf, n):
            return True

        # General path: sliding exact-repeat windows.
        window = _REPEAT_WINDOW
        needed = max(
            _MIN_REPEAT_COUNT,
            math.ceil(n * _DOMINANCE_RATIO / window),
        )
        counts: dict[str, int] = {}
        for i in range(n - window + 1):
            key = buf[i : i + window]
            c = counts.get(key, 0) + 1
            if c >= needed:
                return True
            counts[key] = c
        return False

    @staticmethod
    def _line_repetition_dominated(text: str, n: int) -> bool:
        """True when a single normalized line covers half the buffer."""
        counts: dict[str, int] = {}
        for line in text.splitlines():
            norm = line.strip()
            if not norm:
                continue
            counts[norm] = counts.get(norm, 0) + 1
        for line, c in counts.items():
            if c >= _MIN_REPEAT_COUNT and c * len(line) >= n * _DOMINANCE_RATIO:
                return True
        return False

    @property
    def accumulated_length(self) -> int:
        return len(self._accumulated)

    @property
    def was_checked(self) -> bool:
        return self._checked
