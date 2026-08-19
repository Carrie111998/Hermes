"""Closed, non-secret receipts for model-bound SOUL and skill activation."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ACTIVATION_MODES = {
    "auto",
    "bundle",
    "cache_rebuild",
    "cron",
    "preload",
    "session_start",
    "skill_view",
    "slash",
}


def digest_bytes(content: bytes) -> str:
    """Return the receipt wire-format digest for exact source bytes."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def digest_text(content: str) -> str:
    """Return the receipt wire-format digest for exact effective UTF-8 text."""
    return digest_bytes(content.encode("utf-8"))


def current_profile_id() -> str:
    """Return the non-secret profile identifier for the active Hermes home."""
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        home = Path(get_hermes_home()).resolve()
        profiles = (Path(get_default_hermes_root()) / "profiles").resolve()
        relative = home.relative_to(profiles)
        return relative.parts[0] if relative.parts else "default"
    except Exception:
        return "default"


def emit_activation_receipt(
    *,
    profile_id: str,
    session_id: str,
    component_type: str,
    component_name: str,
    activation_mode: str,
    raw_digest: str,
    effective_digest: str,
) -> None:
    """Notify observers with a validated, content-free activation receipt.

    Invalid or incomplete metadata is not emitted. Observer discovery and
    callback failures are isolated so this optional capability cannot alter
    prompt construction or request behavior.
    """
    try:
        if (
            component_type not in {"soul", "skill"}
            or activation_mode not in _ACTIVATION_MODES
            or not _IDENTIFIER_RE.fullmatch(profile_id or "")
            or not component_name
            or len(component_name) > 256
            or not component_name.isprintable()
            or not session_id
            or len(session_id) > 512
            or not session_id.isprintable()
            or not _DIGEST_RE.fullmatch(raw_digest or "")
            or not _DIGEST_RE.fullmatch(effective_digest or "")
        ):
            logger.warning("Skipped invalid activation receipt metadata")
            return

        from hermes_cli import lifecycle

        if not lifecycle.has_hook("on_activation_receipt"):
            return
        lifecycle.invoke_hook(
            "on_activation_receipt",
            receipt={
                "schema_version": 1,
                "receipt_id": f"activation:{uuid.uuid4().hex}",
                "profile_id": profile_id,
                "session_id": session_id,
                "component_type": component_type,
                "component_name": component_name,
                "activation_mode": activation_mode,
                "raw_digest": raw_digest,
                "effective_digest": effective_digest,
            },
        )
    except Exception:
        logger.warning("Activation receipt observer failed", exc_info=True)
