"""Managed plugin update handoff between isolated workers and the dashboard host."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_process_hermes_home


class ManagedPluginUpdateError(RuntimeError):
    """A managed update cannot be coordinated safely."""


@dataclass(frozen=True)
class ManagedUpdateSpec:
    contract: str
    entrypoint: str


@dataclass(frozen=True)
class ManagedUpdateCandidate:
    source_commit: str
    prior_commit: str
    staged_root: Path
    spec: ManagedUpdateSpec


def get_managed_update_spec(
    plugin_root: Path, *, strict: bool = False
) -> ManagedUpdateSpec | None:
    """Return a validated managed-update declaration, or ``None`` if unmanaged."""
    manifest_path = plugin_root / "plugin.yaml"
    if not manifest_path.is_file():
        return None
    try:
        import yaml

        raw_manifest = manifest_path.read_text(encoding="utf-8")
        if not isinstance(raw_manifest, str):
            return None
        manifest = yaml.safe_load(raw_manifest) or {}
    except Exception as exc:
        if not strict:
            return None
        raise ManagedPluginUpdateError(
            f"Could not read managed plugin manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        if strict:
            raise ManagedPluginUpdateError("Managed plugin manifest must be a mapping.")
        return None

    update = manifest.get("update")
    if not isinstance(update, dict) or update.get("mode") != "managed":
        return None
    contract = update.get("contract")
    entrypoint = update.get("entrypoint")
    if not isinstance(contract, str) or not contract.strip():
        raise ManagedPluginUpdateError(
            "Managed plugin manifest requires update.contract."
        )
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ManagedPluginUpdateError(
            "Managed plugin manifest requires update.entrypoint."
        )

    candidate = Path(entrypoint)
    if candidate.is_absolute():
        raise ManagedPluginUpdateError(
            "Managed plugin update.entrypoint must be relative to the plugin root."
        )
    try:
        resolved = (plugin_root / candidate).resolve()
        resolved.relative_to(plugin_root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManagedPluginUpdateError(
            "Managed plugin update.entrypoint escapes the plugin root."
        ) from exc
    if not resolved.is_file():
        raise ManagedPluginUpdateError(
            f"Managed plugin update entrypoint was not found: {entrypoint}"
        )
    return ManagedUpdateSpec(contract=contract.strip(), entrypoint=entrypoint)


def _redact_error(value: object) -> str:
    message = str(value)
    message = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", message)
    message = re.sub(
        r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", message
    )
    message = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
        "[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)\b(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", message
    )
    message = re.sub(
        r"(?i)\b(access[_-]?token|pairing[_-]?token|token|password|secret)"
        r"\s*[=:]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        message,
    )
    return message


def run_managed_update(
    plugin_name: str,
    plugin_root: Path,
    spec: ManagedUpdateSpec,
    *,
    implementation_root: Path | None = None,
    bootstrap: ManagedUpdateCandidate | None = None,
) -> dict[str, Any]:
    """Run the plugin-owned product transaction in its declared isolated worker."""
    worker_root = implementation_root or plugin_root
    entrypoint = (worker_root / spec.entrypoint).resolve()
    environment = os.environ.copy()
    if bootstrap is not None:
        environment[_BOOTSTRAP_ENV] = json.dumps(
            {
                "plugin_name": plugin_name,
                "prior_commit": bootstrap.prior_commit,
                "candidate_commit": bootstrap.source_commit,
                "contract": spec.contract,
            },
            sort_keys=True,
        )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(entrypoint),
                str(plugin_root.resolve()),
                "migrate" if bootstrap is not None else "update",
            ],
            cwd=str(plugin_root),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=360,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPluginUpdateError(
            f"Could not start managed Update for '{plugin_name}': {_redact_error(exc)}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManagedPluginUpdateError(
            _redact_error(detail)
            or f"Managed Update for '{plugin_name}' exited with status {result.returncode}."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ManagedPluginUpdateError(
            f"Managed Update for '{plugin_name}' returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ManagedPluginUpdateError(
            f"Managed Update for '{plugin_name}' did not report coherent success."
        )
    return payload


def _git(
    plugin_root: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if not git:
        raise ManagedPluginUpdateError("git is not installed or not in PATH.")
    try:
        result = subprocess.run(
            [git, *args],
            cwd=str(plugin_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPluginUpdateError(f"Could not run git: {_redact_error(exc)}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManagedPluginUpdateError(
            _redact_error(detail) or f"git exited with status {result.returncode}."
        )
    return result


def _candidate_spec_from_git(
    plugin_name: str,
    plugin_root: Path,
    source_commit: str,
) -> ManagedUpdateSpec | None:
    manifest = _git(
        plugin_root,
        ["show", f"{source_commit}:plugin.yaml"],
        check=False,
    )
    if manifest.returncode != 0:
        return None
    try:
        import yaml

        data = yaml.safe_load(manifest.stdout) or {}
    except Exception as exc:
        raise ManagedPluginUpdateError(
            f"Could not read fetched plugin manifest: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ManagedPluginUpdateError("Fetched plugin manifest must be a mapping.")
    update = data.get("update")
    if not isinstance(update, dict) or update.get("mode") != "managed":
        return None
    if data.get("name") != plugin_name:
        raise ManagedPluginUpdateError(
            "Fetched managed plugin identity does not match the installed plugin."
        )
    contract = update.get("contract")
    entrypoint = update.get("entrypoint")
    if (
        not isinstance(contract, str)
        or contract not in _SUPPORTED_CONTRACTS
        or not isinstance(entrypoint, str)
        or not entrypoint.strip()
    ):
        raise ManagedPluginUpdateError(
            "Fetched plugin does not declare a supported managed update contract."
        )
    candidate = Path(entrypoint)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ManagedPluginUpdateError(
            "Fetched managed update entrypoint is not a safe relative path."
        )
    tree = _git(
        plugin_root,
        ["ls-tree", source_commit, "--", candidate.as_posix()],
        check=False,
    )
    fields = tree.stdout.rstrip("\n").split(maxsplit=3)
    if (
        tree.returncode != 0
        or len(fields) != 4
        or fields[0] not in {"100644", "100755"}
        or fields[1] != "blob"
        or fields[3] != candidate.as_posix()
    ):
        raise ManagedPluginUpdateError(
            "Fetched managed update entrypoint is not a regular Git file."
        )
    return ManagedUpdateSpec(contract=contract, entrypoint=entrypoint)


def _extract_git_archive(plugin_root: Path, source_commit: str, destination: Path) -> None:
    archive_path = destination.parent / "candidate.tar"
    git = shutil.which("git")
    if not git:
        raise ManagedPluginUpdateError("git is not installed or not in PATH.")
    try:
        with archive_path.open("wb") as archive:
            result = subprocess.run(
                [git, "archive", "--format=tar", source_commit],
                cwd=str(plugin_root),
                stdout=archive,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPluginUpdateError(
            f"Could not stage fetched plugin source: {_redact_error(exc)}"
        ) from exc
    if result.returncode != 0:
        raise ManagedPluginUpdateError(
            _redact_error(result.stderr.decode("utf-8", errors="replace"))
            or "Could not archive fetched plugin source."
        )
    destination.mkdir(mode=0o700)
    with tarfile.open(archive_path, "r:") as bundle:
        for member in bundle:
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ManagedPluginUpdateError(
                    "Fetched plugin archive contains an unsafe path."
                ) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                # Links are not needed to execute a regular declared entrypoint
                # and omitting them avoids platform-specific escape semantics.
                continue
            if not member.isfile():
                raise ManagedPluginUpdateError(
                    "Fetched plugin archive contains a non-regular file."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ManagedPluginUpdateError(
                    "Fetched plugin archive contains an unreadable file."
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


@contextmanager
def stage_managed_update_candidate(
    plugin_name: str, plugin_root: Path
):
    """Fetch and privately stage an exact FF target when it becomes managed."""
    prior_commit = _git(plugin_root, ["rev-parse", "HEAD"]).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", prior_commit) is None:
        raise ManagedPluginUpdateError("Could not identify installed plugin source.")
    # Honor the checkout's configured submodule recursion, as legacy
    # ``git pull --ff-only`` did, while separating inspection from cutover.
    _git(plugin_root, ["fetch"])
    candidate_commit = _git(
        plugin_root, ["rev-parse", "@{upstream}^{commit}"]
    ).stdout.strip()
    spec = _candidate_spec_from_git(plugin_name, plugin_root, candidate_commit)
    if spec is None:
        yield None, candidate_commit
        return
    if _git(
        plugin_root,
        ["merge-base", "--is-ancestor", prior_commit, candidate_commit],
        check=False,
    ).returncode != 0:
        raise ManagedPluginUpdateError(
            "Fetched managed update is not a fast-forward of the installed plugin."
        )
    if _git(
        plugin_root,
        ["status", "--porcelain", "--untracked-files=all"],
    ).stdout.strip():
        raise ManagedPluginUpdateError(
            "Plugin checkout has local changes; managed Update made no source cutover."
        )
    with tempfile.TemporaryDirectory(prefix="hermes-managed-update-") as temporary:
        staged_root = Path(temporary) / "plugin"
        _extract_git_archive(plugin_root, candidate_commit, staged_root)
        staged_spec = get_managed_update_spec(staged_root, strict=True)
        if staged_spec != spec:
            raise ManagedPluginUpdateError(
                "Staged managed plugin declaration changed during validation."
            )
        yield (
            ManagedUpdateCandidate(
                source_commit=candidate_commit,
                prior_commit=prior_commit,
                staged_root=staged_root,
                spec=spec,
            ),
            candidate_commit,
        )


_RUNTIME_DIR = "runtime"
_DESCRIPTOR_DIR = "managed-plugin-update-hosts"
_SUPPORTED_CONTRACTS = {"t3code-hermes-v1"}
_CONNECT_TIMEOUT_SECONDS = 2.0
_HANDSHAKE_TIMEOUT_SECONDS = 2.0
_OPERATION_TIMEOUT_SECONDS = 60.0
_MAX_MESSAGE_BYTES = 1024 * 1024
_BOOTSTRAP_ENV = "HERMES_INTERNAL_MANAGED_UPDATE_BOOTSTRAP"


def _runtime_dir(*, create: bool = False) -> Path:
    path = get_process_hermes_home() / _RUNTIME_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _descriptor_dir(*, create: bool = False) -> Path:
    path = _runtime_dir(create=create) / _DESCRIPTOR_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_descriptor(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 4096:
            raise ValueError("descriptor is too large")
        if os.name != "nt":
            stat_result = path.stat()
            if stat_result.st_uid != os.geteuid() or stat_result.st_mode & 0o077:
                raise PermissionError("descriptor permissions are not private")
        value = json.loads(path.read_text(encoding="utf-8"))
        address = value["address"]
        authkey = bytes.fromhex(value["authkey"])
        if (
            not isinstance(address, list)
            or len(address) != 2
            or address[0] != "127.0.0.1"
            or type(address[1]) is not int
            or not (1 <= address[1] <= 65535)
            or len(authkey) != 32
            or type(value.get("pid")) is not int
        ):
            raise ValueError("invalid descriptor")
        return {
            "address": (address[0], address[1]),
            "authkey": authkey,
            "pid": value["pid"],
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ManagedPluginUpdateError(
            "A Hermes managed-update host descriptor is invalid or unreadable."
        ) from exc


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("managed update connection closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_frame(connection: socket.socket, payload: bytes) -> None:
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ValueError("managed update message is too large")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _recv_frame(connection: socket.socket) -> bytes:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if size > _MAX_MESSAGE_BYTES:
        raise ValueError("managed update message is too large")
    return _recv_exact(connection, size)


def _ipc_call_one(
    descriptor: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    authkey = descriptor["authkey"]
    nonce = secrets.token_bytes(32)
    request = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.digest(authkey, b"request" + nonce + request, "sha256")
    try:
        with socket.create_connection(
            descriptor["address"], timeout=_CONNECT_TIMEOUT_SECONDS
        ) as connection:
            connection.settimeout(_OPERATION_TIMEOUT_SECONDS)
            _send_frame(connection, nonce + signature + request)
            response_frame = _recv_frame(connection)
    except (OSError, EOFError, ValueError) as exc:
        raise ManagedPluginUpdateError(
            "A live Hermes managed-update host did not respond before the deadline."
        ) from exc
    if len(response_frame) < 32:
        raise ManagedPluginUpdateError(
            "The Hermes managed-update host returned an invalid response."
        )
    response_signature, response_bytes = response_frame[:32], response_frame[32:]
    expected = hmac.digest(
        authkey, b"response" + nonce + response_bytes, "sha256"
    )
    if not hmac.compare_digest(response_signature, expected):
        raise ManagedPluginUpdateError(
            "The Hermes managed-update host response was not authenticated."
        )
    try:
        response = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedPluginUpdateError(
            "The Hermes managed-update host returned invalid JSON."
        ) from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise ManagedPluginUpdateError(
            str(detail or "The Hermes host rejected the managed update handoff.")
        )
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def _ipc_call(payload: dict[str, Any]) -> dict[str, Any]:
    directory = _descriptor_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        paths = []
    live_descriptors: list[dict[str, Any]] = []
    for path in paths:
        try:
            descriptor = _read_descriptor(path)
        except ManagedPluginUpdateError:
            continue
        if _process_is_running(descriptor["pid"]):
            live_descriptors.append(descriptor)
    if not live_descriptors:
        raise ManagedPluginUpdateError(
            "The Hermes managed-update coordinator is unavailable. Start the "
            "dashboard or desktop backend with this Hermes profile and retry; "
            "no plugin or product files were changed."
        )

    results: list[dict[str, Any]] = []
    for descriptor in live_descriptors:
        try:
            results.append(_ipc_call_one(descriptor, payload))
        except ManagedPluginUpdateError:
            if _process_is_running(descriptor["pid"]):
                raise
    if not results:
        raise ManagedPluginUpdateError(
            "The Hermes managed-update coordinator is unavailable. Start or "
            "restart the dashboard or desktop backend with this Hermes profile "
            "and retry; no source-only update was attempted."
        )
    first = results[0]
    if any(result != first for result in results[1:]):
        raise ManagedPluginUpdateError(
            "Hermes dashboard hosts returned inconsistent managed-update attestations."
        )
    return first


def _bootstrap_context(name: str) -> dict[str, str] | None:
    raw = os.environ.get(_BOOTSTRAP_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("plugin_name") != name
        or value.get("contract") not in _SUPPORTED_CONTRACTS
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("prior_commit", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("candidate_commit", "")))
        is None
    ):
        return None
    return {
        "bootstrap_prior_commit": str(value["prior_commit"]),
        "bootstrap_candidate_commit": str(value["candidate_commit"]),
        "bootstrap_contract": str(value["contract"]),
    }


class _ManagedUpdateContractV1:
    version = 1

    def __init__(
        self, plugin_name: str, bootstrap: dict[str, str] | None = None
    ):
        self._plugin_name = plugin_name
        self._bootstrap = bootstrap or {}

    def _call(self, operation: str, **payload: Any) -> dict[str, Any]:
        return _ipc_call(
            {
                "operation": operation,
                "plugin_name": self._plugin_name,
                **self._bootstrap,
                **payload,
            }
        )

    def preflight(self, *, plugin_name: str, plugin_root: Path) -> None:
        self._call(
            "preflight",
            requested_plugin_name=plugin_name,
            plugin_root=str(plugin_root.resolve()),
        )

    def complete(
        self,
        *,
        plugin_name: str,
        plugin_root: Path,
        source_commit: str,
        product_version: str,
    ) -> dict[str, Any]:
        return self._call(
            "complete",
            requested_plugin_name=plugin_name,
            plugin_root=str(plugin_root.resolve()),
            source_commit=source_commit,
            product_version=product_version,
        )

    def rollback(
        self,
        *,
        plugin_name: str,
        plugin_root: Path,
        source_commit: str,
        product_version: str | None,
    ) -> dict[str, Any]:
        return self._call(
            "rollback",
            requested_plugin_name=plugin_name,
            plugin_root=str(plugin_root.resolve()),
            source_commit=source_commit,
            product_version=product_version,
        )


def get_managed_update_contract(name: str) -> _ManagedUpdateContractV1 | None:
    """Return the synchronous host contract requested by managed workers."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    bootstrap = _bootstrap_context(name)
    if bootstrap is not None:
        return _ManagedUpdateContractV1(name, bootstrap)
    plugin_root = get_process_hermes_home() / "plugins" / name
    try:
        spec = get_managed_update_spec(plugin_root, strict=True)
    except ManagedPluginUpdateError:
        return None
    if spec is None or spec.contract not in _SUPPORTED_CONTRACTS:
        return None
    return _ManagedUpdateContractV1(name)


def begin_managed_update_bootstrap(
    plugin_name: str,
    plugin_root: Path,
    candidate: ManagedUpdateCandidate,
) -> None:
    _ipc_call(
        {
            "operation": "bootstrap_begin",
            "plugin_name": plugin_name,
            "requested_plugin_name": plugin_name,
            "plugin_root": str(plugin_root.resolve()),
            "bootstrap_prior_commit": candidate.prior_commit,
            "bootstrap_candidate_commit": candidate.source_commit,
            "bootstrap_contract": candidate.spec.contract,
        }
    )


def abort_managed_update_bootstrap(
    plugin_name: str,
    plugin_root: Path,
    candidate: ManagedUpdateCandidate,
) -> None:
    _ipc_call(
        {
            "operation": "bootstrap_abort",
            "plugin_name": plugin_name,
            "requested_plugin_name": plugin_name,
            "plugin_root": str(plugin_root.resolve()),
            "bootstrap_prior_commit": candidate.prior_commit,
            "bootstrap_candidate_commit": candidate.source_commit,
            "bootstrap_contract": candidate.spec.contract,
        }
    )


def finalize_managed_update_bootstrap(
    plugin_name: str,
    plugin_root: Path,
    candidate: ManagedUpdateCandidate,
) -> None:
    payload = {
        "plugin_name": plugin_name,
        "requested_plugin_name": plugin_name,
        "plugin_root": str(plugin_root.resolve()),
        "bootstrap_prior_commit": candidate.prior_commit,
        "bootstrap_candidate_commit": candidate.source_commit,
        "bootstrap_contract": candidate.spec.contract,
    }
    _ipc_call({**payload, "operation": "bootstrap_prepare_finalize"})
    _ipc_call({**payload, "operation": "bootstrap_finalize"})


def _acquire_owner_lock(path: Path, *, contention_message: str) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError) as exc:
        os.close(fd)
        raise ManagedPluginUpdateError(contention_message) from exc
    return fd


def _release_owner_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    finally:
        os.close(fd)


@contextmanager
def plugin_update_lock(plugin_root: Path):
    """Serialize CLI and dashboard update requests for one installed plugin."""
    key = hashlib.sha256(str(plugin_root.resolve()).encode("utf-8")).hexdigest()[:16]
    fd = _acquire_owner_lock(
        _runtime_dir(create=True) / f"plugin-update-{key}.lock",
        contention_message="Another update for this plugin is already in progress.",
    )
    try:
        yield
    finally:
        _release_owner_lock(fd)


@dataclass
class _BootstrapAuthorization:
    prior_commit: str
    candidate_commit: str
    contract: str
    phase: str = "begun"


class ManagedUpdateCoordinator:
    """Authenticated local coordinator owned by the running dashboard host."""

    def __init__(
        self,
        *,
        preflight: Callable[[str, Path], None],
        reload_backend: Callable[[str, Path, str, str | None], dict[str, Any]],
        begin_bootstrap: Callable[[str, Path], None] | None = None,
        release_bootstrap: Callable[[str, Path], None] | None = None,
    ):
        self._preflight = preflight
        self._reload_backend = reload_backend
        self._begin_bootstrap = begin_bootstrap or (lambda _name, _root: None)
        self._release_bootstrap = release_bootstrap or (lambda _name, _root: None)
        self._bootstraps: dict[str, _BootstrapAuthorization] = {}
        self._operation_lock = threading.Lock()
        self._client_threads: set[threading.Thread] = set()
        self._client_threads_lock = threading.Lock()
        self._stopped = threading.Event()
        self._authkey = secrets.token_bytes(32)
        self._instance = secrets.token_hex(16)
        self._descriptor_path = (
            _descriptor_dir(create=True) / f"{self._instance}.json"
        )
        try:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen()
            self._listener.settimeout(0.5)
            host, port = self._listener.getsockname()
            self._address = (host, port)
            _write_private_json(
                self._descriptor_path,
                {
                    "address": [host, port],
                    "authkey": self._authkey.hex(),
                    "instance": self._instance,
                    "pid": os.getpid(),
                    "version": 1,
                },
            )
        except Exception:
            try:
                self._listener.close()
            except (AttributeError, OSError):
                pass
            raise
        self._thread = threading.Thread(
            target=self._serve,
            name="managed-plugin-update-coordinator",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                name="managed-plugin-update-client",
                daemon=True,
            )
            with self._client_threads_lock:
                self._client_threads.add(thread)
            thread.start()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(_HANDSHAKE_TIMEOUT_SECONDS)
                try:
                    frame = _recv_frame(connection)
                    if len(frame) < 64:
                        raise ValueError("managed update request is too short")
                    nonce = frame[:32]
                    signature = frame[32:64]
                    request_bytes = frame[64:]
                    expected = hmac.digest(
                        self._authkey,
                        b"request" + nonce + request_bytes,
                        "sha256",
                    )
                    if not hmac.compare_digest(signature, expected):
                        raise PermissionError(
                            "managed update request was not authenticated"
                        )
                except (OSError, EOFError, ValueError, PermissionError):
                    return
                try:
                    request = json.loads(request_bytes.decode("utf-8"))
                    response = self._handle(request)
                except Exception as exc:
                    response = {"ok": False, "error": _redact_error(exc)}
                try:
                    response_bytes = json.dumps(
                        response, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    response_signature = hmac.digest(
                        self._authkey,
                        b"response" + nonce + response_bytes,
                        "sha256",
                    )
                    _send_frame(
                        connection, response_signature + response_bytes
                    )
                except (OSError, ValueError):
                    pass
        finally:
            with self._client_threads_lock:
                self._client_threads.discard(threading.current_thread())

    def _validate_request(self, request: dict[str, Any]) -> tuple[str, Path]:
        if not isinstance(request, dict):
            raise ManagedPluginUpdateError("Invalid managed update request.")
        name = request.get("plugin_name")
        requested_name = request.get("requested_plugin_name")
        if (
            not isinstance(name, str)
            or name != requested_name
            or not name
            or "/" in name
            or "\\" in name
        ):
            raise ManagedPluginUpdateError("Invalid managed plugin identity.")
        expected_root = (get_process_hermes_home() / "plugins" / name).resolve()
        try:
            requested_root = Path(str(request["plugin_root"])).resolve()
        except (KeyError, OSError, RuntimeError) as exc:
            raise ManagedPluginUpdateError("Invalid managed plugin root.") from exc
        if requested_root != expected_root:
            raise ManagedPluginUpdateError(
                "Managed update request does not match the installed plugin root."
            )
        return name, expected_root

    @staticmethod
    def _requested_bootstrap(
        request: dict[str, Any],
    ) -> _BootstrapAuthorization | None:
        prior = request.get("bootstrap_prior_commit")
        candidate = request.get("bootstrap_candidate_commit")
        contract = request.get("bootstrap_contract")
        if prior is None and candidate is None and contract is None:
            return None
        if (
            not isinstance(prior, str)
            or re.fullmatch(r"[0-9a-f]{40}", prior) is None
            or not isinstance(candidate, str)
            or re.fullmatch(r"[0-9a-f]{40}", candidate) is None
            or contract not in _SUPPORTED_CONTRACTS
        ):
            raise ManagedPluginUpdateError(
                "Invalid managed update bootstrap identity."
            )
        return _BootstrapAuthorization(prior, candidate, str(contract))

    def _validate_bootstrap_candidate(
        self,
        name: str,
        root: Path,
        authorization: _BootstrapAuthorization,
    ) -> None:
        head = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
        upstream = _git(root, ["rev-parse", "@{upstream}^{commit}"]).stdout.strip()
        if (
            head != authorization.prior_commit
            or upstream != authorization.candidate_commit
            or _git(
                root,
                [
                    "merge-base",
                    "--is-ancestor",
                    authorization.prior_commit,
                    authorization.candidate_commit,
                ],
                check=False,
            ).returncode
            != 0
            or _git(
                root,
                ["status", "--porcelain", "--untracked-files=all"],
            ).stdout.strip()
        ):
            raise ManagedPluginUpdateError(
                "Managed update bootstrap no longer matches a clean fast-forward target."
            )
        spec = _candidate_spec_from_git(
            name, root, authorization.candidate_commit
        )
        if spec is None or spec.contract != authorization.contract:
            raise ManagedPluginUpdateError(
                "Managed update bootstrap target is not authorized by its manifest."
            )

    def _matching_bootstrap(
        self,
        name: str,
        request: dict[str, Any],
    ) -> _BootstrapAuthorization | None:
        requested = self._requested_bootstrap(request)
        active = self._bootstraps.get(name)
        if requested is None or active is None:
            return None
        if (
            requested.prior_commit != active.prior_commit
            or requested.candidate_commit != active.candidate_commit
            or requested.contract != active.contract
        ):
            raise ManagedPluginUpdateError(
                "Managed update bootstrap authorization does not match."
            )
        return active

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._operation_lock.acquire(timeout=5):
            raise ManagedPluginUpdateError(
                "Another managed plugin handoff is already in progress."
        )
        try:
            name, root = self._validate_request(request)
            operation = request.get("operation")
            requested_bootstrap = self._requested_bootstrap(request)
            if operation == "bootstrap_begin":
                if requested_bootstrap is None:
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap identity is required."
                    )
                existing = self._bootstraps.get(name)
                if existing is not None:
                    if existing != requested_bootstrap:
                        raise ManagedPluginUpdateError(
                            "Another managed update bootstrap is already active."
                        )
                    return {"ok": True, "result": {}}
                if get_managed_update_spec(root, strict=True) is not None:
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap is only valid for a legacy plugin."
                    )
                self._validate_bootstrap_candidate(
                    name, root, requested_bootstrap
                )
                self._preflight(name, root)
                self._begin_bootstrap(name, root)
                self._bootstraps[name] = requested_bootstrap
                return {"ok": True, "result": {}}

            active_bootstrap = self._matching_bootstrap(name, request)
            if operation == "bootstrap_prepare_finalize":
                if active_bootstrap is None:
                    return {"ok": True, "result": {}}
                if active_bootstrap.phase != "completed":
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap has not completed on this host."
                    )
                return {"ok": True, "result": {}}
            if operation == "bootstrap_finalize":
                if active_bootstrap is None:
                    return {"ok": True, "result": {}}
                if active_bootstrap.phase != "completed":
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap has not completed on this host."
                    )
                self._release_bootstrap(name, root)
                self._bootstraps.pop(name, None)
                return {"ok": True, "result": {}}
            if operation == "bootstrap_abort":
                if active_bootstrap is None:
                    return {"ok": True, "result": {}}
                if active_bootstrap.phase not in {
                    "begun",
                    "preflighted",
                    "rolled_back",
                }:
                    raise ManagedPluginUpdateError(
                        "Managed update recovery is incomplete; plugin routes remain gated."
                    )
                head = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
                dirty = _git(
                    root, ["status", "--porcelain", "--untracked-files=all"]
                ).stdout.strip()
                if head != active_bootstrap.prior_commit or dirty:
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap could not safely restore the prior source."
                    )
                self._release_bootstrap(name, root)
                self._bootstraps.pop(name, None)
                return {"ok": True, "result": {}}

            spec = get_managed_update_spec(root, strict=True)
            supported_installed = (
                spec is not None and spec.contract in _SUPPORTED_CONTRACTS
            )
            if not supported_installed and active_bootstrap is None:
                raise ManagedPluginUpdateError(
                    "The installed plugin does not declare a supported managed update."
                )
            if operation == "preflight":
                self._preflight(name, root)
                if active_bootstrap is not None:
                    active_bootstrap.phase = "preflighted"
                return {"ok": True, "result": {}}
            if operation not in {"complete", "rollback"}:
                raise ManagedPluginUpdateError("Invalid managed update operation.")
            source_commit = request.get("source_commit")
            product_version = request.get("product_version")
            if (
                not isinstance(source_commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
                or (
                    product_version is not None
                    and (
                        not isinstance(product_version, str)
                        or not product_version.strip()
                    )
                )
                or (operation == "complete" and product_version is None)
            ):
                raise ManagedPluginUpdateError(
                    "Invalid managed update target identity."
                )
            if active_bootstrap is not None:
                if (
                    operation == "rollback"
                    and source_commit != active_bootstrap.prior_commit
                ):
                    raise ManagedPluginUpdateError(
                        "Managed update bootstrap requested an unauthorized "
                        "rollback source commit."
                    )
                if operation == "complete":
                    within_validated_history = (
                        _git(
                            root,
                            [
                                "merge-base",
                                "--is-ancestor",
                                active_bootstrap.prior_commit,
                                source_commit,
                            ],
                            check=False,
                        ).returncode
                        == 0
                        and _git(
                            root,
                            [
                                "merge-base",
                                "--is-ancestor",
                                source_commit,
                                active_bootstrap.candidate_commit,
                            ],
                            check=False,
                        ).returncode
                        == 0
                    )
                    target_spec = _candidate_spec_from_git(
                        name, root, source_commit
                    )
                    if (
                        not within_validated_history
                        or target_spec is None
                        or target_spec.contract != active_bootstrap.contract
                    ):
                        raise ManagedPluginUpdateError(
                            "Managed update bootstrap target is outside the "
                            "validated managed fast-forward history."
                        )
                active_bootstrap.phase = "reloading"
            result = self._reload_backend(
                name, root, source_commit, product_version
            )
            if (
                result.get("reloaded") is not True
                or result.get("loaded_source_commit") != source_commit
                or result.get("loaded_product_version") != product_version
            ):
                raise ManagedPluginUpdateError(
                    "The dashboard host could not attest the requested loaded product."
                )
            if active_bootstrap is not None:
                active_bootstrap.phase = (
                    "completed" if operation == "complete" else "rolled_back"
                )
            return {"ok": True, "result": result}
        finally:
            self._operation_lock.release()

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._listener.close()
        self._thread.join(timeout=_HANDSHAKE_TIMEOUT_SECONDS + 1)
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS + 1
        with self._client_threads_lock:
            client_threads = list(self._client_threads)
        for thread in client_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for name in tuple(self._bootstraps):
            try:
                self._release_bootstrap(
                    name, (get_process_hermes_home() / "plugins" / name).resolve()
                )
            except Exception:
                pass
        self._bootstraps.clear()
        try:
            self._descriptor_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ManagedUpdateCoordinator":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
