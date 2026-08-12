"""Host-owned integrity evidence for native directory plugins.

The evidence store lives under the active profile, outside every plugin
package.  User and project plugin entrypoints are read, hashed, and returned
from the same open file so callers can execute the verified bytes without a
second path lookup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_STORE_VERSION = 1
_STORE_DIR = "plugin-integrity"
_STORE_FILE = "directory-plugins.json"
_CONFIG_VERSION_GATE = 35
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_LOCK = threading.RLock()


class PluginIntegrityError(RuntimeError):
    """Raised when directory-plugin integrity cannot be established safely."""


def evidence_path() -> Path:
    return get_hermes_home() / _STORE_DIR / _STORE_FILE


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise PluginIntegrityError(
                f"duplicate key {key!r} in plugin integrity evidence"
            )
        obj[key] = value
    return obj


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    """Read one ordinary non-symlink file without following a replaced link."""
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise PluginIntegrityError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise PluginIntegrityError(f"cannot inspect {label} {path}: {exc}") from exc

    if stat.S_ISLNK(before.st_mode):
        raise PluginIntegrityError(f"refusing symlinked {label}: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise PluginIntegrityError(f"refusing non-regular {label}: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PluginIntegrityError(f"cannot open {label} {path}: {exc}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PluginIntegrityError(f"refusing non-regular {label}: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)

    try:
        after = path.lstat()
    except OSError as exc:
        raise PluginIntegrityError(f"{label} changed while being read: {path}") from exc
    if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
        raise PluginIntegrityError(f"{label} changed type while being read: {path}")
    if (
        before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or opened.st_size != len(data)
    ):
        raise PluginIntegrityError(f"{label} changed while being read: {path}")
    return data


def _canonical_plugin_dir(plugin_dir: Path) -> str:
    try:
        if plugin_dir.is_symlink():
            raise PluginIntegrityError(f"refusing symlinked plugin directory: {plugin_dir}")
        resolved = plugin_dir.resolve(strict=True)
    except OSError as exc:
        raise PluginIntegrityError(
            f"cannot resolve plugin directory {plugin_dir}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise PluginIntegrityError(f"plugin path is not a directory: {plugin_dir}")
    return str(resolved)


def _validate_record(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PluginIntegrityError("plugin integrity record must be an object")
    if set(raw) != {"key", "path", "entrypoint", "sha256"}:
        raise PluginIntegrityError("plugin integrity record has unsafe fields")
    key = raw.get("key")
    plugin_path = raw.get("path")
    entrypoint = raw.get("entrypoint")
    digest = raw.get("sha256")
    if (
        not isinstance(key, str)
        or not key
        or key.strip() != key
        or "\\" in key
        or any(segment in {"", ".", ".."} for segment in key.split("/"))
    ):
        raise PluginIntegrityError("plugin integrity record has an unsafe key")
    if not isinstance(plugin_path, str) or not Path(plugin_path).is_absolute():
        raise PluginIntegrityError("plugin integrity record path must be absolute")
    resolved_path = str(Path(plugin_path).resolve(strict=False))
    if os.path.normcase(plugin_path) != os.path.normcase(resolved_path):
        raise PluginIntegrityError("plugin integrity record path is not canonical")
    if entrypoint != "__init__.py":
        raise PluginIntegrityError("plugin integrity record has an unsafe entrypoint")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise PluginIntegrityError("plugin integrity record has an invalid SHA-256 digest")
    return {
        "key": key,
        "path": plugin_path,
        "entrypoint": entrypoint,
        "sha256": digest,
    }


def _load_records(*, missing_ok: bool) -> list[dict[str, str]]:
    store = evidence_path()
    if not store.exists() and not store.is_symlink():
        if missing_ok:
            return []
        raise PluginIntegrityError(f"missing plugin integrity evidence: {store}")
    data = _read_regular_bytes(store, label="plugin integrity evidence")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except PluginIntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginIntegrityError(f"invalid plugin integrity evidence: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "plugins"}:
        raise PluginIntegrityError("plugin integrity evidence has an unsafe structure")
    if payload.get("version") != _STORE_VERSION:
        raise PluginIntegrityError("unsupported plugin integrity evidence version")
    raw_records = payload.get("plugins")
    if not isinstance(raw_records, list):
        raise PluginIntegrityError("plugin integrity evidence plugins must be a list")

    records = [_validate_record(raw) for raw in raw_records]
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()
    for record in records:
        if record["key"] in seen_keys or record["path"] in seen_paths:
            raise PluginIntegrityError("duplicate plugin integrity evidence")
        seen_keys.add(record["key"])
        seen_paths.add(record["path"])
    return records


def _write_records(records: list[dict[str, str]]) -> None:
    store = evidence_path()
    parent = store.parent
    if parent.exists() and parent.is_symlink():
        raise PluginIntegrityError(f"refusing symlinked integrity directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise PluginIntegrityError(f"unsafe integrity directory: {parent}")

    payload = {
        "version": _STORE_VERSION,
        "plugins": sorted(records, key=lambda item: (item["key"], item["path"])),
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".directory-plugins-", dir=parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, store)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@contextmanager
def _locked_evidence_update():
    """Serialize evidence read-modify-write across threads and processes."""
    store = evidence_path()
    parent = store.parent
    if parent.exists() and parent.is_symlink():
        raise PluginIntegrityError(f"refusing symlinked integrity directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise PluginIntegrityError(f"unsafe integrity directory: {parent}")

    lock_path = parent / ".directory-plugins.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _PROCESS_LOCK:
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise PluginIntegrityError(
                f"cannot open plugin integrity lock {lock_path}: {exc}"
            ) from exc
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or lock_path.is_symlink():
                raise PluginIntegrityError(
                    f"refusing unsafe plugin integrity lock: {lock_path}"
                )
            if os.name == "nt":
                import msvcrt

                if opened.st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except PluginIntegrityError:
            raise
        except OSError as exc:
            raise PluginIntegrityError(
                f"cannot lock plugin integrity evidence: {exc}"
            ) from exc
        finally:
            handle.close()


def integrity_enforced() -> bool:
    """Return whether this profile has crossed the host-evidence boundary."""
    store = evidence_path()
    if store.exists() or store.is_symlink():
        return True
    config_path = get_hermes_home() / "config.yaml"
    try:
        raw = config_path.read_text(encoding="utf-8")
        from utils import fast_safe_load

        config = fast_safe_load(raw) or {}
        version = config.get("_config_version", 0) if isinstance(config, dict) else 0
        return int(version) >= _CONFIG_VERSION_GATE
    except (OSError, TypeError, ValueError):
        return False


def record_plugin_entrypoint(key: str, plugin_dir: Path) -> None:
    """Replace host-owned evidence for one exact directory entrypoint."""
    canonical = _canonical_plugin_dir(plugin_dir)
    entrypoint = plugin_dir / "__init__.py"
    data = _read_regular_bytes(entrypoint, label="plugin entrypoint")
    record = {
        "key": key,
        "path": canonical,
        "entrypoint": "__init__.py",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _validate_record(record)
    with _locked_evidence_update():
        records = _load_records(missing_ok=True)
        records = [
            item
            for item in records
            if item["key"] != key and item["path"] != canonical
        ]
        records.append(record)
        _write_records(records)


def remove_plugin_evidence(key: str, plugin_dir: Path | None = None) -> None:
    store = evidence_path()
    canonical: str | None = None
    if plugin_dir is not None:
        try:
            canonical = str(plugin_dir.resolve(strict=False))
        except OSError:
            canonical = None
    with _locked_evidence_update():
        if not store.exists() and not store.is_symlink():
            return
        records = _load_records(missing_ok=False)
        kept = [
            item
            for item in records
            if item["key"] != key and (canonical is None or item["path"] != canonical)
        ]
        if kept != records:
            _write_records(kept)


def verified_entrypoint_bytes(key: str, plugin_dir: Path) -> bytes:
    """Return exact verified entrypoint bytes or fail before Python executes."""
    canonical = _canonical_plugin_dir(plugin_dir)
    records = _load_records(missing_ok=False)
    matches = [
        item for item in records if item["key"] == key and item["path"] == canonical
    ]
    if len(matches) != 1:
        if not matches:
            raise PluginIntegrityError(
                f"missing plugin integrity evidence for {key!r} at {canonical}"
            )
        raise PluginIntegrityError(f"duplicate plugin integrity evidence for {key!r}")
    data = _read_regular_bytes(
        plugin_dir / matches[0]["entrypoint"], label="plugin entrypoint"
    )
    actual = hashlib.sha256(data).hexdigest()
    if actual != matches[0]["sha256"]:
        raise PluginIntegrityError(
            f"plugin entrypoint integrity mismatch for {key!r}"
        )
    return data
