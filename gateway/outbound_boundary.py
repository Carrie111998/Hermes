"""Fail-closed synchronous adapter for outbound decision hooks.

Cron runs outside the Gateway request loop, while output handlers are exposed
through :class:`gateway.hooks.HookRegistry`.  This adapter is the small bridge
between those worlds; it never sends, retries, or resolves a route itself.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from dataclasses import dataclass
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any


BEFORE_SEND = "outbound:before_send"
AFTER_SEND = "outbound:after_send"
OUTBOUND_ACTIONABLE_HOOK = "outbound-actionable"
DECISIONS = {"allow", "rewrite", "deny"}
RELEASE_TUPLE_FIELDS = (
    "hak_commit", "hak_tag", "hak_source_path", "hak_archive_path", "hak_archive_sha256",
    "homebrew_keg_path", "homebrew_keg_sha256", "core_commit",
    "core_runtime_path", "core_patch_path", "core_patch_sha256",
)
BUNDLE_MANIFEST_NAME = "kit-bundle-manifest.json"
_ACTIVATION_FINGERPRINT_FILES = (
    "HOOK.yaml",
    "handler.py",
    BUNDLE_MANIFEST_NAME,
    "kit-root.txt",
    "kit_root_paths.py",
    "runtime_loader_attestation.py",
    "release-tuple.json",
)
_ACTIVATION_RUNTIME_FIELDS = (
    "kit_root_paths",
    "runtime_loader_identity",
    "runtime_loader_generation",
)


class BoundaryLoadError(RuntimeError):
    """An installed outbound boundary could not be loaded safely."""


@dataclass(frozen=True)
class OutboundDecision:
    decision: str
    reason: str
    content: str
    raw: dict[str, Any]

    @property
    def transmit(self) -> bool:
        return self.decision in {"allow", "rewrite"}


def build_outbound_context(**values: Any) -> dict[str, Any]:
    """Return a detached event context without deriving authority from fields."""
    return dict(values)


def _private_regular(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _read_regular_bytes(path: Path) -> bytes | None:
    """Read one non-symlink file through the descriptor that was checked."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_private_bytes(path: Path) -> bytes | None:
    """Read a 0600 control through the descriptor whose metadata was checked."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _release_tuple_digest(version_tuple: dict[str, Any]) -> str:
    if set(version_tuple) != set(RELEASE_TUPLE_FIELDS) or any(
        not isinstance(version_tuple.get(name), str) or not version_tuple[name].strip()
        for name in RELEASE_TUPLE_FIELDS
    ):
        raise BoundaryLoadError("installed outbound boundary release tuple is incomplete")
    canonical = json.dumps(
        {name: version_tuple[name].strip() for name in RELEASE_TUPLE_FIELDS},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def outbound_activation_is_ready(hook_dir: Path) -> bool:
    """Verify the release controls shared by generic Gateway and Cron loaders."""
    release = hook_dir / "release-tuple.json"
    activation = hook_dir / "release-activation.json"
    if not _private_regular(release) or not _private_regular(activation):
        return False
    try:
        release_payload = json.loads(release.read_text(encoding="utf-8"))
        activation_payload = json.loads(activation.read_text(encoding="utf-8"))
        version_tuple = release_payload["version_tuple"]
        if (
            release_payload.get("schema_version") != "outbound-actionable-release/v1"
            or not isinstance(version_tuple, dict)
            or _release_tuple_digest(version_tuple) != release_payload.get("tuple_digest")
            or activation_payload.get("schema_version") != "outbound-actionable-dual-activation/v1"
            or activation_payload.get("tuple_digest") != release_payload.get("tuple_digest")
        ):
            return False
        activation_path = Path(str(activation_payload["activation_path"]))
        if not activation_path.is_absolute() or not _private_regular(activation_path):
            return False
        payload = activation_path.read_bytes()
        shared = json.loads(payload.decode("utf-8"))
        if "sha256:" + hashlib.sha256(payload).hexdigest() != activation_payload.get("activation_sha256"):
            return False
        profiles = shared.get("profiles") if isinstance(shared, dict) else None
        profile_id = activation_payload.get("profile_id")
        own_entry = next(
            (
                item for item in profiles or []
                if isinstance(item, dict)
                and item.get("profile_id") == profile_id
                and item.get("hook_dir") == str(hook_dir.absolute())
            ),
            None,
        )
        fingerprint = own_entry.get("fingerprint") if isinstance(own_entry, dict) else None
        if (
            not isinstance(fingerprint, dict)
            or set(fingerprint) != set(_ACTIVATION_FINGERPRINT_FILES + _ACTIVATION_RUNTIME_FIELDS)
            or any(not isinstance(fingerprint.get(name), str) for name in _ACTIVATION_RUNTIME_FIELDS)
            or any(
                (contents := _read_regular_bytes(hook_dir / name)) is None
                or fingerprint.get(name) != _sha256(contents)
                for name in _ACTIVATION_FINGERPRINT_FILES
            )
            or not isinstance(release_payload.get("bundle_manifest_sha256"), str)
            or not _bundle_manifest_is_valid(
                hook_dir,
                hook_dir / BUNDLE_MANIFEST_NAME,
                release_payload["bundle_manifest_sha256"],
            )
        ):
            return False
        return bool(
            isinstance(shared, dict)
            and shared.get("schema_version") == "outbound-actionable-dual-activation/v1"
            and shared.get("tuple_digest") == release_payload.get("tuple_digest")
            and shared.get("version_tuple") == version_tuple
            and isinstance(profiles, list)
            and len(profiles) == 2
            and {item.get("profile_id") for item in profiles if isinstance(item, dict)} == {"atlas", "yuange"}
            and profile_id in {"atlas", "yuange"}
            and isinstance(own_entry, dict)
            and isinstance(own_entry.get("runtime_identity"), dict)
        )
    except (KeyError, OSError, TypeError, ValueError, UnicodeError, BoundaryLoadError):
        return False


def _prepared_release_for_hook(hook_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read the Keg-external authority that permits inert capture once."""
    release = hook_dir / "release-tuple.json"
    if not _private_regular(release):
        return None
    try:
        release_payload = json.loads(release.read_text(encoding="utf-8"))
        version_tuple = release_payload["version_tuple"]
        tuple_digest = _release_tuple_digest(version_tuple)
        if (
            release_payload.get("schema_version") != "outbound-actionable-release/v1"
            or release_payload.get("tuple_digest") != tuple_digest
            or not isinstance(release_payload.get("bundle_manifest_sha256"), str)
        ):
            return None
        keg = Path(version_tuple["homebrew_keg_path"]).expanduser()
        prepared = keg.parent / ".outbound-actionable-activations" / f"{tuple_digest.removeprefix('sha256:')}.prepared.json"
        if not _private_regular(prepared):
            return None
        prepared_payload = json.loads(prepared.read_text(encoding="utf-8"))
        profiles = prepared_payload.get("profiles") if isinstance(prepared_payload, dict) else None
        own_entry = next(
            (
                entry for entry in profiles or []
                if isinstance(entry, dict) and entry.get("hook_dir") == str(hook_dir.absolute())
            ),
            None,
        )
        fingerprint = own_entry.get("fingerprint") if isinstance(own_entry, dict) else None
        runtime_identity = own_entry.get("runtime_identity") if isinstance(own_entry, dict) else None
        if (
            prepared_payload.get("schema_version") != "outbound-actionable-dual-prepared/v1"
            or prepared_payload.get("tuple_digest") != tuple_digest
            or prepared_payload.get("version_tuple") != version_tuple
            or not isinstance(profiles, list)
            or len(profiles) != 2
            or {entry.get("profile_id") for entry in profiles if isinstance(entry, dict)} != {"atlas", "yuange"}
            or not isinstance(own_entry, dict)
            or not isinstance(fingerprint, dict)
            or not isinstance(runtime_identity, dict)
            or fingerprint.get(BUNDLE_MANIFEST_NAME) != release_payload["bundle_manifest_sha256"]
        ):
            return None
        return release_payload, own_entry
    except (KeyError, OSError, TypeError, ValueError, UnicodeError, BoundaryLoadError):
        return None


def _prepared_runtime_root(entry: dict[str, Any]) -> Path | None:
    """Verify the already prepared seam bundle before its handler can import it."""
    identity = entry.get("runtime_identity")
    if not isinstance(identity, dict):
        return None
    raw_root = identity.get("raw_root")
    module_hashes = identity.get("module_hashes")
    if not isinstance(raw_root, str) or not raw_root or not isinstance(module_hashes, dict) or not module_hashes:
        return None
    root = Path(raw_root)
    try:
        if not root.is_dir() or root.is_symlink():
            return None
        for name, expected in module_hashes.items():
            relative = Path(str(name))
            if (
                not isinstance(name, str)
                or not isinstance(expected, str)
                or relative.is_absolute()
                or ".." in relative.parts
                or str(relative) in {"", "."}
            ):
                return None
            contents = _read_regular_bytes(root / "scripts" / relative)
            if contents is None or hashlib.sha256(contents).hexdigest() != expected:
                return None
    except OSError:
        return None
    return root


@contextmanager
def _pinned_prepared_runtime(root: Path):
    """Prevent environment precedence from redirecting inert capture imports."""
    keys = ("HERMES_AGENT_KIT_SCRIPTS", "HERMES_AGENT_KIT_HOME", "HERMES_AGENT_KIT_ROOT")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.pop("HERMES_AGENT_KIT_SCRIPTS", None)
        os.environ.pop("HERMES_AGENT_KIT_ROOT", None)
        os.environ["HERMES_AGENT_KIT_HOME"] = str(root)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def capture_pending_outbound_attestation(hook_dir: Path) -> None:
    """Let a restarted Gateway attest copied bytes while the hook remains inert."""
    handler = hook_dir / "handler.py"
    manifest = hook_dir / BUNDLE_MANIFEST_NAME
    prepared = _prepared_release_for_hook(hook_dir)
    if prepared is None:
        return
    release_payload, own_entry = prepared
    runtime_root = _prepared_runtime_root(own_entry)
    if runtime_root is None:
        return
    expected_manifest = release_payload["bundle_manifest_sha256"]
    entries = _validated_bundle_manifest(hook_dir, manifest, expected_manifest)
    expected_handler = entries.get("handler.py") if entries else None
    handler_bytes = _read_regular_bytes(handler) if isinstance(expected_handler, str) else None
    if handler_bytes is None or _sha256(handler_bytes) != expected_handler:
        return
    try:
        with _pinned_prepared_runtime(runtime_root):
            module = _exec_pinned_handler(
                "hermes_pending_outbound_attestation", handler, handler_bytes,
            )
            capture = getattr(module, "capture_runtime_attestation", None)
            if callable(capture):
                capture(hook_dir.parent.parent)
    except Exception:
        return


def _exec_pinned_handler(module_name: str, handler: Path, source: bytes) -> types.ModuleType:
    """Execute exactly the handler bytes that manifest verification consumed."""
    module = types.ModuleType(module_name)
    module.__file__ = str(handler)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(handler), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _validated_bundle_manifest(
    root: Path, path: Path, expected_sha256: str | None = None,
) -> dict[str, str] | None:
    """Return the fully verified bundle file digest map, or ``None``."""
    try:
        manifest_bytes = _read_regular_bytes(path)
        if manifest_bytes is None or (expected_sha256 is not None and expected_sha256 != _sha256(manifest_bytes)):
            return None
        payload = json.loads(manifest_bytes.decode("utf-8"))
        files = payload.get("files") if isinstance(payload, dict) else None
        if payload.get("schema_version") != 1 or not isinstance(files, list) or not files:
            return None
        canonical = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if payload.get("bundle_sha256") != hashlib.sha256(canonical).hexdigest():
            return None
        verified: dict[str, str] = {}
        for entry in files:
            name = entry.get("path") if isinstance(entry, dict) else None
            digest = entry.get("sha256") if isinstance(entry, dict) else None
            relative = Path(str(name or ""))
            if not isinstance(name, str) or not isinstance(digest, str) or relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."} or name in verified:
                return None
            contents = _read_regular_bytes(root / relative)
            if contents is None or hashlib.sha256(contents).hexdigest() != digest:
                return None
            verified[name] = _sha256(contents)
        return verified if "handler.py" in verified else None
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def _bundle_manifest_is_valid(root: Path, path: Path, expected_sha256: str | None = None) -> bool:
    """Admit only a self-consistent, path-safe managed bundle before capture."""
    return _validated_bundle_manifest(root, path, expected_sha256) is not None


def _activated_handler_snapshot(hook_dir: Path) -> tuple[Path, bytes] | None:
    """Pin all active controls and handler bytes to one self-consistent snapshot."""
    release_bytes = _read_private_bytes(hook_dir / "release-tuple.json")
    activation_bytes = _read_private_bytes(hook_dir / "release-activation.json")
    if release_bytes is None or activation_bytes is None:
        return None
    try:
        release_payload = json.loads(release_bytes.decode("utf-8"))
        activation_payload = json.loads(activation_bytes.decode("utf-8"))
        version_tuple = release_payload["version_tuple"]
        if (
            release_payload.get("schema_version") != "outbound-actionable-release/v1"
            or not isinstance(version_tuple, dict)
            or _release_tuple_digest(version_tuple) != release_payload.get("tuple_digest")
            or activation_payload.get("schema_version") != "outbound-actionable-dual-activation/v1"
            or activation_payload.get("tuple_digest") != release_payload.get("tuple_digest")
        ):
            return None
        activation_path = Path(str(activation_payload["activation_path"]))
        shared_bytes = _read_private_bytes(activation_path) if activation_path.is_absolute() else None
        if shared_bytes is None or _sha256(shared_bytes) != activation_payload.get("activation_sha256"):
            return None
        shared = json.loads(shared_bytes.decode("utf-8"))
        profiles = shared.get("profiles") if isinstance(shared, dict) else None
        profile_id = activation_payload.get("profile_id")
        own_entry = next(
            item for item in profiles or []
            if isinstance(item, dict)
            and item.get("profile_id") == profile_id
            and item.get("hook_dir") == str(hook_dir.absolute())
        )
        fingerprint = own_entry.get("fingerprint") if isinstance(own_entry, dict) else None
        if (
            not isinstance(fingerprint, dict)
            or set(fingerprint) != set(_ACTIVATION_FINGERPRINT_FILES + _ACTIVATION_RUNTIME_FIELDS)
            or any(not isinstance(fingerprint.get(name), str) for name in _ACTIVATION_RUNTIME_FIELDS)
            or any(
                (contents := _read_regular_bytes(hook_dir / name)) is None
                or fingerprint.get(name) != _sha256(contents)
                for name in _ACTIVATION_FINGERPRINT_FILES
            )
            or not isinstance(release_payload.get("bundle_manifest_sha256"), str)
            or not _bundle_manifest_is_valid(hook_dir, hook_dir / BUNDLE_MANIFEST_NAME, release_payload["bundle_manifest_sha256"])
            or not isinstance(shared, dict)
            or shared.get("schema_version") != "outbound-actionable-dual-activation/v1"
            or shared.get("tuple_digest") != release_payload.get("tuple_digest")
            or shared.get("version_tuple") != version_tuple
            or not isinstance(profiles, list)
            or len(profiles) != 2
            or {item.get("profile_id") for item in profiles if isinstance(item, dict)} != {"atlas", "yuange"}
            or profile_id not in {"atlas", "yuange"}
            or not isinstance(own_entry.get("runtime_identity"), dict)
        ):
            return None
        handler = hook_dir / "handler.py"
        source = _read_regular_bytes(handler)
        if source is None or fingerprint.get("handler.py") != _sha256(source):
            return None
        return handler, source
    except (KeyError, OSError, StopIteration, TypeError, ValueError, UnicodeError, BoundaryLoadError):
        return None


def load_activated_outbound_handler(hook_dir: Path, module_name: str) -> types.ModuleType:
    """Load the active boundary from bytes pinned to its immutable activation."""
    snapshot = _activated_handler_snapshot(hook_dir)
    if snapshot is None:
        if not _private_regular(hook_dir / "release-tuple.json") or not _private_regular(hook_dir / "release-activation.json"):
            raise BoundaryLoadError("installed outbound boundary is not release-activated")
        raise BoundaryLoadError("installed outbound boundary activation changed")
    handler, source = snapshot
    return _exec_pinned_handler(module_name, handler, source)


def load_installed_outbound_hooks(home: str | Path) -> Any | None:
    """Load the active outbound hook for one Profile, if it is installed.

    No manifest means the existing Core delivery behavior remains in force.
    Once a manifest declares the before-send boundary, a failed or incomplete
    load is an explicit error for the scheduler to fail closed on.
    """
    home_path = Path(home)
    hooks_root = home_path / "hooks"
    hook_dir = hooks_root / "outbound-actionable"
    manifest = hook_dir / "HOOK.yaml"
    if not manifest.exists():
        return None
    for path in (hooks_root, hook_dir, manifest, hook_dir / "handler.py"):
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise BoundaryLoadError("installed outbound boundary is incomplete") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BoundaryLoadError("installed outbound boundary must not cross a symlink")
    if hook_dir.parent != hooks_root or not manifest.is_file() or not (hook_dir / "handler.py").is_file():
        raise BoundaryLoadError("installed outbound boundary is incomplete")
    if not outbound_activation_is_ready(hook_dir):
        raise BoundaryLoadError("installed outbound boundary is not release-activated")
    release = hook_dir / "release-tuple.json"
    activation = hook_dir / "release-activation.json"
    if not _private_regular(release) or not _private_regular(activation):
        raise BoundaryLoadError("installed outbound boundary release controls are invalid")
    try:
        release_payload = json.loads(release.read_text(encoding="utf-8"))
        activation_payload = json.loads(activation.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoundaryLoadError("installed outbound boundary is not release-activated") from exc
    if not isinstance(release_payload, dict) or activation_payload.get("tuple_digest") != release_payload.get("tuple_digest"):
        raise BoundaryLoadError("installed outbound boundary activation does not match release tuple")
    activation_path_value = activation_payload.get("activation_path")
    activation_sha256 = activation_payload.get("activation_sha256")
    profile_id = activation_payload.get("profile_id")
    if not isinstance(activation_path_value, str) or not isinstance(activation_sha256, str) or not isinstance(profile_id, str):
        raise BoundaryLoadError("installed outbound boundary has no shared activation record")
    activation_path = Path(activation_path_value)
    try:
        activation_info = os.lstat(activation_path)
        if (
            not activation_path.is_absolute()
            or stat.S_ISLNK(activation_info.st_mode)
            or not stat.S_ISREG(activation_info.st_mode)
            or activation_info.st_uid != os.getuid()
            or stat.S_IMODE(activation_info.st_mode) != 0o600
            or activation_info.st_nlink != 1
        ):
            raise OSError("activation record is not a regular absolute file")
        activation_bytes = activation_path.read_bytes()
        shared_activation = json.loads(activation_bytes.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BoundaryLoadError("installed outbound boundary shared activation is unavailable") from exc
    if "sha256:" + hashlib.sha256(activation_bytes).hexdigest() != activation_sha256:
        raise BoundaryLoadError("installed outbound boundary shared activation changed")
    profiles = shared_activation.get("profiles") if isinstance(shared_activation, dict) else None
    own_entry = next(
        (
            item for item in profiles or []
            if isinstance(item, dict)
            and item.get("profile_id") == profile_id
            and item.get("hook_dir") == str(hook_dir.absolute())
        ),
        None,
    )
    if (
        not isinstance(shared_activation, dict)
        or shared_activation.get("tuple_digest") != release_payload.get("tuple_digest")
        or not isinstance(profiles, list)
        or len(profiles) != 2
        or {item.get("profile_id") for item in profiles if isinstance(item, dict)} != {"atlas", "yuange"}
        or not isinstance(own_entry, dict)
    ):
        raise BoundaryLoadError("installed outbound boundary shared activation does not bind both Profiles")
    try:
        from gateway.hooks import HookRegistry

        hooks = HookRegistry(hooks_dir=hook_dir.parent)
        hooks.discover_and_load()
    except Exception as exc:  # pragma: no cover - discovery is integration-owned
        raise BoundaryLoadError("installed outbound boundary failed to load") from exc
    expected_path = hook_dir.absolute()
    if not any(
        item.get("name") == "outbound-actionable"
        and Path(str(item.get("path") or "")).absolute() == expected_path
        and BEFORE_SEND in item.get("events", [])
        for item in hooks.loaded_hooks
    ):
        raise BoundaryLoadError("installed outbound boundary is missing before-send handler")
    return hooks


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            error.append(exc)

    import threading

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def outbound_before_send_sync(hooks: Any, context: dict[str, Any]) -> OutboundDecision:
    """Collect exactly one boundary decision, otherwise deny before transport."""
    try:
        collector = getattr(hooks, "emit_collect_strict", None)
        if collector is None:
            collector = hooks.emit_collect
        results = _run(collector(BEFORE_SEND, dict(context)))
    except Exception:
        return OutboundDecision("deny", "boundary_unavailable", "", {})
    normalized = [item for item in results if isinstance(item, dict) and item.get("decision") in DECISIONS]
    if len(normalized) != 1:
        return OutboundDecision("deny", "boundary_decision_missing_or_ambiguous", "", {})
    raw = dict(normalized[0])
    decision = str(raw["decision"])
    if decision == "rewrite":
        replacement = raw.get("content")
        if not isinstance(replacement, str) or not replacement:
            return OutboundDecision("deny", "boundary_rewrite_missing_content", "", {})
        content = replacement
    elif decision == "allow":
        content = str(context.get("content") or "")
    else:
        content = ""
    return OutboundDecision(decision, str(raw.get("reason") or decision), content, raw)


def outbound_after_send_sync(hooks: Any, context: dict[str, Any]) -> None:
    """Replay only the durable outbound-actionable observer for a final result."""
    named_collector = getattr(hooks, "emit_collect_strict_named", None)
    if named_collector is not None:
        _run(named_collector(AFTER_SEND, OUTBOUND_ACTIONABLE_HOOK, dict(context)))
        return
    collector = getattr(hooks, "emit_collect_strict", None)
    if collector is None:
        collector = hooks.emit_collect
    _run(collector(AFTER_SEND, dict(context)))
