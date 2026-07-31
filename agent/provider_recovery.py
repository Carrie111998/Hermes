"""Core success-only publication of durable provider recovery proof.

This module deliberately accepts no request/response payload, credential, usage,
or retry metadata.  A publication is authorized only by the three trusted scope
values explicitly injected into a supervised Kanban worker's environment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Mapping, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.profiles import normalize_profile_name, validate_profile_name


logger = logging.getLogger(__name__)

KANBAN_DB_ENV = "HERMES_KANBAN_DB"
WORKER_PROFILE_ENV = "HERMES_PROFILE"
CREDENTIAL_GENERATION_ENV = "HERMES_PROVIDER_CREDENTIAL_GENERATION"

_PUBLISHER_ID = "hermes-agent-core"
_PUBLISHER_VERSION = "r2a"
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
_DB_BUSY_TIMEOUT_MS = 250


def _trusted_scope_from_env(
    environ: Mapping[str, str],
) -> Optional[tuple[Path, str, int]]:
    """Return strict supervised scope or ``None`` without touching the DB."""
    raw_db_path = environ.get(KANBAN_DB_ENV)
    raw_profile = environ.get(WORKER_PROFILE_ENV)
    raw_generation = environ.get(CREDENTIAL_GENERATION_ENV)
    if not raw_db_path or not raw_profile or not raw_generation:
        return None

    db_path = Path(raw_db_path)
    try:
        if not db_path.is_absolute() or not db_path.is_file():
            return None
    except OSError:
        return None

    try:
        normalized_profile = normalize_profile_name(raw_profile)
        validate_profile_name(normalized_profile)
    except (TypeError, ValueError):
        return None
    if normalized_profile != raw_profile:
        return None

    if _POSITIVE_INTEGER_RE.fullmatch(raw_generation) is None:
        return None
    generation = int(raw_generation)
    if generation <= 0:
        return None

    return db_path, normalized_profile, generation


def _stable_proof_id(*, request_id: str, session_id: str) -> Optional[str]:
    """Derive an opaque id from non-secret request/session identifiers only."""
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    request_bytes = request_id.encode("utf-8")
    session_bytes = session_id.encode("utf-8")
    material = (
        len(session_bytes).to_bytes(8, "big")
        + session_bytes
        + len(request_bytes).to_bytes(8, "big")
        + request_bytes
    )
    digest = hashlib.sha256(material).hexdigest()
    return f"provider-request:{digest}"


def _open_recovery_db(db_path: Path) -> sqlite3.Connection:
    """Open an existing trusted DB read/write with a short lock timeout."""
    uri = f"{db_path.as_uri()}?mode=rw"
    conn = sqlite3.connect(
        uri,
        uri=True,
        isolation_level=None,
        timeout=_DB_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_DB_BUSY_TIMEOUT_MS}")
    return conn


def publish_successful_live_provider_request(
    *,
    provider: str,
    request_id: str,
    session_id: str,
    provider_observed_at: int,
) -> bool:
    """Durably publish one normalized live-provider success proof.

    Invalid or absent supervised scope fails closed.  Once a model request has
    succeeded, database failures fail open: they emit one fixed, bounded,
    non-secret diagnostic and never alter the successful model result.
    """
    trusted_scope = _trusted_scope_from_env(os.environ)
    if trusted_scope is None:
        return False
    db_path, profile, generation = trusted_scope

    proof_id = _stable_proof_id(request_id=request_id, session_id=session_id)
    if proof_id is None:
        return False
    if (
        not isinstance(provider_observed_at, int)
        or isinstance(provider_observed_at, bool)
        or provider_observed_at <= 0
    ):
        return False

    try:
        scope = kb.ProviderRecoveryScope(
            profile=profile,
            provider=provider,
            credential_generation=generation,
        )
        proof = kb.ProviderRecoveryProof(
            stable_proof_id=proof_id,
            scope=scope,
            kind=kb.ProviderRecoveryProofKind.LIVE_REQUEST_SUCCEEDED,
            provider_observed_at=provider_observed_at,
            publisher_id=_PUBLISHER_ID,
            publisher_version=_PUBLISHER_VERSION,
        )
    except (TypeError, ValueError):
        return False

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _open_recovery_db(db_path)
        kb.publish_provider_recovery_event(conn, proof)
    except Exception as exc:
        error_type = type(exc).__name__[:64]
        logger.warning(
            "Provider recovery proof publication failed (%s)",
            error_type,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return True
