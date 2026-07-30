"""MongoDB client helpers for Hermes storage."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CLIENT = None
_URI: Optional[str] = None
_CLIENT_OPTS_KEY: Optional[str] = None


def get_pymongo():
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError

        return MongoClient, PyMongoError
    except ImportError as exc:
        raise ImportError(
            "pymongo is required for Mongo storage mode. "
            "Install with: pip install 'pymongo>=4.6'  or  uv add pymongo"
        ) from exc


def _client_kwargs_from_bootstrap(boot: Any) -> dict[str, Any]:
    """Build MongoClient TLS/auth kwargs from BootstrapConfig."""
    kwargs: dict[str, Any] = {
        "serverSelectionTimeoutMS": 8000,
        "retryWrites": True,
        "w": "majority",
    }
    if boot is None:
        return kwargs

    ca = boot.resolved_tls_ca() if hasattr(boot, "resolved_tls_ca") else None
    cert = boot.resolved_tls_cert_key() if hasattr(boot, "resolved_tls_cert_key") else None
    auth_mode = getattr(boot, "auth_mode", "uri") or "uri"

    if ca is not None:
        kwargs["tls"] = True
        kwargs["tlsCAFile"] = str(ca)
    if cert is not None:
        kwargs["tls"] = True
        kwargs["tlsCertificateKeyFile"] = str(cert)
    if getattr(boot, "tls_allow_invalid_hostnames", False):
        kwargs["tlsAllowInvalidHostnames"] = True

    if auth_mode == "x509":
        # Prefer explicit mechanism; URI should also include authMechanism.
        kwargs["authMechanism"] = "MONGODB-X509"
        kwargs.setdefault("tls", True)

    return kwargs


def get_client(uri: str, *, force: bool = False, bootstrap: Any = None):
    global _CLIENT, _URI, _CLIENT_OPTS_KEY

    if bootstrap is None:
        try:
            from hermes_storage.bootstrap import get_bootstrap
            bootstrap = get_bootstrap()
        except Exception:
            bootstrap = None

    kwargs = _client_kwargs_from_bootstrap(bootstrap)
    opts_key = f"{uri}|{sorted((k, str(v)) for k, v in kwargs.items())}"

    if _CLIENT is not None and _URI == uri and _CLIENT_OPTS_KEY == opts_key and not force:
        return _CLIENT

    MongoClient, _ = get_pymongo()
    _CLIENT = MongoClient(uri, **kwargs)
    _URI = uri
    _CLIENT_OPTS_KEY = opts_key
    # Fail fast on bad URI / certs
    _CLIENT.admin.command("ping")
    return _CLIENT


def close_client() -> None:
    global _CLIENT, _URI, _CLIENT_OPTS_KEY
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
    _CLIENT = None
    _URI = None
    _CLIENT_OPTS_KEY = None


def ensure_indexes(shared_db: Any, profile_db: Any) -> None:
    """Create indexes used by Hermes stores (idempotent)."""
    try:
        shared_db["cluster_nodes"].create_index("node_id", unique=True)
        shared_db["cluster_nodes"].create_index("heartbeat_at")
        shared_db["cluster_state"].create_index("_id", unique=True)
        shared_db["skills"].create_index("name", unique=True)
        shared_db["settings"].create_index("key", unique=True)
        shared_db["knowledge"].create_index("key", unique=True)
        shared_db["agent_registry"].create_index("machine_id", unique=True)
        shared_db["agent_registry"].create_index("cert_cn", unique=True, sparse=True)

        profile_db["config"].create_index("key", unique=True)
        profile_db["secrets"].create_index("key", unique=True)
        profile_db["soul"].create_index("key", unique=True)
        profile_db["memories"].create_index("key", unique=True)
        profile_db["sessions"].create_index("session_id", unique=True)
        profile_db["sessions"].create_index([("source", 1), ("started_at", -1)])
        profile_db["messages"].create_index([("session_id", 1), ("message_index", 1)], unique=True)
        profile_db["messages"].create_index([("content", "text")])
        profile_db["gateway_routing"].create_index([("scope", 1), ("key", 1)], unique=True)
        profile_db["machines"].create_index("machine_id", unique=True)
    except Exception as exc:
        logger.warning("Failed to ensure Mongo indexes: %s", exc)
