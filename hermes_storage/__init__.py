"""Remote MongoDB-backed storage for Hermes Agent.

When bootstrap.yaml (or HERMES_MONGO_URI) is present, durable state lives in
MongoDB rather than local files / SQLite. Local disk keeps only the bootstrap
file plus optional caches (skill materialize, logs, PIDs).
"""

from __future__ import annotations

from hermes_storage.bootstrap import (
    BootstrapConfig,
    get_bootstrap,
    is_mongo_mode,
    load_bootstrap,
    reset_bootstrap_cache,
)
from hermes_storage.errors import MongoStorageError
from hermes_storage.factory import get_storage, require_storage, reset_storage

__all__ = [
    "BootstrapConfig",
    "MongoStorageError",
    "get_bootstrap",
    "is_mongo_mode",
    "load_bootstrap",
    "reset_bootstrap_cache",
    "get_storage",
    "require_storage",
    "reset_storage",
]
