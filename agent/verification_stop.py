"""Retired compatibility surface for the former verify-on-stop guard.

The former implementation classified changed filenames and verification state
to decide whether to override a model-authored completion with a synthetic
follow-up. Completion and verification strategy now remain with the model.
These shims preserve imports while ensuring configuration cannot reactivate
host-side semantic routing.
"""

from __future__ import annotations

from typing import Any, Iterable


def verify_on_stop_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return ``False``: host-authored verification continuations are retired."""

    del config
    return False


def build_verify_on_stop_nudge(
    *,
    session_id: str | None,
    changed_paths: Iterable[str],
    attempts: int = 0,
    max_attempts: int = 2,
) -> str | None:
    """Never interpret changed paths or synthesize a verification follow-up."""

    del session_id, changed_paths, attempts, max_attempts
    return None


__all__ = ["build_verify_on_stop_nudge", "verify_on_stop_enabled"]
