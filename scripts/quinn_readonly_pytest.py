#!/usr/bin/env python3
"""Run pytest against an exact Git tree in a disposable macOS sandbox.

The interpreter is supplied only through HERMES_PYTHON. No environment is
installed or modified. The reviewed tree is exported from immutable Git bytes,
and ``sandbox-exec`` denies all network access and every filesystem write other
than isolated reviewer-owned temporary output. The policy is inherited by test
subprocesses; unsupported hosts fail closed rather than falling back to Python
monkeypatching or Unix mode bits.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import BinaryIO, Iterable
import unicodedata


RESULT_SCHEMA_VERSION = 3
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
UNSUPPORTED_SANDBOX_EXIT = 4
_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONTENT_SAFE_NODE_RE = re.compile(r"[A-Za-z0-9_./:\[\],=+-]{1,512}\Z")
_COUNT_KEYS = (
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "deselected",
)

_SANDBOX_PROBE = r"""
import errno
import json
from pathlib import Path
import socket
import subprocess
import sys

DENIED = {errno.EPERM, errno.EACCES}
protected = Path(sys.argv[1])
scratch = Path(sys.argv[2])
default_home = Path(sys.argv[3])


def tcp_errno():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return sock.connect_ex(("127.0.0.1", 9))
    finally:
        sock.close()


def udp_errno():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.sendto(b"probe", ("127.0.0.1", 9))
        except OSError as exc:
            return exc.errno
        return 0
    finally:
        sock.close()


def operation_errno(callback):
    try:
        callback()
    except OSError as exc:
        return exc.errno
    return 0


child_code = (
    "import json,socket;"
    "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);"
    "r=s.connect_ex(('127.0.0.1',9));s.close();"
    "print(json.dumps({'tcp_errno':r}))"
)
child = subprocess.run(
    [sys.executable, "-I", "-c", child_code],
    check=False,
    capture_output=True,
    text=True,
)
try:
    child_tcp_errno = int(json.loads(child.stdout)["tcp_errno"])
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    child_tcp_errno = 0

source_original = protected.read_bytes()
source_parent = protected.parent
scratch_root = scratch.parent
writable_link = scratch_root / "reviewed-source-link"
writable_link.symlink_to(protected)
default_escape = default_home / "sandbox-escape"

results = {
    "tcp_errno": tcp_errno(),
    "udp_errno": udp_errno(),
    "subprocess_tcp_errno": child_tcp_errno,
    "subprocess_exit_code": child.returncode,
    "source_write_errno": operation_errno(
        lambda: protected.write_text("mutated", encoding="utf-8")
    ),
    "source_chmod_errno": operation_errno(lambda: protected.chmod(0o600)),
    "source_parent_chmod_errno": operation_errno(lambda: source_parent.chmod(0o700)),
    "source_rename_errno": operation_errno(
        lambda: protected.rename(scratch_root / "renamed-source")
    ),
    "writable_symlink_created": writable_link.is_symlink(),
    "symlink_escape_write_errno": operation_errno(
        lambda: writable_link.write_bytes(b"via-link")
    ),
    "source_symlink_create_errno": operation_errno(
        lambda: (source_parent / "source-created-link").symlink_to(scratch_root)
    ),
    "default_home_write_errno": operation_errno(
        lambda: default_escape.write_text("forbidden", encoding="utf-8")
    ),
}
scratch.write_text("allowed", encoding="utf-8")
results["scratch_write"] = scratch.read_text(encoding="utf-8") == "allowed"
results["source_unchanged_in_child"] = (
    protected.is_file() and protected.read_bytes() == source_original
)
results["default_home_untouched_in_child"] = (
    (default_home / "marker").read_text(encoding="utf-8") == "unchanged"
    and not default_escape.exists()
)
results["enforced"] = (
    results["tcp_errno"] in DENIED
    and results["udp_errno"] in DENIED
    and results["subprocess_tcp_errno"] in DENIED
    and results["subprocess_exit_code"] == 0
    and results["source_write_errno"] in DENIED
    and results["source_chmod_errno"] in DENIED
    and results["source_parent_chmod_errno"] in DENIED
    and results["source_rename_errno"] in DENIED
    and results["writable_symlink_created"]
    and results["symlink_escape_write_errno"] in DENIED
    and results["source_symlink_create_errno"] in DENIED
    and results["default_home_write_errno"] in DENIED
    and results["scratch_write"]
    and results["source_unchanged_in_child"]
    and results["default_home_untouched_in_child"]
)
print(json.dumps(results, sort_keys=True))
raise SystemExit(0 if results["enforced"] else 1)
"""


def _fail(message: str, code: int = 2) -> int:
    print(f"quinn-readonly-pytest: {message}", file=sys.stderr)
    return code


def _run_git(repo: Path, *args: str, binary: bool = False):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=not binary,
    )


class MaterializationError(RuntimeError):
    """Fixed-class failure whose code is safe for structured result metadata."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GitEntry:
    mode: str
    object_type: str
    object_id: str
    path: PurePosixPath


@dataclass(frozen=True)
class GitManifest:
    entries: tuple[GitEntry, ...]
    digest_sha256: str


def _parse_ls_tree_manifest(listing: bytes) -> GitManifest:
    """Parse and validate a recursive NUL-delimited ``git ls-tree`` listing."""
    records = listing.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    elif records:
        raise MaterializationError("malformed_git_manifest")

    entries: list[GitEntry] = []
    by_path: dict[str, GitEntry] = {}
    normalized_paths: set[str] = set()
    for record in records:
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_bytes, type_bytes, oid_bytes = header.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            object_type = type_bytes.decode("ascii")
            object_id = oid_bytes.decode("ascii")
            path_text = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise MaterializationError("malformed_git_manifest") from exc

        expected_type = {
            "040000": "tree",
            "100644": "blob",
            "100755": "blob",
            "120000": "blob",
        }.get(mode)
        if expected_type is None or object_type != expected_type:
            raise MaterializationError("unsupported_git_entry")
        if not _OBJECT_ID_RE.fullmatch(object_id):
            raise MaterializationError("malformed_git_manifest")
        path = PurePosixPath(path_text)
        if (
            not path_text
            or path.is_absolute()
            or path.as_posix() != path_text
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in path_text
            or any(ord(character) < 32 or ord(character) == 127 for character in path_text)
        ):
            raise MaterializationError("unsafe_git_path")
        if path_text in by_path:
            raise MaterializationError("duplicate_git_path")
        normalized = unicodedata.normalize("NFC", path_text).casefold()
        if normalized in normalized_paths:
            raise MaterializationError("filesystem_path_collision")
        normalized_paths.add(normalized)
        entry = GitEntry(mode, object_type, object_id, path)
        by_path[path_text] = entry
        entries.append(entry)

    for entry in entries:
        parents = list(entry.path.parents)
        for parent in parents:
            if parent == PurePosixPath("."):
                continue
            parent_entry = by_path.get(parent.as_posix())
            if parent_entry is not None and parent_entry.mode != "040000":
                raise MaterializationError("git_path_prefix_conflict")

    entries.sort(key=lambda item: item.path.as_posix())
    digest = hashlib.sha256()
    for entry in entries:
        encoded_path = entry.path.as_posix().encode("utf-8")
        digest.update(entry.mode.encode("ascii") + b"\0")
        digest.update(entry.object_type.encode("ascii") + b"\0")
        digest.update(entry.object_id.encode("ascii") + b"\0")
        digest.update(len(encoded_path).to_bytes(4, "big") + encoded_path)
    return GitManifest(tuple(entries), digest.hexdigest())


def _read_git_manifest(repo: Path, tree: str) -> GitManifest:
    listed = _run_git(
        repo,
        "ls-tree",
        "-r",
        "-t",
        "-z",
        "--full-tree",
        tree,
        binary=True,
    )
    if listed.returncode != 0:
        raise MaterializationError("git_manifest_read_failed")
    return _parse_ls_tree_manifest(listed.stdout)


def _safe_symlink_target(entry: GitEntry, payload: bytes) -> str:
    if len(payload) > 4096 or b"\0" in payload:
        raise MaterializationError("unsafe_git_symlink")
    try:
        target = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MaterializationError("unsafe_git_symlink") from exc
    target_path = PurePosixPath(target)
    if not target or target_path.is_absolute() or "\\" in target:
        raise MaterializationError("unsafe_git_symlink")
    resolved_parts = list(entry.path.parent.parts)
    if resolved_parts == ["."]:
        resolved_parts = []
    for component in target_path.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved_parts:
                raise MaterializationError("unsafe_git_symlink")
            resolved_parts.pop()
        else:
            resolved_parts.append(component)
    return target


class _CatFileBatch:
    """Bounded direct blob reader over one ``git cat-file --batch`` process."""

    def __init__(self, repo: Path):
        self._process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def __enter__(self):
        return self

    @staticmethod
    def _read_exact(stream: BinaryIO, size: int, sink: BinaryIO | None = None) -> bytes:
        remaining = size
        chunks: list[bytes] = []
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise MaterializationError("git_object_read_failed")
            if sink is None:
                chunks.append(chunk)
            else:
                sink.write(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_blob(self, entry: GitEntry, sink: BinaryIO | None = None) -> bytes:
        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            raise MaterializationError("git_object_read_failed")
        stdin.write(entry.object_id.encode("ascii") + b"\n")
        stdin.flush()
        header = stdout.readline(256)
        if not header or len(header) >= 256 or not header.endswith(b"\n"):
            raise MaterializationError("git_object_read_failed")
        fields = header.rstrip(b"\n").split(b" ")
        if len(fields) == 2 and fields[1] == b"missing":
            raise MaterializationError("git_object_missing")
        if len(fields) != 3:
            raise MaterializationError("git_object_read_failed")
        try:
            actual_id = fields[0].decode("ascii")
            object_type = fields[1].decode("ascii")
            size = int(fields[2])
        except (UnicodeDecodeError, ValueError) as exc:
            raise MaterializationError("git_object_read_failed") from exc
        if actual_id != entry.object_id or object_type != "blob" or size < 0:
            raise MaterializationError("git_object_identity_mismatch")
        payload = self._read_exact(stdout, size, sink)
        if stdout.read(1) != b"\n":
            raise MaterializationError("git_object_read_failed")
        return payload

    def __exit__(self, exc_type, _exc, _traceback):
        process = self._process
        if exc_type is not None:
            process.kill()
            process.wait()
            return False
        if process.stdin is not None:
            process.stdin.close()
        return_code = process.wait()
        if return_code != 0:
            raise MaterializationError("git_object_read_failed")
        return False


def _expected_paths(manifest: GitManifest) -> dict[str, GitEntry | None]:
    expected: dict[str, GitEntry | None] = {}
    for entry in manifest.entries:
        expected[entry.path.as_posix()] = entry
        for parent in entry.path.parents:
            if parent == PurePosixPath("."):
                continue
            expected.setdefault(parent.as_posix(), None)
    return expected


def _materialized_paths(root: Path) -> dict[str, tuple[str, int]]:
    found: dict[str, tuple[str, int]] = {}
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise MaterializationError("materialization_verification_failed") from exc
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            child_stat = child.stat(follow_symlinks=False)
            mode = stat.S_IMODE(child_stat.st_mode)
            if stat.S_ISLNK(child_stat.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(child_stat.st_mode):
                kind = "directory"
                stack.append((Path(child.path), relative))
            elif stat.S_ISREG(child_stat.st_mode):
                kind = "file"
            else:
                raise MaterializationError("materialization_verification_failed")
            found[relative] = (kind, mode)
    return found


def _git_blob_id(path: Path, *, object_id_length: int, symlink: bool = False) -> str:
    algorithm = hashlib.sha1 if object_id_length == 40 else hashlib.sha256
    digest = algorithm()
    if symlink:
        payload = os.readlink(path).encode("utf-8")
        digest.update(f"blob {len(payload)}\0".encode("ascii"))
        digest.update(payload)
        return digest.hexdigest()
    size = path.stat().st_size
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verify_materialized_tree(
    repo: Path,
    manifest: GitManifest,
    destination: Path,
    *,
    read_only: bool,
) -> bool:
    del repo
    expected = _expected_paths(manifest)
    actual = _materialized_paths(destination)
    if set(expected) != set(actual):
        raise MaterializationError("materialization_verification_failed")
    by_path = {entry.path.as_posix(): entry for entry in manifest.entries}
    for path_text, expected_entry in expected.items():
        kind, actual_mode = actual[path_text]
        target = destination.joinpath(*PurePosixPath(path_text).parts)
        if expected_entry is None or expected_entry.mode == "040000":
            wanted_mode = 0o555 if read_only else 0o755
            if kind != "directory" or actual_mode != wanted_mode:
                raise MaterializationError("materialization_verification_failed")
            continue
        if expected_entry.mode == "120000":
            if kind != "symlink" or _git_blob_id(
                target,
                object_id_length=len(expected_entry.object_id),
                symlink=True,
            ) != expected_entry.object_id:
                raise MaterializationError("materialization_verification_failed")
            continue
        wanted_mode = (
            0o555
            if read_only and expected_entry.mode == "100755"
            else 0o444
            if read_only
            else 0o755
            if expected_entry.mode == "100755"
            else 0o644
        )
        if (
            kind != "file"
            or actual_mode != wanted_mode
            or _git_blob_id(
                target, object_id_length=len(expected_entry.object_id)
            )
            != expected_entry.object_id
        ):
            raise MaterializationError("materialization_verification_failed")
    # Every explicit manifest path was consumed; this also makes accidental
    # omission of a recursive tree entry visible during review/static checks.
    if set(by_path) - set(expected):
        raise MaterializationError("materialization_verification_failed")
    return True


def _materialize_git_tree(
    repo: Path, manifest: GitManifest, destination: Path
) -> dict[str, object]:
    symlink_payloads: dict[str, tuple[str, bytes]] = {}
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with _CatFileBatch(repo) as objects:
        for entry in manifest.entries:
            if entry.mode == "120000":
                payload = objects.read_blob(entry)
                symlink_payloads[entry.path.as_posix()] = (
                    _safe_symlink_target(entry, payload),
                    payload,
                )

        directories = [
            PurePosixPath(path)
            for path, entry in _expected_paths(manifest).items()
            if entry is None or entry.mode == "040000"
        ]
        for relative in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
            target = destination.joinpath(*relative.parts)
            target.mkdir(mode=0o755, exist_ok=True)
            target.chmod(0o755)

        for entry in manifest.entries:
            if entry.mode not in {"100644", "100755"}:
                continue
            target = destination.joinpath(*entry.path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(target, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    objects.read_blob(entry, stream)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            target.chmod(0o755 if entry.mode == "100755" else 0o644)

    for entry in manifest.entries:
        if entry.mode != "120000":
            continue
        target_text, _payload = symlink_payloads[entry.path.as_posix()]
        target = destination.joinpath(*entry.path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target_text, target)

    _verify_materialized_tree(repo, manifest, destination, read_only=False)
    return {
        "method": "git_ls_tree_cat_file_batch",
        "manifest_digest_sha256": manifest.digest_sha256,
        "entry_count": len(manifest.entries),
        "tree_count": sum(entry.mode == "040000" for entry in manifest.entries),
        "regular_blob_count": sum(
            entry.mode in {"100644", "100755"} for entry in manifest.entries
        ),
        "executable_blob_count": sum(
            entry.mode == "100755" for entry in manifest.entries
        ),
        "symlink_count": sum(entry.mode == "120000" for entry in manifest.entries),
        "verified_before_pytest": True,
        "verified_after_pytest": False,
    }


def _make_read_only(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            continue
        current = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            path.chmod(0o555)
        elif current & 0o111:
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


def _is_read_only(root: Path) -> bool:
    if stat.S_IMODE(root.stat().st_mode) & 0o222:
        return False
    return all(
        path.is_symlink() or not (stat.S_IMODE(path.stat().st_mode) & 0o222)
        for path in root.rglob("*")
    )


def _sandbox_support_error() -> str:
    if sys.platform != "darwin":
        return "unsupported host: the Quinn reviewer sandbox requires macOS"
    if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        return "unsupported host: /usr/bin/sandbox-exec is unavailable"
    return ""


def _sandbox_profile(writable_root: Path) -> str:
    """Return a Seatbelt profile with one canonical writable subtree.

    ``allow default`` keeps interpreter/framework/process behavior compatible,
    while the explicit denials remove network and filesystem-write authority.
    The later, path-specific allow grants writes only beneath reviewer-owned
    scratch space. macOS canonicalizes symlink targets before applying these
    rules, so a link in scratch cannot grant writes back into reviewed source.
    """
    writable_literal = json.dumps(str(writable_root.resolve()))
    return textwrap.dedent(
        f"""\
        (version 1)
        (allow default)
        (deny network*)
        (deny file-write*)
        (allow file-write*
            (subpath {writable_literal})
            (literal "/dev/null"))
        """
    )


def _run_sandboxed(
    *,
    profile: Path,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SANDBOX_EXEC), "-f", str(profile), *command],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _parse_last_json_line(value: str) -> dict:
    for line in reversed(value.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _verify_sandbox(
    *,
    python: Path,
    profile: Path,
    source: Path,
    writable_root: Path,
    env: dict[str, str],
) -> tuple[bool, dict[str, object]]:
    """Exercise the real kernel policy before any reviewed test is launched."""
    protected = next(
        (
            path
            for path in sorted(source.rglob("*"), key=lambda item: item.as_posix())
            if path.is_file() and not path.is_symlink()
        ),
        None,
    )
    if protected is None:
        return False, {"enforced": False, "error_class": "empty_reviewed_tree"}
    protected_bytes = protected.read_bytes()
    protected_mode = stat.S_IMODE(protected.stat().st_mode)
    protected_parent_mode = stat.S_IMODE(protected.parent.stat().st_mode)
    default_home = profile.parent / "default-home-probe"
    default_home.mkdir()
    default_home_marker = default_home / "marker"
    default_home_marker.write_text("unchanged", encoding="utf-8")
    probe_script = writable_root / "sandbox-probe.py"
    probe_script.write_text(_SANDBOX_PROBE, encoding="utf-8")
    scratch = writable_root / "sandbox-probe-output"
    completed = _run_sandboxed(
        profile=profile,
        command=[
            str(python),
            "-I",
            str(probe_script),
            str(protected),
            str(scratch),
            str(default_home),
        ],
        cwd=writable_root,
        env=env,
    )
    payload = _parse_last_json_line(completed.stdout)
    protected_unchanged = (
        protected.is_file()
        and protected.read_bytes() == protected_bytes
        and stat.S_IMODE(protected.stat().st_mode) == protected_mode
        and stat.S_IMODE(protected.parent.stat().st_mode) == protected_parent_mode
    )
    default_home_untouched = (
        default_home_marker.read_text(encoding="utf-8") == "unchanged"
        and not (default_home / "sandbox-escape").exists()
    )
    scratch_allowed = scratch.is_file() and scratch.read_text(encoding="utf-8") == "allowed"
    enforced = bool(
        completed.returncode == 0
        and payload.get("enforced") is True
        and protected_unchanged
        and default_home_untouched
        and scratch_allowed
    )
    evidence: dict[str, object] = {
        "enforced": enforced,
        "probe_exit_code": completed.returncode,
        "tcp_errno": payload.get("tcp_errno"),
        "udp_errno": payload.get("udp_errno"),
        "subprocess_tcp_errno": payload.get("subprocess_tcp_errno"),
        "source_write_errno": payload.get("source_write_errno"),
        "source_chmod_errno": payload.get("source_chmod_errno"),
        "source_parent_chmod_errno": payload.get("source_parent_chmod_errno"),
        "source_rename_errno": payload.get("source_rename_errno"),
        "writable_symlink_created": payload.get("writable_symlink_created"),
        "symlink_escape_write_errno": payload.get("symlink_escape_write_errno"),
        "source_symlink_create_errno": payload.get("source_symlink_create_errno"),
        "default_home_write_errno": payload.get("default_home_write_errno"),
        # Backward-compatible aliases retained for existing evidence readers.
        "protected_write_errno": payload.get("source_write_errno"),
        "protected_chmod_errno": payload.get("source_chmod_errno"),
        "protected_unchanged": protected_unchanged,
        "source_unchanged": protected_unchanged,
        "default_home_untouched": default_home_untouched,
        "scratch_write_allowed": scratch_allowed,
    }
    return enforced, evidence


def _sanitized_environment(
    *,
    python: Path,
    source: Path,
    home: Path,
    hermes_home: Path,
    temp_dir: Path,
    writable_root: Path,
) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_PYTHON": str(python),
        "TMPDIR": str(temp_dir),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONPATH": str(source),
        "PYTHONPYCACHEPREFIX": str(writable_root / "pycache"),
        "PYTEST_GATEWAY_GUARD_CACHE_DIR": str(
            writable_root / "gateway-guard-cache"
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TZ": "UTC",
        "LANG": "C",
        "LC_ALL": "C",
        "TERM": "dumb",
    }


def _write_result(path: Path | None, payload: dict) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(serialized, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def _empty_test_counts() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}


def _parse_pytest_counts(stdout: str, stderr: str) -> dict[str, int]:
    """Extract only fixed integer counters from pytest's final summary line."""
    counts = _empty_test_counts()
    labels = r"passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?"
    summary = re.compile(
        rf"^(?:\d+ (?:{labels})(?:, )?)+ in \d+(?:\.\d+)?s$"
    )
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        candidate = line.strip().strip("=").strip()
        if not summary.fullmatch(candidate):
            continue
        for amount, label in re.findall(rf"(\d+) ({labels})", candidate):
            normalized = "errors" if label in {"error", "errors"} else label
            if normalized in counts:
                counts[normalized] = int(amount)
        break
    return counts


def _content_safe_test_nodes(pytest_args: list[str]) -> list[str]:
    nodes: list[str] = []
    for value in pytest_args:
        if value.startswith("-") or not _CONTENT_SAFE_NODE_RE.fullmatch(value):
            continue
        base = value.split("::", 1)[0]
        path = PurePosixPath(base)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not (base.endswith(".py") or base == "tests" or base.startswith("tests/"))
        ):
            continue
        nodes.append(value)
    return nodes


def _safe_interpreter_provenance(payload: dict) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key in ("python_version", "pytest_version", "pytest_asyncio_version"):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", value):
            safe[key] = value
    return safe


def _common_result(
    *,
    tree: str,
    reviewed_head: str,
    reviewed_head_tree: str,
    repo_dirty: bool,
    pytest_args: list[str],
) -> dict[str, object]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "tree": tree,
        "reviewed_head": reviewed_head,
        "reviewed_head_tree": reviewed_head_tree,
        "repo_dirty_at_start": repo_dirty,
        "pytest_exit_code": None,
        "test_counts": _empty_test_counts(),
        "pytest_arg_count": len(pytest_args),
        "test_node_args": _content_safe_test_nodes(pytest_args),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run pytest against an exact Git tree in a fail-closed macOS sandbox"
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tree", default="HEAD")
    parser.add_argument("--output", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)

    output = args.output.expanduser().resolve() if args.output else None
    raw_python = os.environ.get("HERMES_PYTHON", "").strip()
    if not raw_python:
        return _fail("HERMES_PYTHON is required; no fallback interpreter is allowed")
    # Preserve a venv entry-point path instead of resolving its interpreter
    # symlink to the base Python (which would lose the D2 pytest environment).
    python = Path(os.path.abspath(os.path.expanduser(raw_python)))
    if not python.is_file() or not os.access(python, os.X_OK):
        return _fail("HERMES_PYTHON is not an executable file")

    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        return _fail("repository directory not found")
    resolved = _run_git(repo, "rev-parse", "--verify", f"{args.tree}^{{tree}}")
    if resolved.returncode != 0:
        return _fail("Git tree not found")
    tree = resolved.stdout.strip()
    head_result = _run_git(repo, "rev-parse", "--verify", "HEAD")
    repo_head = head_result.stdout.strip() if head_result.returncode == 0 else ""
    head_tree_result = _run_git(repo, "rev-parse", "--verify", "HEAD^{tree}")
    repo_head_tree = (
        head_tree_result.stdout.strip() if head_tree_result.returncode == 0 else ""
    )
    status_result = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    repo_dirty = status_result.returncode != 0 or bool(status_result.stdout.strip())

    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args.pop(0)
    if not pytest_args:
        pytest_args = ["-q"]
    common = _common_result(
        tree=tree,
        reviewed_head=repo_head,
        reviewed_head_tree=repo_head_tree,
        repo_dirty=repo_dirty,
        pytest_args=pytest_args,
    )

    support_error = _sandbox_support_error()
    if support_error:
        result = {
            **common,
            "status": "unsupported_sandbox",
            "sandbox_backend": "unavailable",
            "error_class": "unsupported_os_sandbox",
            "exit_code": UNSUPPORTED_SANDBOX_EXIT,
            "return_code": UNSUPPORTED_SANDBOX_EXIT,
        }
        _write_result(output, result)
        return _fail(support_error, code=UNSUPPORTED_SANDBOX_EXIT)

    stage = "prepare_isolated_roots"
    materialization: dict[str, object] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="quinn-pytest-") as temp:
            sandbox = Path(temp).resolve()
            source = sandbox / "source"
            writable_root = sandbox / "writable"
            home = writable_root / "home"
            hermes_home = writable_root / "hermes-home"
            temp_dir = writable_root / "tmp"
            pytest_temp = writable_root / "pytest-tmp"
            for directory in (
                writable_root,
                home,
                hermes_home,
                temp_dir,
                pytest_temp,
                home / ".cache",
                home / ".config",
                home / ".local" / "share",
            ):
                directory.mkdir(parents=True)

            stage = "materialize_git_tree"
            manifest = _read_git_manifest(repo, tree)
            materialization = _materialize_git_tree(repo, manifest, source)
            _make_read_only(source)
            _verify_materialized_tree(repo, manifest, source, read_only=True)
            read_only_before = _is_read_only(source)
            if not read_only_before:
                raise MaterializationError("materialization_verification_failed")

            profile = sandbox / "quinn-review.sb"
            profile.write_text(_sandbox_profile(writable_root), encoding="utf-8")
            profile.chmod(0o444)
            environment = _sanitized_environment(
                python=python,
                source=source,
                home=home,
                hermes_home=hermes_home,
                temp_dir=temp_dir,
                writable_root=writable_root,
            )
            sandbox_policy = {
                "network_denied": True,
                "writes_isolated": True,
                "descendant_inherited": True,
            }

            stage = "verify_os_sandbox"
            sandbox_enforced, sandbox_probe = _verify_sandbox(
                python=python,
                profile=profile,
                source=source,
                writable_root=writable_root,
                env=environment,
            )
            try:
                unchanged_after_probe = _verify_materialized_tree(
                    repo, manifest, source, read_only=True
                )
            except MaterializationError:
                unchanged_after_probe = False
            if not sandbox_enforced or not unchanged_after_probe:
                materialization["verified_after_pytest"] = False
                result = {
                    **common,
                    "status": "unsupported_sandbox",
                    "sandbox_backend": "macos_sandbox_exec",
                    "error_class": "sandbox_policy_not_enforced",
                    "sandbox_probe": sandbox_probe,
                    "sandbox_policy": sandbox_policy,
                    "materialization": materialization,
                    "source_unchanged": unchanged_after_probe,
                    "source_read_only": read_only_before and _is_read_only(source),
                    "source_read_only_before": read_only_before,
                    "source_read_only_after": _is_read_only(source),
                    "exit_code": UNSUPPORTED_SANDBOX_EXIT,
                    "return_code": UNSUPPORTED_SANDBOX_EXIT,
                }
                _write_result(output, result)
                return _fail(
                    "macOS sandbox policy probe did not enforce network/write denial",
                    code=UNSUPPORTED_SANDBOX_EXIT,
                )

            stage = "verify_interpreter"
            capability_command = [
                str(python),
                "-I",
                "-c",
                (
                    "import json,platform;import pytest,pytest_asyncio;"
                    "print(json.dumps({'python_version':platform.python_version(),"
                    "'pytest_version':pytest.__version__,"
                    "'pytest_asyncio_version':pytest_asyncio.__version__},sort_keys=True))"
                ),
            ]
            capability = _run_sandboxed(
                profile=profile,
                command=capability_command,
                cwd=source,
                env=environment,
            )
            interpreter_provenance = _safe_interpreter_provenance(
                _parse_last_json_line(capability.stdout)
            )
            if capability.returncode != 0 or not interpreter_provenance.get(
                "pytest_version"
            ):
                result = {
                    **common,
                    "status": "interpreter_unavailable",
                    "error_class": "pytest_import_failed",
                    "sandbox_backend": "macos_sandbox_exec",
                    "sandbox_probe": sandbox_probe,
                    "sandbox_policy": sandbox_policy,
                    "materialization": materialization,
                    "source_unchanged": True,
                    "source_read_only": read_only_before,
                    "source_read_only_before": read_only_before,
                    "source_read_only_after": read_only_before,
                    "interpreter": str(python),
                    "interpreter_resolved": str(python.resolve()),
                    "hermes_python": str(python.resolve()),
                    "capability_exit_code": capability.returncode,
                    "exit_code": 2,
                    "return_code": 2,
                }
                _write_result(output, result)
                return _fail(
                    "HERMES_PYTHON cannot import pytest and pytest_asyncio in the sandbox"
                )

            pytest_command = [
                str(python),
                "-m",
                "pytest",
                "-p",
                "pytest_asyncio.plugin",
                "-p",
                "no:cacheprovider",
                f"--rootdir={source}",
                f"--basetemp={pytest_temp}",
                *pytest_args,
            ]
            print(
                "quinn-readonly-pytest: sandbox=macos_sandbox_exec "
                "network=denied writes=isolated",
                file=sys.stderr,
            )
            started = time.monotonic()
            stage = "run_pytest"
            completed = _run_sandboxed(
                profile=profile,
                command=pytest_command,
                cwd=source,
                env=environment,
            )
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            counts = _parse_pytest_counts(completed.stdout, completed.stderr)

            stage = "verify_materialized_tree_after_pytest"
            try:
                unchanged = _verify_materialized_tree(
                    repo, manifest, source, read_only=True
                )
            except MaterializationError:
                unchanged = False
            read_only_after = _is_read_only(source)
            materialization["verified_after_pytest"] = bool(
                unchanged and read_only_after
            )
            effective_exit = (
                completed.returncode
                if unchanged and read_only_before and read_only_after
                else 3
            )
            status = (
                "passed"
                if effective_exit == 0
                else "sandbox_violation"
                if not unchanged or not read_only_before or not read_only_after
                else "failed"
            )
            result = {
                **common,
                "status": status,
                "exit_code": effective_exit,
                "return_code": effective_exit,
                "pytest_exit_code": completed.returncode,
                "duration_ms": duration_ms,
                "test_counts": counts,
                "materialization": materialization,
                "source_unchanged": unchanged,
                "source_read_only": read_only_before and read_only_after,
                "source_read_only_before": read_only_before,
                "source_read_only_after": read_only_after,
                "sandbox_backend": "macos_sandbox_exec",
                "sandbox_probe": sandbox_probe,
                "sandbox_policy": sandbox_policy,
                "interpreter": str(python),
                "interpreter_resolved": str(python.resolve()),
                "hermes_python": str(python.resolve()),
                "interpreter_provenance": interpreter_provenance,
            }
            _write_result(output, result)
            print(
                "quinn-readonly-pytest: "
                f"status={status} pytest_exit={completed.returncode} "
                f"passed={counts['passed']} failed={counts['failed']} "
                f"errors={counts['errors']}",
                file=sys.stderr,
            )
            return effective_exit
    except Exception as exc:
        error_class = (
            exc.code
            if isinstance(exc, MaterializationError)
            else "runner_infrastructure_error"
        )
        result = {
            **common,
            "status": "infrastructure_error",
            "error_class": error_class,
            "failed_stage": stage,
            "exit_code": 3,
            "return_code": 3,
        }
        if materialization is not None:
            result["materialization"] = materialization
        _write_result(output, result)
        return _fail(
            f"sandbox setup failed at {stage} ({error_class})",
            code=3,
        )


if __name__ == "__main__":
    raise SystemExit(main())
