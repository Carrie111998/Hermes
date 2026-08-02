"""Thread-scope identity, manifest storage, and fail-closed resolution.

A "scope" is the durable object representing one piece of work — one
Discord thread, one DM, one CLI project — so that a progress/status
question asked inside it can be answered from artifacts *that scope
actually owns*, not from whatever else is active in the same profile or
repository. See docs/design/thread-scope-isolation.md for the full design
and root-cause writeup.

Storage layout under the active profile's ``$HERMES_HOME``:

    scopes/<scope_id>.json   # manifest, mode 0600, atomic_json_write
    scopes/<scope_id>.lock   # advisory file lock (mirrors active_sessions.py)
    scopes/index.json        # identity-hash -> scope_id, same write pattern

Every read is fail-closed: a missing, corrupt, ambiguous, or
cross-profile/cross-scope reference is treated as absent and logged, never
guessed at. Identity is always derived from adapter-supplied fields
(platform, account, guild/workspace, chat, thread) — never from a display
name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

MANIFEST_MODE = 0o600

_OWNED_CATEGORIES = (
    "session_keys",
    "branches",
    "worktrees",
    "tmux_session_keys",
    "delegation_ids",
    "background_task_ids",
    "cron_job_ids",
    "prs",
)

_LIFECYCLES = ("active", "completed", "archived")


class ScopeIdentityError(ValueError):
    """Raised when a scope identity tuple can't be resolved.

    Callers must treat this as "scope unknown" and fail closed — never
    fall back to an unscoped search silently.
    """


# ---------------------------------------------------------------------------
# Identity normalization
# ---------------------------------------------------------------------------


def normalize_scope_identity(
    *,
    profile: str,
    platform: str,
    chat_id: str,
    account_id: Optional[str] = None,
    guild_scope_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    topic: Optional[str] = None,
) -> dict:
    """Build the canonical identity tuple a scope is keyed on.

    ``profile``, ``platform``, and ``chat_id`` are required — without them
    there is no durable identity to hash. Everything else is optional but
    included verbatim when present so two scopes that differ only by
    thread/topic never collide. Raises ScopeIdentityError (fail closed) on
    a missing required field or a value that is empty after stripping.
    """
    required = {"profile": profile, "platform": platform, "chat_id": chat_id}
    for name, value in required.items():
        if not isinstance(value, str) or not value.strip():
            raise ScopeIdentityError(f"missing or empty required identity field: {name}")

    def _clean(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    return {
        "profile": profile.strip(),
        "platform": platform.strip(),
        "account_id": _clean(account_id),
        "guild_scope_id": _clean(guild_scope_id),
        "chat_id": chat_id.strip(),
        "thread_id": _clean(thread_id),
        "topic": _clean(topic),
    }


def compute_scope_id(identity: dict) -> str:
    """Stable hash of the normalized identity tuple.

    Recomputed from live identity on every lookup — never trust a
    scope_id remembered from prior conversation text (that is exactly the
    compaction-summary contamination vector this feature exists to close).
    """
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


def _scopes_dir(hermes_home: Optional[Path] = None) -> Path:
    base = Path(hermes_home) if hermes_home is not None else Path(get_hermes_home())
    return base / "scopes"


def _manifest_path(scope_id: str, hermes_home: Optional[Path] = None) -> Path:
    return _scopes_dir(hermes_home) / f"{_safe_filename(scope_id)}.json"


def _lock_path(scope_id: str, hermes_home: Optional[Path] = None) -> Path:
    return _scopes_dir(hermes_home) / f"{_safe_filename(scope_id)}.lock"


def _index_path(hermes_home: Optional[Path] = None) -> Path:
    return _scopes_dir(hermes_home) / "index.json"


def _index_lock_path(hermes_home: Optional[Path] = None) -> Path:
    return _scopes_dir(hermes_home) / "index.lock"


def _safe_filename(scope_id: str) -> str:
    # scope_id is "sha256:<hex>" — drop the colon so it's a plain filename.
    return scope_id.replace(":", "_")


class _FileLock:
    """Advisory cross-platform exclusive lock. Mirrors hermes_cli/active_sessions.py."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        if os.name == "nt":
            try:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            except Exception as exc:
                self._fh.close()
                self._fh = None
                raise RuntimeError("scope manifest lock unavailable") from exc
        else:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except Exception as exc:
                self._fh.close()
                self._fh = None
                raise RuntimeError("scope manifest lock unavailable") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is None:
            return
        if os.name == "nt":
            try:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            self._fh.close()
        finally:
            self._fh = None


# ---------------------------------------------------------------------------
# Manifest read/write (fail-closed)
# ---------------------------------------------------------------------------


def _read_json_fail_closed(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Ignoring corrupt scope state at %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed (non-object) scope state at %s", path)
        return None
    return data


def load_scope(scope_id: str, hermes_home: Optional[Path] = None) -> Optional[dict]:
    """Fail-closed manifest read: missing/corrupt -> None, never raises."""
    return _read_json_fail_closed(_manifest_path(scope_id, hermes_home))


def _write_manifest(manifest: dict, hermes_home: Optional[Path] = None) -> None:
    path = _manifest_path(manifest["scope_id"], hermes_home)
    with _FileLock(_lock_path(manifest["scope_id"], hermes_home)):
        atomic_json_write(path, manifest, mode=MANIFEST_MODE)


def _load_index(hermes_home: Optional[Path] = None) -> dict:
    data = _read_json_fail_closed(_index_path(hermes_home))
    if data is None:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _write_index(entries: dict, hermes_home: Optional[Path] = None) -> None:
    atomic_json_write(_index_path(hermes_home), {"entries": entries}, mode=MANIFEST_MODE)


def resolve_scope_id(identity: dict, hermes_home: Optional[Path] = None) -> Optional[str]:
    """Look up an existing scope_id for a freshly-derived identity tuple.

    Never trusts a scope_id passed in from conversation text — always
    re-derives from live identity and looks it up here.
    """
    scope_id = compute_scope_id(identity)
    index = _load_index(hermes_home)
    entry = index.get(scope_id)
    if entry is None:
        return None
    # Ambiguity guard: by construction the hash key IS the scope_id, so a
    # mismatched stored value means index corruption -- fail closed.
    if entry != scope_id:
        logger.warning("Scope index entry mismatch for %s -- treating as absent", scope_id)
        return None
    return scope_id


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------


def create_scope(
    identity: dict,
    goal: str,
    *,
    included_topics: Optional[list] = None,
    excluded_topics: Optional[list] = None,
    hermes_home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Create (or return the existing) manifest for this identity tuple.

    Idempotent by identity: creating a scope twice for the same identity
    tuple returns the existing manifest rather than silently forking a
    duplicate scope for the same conversation.
    """
    scope_id = compute_scope_id(identity)
    # Hold the index lock across the existing-check + manifest write + index
    # update so two concurrent create_scope() calls for the same identity
    # can't both pass the existence check and race the index read-modify-
    # write (a lost index entry would leave a valid manifest on disk that
    # resolve_scope_id() could never find again).
    with _FileLock(_index_lock_path(hermes_home)):
        existing = load_scope(scope_id, hermes_home)
        if existing is not None:
            return existing

        ts = _iso(now)
        manifest = {
            "scope_id": scope_id,
            "identity": identity,
            "goal": goal,
            "included_topics": list(included_topics or []),
            "excluded_topics": list(excluded_topics or []),
            "lifecycle": "active",
            "created_at": ts,
            "updated_at": ts,
            "owned": {category: [] for category in _OWNED_CATEGORIES},
            "external_dependencies": [],
        }
        _write_manifest(manifest, hermes_home)

        index = _load_index(hermes_home)
        index[scope_id] = scope_id
        _write_index(index, hermes_home)
        return manifest


def _iso(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def link_artifact(
    scope_id: str,
    category: str,
    value: str,
    hermes_home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Append an owned-artifact id to a scope manifest. Dedupes; fail-closed on unknown scope/category."""
    if category not in _OWNED_CATEGORIES:
        raise ScopeIdentityError(f"unknown owned-artifact category: {category}")
    with _FileLock(_lock_path(scope_id, hermes_home)):
        manifest = load_scope(scope_id, hermes_home)
        if manifest is None:
            raise ScopeIdentityError(f"unknown scope_id: {scope_id}")
        bucket = manifest["owned"].setdefault(category, [])
        if value not in bucket:
            bucket.append(value)
            manifest["updated_at"] = _iso(now)
            atomic_json_write(
                _manifest_path(scope_id, hermes_home), manifest, mode=MANIFEST_MODE
            )
        return manifest


def unlink_artifact(
    scope_id: str,
    category: str,
    value: str,
    hermes_home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    if category not in _OWNED_CATEGORIES:
        raise ScopeIdentityError(f"unknown owned-artifact category: {category}")
    with _FileLock(_lock_path(scope_id, hermes_home)):
        manifest = load_scope(scope_id, hermes_home)
        if manifest is None:
            raise ScopeIdentityError(f"unknown scope_id: {scope_id}")
        bucket = manifest["owned"].setdefault(category, [])
        if value in bucket:
            bucket.remove(value)
            manifest["updated_at"] = _iso(now)
            atomic_json_write(
                _manifest_path(scope_id, hermes_home), manifest, mode=MANIFEST_MODE
            )
        return manifest


def add_dependency(
    scope_id: str,
    description: str,
    hermes_home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """External dependencies are tracked separately from verified progress."""
    with _FileLock(_lock_path(scope_id, hermes_home)):
        manifest = load_scope(scope_id, hermes_home)
        if manifest is None:
            raise ScopeIdentityError(f"unknown scope_id: {scope_id}")
        ts = _iso(now)
        manifest["external_dependencies"].append({"description": description, "linked_at": ts})
        manifest["updated_at"] = ts
        atomic_json_write(_manifest_path(scope_id, hermes_home), manifest, mode=MANIFEST_MODE)
        return manifest


def set_lifecycle(
    scope_id: str,
    lifecycle: str,
    hermes_home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    if lifecycle not in _LIFECYCLES:
        raise ScopeIdentityError(f"invalid lifecycle: {lifecycle}")
    with _FileLock(_lock_path(scope_id, hermes_home)):
        manifest = load_scope(scope_id, hermes_home)
        if manifest is None:
            raise ScopeIdentityError(f"unknown scope_id: {scope_id}")
        manifest["lifecycle"] = lifecycle
        manifest["updated_at"] = _iso(now)
        atomic_json_write(_manifest_path(scope_id, hermes_home), manifest, mode=MANIFEST_MODE)
        return manifest


def owns(scope_id: str, category: str, value: str, hermes_home: Optional[Path] = None) -> bool:
    """Positive-proof ownership check -- the only way callers should gate
    "is this artifact part of this scope's verified progress."

    Fail-closed: any error resolving the manifest returns False rather
    than raising, so a caller that forgets to catch an exception can't
    accidentally treat "unknown" as "owned."
    """
    try:
        manifest = load_scope(scope_id, hermes_home)
    except Exception:
        logger.warning("owns() failed to load scope %s", scope_id, exc_info=True)
        return False
    if manifest is None:
        return False
    bucket = manifest.get("owned", {}).get(category, [])
    return value in bucket


# ---------------------------------------------------------------------------
# Live-session identity bridge
# ---------------------------------------------------------------------------


def identity_from_session_env() -> Optional[dict]:
    """Normalize the current turn's identity from ``HERMES_SESSION_*``.

    Mirrors ``tools/cronjob_tools.py::_origin_from_env`` -- the same
    ContextVar/env-var bridge (``gateway.session_context.get_session_env``)
    already used to carry live session identity across tool-call boundaries.
    Returns None (fail closed) when the required fields aren't resolvable
    (e.g. a bare CLI/TUI session with no messaging origin) rather than
    guessing at a scope.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None

    platform = get_session_env("HERMES_SESSION_PLATFORM") or None
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID") or None
    profile = get_session_env("HERMES_SESSION_PROFILE") or "main"
    if not platform or not chat_id:
        return None

    try:
        return normalize_scope_identity(
            profile=profile,
            platform=platform,
            chat_id=chat_id,
            thread_id=get_session_env("HERMES_SESSION_THREAD_ID") or None,
        )
    except ScopeIdentityError:
        return None


def resolve_current_scope_id(hermes_home: Optional[Path] = None) -> Optional[str]:
    """Resolve (never create) the scope owning the current turn, if any.

    Fail-closed: returns None on any missing/ambiguous identity or absent
    scope -- callers must treat that as "scope unknown," never fall back
    to unscoped behavior silently.
    """
    identity = identity_from_session_env()
    if identity is None:
        return None
    return resolve_scope_id(identity, hermes_home=hermes_home)
