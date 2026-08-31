"""Runtime-backed validation behind ``hermes plugins doctor``.

The Doctor originated in #46456 / contributor PR #46457 by 峯岸 亮
(@zapabob).  This core command keeps that contribution's manifest/import/
registration validation intent while routing every check through the current
runtime contracts instead of maintaining a parallel scanner.
"""

from __future__ import annotations

import errno
import fnmatch
import inspect
import os
import signal
import shutil
import socket
import stat
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal, cast
from unittest.mock import patch

from hermes_constants import get_hermes_home


class _DoctorLoadError(RuntimeError):
    """Raised when the real plugin runtime cannot load the target."""


class _DoctorTerminated(SystemExit):
    """Turn SIGTERM into normal unwinding so temporary files are removed."""

    def __init__(self, signum: int, frame: Any) -> None:
        super().__init__(128 + signum)
        self.signum = signum
        self.frame = frame


def _deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network access is disabled while Plugin Doctor runs")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_symlink_target(candidate: Path, raw_target: Path, root: Path) -> None:
    link_target = raw_target
    if not raw_target.is_absolute():
        link_target = Path(os.path.normpath(candidate.parent / raw_target))
    if raw_target.is_absolute() or not _is_within(link_target, root):
        raise ValueError(
            f"Plugin symlink escapes plugin root: {candidate} -> {raw_target}"
        )


def _validate_plugin_symlinks(root: Path) -> None:
    """Reject absolute or lexically escaping links without resolving targets."""
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in (*directory_names, *file_names):
            candidate = directory_path / name
            if not candidate.is_symlink():
                continue
            raw_target = Path(os.readlink(candidate))
            _validate_symlink_target(candidate, raw_target, root)


def _validate_plugin_root(path: Path) -> Path:
    """Require one bounded plugin tree before creating any Doctor temp data."""
    if path.is_symlink():
        raise ValueError(f"Plugin target must not be a symlink: {path}")

    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Plugin Doctor refuses the filesystem root: {resolved}")
    if resolved == Path.home().resolve():
        raise ValueError(f"Plugin Doctor refuses the home directory: {resolved}")
    manifest_names = ("plugin.yaml", "plugin.yml", "plugin.json")
    manifest_paths = tuple(resolved / name for name in manifest_names)
    if not any(os.path.lexists(path) for path in manifest_paths):
        raise ValueError(
            "Plugin Doctor requires one plugin directory containing plugin.yaml, "
            "plugin.yml, or plugin.json; "
            f"refusing broad or non-plugin target: {resolved}"
        )
    if not any(
        stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
        for path in manifest_paths
        if os.path.lexists(path)
    ):
        raise ValueError(f"Plugin manifest is not a regular file under: {resolved}")
    return resolved


_COPY_IGNORE_PATTERNS = (".git", "__pycache__", ".pytest_cache", "*.pyc")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_SECURE_STAGING_REQUIREMENTS = {
    "os.open(dir_fd)": os.open in os.supports_dir_fd,
    "os.stat(dir_fd)": os.stat in os.supports_dir_fd,
    "os.readlink(dir_fd)": os.readlink in os.supports_dir_fd,
    "os.scandir(fd)": os.scandir in os.supports_fd,
}
_MISSING_SECURE_STAGING_APIS = tuple(
    name for name, supported in _SECURE_STAGING_REQUIREMENTS.items() if not supported
)


def _require_secure_staging_support() -> None:
    if _MISSING_SECURE_STAGING_APIS:
        raise ValueError(
            "Secure plugin staging is unavailable on this platform; "
            "refusing an unsafe path-based copy (missing: "
            + ", ".join(_MISSING_SECURE_STAGING_APIS)
            + ")"
        )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_verified_entry(
    name: str, source_fd: int, expected: os.stat_result, flags: int
) -> int:
    descriptor = os.open(name, flags, dir_fd=source_fd)
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(expected, opened):
            raise ValueError(
                f"Plugin entry changed while Doctor was copying it: {name}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _copy_directory_fd(
    source_fd: int,
    logical_source: Path,
    destination: Path,
    plugin_root: Path,
) -> None:
    with os.scandir(source_fd) as entries:
        for entry in entries:
            if any(
                fnmatch.fnmatch(entry.name, pattern)
                for pattern in _COPY_IGNORE_PATTERNS
            ):
                continue

            expected = entry.stat(follow_symlinks=False)
            logical_entry = logical_source / entry.name
            copied_entry = destination / entry.name
            if stat.S_ISLNK(expected.st_mode):
                raw_target = Path(os.readlink(entry.name, dir_fd=source_fd))
                _validate_symlink_target(logical_entry, raw_target, plugin_root)
                os.symlink(os.fspath(raw_target), copied_entry)
                continue

            if stat.S_ISDIR(expected.st_mode):
                child_fd = _open_verified_entry(
                    entry.name, source_fd, expected, _DIRECTORY_OPEN_FLAGS
                )
                try:
                    copied_entry.mkdir(mode=0o700)
                    _copy_directory_fd(
                        child_fd, logical_entry, copied_entry, plugin_root
                    )
                    os.chmod(copied_entry, stat.S_IMODE(expected.st_mode))
                finally:
                    os.close(child_fd)
                continue

            if stat.S_ISREG(expected.st_mode):
                child_fd = _open_verified_entry(
                    entry.name, source_fd, expected, _FILE_OPEN_FLAGS
                )
                with (
                    os.fdopen(child_fd, "rb") as source_file,
                    copied_entry.open("xb") as copied_file,
                ):
                    shutil.copyfileobj(source_file, copied_file)
                os.chmod(copied_entry, stat.S_IMODE(expected.st_mode))
                continue

            raise ValueError(
                f"Plugin contains unsupported special file: {logical_entry}"
            )


def _copy_plugin_tree(source: Path, destination: Path) -> None:
    """Copy a plugin through a pinned directory FD without following links."""
    expected_root = os.stat(source, follow_symlinks=False)
    if stat.S_ISLNK(expected_root.st_mode):
        raise ValueError(f"Plugin target must not be a symlink: {source}")
    try:
        source_fd = os.open(source, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP or source.is_symlink():
            raise ValueError(
                f"Plugin target changed or became a symlink before copy: {source}"
            ) from exc
        raise

    try:
        opened_root = os.fstat(source_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or not _same_identity(
            expected_root, opened_root
        ):
            raise ValueError(f"Plugin target changed while Doctor opened it: {source}")

        manifest_names = ("plugin.yaml", "plugin.yml", "plugin.json")
        has_manifest = False
        for name in manifest_names:
            try:
                manifest_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(manifest_stat.st_mode):
                has_manifest = True
                break
        if not has_manifest:
            raise ValueError(
                "Plugin Doctor requires one plugin directory containing plugin.yaml, "
                "plugin.yml, or plugin.json; source changed before copy"
            )

        destination.mkdir()
        _copy_directory_fd(source_fd, source, destination, source)
    finally:
        os.close(source_fd)


@contextmanager
def _cleanup_on_sigterm():
    """Let SIGTERM unwind TemporaryDirectory on the Python main thread."""
    if (
        not hasattr(signal, "SIGTERM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, frame: Any) -> None:
        raise _DoctorTerminated(signum, frame)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    except _DoctorTerminated as exc:
        if callable(previous_handler):
            handler = cast(Callable[[int, Any], Any], previous_handler)
            handler(exc.signum, exc.frame)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


@contextmanager
def _doctor_runtime(plugin_path: Path):
    """Load one plugin through the real runtime and restore global state.

    This is deliberately private Doctor machinery, not a standalone plugin
    test framework. Registration code executes under a temporary HERMES_HOME
    with outbound socket connects blocked.
    """
    _require_secure_staging_support()
    with _cleanup_on_sigterm(), ExitStack() as stack:
        home = Path(
            stack.enter_context(
                tempfile.TemporaryDirectory(prefix="hermes-plugin-doctor-")
            )
        )
        if _is_within(home.resolve(), plugin_path.resolve()):
            raise ValueError(
                "Plugin Doctor temporary destination is inside the plugin source; "
                "refusing a recursive copy"
            )

        bundled = home / "bundled-plugins"
        plugins_root = home / "plugins"
        bundled.mkdir(parents=True)
        plugins_root.mkdir(parents=True)
        copied = plugins_root / plugin_path.name
        _copy_plugin_tree(plugin_path, copied)
        _validate_plugin_symlinks(copied)

        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(home),
                    "HERMES_BUNDLED_PLUGINS": str(bundled),
                    "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                },
                clear=False,
            )
        )
        stack.enter_context(patch.object(socket, "create_connection", _deny_network))
        stack.enter_context(patch.object(socket.socket, "connect", _deny_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _deny_network))

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        entries_before = {entry.name: entry for entry in registry._snapshot_entries()}
        policy_before = dict(registry._plugin_override_policy)
        modules_before = {
            name
            for name in sys.modules
            if name == "hermes_plugins" or name.startswith("hermes_plugins.")
        }
        manager = PluginManager()
        try:
            manifests = manager._scan_directory(plugins_root, source="user")
            if not manifests:
                raise _DoctorLoadError(
                    f"Hermes discovery found no valid plugin manifest under {copied}"
                )
            if len(manifests) != 1:
                raise _DoctorLoadError(
                    f"Expected one plugin manifest, discovered {len(manifests)} under {copied}"
                )
            manifest = manifests[0]
            manager._load_plugin(manifest)
            loaded = manager._plugins.get(manifest.key or manifest.name)
            if loaded is None:
                raise _DoctorLoadError("Plugin registration produced no runtime record")
            if loaded.error:
                raise _DoctorLoadError(f"Plugin registration failed: {loaded.error}")
            if not loaded.enabled:
                raise _DoctorLoadError(
                    "Plugin registration did not enable the runtime record"
                )
            yield SimpleNamespace(
                manifest=manifest,
                manager=manager,
                registered_tools=tuple(sorted(loaded.tools_registered)),
                registered_hooks=tuple(loaded.hooks_registered),
            )
        finally:
            entries_after = {
                entry.name: entry for entry in registry._snapshot_entries()
            }
            changed_names = {
                name
                for name in set(entries_before) | set(entries_after)
                if entries_after.get(name) is not entries_before.get(name)
            }
            with registry._lock:
                for name in changed_names:
                    previous = entries_before.get(name)
                    if previous is None:
                        registry._tools.pop(name, None)
                    else:
                        registry._tools[name] = previous
                registry._plugin_override_policy.clear()
                registry._plugin_override_policy.update(policy_before)
                if changed_names:
                    registry._generation += 1
            for name in list(sys.modules):
                if name not in modules_before and (
                    name == "hermes_plugins" or name.startswith("hermes_plugins.")
                ):
                    sys.modules.pop(name, None)


@dataclass(frozen=True)
class DoctorFinding:
    level: Literal["error", "warning"]
    message: str


@dataclass
class DoctorReport:
    path: Path
    manifest: Any | None = None
    findings: list[DoctorFinding] = field(default_factory=list)
    registered_tools: tuple[str, ...] = ()
    registered_hooks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.level == "error" for finding in self.findings)

    def error(self, message: str) -> None:
        self.findings.append(DoctorFinding("error", message))

    def warning(self, message: str) -> None:
        self.findings.append(DoctorFinding("warning", message))

    def format_text(self) -> str:
        lines = [f"Plugin Doctor: {self.path}"]
        if self.manifest is not None:
            lines.append(
                f"  manifest: {self.manifest.name} "
                f"{self.manifest.version or '(no version)'} ({self.manifest.kind})"
            )
        for finding in self.findings:
            marker = "ERROR" if finding.level == "error" else "WARN"
            lines.append(f"  {marker}: {finding.message}")
        if self.ok:
            lines.append(
                "  OK: runtime discovery, manifest parsing, import, and registration passed"
            )
        lines.append(
            f"  registrations: {len(self.registered_tools)} tool(s), "
            f"{len(self.registered_hooks)} hook(s)"
        )
        return "\n".join(lines)


def resolve_plugin_path(target: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit path or an installed/bundled plugin id."""
    if target is None:
        raise ValueError(
            "Plugin Doctor requires an explicit plugin path or installed plugin id"
        )
    raw = os.fspath(target)
    direct = Path(raw).expanduser()
    if direct.is_dir():
        return _validate_plugin_root(direct)

    candidates: list[Path] = []
    user_root = get_hermes_home() / "plugins"
    candidates.append(user_root / raw)
    try:
        from hermes_cli.plugins import get_bundled_plugins_dir

        bundled = get_bundled_plugins_dir()
        candidates.extend([
            bundled / raw,
            bundled / "platforms" / raw,
            bundled / "model-providers" / raw,
        ])
    except Exception:
        pass
    candidates.append(Path.cwd() / ".hermes" / "plugins" / raw)
    for candidate in candidates:
        if candidate.is_dir():
            return _validate_plugin_root(candidate)
    raise FileNotFoundError(
        f"Plugin {raw!r} was not found as a path or installed plugin id"
    )


def _accepts_var_kwargs(callback: Any) -> bool:
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )


def _check_manifest_v2(report: "DoctorReport", manifest: Any) -> None:
    """Manifest v2 (#64165) checks: versions, deps, pip declarations, schema."""
    import importlib.metadata
    import re as _re

    from hermes_cli.plugins import SUPPORTED_MANIFEST_VERSION

    mv = getattr(manifest, "manifest_version", 1)
    if mv > SUPPORTED_MANIFEST_VERSION:
        report.warning(
            f"manifest_version {mv} is newer than this Hermes supports "
            f"({SUPPORTED_MANIFEST_VERSION}); unknown fields are ignored"
        )

    api_version = getattr(manifest, "api_version", None)
    if api_version is not None and api_version < 1:
        report.warning(
            f"api_version {api_version} is not a valid API generation (>= 1)"
        )

    for dep in getattr(manifest, "requires_plugins", []) or []:
        dep_id = dep.get("id") if isinstance(dep, dict) else None
        if not dep_id:
            report.warning(f"requires_plugins entry {dep!r} has no plugin id")
            continue
        vr = dep.get("version_range")
        if vr:
            report.warning(
                f"requires plugin {dep_id!r} ({vr}) — version ranges are "
                "advisory; a missing dependency logs a warning at load"
            )

    pydeps = getattr(manifest, "python_dependencies", []) or []
    missing: list[str] = []
    unpinned: list[str] = []
    for req in pydeps:
        dist = _re.split(r"[<>=!~\[;\s]", req, maxsplit=1)[0].strip()
        if not _re.search(r"<|==|~=", req):
            unpinned.append(req)
        if not dist:
            continue
        try:
            importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            missing.append(req)
        except Exception:
            continue
    for req in unpinned:
        report.warning(
            f"python_dependencies entry {req!r} has no upper bound — "
            "pin an upper bound (e.g. 'pkg>=1.0,<2') per the dependency policy"
        )
    if missing:
        report.warning(
            "declared python_dependencies not installed: "
            + ", ".join(missing)
            + " — Hermes never auto-installs plugin dependencies; "
            + "install manually: pip install "
            + " ".join(f"'{m}'" for m in missing)
        )

    schema = getattr(manifest, "config_schema", {}) or {}
    if schema:
        from hermes_cli.plugins import _CONFIG_SCHEMA_TYPES

        for skey, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            stype = spec.get("type")
            if stype is not None and str(stype).lower() not in _CONFIG_SCHEMA_TYPES:
                report.warning(
                    f"config_schema key {skey!r} declares unknown type {stype!r}"
                )


def doctor_plugin(target: str | os.PathLike[str] | None = None) -> DoctorReport:
    """Validate one plugin through Hermes' real scanner and registration path."""
    try:
        path = resolve_plugin_path(target)
    except (FileNotFoundError, ValueError) as exc:
        report = DoctorReport(Path(os.fspath(target or ".")).expanduser())
        report.error(str(exc))
        return report

    report = DoctorReport(path)
    try:
        with _doctor_runtime(path) as host:
            report.manifest = host.manifest
            report.registered_tools = host.registered_tools
            report.registered_hooks = host.registered_hooks

            from hermes_cli.plugins import VALID_HOOKS

            declared_hooks = host.manifest.provides_hooks
            declared_tools = host.manifest.provides_tools
            if not isinstance(declared_hooks, list):
                report.error("provides_hooks must be a list")
                declared_hooks = []
            if not isinstance(declared_tools, list):
                report.error("provides_tools must be a list")
                declared_tools = []

            for name in declared_hooks:
                if not isinstance(name, str):
                    report.error("provides_hooks entries must be strings")
                elif name not in VALID_HOOKS:
                    report.error(f"unknown hook {name!r} in provides_hooks")

            for hook_name, callbacks in host.manager._hooks.items():
                if hook_name not in VALID_HOOKS:
                    report.error(f"registered unknown hook {hook_name!r}")
                for callback in callbacks:
                    if not _accepts_var_kwargs(callback):
                        callback_name = getattr(callback, "__name__", repr(callback))
                        report.error(
                            f"hook callback {callback_name!r} for {hook_name!r} "
                            "must accept **kwargs for forward compatibility"
                        )

            declared_hook_names = {
                name for name in declared_hooks if isinstance(name, str)
            }
            registered_hook_names = set(host.registered_hooks)
            for name in sorted(declared_hook_names - registered_hook_names):
                report.warning(
                    f"manifest declares hook {name!r} but registration did not add it"
                )
            for name in sorted(registered_hook_names - declared_hook_names):
                report.warning(
                    f"registration adds hook {name!r} not listed in provides_hooks"
                )

            declared_tool_names = {
                name for name in declared_tools if isinstance(name, str)
            }
            registered_tool_names = set(host.registered_tools)
            for name in sorted(declared_tool_names - registered_tool_names):
                report.warning(
                    f"manifest declares tool {name!r} but registration did not add it"
                )
            for name in sorted(registered_tool_names - declared_tool_names):
                report.warning(
                    f"registration adds tool {name!r} not listed in provides_tools"
                )

            _check_manifest_v2(report, host.manifest)
    except (_DoctorLoadError, ValueError) as exc:
        report.error(str(exc))
    except Exception as exc:
        report.error(f"unexpected validation failure: {type(exc).__name__}: {exc}")
    return report


__all__ = [
    "DoctorFinding",
    "DoctorReport",
    "doctor_plugin",
    "resolve_plugin_path",
]
