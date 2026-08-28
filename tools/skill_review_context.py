"""Read authorization shared across one background skill-review context.

Background review workers run in copied contexts.  They must share the set of
skill files actually shown to the reviewer without leaking those read marks
into the next review run.
"""

import contextvars
import threading
from pathlib import Path
from typing import Optional


class _BackgroundReviewReadMarks:
    """Thread-safe read marks shared by copied tool contexts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)

    def contains(self, path: str) -> bool:
        with self._lock:
            return path in self._paths


_read_paths: "contextvars.ContextVar[Optional[_BackgroundReviewReadMarks]]" = (
    contextvars.ContextVar("background_review_read_paths", default=None)
)


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork read one skill file."""
    try:
        from tools.skill_provenance import is_background_review

        if not is_background_review():
            return
    except Exception:
        return

    marks = _read_paths.get()
    if marks is None:
        marks = _BackgroundReviewReadMarks()
        _read_paths.set(marks)
    marks.add(_resolved_key(path))


def background_review_has_read(path: Path) -> bool:
    """Return whether this review context has loaded the exact target."""
    marks = _read_paths.get()
    return marks is not None and marks.contains(_resolved_key(path))


def reset_background_review_read_marks() -> None:
    """Start a fresh read set isolated from previous review contexts."""
    _read_paths.set(_BackgroundReviewReadMarks())
