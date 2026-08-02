"""Managed scope — IT-pushed, user-immutable config & env layer.

A system-level directory (default ``/etc/hermes``, root-owned and not
user-writable) supplies ``config.yaml`` and ``.env`` values that WIN over the
user's ``~/.hermes/config.yaml`` and ``~/.hermes/.env`` on a per-leaf-key basis.

This is DISTINCT from ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED``,
which is a coarse package-manager write-lock (declarative-distro / formula
installs). That lock blocks all mutation; this layer injects specific immutable
values. The two are independent and may coexist.

v1 enforcement is filesystem permissions only — see
``docs/design/managed-scope.md`` §7. v1 is Linux/POSIX-first; ``get_managed_dir()``
is the single seam for adding macOS / Windows native locations later.

Attribution: do not reference any third-party product by name in this file.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations are a deliberate v2 item; when added,
# they belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")

_CACHE_LOCK = threading.Lock()
# canonical target path -> (mtime_ns, size, device, inode, parsed)
_CONFIG_CACHE: Dict[str, tuple] = {}
_ENV_CACHE: Dict[str, tuple] = {}
# canonical target path -> (mtime_ns, size, device, inode), or None when even
# stat/read failed. This
# not change managed scope's startup-safe fail-open merge semantics; it lets
# security-sensitive request guards detect that current administrator policy
# is degraded and choose a stricter local behavior.
_CONFIG_FAILURE_BY_PATH: Dict[str, Optional[tuple[int, int, int, int]]] = {}


def _under_pytest() -> bool:
    """True when running inside the test suite.

    Used to ignore the system default ``/etc/hermes`` during tests so a real
    managed scope on a developer/CI box can't leak policy into the suite. Tests
    that exercise managed scope set ``HERMES_MANAGED_DIR`` explicitly, which is
    still honored (the override path below runs before this guard takes effect).
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def get_managed_dir() -> Optional[Path]:
    """Resolve the managed-scope directory, or None when no scope is present.

    Resolution (highest priority first):
      1. ``$HERMES_MANAGED_DIR`` — deployment/bootstrap path override (IT-only;
         never persisted to any .env). Honored only when set to a non-empty value
         AND the directory exists.
      2. ``/etc/hermes`` — POSIX default, when it exists. Ignored under pytest so
         a real system managed scope can't leak into the test suite.

    A non-existent directory at either tier resolves to None (no managed scope),
    which is the common case and must be cheap + side-effect-free.
    """
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    if _under_pytest():
        return None
    return _DEFAULT_MANAGED_DIR if _DEFAULT_MANAGED_DIR.is_dir() else None


def invalidate_managed_cache() -> None:
    """Drop cached managed config/env. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()
        _CONFIG_FAILURE_BY_PATH.clear()


def _cache_path_key(path: Path) -> str:
    """Canonical target identity seam for symlink-safe managed caches."""
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _cached_read(
    path: Path,
    cache: Dict[str, tuple],
    parse,
    *,
    failure_cache: Optional[Dict[str, Optional[tuple[int, int, int, int]]]] = None,
):
    """Target-identity-keyed read. Returns a deepcopy of the parsed value.

    Returns ``None`` when the file is absent or fails to parse (fail-open). A
    parse failure is logged LOUDLY — the admin needs to know their policy isn't
    being applied — but never raises, so a malformed managed file can't brick
    startup. ``failure_cache`` records only the current degraded file state for
    security-sensitive consumers; normal managed-overlay behavior is unchanged.
    """
    path_key = _cache_path_key(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        if failure_cache is not None:
            with _CACHE_LOCK:
                failure_cache.pop(path_key, None)
        return None
    except OSError as exc:
        if failure_cache is not None:
            with _CACHE_LOCK:
                failure_cache[path_key] = None
        logger.warning(
            "managed scope: failed to access %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path,
            exc,
        )
        return None
    key = (st.st_mtime_ns, st.st_size, st.st_dev, st.st_ino)
    with _CACHE_LOCK:
        hit = cache.get(path_key)
        if hit is not None and hit[:4] == key:
            if failure_cache is not None:
                failure_cache.pop(path_key, None)
            return copy.deepcopy(hit[4])
    try:
        with open(path, encoding="utf-8") as f:
            parsed = parse(f)
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        if failure_cache is not None:
            with _CACHE_LOCK:
                failure_cache[path_key] = key
        logger.warning(
            "managed scope: failed to parse %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path,
            exc,
        )
        return None
    with _CACHE_LOCK:
        cache[path_key] = (*key, copy.deepcopy(parsed))
        if failure_cache is not None:
            failure_cache.pop(path_key, None)
    return parsed


def _parse_managed_config(f) -> dict:
    parsed = yaml.safe_load(f) or {}
    if not isinstance(parsed, dict):
        raise ValueError("managed config root must be a mapping")
    return parsed


def load_managed_config() -> dict:
    """Parsed managed config.yaml, or {} when absent/malformed (fail-open)."""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(
        managed_dir / "config.yaml",
        _CONFIG_CACHE,
        _parse_managed_config,
        failure_cache=_CONFIG_FAILURE_BY_PATH,
    )
    return parsed if isinstance(parsed, dict) else {}


def managed_config_load_degraded() -> bool:
    """Return whether the current managed config exists but cannot be trusted.

    The ordinary overlay remains startup-safe and fail-open. Privacy/security
    boundaries can call this after loading effective config and enforce a local
    fail-closed policy while the administrator repairs the managed file.
    """
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return False
    config_path = managed_dir / "config.yaml"
    load_managed_config()  # refresh signature-bound success/failure state
    with _CACHE_LOCK:
        return _cache_path_key(config_path) in _CONFIG_FAILURE_BY_PATH


def load_managed_env() -> Dict[str, str]:
    """Parsed managed .env (KEY=VALUE), or {} when absent (fail-open)."""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(managed_dir / ".env", _ENV_CACHE, _parse_env)
    return parsed if isinstance(parsed, dict) else {}


def apply_managed_overlay(config: dict) -> dict:
    """Overlay administrator-pinned config values on top of an already-built dict.

    The single, shared way for any config loader that builds its own dict
    (rather than going through hermes_cli.config.load_config) to honor managed
    scope. Mirrors hermes_cli.config._load_config_impl's managed merge exactly:

      * expand the managed config's ``${VAR}`` refs against the PROCESS env only
        (never user-config-defined refs), so a user cannot shadow a managed
        literal via a ${VAR} they control;
      * normalize the managed config's root ``model`` key (a bare ``model: x/y``
        string is promoted to ``model.default``) so it can't clobber the dict
        shape callers expect;
      * leaf-level deep-merge managed ON TOP, so managed wins per-leaf while
        sibling keys stay user-controlled.

    Fail-open: returns ``config`` unchanged if no managed scope is present or on
    any error — managed scope must never break a caller's startup. Mutates and
    returns ``config`` (callers pass a dict they own).
    """
    try:
        managed = load_managed_config()
        if not managed:
            return config
        # Imported lazily to avoid an import cycle (config imports managed_scope).
        from hermes_cli.config import _deep_merge, _expand_env_vars, _normalize_root_model_keys

        managed_expanded = _normalize_root_model_keys(_expand_env_vars(managed))
        # A bare ``model: x/y`` string in the managed file must merge as
        # ``model.default`` — otherwise _deep_merge would replace the caller's
        # ``model`` dict with a string and break every ``cfg["model"]["..."]``
        # read. _normalize_root_model_keys only promotes the string when there
        # are root provider/base_url keys to migrate, so handle the bare case
        # here (matches cli.py's own string-model handling).
        if isinstance(managed_expanded.get("model"), str):
            managed_expanded = dict(managed_expanded)
            managed_expanded["model"] = {"default": managed_expanded["model"]}
        return _deep_merge(config, managed_expanded)
    except Exception:  # noqa: BLE001 — overlay must never break a caller
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _parse_env(f) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """Dotted leaf keys pinned by the managed config (e.g. {'model.default'})."""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """True if the exact dotted config key is pinned by the managed layer."""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """True if the env var name is pinned by the managed .env layer."""
    return name in load_managed_env()
