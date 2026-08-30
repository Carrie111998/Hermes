"""ACP policy for artifact-certification capability and authorization.

The current certification filesystem primitive depends on POSIX openat and
no-follow flags. Unsupported runtimes are advertised explicitly and refused
before model execution. Certified turns also bind non-rendering denial gates
instead of omitting mutation authorization.
"""

from __future__ import annotations

import os
import sys
from typing import Any


class CertificationCapabilityError(RuntimeError):
    """The runtime cannot safely realize the artifact-certification contract."""


def _runtime_platform() -> str:
    return sys.platform


def artifact_certification_capability() -> dict[str, Any]:
    """Return the ACP extension payload for the current filesystem realization."""

    platform = _runtime_platform()
    required = (
        os.environ.get("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "").strip().lower()
        == "required"
    )
    if platform == "win32":
        return {
            "version": 1,
            "available": False,
            "required": required,
            "reason": "unsupported_platform",
        }

    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, flag) for flag in required_flags):
        return {
            "version": 1,
            "available": False,
            "required": required,
            "reason": "missing_secure_filesystem_primitives",
        }

    return {"version": 1, "available": True, "required": required}


def require_artifact_certification_capability() -> None:
    """Refuse unsupported certification before contract parsing or model work."""

    capability = artifact_certification_capability()
    if capability["available"]:
        return
    if _runtime_platform() == "win32":
        raise CertificationCapabilityError(
            "artifact certification is not supported on Windows by this Hermes build"
        )
    raise CertificationCapabilityError(
        "artifact certification is unavailable because secure POSIX filesystem "
        "primitives are missing"
    )


def deny_unrendered_terminal_approval(*_args: Any, **_kwargs: Any) -> str:
    """Deny a certified mutation without publishing model-authored details."""

    return "deny"


def deny_unrendered_edit_approval(_proposal: Any) -> bool:
    """Deny a certified edit without publishing its diff over ACP."""

    return False


__all__ = [
    "CertificationCapabilityError",
    "artifact_certification_capability",
    "deny_unrendered_edit_approval",
    "deny_unrendered_terminal_approval",
    "require_artifact_certification_capability",
]
