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
import io
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations are a deliberate v2 item; when added,
# they belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")
_OVERRIDE_MARKER = ".hermes-managed"
_OVERRIDE_MARKER_CONTENT = b"hermes-managed-scope-v1\n"

_CACHE_LOCK = threading.Lock()
# path_key -> ((dev, ino, mtime_ns, ctime_ns, size), parsed)
_CONFIG_CACHE: Dict[str, tuple] = {}


def _has_valid_override_marker(directory_fd: int) -> bool:
    """Return whether an opened directory carries explicit admin authorization."""
    marker_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        marker_fd = os.open(_OVERRIDE_MARKER, marker_flags, dir_fd=directory_fd)
    except OSError:
        return False
    try:
        marker_stat = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or marker_stat.st_uid != 0
            or marker_stat.st_mode & 0o022
        ):
            return False
        data = b""
        limit = len(_OVERRIDE_MARKER_CONTENT) + 1
        while len(data) < limit:
            chunk = os.read(marker_fd, limit - len(data))
            if not chunk:
                break
            data += chunk
        return data == _OVERRIDE_MARKER_CONTENT
    except OSError:
        return False
    finally:
        os.close(marker_fd)


def _is_trusted_managed_dir(path: Path) -> Optional[Path]:
    """Return a pinned-and-validated canonical admin policy directory."""
    opened_dir = _open_trusted_managed_dir(path)
    if opened_dir is None:
        return None
    resolved, directory_fd = opened_dir
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != 0
            or directory_stat.st_mode & 0o022
        ):
            return None
        if not _has_valid_override_marker(directory_fd):
            return None

        present = False
        for filename in ("config.yaml", ".env"):
            try:
                file_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return None
            present = True
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != 0
                or file_stat.st_mode & 0o022
            ):
                return None
        return resolved if present else None
    finally:
        os.close(directory_fd)


def _managed_dir_is_trusted(path: Path) -> Optional[Path]:
    """Canonical resolver seam overridden only by the hermetic test harness."""
    return _is_trusted_managed_dir(path)


def _managed_stat_is_trusted(file_stat: os.stat_result) -> bool:
    """Return whether an opened managed-scope inode is admin-controlled."""
    return file_stat.st_uid == 0 and not file_stat.st_mode & 0o022


def _managed_ancestor_stat_is_trusted(directory_stat: os.stat_result) -> bool:
    """Return whether users cannot replace root-owned children in an ancestor."""
    if directory_stat.st_uid != 0:
        return False
    writable = bool(directory_stat.st_mode & 0o022)
    sticky = bool(directory_stat.st_mode & stat.S_ISVTX)
    return not writable or sticky


def _open_trusted_managed_dir(managed_dir: Path):
    """Resolve and pin a managed directory through a trusted POSIX namespace.

    Each canonical component is opened relative to the already-pinned parent.
    That prevents a later pathname reopen from selecting a different inode.
    """
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        resolved = managed_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = -1
    try:
        current_fd = os.open(resolved.anchor or os.sep, directory_flags)
        for component in resolved.parts[1:]:
            ancestor_stat = os.fstat(current_fd)
            if not stat.S_ISDIR(
                ancestor_stat.st_mode
            ) or not _managed_ancestor_stat_is_trusted(ancestor_stat):
                return None
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd

        directory_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or not _managed_stat_is_trusted(
            directory_stat
        ):
            return None
        result = (resolved, current_fd)
        current_fd = -1
        return result
    except OSError:
        return None
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def get_managed_dir() -> Optional[Path]:
    """Resolve the trusted managed-scope directory, or ``None`` when absent."""
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        path = Path(override)
        trusted = _managed_dir_is_trusted(path)
        if trusted:
            if trusted is True:
                try:
                    return path.resolve(strict=True)
                except (OSError, RuntimeError):
                    trusted = None
            else:
                return Path(trusted)
        logger.warning(
            "managed scope: refusing invalid or untrusted "
            "HERMES_MANAGED_DIR=%s; falling back to the system managed scope",
            path,
        )
    return _DEFAULT_MANAGED_DIR if _DEFAULT_MANAGED_DIR.is_dir() else None


def invalidate_managed_cache() -> None:
    """Drop cached managed config. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()


def _open_managed_policy(managed_dir: Path, filename: str):
    """Open a validated policy inode relative to a pinned directory descriptor."""
    opened_dir = _open_trusted_managed_dir(managed_dir)
    if opened_dir is None:
        return None
    _resolved, directory_fd = opened_dir

    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            file_fd = os.open(filename, file_flags, dir_fd=directory_fd)
        except OSError:
            return None
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode) or not _managed_stat_is_trusted(
                file_stat
            ):
                os.close(file_fd)
                return None
        except OSError:
            os.close(file_fd)
            return None
        return file_fd, file_stat
    finally:
        os.close(directory_fd)


def get_managed_config_revision() -> Optional[Tuple[int, int, int, int, int]]:
    """Return the validated inode revision for managed ``config.yaml``.

    The signature comes from ``fstat`` on the same ``openat``/``O_NOFOLLOW``
    policy inode used by the loader, so outer caches never re-stat a pathname.
    """
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return None
    opened = _open_managed_policy(managed_dir, "config.yaml")
    if opened is None:
        return None
    file_fd, file_stat = opened
    try:
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
            file_stat.st_size,
        )
    finally:
        os.close(file_fd)


def _cached_read(
    managed_dir: Path,
    filename: str,
    cache: Optional[Dict[str, tuple]],
    parse,
):
    """Read a trusted policy inode and return a deepcopy of the parsed value.

    Returns ``None`` when the file is absent or fails to parse (fail-open). A
    parse failure is logged LOUDLY — the admin needs to know their policy isn't
    being applied — but never raises, so a malformed managed file can't brick
    startup.
    """
    opened = _open_managed_policy(managed_dir, filename)
    if opened is None:
        return None
    file_fd, file_stat = opened
    key = (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_size,
    )
    path = managed_dir / filename
    path_key = str(path)
    if cache is not None:
        with _CACHE_LOCK:
            hit = cache.get(path_key)
            if hit is not None and hit[0] == key:
                os.close(file_fd)
                return copy.deepcopy(hit[1])
    try:
        with os.fdopen(file_fd, "rb") as f:
            file_fd = -1
            parsed = parse(f.read())
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        logger.warning(
            "managed scope: failed to parse %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path,
            exc,
        )
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    if cache is not None:
        with _CACHE_LOCK:
            cache[path_key] = (key, copy.deepcopy(parsed))
    return parsed


def load_managed_config() -> dict:
    """Parsed managed config.yaml, or {} when absent/malformed (fail-open)."""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(
        managed_dir,
        "config.yaml",
        _CONFIG_CACHE,
        lambda raw: yaml.safe_load(raw.decode("utf-8")) or {},
    )
    return parsed if isinstance(parsed, dict) else {}


def load_managed_env() -> Dict[str, str]:
    """Parsed managed .env (KEY=VALUE), or {} when absent (fail-open)."""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    # dotenv interpolation depends on the live process environment. Caching the
    # parsed mapping would pin stale expansions when a referenced variable rotates.
    parsed = _cached_read(managed_dir, ".env", None, _parse_env)
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


def _parse_env(raw: bytes) -> Dict[str, str]:
    from dotenv import dotenv_values

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    parsed = dotenv_values(stream=io.StringIO(text))
    return {key: value for key, value in parsed.items() if value is not None}


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
