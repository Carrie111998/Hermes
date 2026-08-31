"""Compression dispatch helpers — attempt creation + indeterminate response.

Bounded module owning the gateway-side attempt lifecycle at dispatch time:
creating the durable attempt record, resolving the family key, capturing
the watermark, and constructing the indeterminate response.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def create_attempt_for_dispatch(
    db: Any,
    session: Dict[str, Any],
    sid: str,
) -> Tuple[str, str, str, int, int]:
    """Create a compression attempt for the compute-host dispatch path.

    Returns (attempt_id, family_key, parent_session_id, input_watermark, input_history_version).
    The attempt is inserted in pending state; caller transitions to running after lock acquisition.
    """
    attempt_id = uuid.uuid4().hex
    parent_session_id = str(session.get("session_key") or sid)
    family_key = parent_session_id
    input_watermark = 0
    input_history_version = int(session.get("history_version", 0) or 0)

    if db is None or not parent_session_id:
        raise RuntimeError("compression dispatch: no database or session")

    prow = db.get_session(parent_session_id)
    if prow and prow.get("session_key"):
        family_key = str(prow["session_key"])
    input_watermark = int(db.get_active_message_watermark(parent_session_id) or 0)
    # Hard admission gate: durable attempt creation must succeed before work is admitted.
    db.create_compression_attempt(
        attempt_id=attempt_id,
        session_key=family_key,
        parent_session_id=parent_session_id,
        input_history_version=input_history_version,
        input_watermark=input_watermark,
        holder=attempt_id,
    )

    return attempt_id, family_key, parent_session_id, input_watermark, input_history_version


def create_attempt_for_inline_compress(
    db: Any,
    session: Dict[str, Any],
    sid: str,
) -> Tuple[str, str, str, int, int]:
    """Create a compression attempt for the in-process (non-compute-host) path.

    Same as create_attempt_for_dispatch but also transitions pending → running
    since there is no 120s waiter to wait for lock acquisition.

    Returns (attempt_id, family_key, parent_session_id, input_watermark, input_history_version).
    """
    attempt_id = uuid.uuid4().hex
    parent_session_id = str(session.get("session_key") or sid)
    family_key = parent_session_id
    input_watermark = 0
    input_history_version = int(session.get("history_version", 0) or 0)

    if db is None or not parent_session_id:
        raise RuntimeError("compression dispatch: no database or session")

    prow = db.get_session(parent_session_id)
    if prow and prow.get("session_key"):
        family_key = str(prow["session_key"])
    input_watermark = int(db.get_active_message_watermark(parent_session_id) or 0)
    # Hard admission gate: durable attempt creation must succeed before work is admitted.
    db.create_compression_attempt(
        attempt_id=attempt_id,
        session_key=family_key,
        parent_session_id=parent_session_id,
        input_history_version=input_history_version,
        input_watermark=input_watermark,
        holder=attempt_id,
    )
    db.transition_compression_attempt_pending_to_running(attempt_id)

    return attempt_id, family_key, parent_session_id, input_watermark, input_history_version


def build_indeterminate_response(
    attempt_id: str,
    family_key: str,
    parent_session_id: str,
    input_watermark: int,
    input_history_version: int,
) -> Dict[str, Any]:
    """Construct the indeterminate response dict for session.compress timeout."""
    return {
        "status": "indeterminate",
        "attempt_id": attempt_id,
        "session_key": family_key,
        "parent_session_id": parent_session_id,
        "input_watermark": input_watermark,
        "input_history_version": input_history_version,
    }
