"""Ctrl+S prompt stash — pure state machine for the classic CLI composer.

Park a half-written prompt, send something else, then bring the draft back.
Mirrors Claude Code's ``ctrl + s to stash prompt`` affordance.

The state machine lives here (no prompt_toolkit imports) so it can be unit
tested directly; ``cli.py`` owns only the keybinding and the rendering.

Gesture
-------
- Buffer has content  → push it onto the stash, clear the composer.
- Buffer empty, 1 item → pop it straight back into the composer.
- Buffer empty, 2+ items → open the browse panel (↑↓ / Enter / D / Esc).

Newest-first ordering: index 0 is always the most recently stashed draft, so
the common "undo my last Ctrl+S" case is a single keystroke.

Disk persistence (optional)
----------------------------
The stash can be saved to and loaded from disk via ``save_stash()`` /
``load_stash()``.  The serialized format uses wall-clock timestamps so
entries are meaningful across restarts.  Files are written atomically
(tmp + rename) and locked to owner-only permissions (``0o600``) because
drafts frequently contain credentials, prompts under NDA, or pasted secrets.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# Single-line preview length for the browse panel.
PREVIEW_WIDTH = 60

# Cap the stack so a user leaning on Ctrl+S can't grow it without bound.
MAX_STASH_ITEMS = 20


def build_preview(text: str, width: int = PREVIEW_WIDTH) -> str:
    """Collapse a possibly multi-line draft into one preview line.

    Newlines and tabs become ``⏎``/space so a 40-line draft still renders as a
    single panel row, and the result is ellipsized to ``width`` display chars.
    """
    if not text:
        return ""
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    flat = flat.replace("\n", " ⏎ ").replace("\t", " ")
    flat = " ".join(flat.split())
    if width > 1 and len(flat) > width:
        return flat[: width - 1] + "…"
    return flat


@dataclass
class StashEntry:
    """One parked draft: exact text plus any images that were attached."""

    text: str
    images: List[Any] = field(default_factory=list)
    stashed_at: float = 0.0
    preview: str = ""

    def as_dict(self) -> dict:
        """Render in the shape ``HermesCLI._render_stash_panel`` consumes."""
        return {
            "text": self.text,
            "images": list(self.images),
            "stashed_at": self.stashed_at,
            "preview": self.preview,
        }


class PromptStash:
    """Session-scoped stack of parked composer drafts.

    Pure state: no I/O, no prompt_toolkit, no global clock beyond
    ``time.monotonic`` (injectable for tests via ``clock``).
    """

    def __init__(self, *, max_items: int = MAX_STASH_ITEMS, clock=None):
        self._items: List[StashEntry] = []
        self._max_items = max(1, int(max_items))
        self._clock = clock or time.monotonic
        self.panel_open = False
        self.panel_cursor = 0

    # ---------------------------------------------------------------- queries

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        # Explicit: an empty stash is falsey, but len() drives that anyway.
        return bool(self._items)

    @property
    def items(self) -> List[StashEntry]:
        """Newest-first list of entries (a copy — mutate via the API)."""
        return list(self._items)

    def panel_rows(self) -> List[dict]:
        """Entries as plain dicts for the panel renderer."""
        return [e.as_dict() for e in self._items]

    def indicator(self) -> str:
        """Status-bar indicator, or ``""`` when the stash is empty.

        ``📌 2`` when idle, ``📌 2 ▲`` while the browse panel is open, so the
        user can always tell a parked draft exists without opening anything.
        """
        n = len(self._items)
        if not n:
            return ""
        return f"📌 {n} ▲" if self.panel_open else f"📌 {n}"

    def placeholder_hint(self) -> str:
        """Composer placeholder text advertising the stashed draft."""
        n = len(self._items)
        if not n:
            return ""
        if n == 1:
            return f"Ctrl+S to restore: {self._items[0].preview}"
        return f"Ctrl+S to browse {n} stashed drafts"

    # --------------------------------------------------------------- mutators

    def stash(self, text: str, images: Optional[Sequence[Any]] = None) -> bool:
        """Push a draft. Returns False (no-op) for a blank buffer.

        A buffer that is empty or whitespace-only is not worth parking and
        must stay a no-op, otherwise Ctrl+S on an empty composer would push a
        junk entry instead of triggering the restore half of the gesture.
        Text is stored verbatim — leading/trailing whitespace and newlines are
        preserved so a restore round-trips byte-for-byte.
        """
        has_images = bool(images)
        if not (text or "").strip() and not has_images:
            return False

        entry = StashEntry(
            text=text or "",
            images=list(images or []),
            stashed_at=self._clock(),
            preview=build_preview(text or "") or "(images only)",
        )
        self._items.insert(0, entry)
        # Drop the oldest entries past the cap.
        del self._items[self._max_items:]
        # A push invalidates any open browse session.
        self.panel_open = False
        self.panel_cursor = 0
        return True

    def pop(self, index: int = 0) -> Optional[Tuple[str, List[Any]]]:
        """Remove and return ``(text, images)`` at ``index``, or None."""
        if not self._items or not (0 <= index < len(self._items)):
            return None
        entry = self._items.pop(index)
        if not self._items:
            self.panel_open = False
        self.panel_cursor = self._clamp_cursor(self.panel_cursor)
        return entry.text, list(entry.images)

    def peek(self, index: int = 0) -> Optional[StashEntry]:
        """Return the entry at ``index`` without removing it."""
        if not self._items or not (0 <= index < len(self._items)):
            return None
        return self._items[index]

    def clear(self) -> None:
        self._items.clear()
        self.panel_open = False
        self.panel_cursor = 0

    # ---------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        """Serialize stash items to a JSON-safe dict with wall-clock timestamps.

        Returns ``{"items": [{text, images, stashed_at_wall, preview}, ...]}``.
        Each entry's ``stashed_at_wall`` is derived from the in-memory monotonic
        clock so the value is meaningful across restarts.
        """
        mono_now = self._clock()
        wall_now = time.time()
        items = []
        for entry in self._items:
            wall_time = wall_now - (mono_now - entry.stashed_at)
            items.append({
                "text": entry.text,
                "images": list(entry.images),
                "stashed_at_wall": wall_time,
                "preview": entry.preview,
            })
        return {"items": items}

    @classmethod
    def from_dict(
        cls,
        data: dict,
        *,
        max_items: int = MAX_STASH_ITEMS,
        clock=None,
    ) -> PromptStash:
        """Deserialize stash items from a dict produced by :meth:`to_dict`.

        Wall-clock timestamps are converted back to monotonic offsets so the
        age display stays accurate for the current session.  Entries with
        missing or invalid fields are silently skipped.
        """
        stash = cls(max_items=max_items, clock=clock)
        raw_items = data.get("items", []) if isinstance(data, dict) else []
        entries: list[StashEntry] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            images = item.get("images")
            wall_time = item.get("stashed_at_wall")
            preview = item.get("preview")
            if text is None or images is None or wall_time is None:
                continue
            if not isinstance(wall_time, (int, float)):
                continue
            # Convert wall clock → session monotonic offset.
            _clock = stash._clock
            monotonic_at = _clock() - time.time() + wall_time
            entries.append(StashEntry(
                text=text,
                images=list(images),
                stashed_at=monotonic_at,
                preview=preview or "",
            ))
        # Restore newest-first ordering (matching in-memory convention).
        entries.sort(key=lambda e: e.stashed_at, reverse=True)
        stash._items = entries[: stash._max_items]
        return stash

    # ------------------------------------------------------------ panel state

    def _clamp_cursor(self, value: int) -> int:
        if not self._items:
            return 0
        return max(0, min(int(value), len(self._items) - 1))

    def open_panel(self) -> bool:
        """Open the browse panel. False when there is nothing to browse."""
        if not self._items:
            return False
        self.panel_open = True
        self.panel_cursor = 0
        return True

    def close_panel(self) -> None:
        self.panel_open = False
        self.panel_cursor = 0

    def move_cursor(self, delta: int) -> int:
        """Move the panel cursor, clamped to the list bounds."""
        self.panel_cursor = self._clamp_cursor(self.panel_cursor + int(delta))
        return self.panel_cursor

    def delete_at_cursor(self) -> bool:
        """Delete the highlighted entry. False when there was nothing to drop."""
        if not self._items:
            return False
        idx = self._clamp_cursor(self.panel_cursor)
        self._items.pop(idx)
        if not self._items:
            self.panel_open = False
            self.panel_cursor = 0
        else:
            self.panel_cursor = self._clamp_cursor(idx)
        return True

    def restore_at_cursor(self) -> Optional[Tuple[str, List[Any]]]:
        """Pop the highlighted entry and close the panel."""
        if not self._items:
            return None
        result = self.pop(self._clamp_cursor(self.panel_cursor))
        self.close_panel()
        return result


# --------------------------------------------------------------------- gesture

# Outcomes of a single Ctrl+S press.
ACTION_NOOP = "noop"
ACTION_STASHED = "stashed"
ACTION_RESTORED = "restored"
ACTION_OPEN_PANEL = "open_panel"
ACTION_CLOSE_PANEL = "close_panel"


def resolve_ctrl_s(
    stash: PromptStash,
    buffer_text: str,
    images: Optional[Sequence[Any]] = None,
) -> Tuple[str, Optional[Tuple[str, List[Any]]]]:
    """Decide what one Ctrl+S press does. Returns ``(action, payload)``.

    ``payload`` carries ``(text, images)`` for :data:`ACTION_RESTORED`, else
    None. This is the whole decision table in one pure function so the
    keybinding handler in ``cli.py`` stays a thin adapter.
    """
    # Panel open → Ctrl+S is the "close it" escape hatch.
    if stash.panel_open:
        stash.close_panel()
        return ACTION_CLOSE_PANEL, None

    # Something to park → park it. Never silently clobbers an existing stash:
    # entries push onto a stack, so an earlier draft is still reachable.
    if (buffer_text or "").strip() or images:
        if stash.stash(buffer_text, images):
            return ACTION_STASHED, None
        return ACTION_NOOP, None

    # Empty buffer → restore half of the gesture.
    count = len(stash)
    if count == 0:
        return ACTION_NOOP, None
    if count == 1:
        return ACTION_RESTORED, stash.pop(0)
    stash.open_panel()
    return ACTION_OPEN_PANEL, None


# ------------------------------------------------------------------ persistence

_log = logging.getLogger(__name__)


def save_stash(stash: PromptStash, path: Path | None = None) -> None:
    """Persist *stash* to a JSON file.

    The file is written atomically (to a ``.tmp`` sibling, then renamed) and
    locked to owner-only permissions (``0o600``).  Nothing is written when the
    stash is empty.

    *path* defaults to ``get_hermes_home() / "stash.json"``.
    """
    if not stash:
        return  # no-op — don't create a file for an empty stash.
    if path is None:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "stash.json"

    try:
        import os as _os

        data = stash.to_dict()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        _os.replace(tmp, path)  # os.replace, not Path.rename — atomic on Windows
    except Exception:
        _log.exception("Failed to save prompt stash to %s", path)


def load_stash(path: Path | None = None) -> PromptStash:
    """Load a previously saved stash from disk.

    Returns an empty :class:`PromptStash` on any error (missing file, corrupt
    JSON, invalid data).

    *path* defaults to ``get_hermes_home() / "stash.json"``.
    """
    if path is None:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "stash.json"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PromptStash.from_dict(data)
    except FileNotFoundError:
        return PromptStash()
    except Exception:
        _log.warning("Corrupt or unreadable stash file %s — starting fresh", path)
        return PromptStash()
