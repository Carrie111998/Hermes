"""
Shared utilities for local Hermes extensions (bandit_router, autotune, stm_transforms).

Provides:
  - JsonStateStore[T]  — process-safe JSON persistence with fcntl locking
  - cfg_section()      — safe config section accessor (deduplicates the
                         `cfg.get("x") or {}` pattern across all three modules)
"""

from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def cfg_section(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Return ``cfg[name]`` as a dict, or ``{}`` if missing or wrong type.

    Replaces the recurring ``cfg.get("x") or {}`` pattern used in every
    ``is_enabled`` / ``get_*`` helper across the local extension modules.
    """
    val = cfg.get(name)
    return val if isinstance(val, dict) else {}


class JsonStateStore(Generic[T]):
    """Process-safe JSON state file for local extension modules.

    Uses ``fcntl.LOCK_EX`` on every write so concurrent cron jobs don't
    corrupt the state file through partial writes or truncation races.

    Example::

        _store: JsonStateStore[Dict[str, Any]] = JsonStateStore(
            get_hermes_home() / "bandit_state.json",
            default_factory=lambda: {"version": 1, "priors": {}, "outcomes": []},
        )

        state = _store.load()
        state["priors"]["simple"]["claude-haiku"] = {"alpha": 2.0, "beta": 1.0}
        _store.save(state)
    """

    def __init__(self, path: Path, default_factory: Callable[[], T]) -> None:
        self._path = path
        self._default_factory = default_factory

    def load(self) -> T:
        """Load state from disk. Returns ``default_factory()`` if missing or corrupt."""
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return data  # type: ignore[return-value]
        except Exception as e:
            logger.warning("State load failed (%s): %s", self._path.name, e)
        return self._default_factory()

    def save(self, state: T) -> None:
        """Persist *state* to disk with an exclusive file lock.

        Opens in ``a+`` mode so the file is created if absent, then seeks
        to the start and truncates before writing — this is the standard
        pattern that works with ``fcntl.LOCK_EX``.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a+", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(json.dumps(state, indent=2))
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as e:
            logger.warning("State save failed (%s): %s", self._path.name, e)

    def reset(self) -> None:
        """Overwrite the state file with a fresh default. Irreversible."""
        self.save(self._default_factory())
