"""Persistent MCP tool-schema cache for lazy server startup.

Stores per-server tool manifests on disk so Hermes can register MCP tools
into the agent snapshot without spawning the stdio child process at idle
dashboard startup. Cache entries are partitioned by server, protocol era,
connection configuration, authentication identity, and TLS context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "mcp_schema_cache.json"
_cache_lock = threading.Lock()
CACHE_SCHEMA_EPOCH = 2
MAX_TTL_MS = 24 * 60 * 60 * 1000


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / _CACHE_FILENAME


def _digest(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolved_identity_header(config: dict) -> Any:
    identity = config.get("identity_header")
    if not isinstance(identity, dict):
        return identity
    resolved = dict(identity)
    if str(identity.get("value_from") or "static").strip().lower() == "profile":
        try:
            from hermes_cli.profiles import get_active_profile_name

            resolved["resolved_value"] = get_active_profile_name()
        except Exception:
            resolved["resolved_value"] = None
    return resolved


def _file_context(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_file_context(item) for item in value]
    if not isinstance(value, str) or not value:
        return value
    path = Path(value).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "present": False}
    return {
        "path": str(path),
        "present": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _oauth_credential_context(server_name: Optional[str], config: dict) -> Any:
    if not server_name or str(config.get("auth") or "").strip().lower() != "oauth":
        return None
    try:
        from tools.mcp_oauth import _get_token_dir, _safe_filename

        token_dir = _get_token_dir()
        stem = _safe_filename(server_name)
        paths = {
            "tokens": token_dir / f"{stem}.json",
            "client": token_dir / f"{stem}.client.json",
            "metadata": token_dir / f"{stem}.meta.json",
        }
    except Exception:
        return None
    identity = {}
    for label, path in paths.items():
        try:
            identity[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            identity[label] = None
    return identity


def _configured_policy(config: dict) -> str:
    from tools.mcp_protocol import normalize_protocol_policy

    if "protocol" in config:
        return normalize_protocol_policy(config["protocol"]).value
    return normalize_protocol_policy().value


def expected_protocol_era(config: dict) -> Optional[str]:
    policy = _configured_policy(config)
    if policy == "modern":
        return "modern"
    if policy == "legacy":
        return "legacy"
    return None


def config_digest(config: dict) -> str:
    payload = {
        "config": config,
        "resolved_identity_header": _resolved_identity_header(config),
    }
    return _digest(payload)[:16]


def config_fingerprint(config: dict, *, server_name: Optional[str] = None) -> str:
    tools_filter = config.get("tools") or {}
    ssl_verify = config.get("ssl_verify", True)
    payload = {
        "cache_schema_epoch": CACHE_SCHEMA_EPOCH,
        "command": config.get("command"),
        "args": config.get("args") or [],
        "url": config.get("url"),
        "transport": config.get("transport"),
        "tools_include": sorted(tools_filter.get("include") or []),
        "tools_exclude": sorted(tools_filter.get("exclude") or []),
        "protocol_policy": _configured_policy(config),
        "headers": config.get("headers") or {},
        "environment": config.get("env") or {},
        "auth": config.get("auth"),
        "oauth": config.get("oauth") or {},
        "oauth_credentials": _oauth_credential_context(server_name, config),
        "identity_header": _resolved_identity_header(config),
        "ssl_verify": _file_context(ssl_verify),
        "client_cert": _file_context(config.get("client_cert")),
        "client_key": _file_context(config.get("client_key")),
        "strict_redirect_headers": bool(config.get("strict_redirect_headers")),
    }
    return _digest(payload)[:16]


def _entry_key(server_name: str, fingerprint: str, protocol_era: str) -> str:
    return f"{server_name}::{fingerprint}::{protocol_era}"


def _load_all() -> Dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Could not read MCP schema cache %s: %s", path, exc)
        return {}


def _save_all(data: Dict[str, Any]) -> None:
    from utils import atomic_json_write

    # Cache dir + 0o600: sibling precedent in tools/registry.py
    # _save_discovery_cache; the cache file is trusted input on the lazy
    # registration path, so keep it user-only.
    atomic_json_write(_cache_path(), data, mode=0o600)


def get_cached_entry(
    server_name: str,
    fingerprint: str,
    *,
    config_digest: Optional[str] = None,
    protocol_era: Optional[str] = None,
) -> Optional[dict]:
    """Return cached entry when fingerprint matches (and TTL holds), else None.

    MCP 2026-07-28 (SEP-2549): ``tools/list`` results carry ``ttlMs`` as a
    freshness hint. When the live discovery path recorded one, an entry
    older than its TTL is treated as a miss so the next startup re-probes
    the server instead of serving a stale manifest forever. Entries without
    a recorded TTL (pre-2026 servers) keep the old never-expires behavior.
    ``cacheScope`` is irrelevant here: this cache is per-user local disk,
    which satisfies even ``private``.
    """
    eras = (protocol_era,) if protocol_era is not None else ("modern", "legacy")
    with _cache_lock:
        data = _load_all()
    for era in eras:
        entry = data.get(_entry_key(server_name, fingerprint, era))
        if not isinstance(entry, dict):
            continue
        if entry.get("epoch") != CACHE_SCHEMA_EPOCH:
            continue
        if entry.get("fingerprint") != fingerprint:
            continue
        if entry.get("protocol_era") != era:
            continue
        if config_digest is not None and entry.get("config_digest") != config_digest:
            continue
        cache_scope = entry.get("cache_scope")
        if cache_scope is not None and cache_scope not in {"public", "private"}:
            continue
        ttl_ms = entry.get("ttl_ms")
        written_at = entry.get("written_at")
        if era == "modern" and not (
            isinstance(ttl_ms, (int, float))
            and not isinstance(ttl_ms, bool)
            and isinstance(written_at, (int, float))
        ):
            continue
        if isinstance(ttl_ms, (int, float)) and not isinstance(ttl_ms, bool):
            effective_ttl = min(max(float(ttl_ms), 0.0), float(MAX_TTL_MS))
            if not isinstance(written_at, (int, float)):
                continue
            if (time.time() - written_at) * 1000.0 >= effective_ttl:
                continue
        return entry
    return None


def has_cached_entry(
    server_name: str,
    fingerprint: str,
    *,
    config_digest: Optional[str] = None,
    protocol_era: Optional[str] = None,
) -> bool:
    return (
        get_cached_entry(
            server_name,
            fingerprint,
            config_digest=config_digest,
            protocol_era=protocol_era,
        )
        is not None
    )


def write_cache_entry(
    server_name: str,
    fingerprint: str,
    *,
    config_digest: Optional[str] = None,
    protocol_era: str = "legacy",
    tools: List[dict],
    utility_tools: Optional[List[dict]] = None,
    ttl_ms: Optional[float] = None,
    cache_scope: Optional[str] = None,
) -> None:
    """Persist tool schemas after a successful live connect.

    ``ttl_ms``/``cache_scope`` are the SEP-2549 hints from the server's
    ``tools/list`` result (2026-07-28 servers). ``written_at`` anchors TTL
    expiry in :func:`get_cached_entry`.
    """
    if protocol_era not in {"modern", "legacy"}:
        raise ValueError(f"Unsupported MCP cache protocol era: {protocol_era!r}")
    entry = {
        "epoch": CACHE_SCHEMA_EPOCH,
        "fingerprint": fingerprint,
        "config_digest": config_digest,
        "protocol_era": protocol_era,
        "tools": tools,
        "utility_tools": utility_tools or [],
    }
    valid_ttl = (
        isinstance(ttl_ms, (int, float))
        and not isinstance(ttl_ms, bool)
    )
    if protocol_era == "modern" and not valid_ttl:
        ttl_ms = 0.0
        valid_ttl = True
    if valid_ttl:
        entry["ttl_ms"] = min(max(float(ttl_ms), 0.0), float(MAX_TTL_MS))
        entry["written_at"] = time.time()
    if protocol_era == "modern" and cache_scope not in {"public", "private"}:
        cache_scope = "private"
    if cache_scope in {"public", "private"}:
        entry["cache_scope"] = cache_scope
    key = _entry_key(server_name, fingerprint, protocol_era)
    with _cache_lock:
        data = _load_all()
        # Write-through fires on every registration (reconnects,
        # list_changed refreshes); skip the load-all+rewrite churn when the
        # entry is byte-identical to what is already on disk. TTL'd entries
        # always rewrite: written_at must advance or the entry would expire
        # at its ORIGINAL write time no matter how many live reconnects
        # confirmed it since.
        if "written_at" not in entry and data.get(key) == entry:
            return
        data[key] = entry
        _save_all(data)


def clear_cache_entry(server_name: str) -> None:
    prefix = f"{server_name}::"
    with _cache_lock:
        data = _load_all()
        stale = [
            key
            for key in data
            if key == server_name or key.startswith(prefix)
        ]
        if not stale:
            return
        for key in stale:
            del data[key]
        _save_all(data)


def tools_from_cache_entry(entry: dict) -> List[dict]:
    """Return cached MCP tool dicts (name, description, inputSchema)."""
    tools = entry.get("tools")
    return list(tools) if isinstance(tools, list) else []


def utility_tools_from_cache_entry(entry: dict) -> List[dict]:
    util = entry.get("utility_tools")
    return list(util) if isinstance(util, list) else []
