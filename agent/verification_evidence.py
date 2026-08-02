"""Retired compatibility surface for the former verification ledger.

Hermes used to parse terminal command text, project metadata, filenames, and
exit codes to infer whether the model had performed an adequate verification.
Those inferences gave host code semantic authority over the model's workflow.
They are deliberately retired: command text and changed paths are opaque model
and tool data, not inputs to a host-side task classifier.

The public functions remain as inert shims so older callers and the
``verification.status`` wire method keep a stable shape while upgrades roll
through. No configuration value can reactivate classification or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class VerificationEvidence:
    """Legacy value shape retained for import compatibility only."""

    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    root: str
    session_id: str
    output_summary: str = ""


def verification_ledger_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return ``False``: host-side semantic verification is permanently retired."""

    del config
    return False


def classify_verification_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
    exit_code: int = 0,
    output: str = "",
) -> Optional[VerificationEvidence]:
    """Never infer verification meaning from opaque terminal command text."""

    del command, cwd, session_id, exit_code, output
    return None


def record_terminal_result(
    *,
    command: str,
    cwd: str | Path | None,
    session_id: str | None,
    exit_code: int,
    output: str = "",
) -> Optional[dict[str, Any]]:
    """Ignore terminal results instead of classifying or persisting them."""

    del command, cwd, session_id, exit_code, output
    return None


def mark_workspace_edited(
    *,
    session_id: str | None,
    cwd: str | Path | None,
    paths: list[str] | tuple[str, ...] | None = None,
) -> Optional[dict[str, Any]]:
    """Ignore edit paths instead of assigning verification semantics to them."""

    del session_id, cwd, paths
    return None


def verification_status(
    *,
    session_id: str | None,
    cwd: str | Path | None,
) -> dict[str, Any]:
    """Preserve the status wire shape without exposing inferred evidence."""

    del session_id, cwd
    return {"status": "not_applicable", "evidence": None}


__all__ = [
    "VerificationEvidence",
    "classify_verification_command",
    "mark_workspace_edited",
    "record_terminal_result",
    "verification_ledger_enabled",
    "verification_status",
]
