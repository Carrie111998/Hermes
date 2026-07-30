"""Errors for Mongo remote storage (fail-hard, no silent SQLite/local fallback)."""

from __future__ import annotations


class MongoStorageError(RuntimeError):
    """Mongo mode is enabled but remote storage cannot be used.

    Callers must not fall back to local ``state.db``, ``config.yaml``, ``.env``,
    ``SOUL.md``, or memory files — that splits fleet state across machines.
    """


def raise_mongo_unavailable(reason: str, *, cause: BaseException | None = None) -> None:
    msg = (
        f"Mongo remote storage required but unavailable: {reason}. "
        "Refusing local durable fallback (would split active/passive fleet state). "
        "Fix connectivity/certs or disable Mongo mode (remove bootstrap.yaml)."
    )
    if cause is not None:
        raise MongoStorageError(msg) from cause
    raise MongoStorageError(msg)
