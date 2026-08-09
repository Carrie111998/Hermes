#!/usr/bin/env python
"""Independently verify schema-v3 CUA gateway runtime attestation receipts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import sysconfig
import threading
import types
import uuid
from pathlib import Path
from typing import Any

MODULES = (
    "tools/computer_use/tool.py", "tools/computer_use/cua_backend.py",
    "tools/computer_use/browser_route.py", "gateway/run.py", "gateway/session.py",
    "hermes_state.py",
    "tools/approval.py",
)
CALLABLES = (
    "tools.computer_use.tool:set_computer_use_session_validator", "tools.computer_use.tool:_validate_managed_publication", "tools.computer_use.tool:publish_computer_use_session", "tools.computer_use.tool:unpublish_computer_use_session", "tools.computer_use.tool:begin_computer_use_terminal_transition", "tools.computer_use.tool:end_computer_use_terminal_transition", "tools.computer_use.tool:_cua_permission_mode", "tools.computer_use.tool:_get_backend", "tools.computer_use.tool:_acquire_backend_for_call", "tools.computer_use.tool:release_computer_use_session_result", "tools.computer_use.tool:handle_computer_use", "tools.computer_use.tool:_dispatch",
    "tools.computer_use.cua_backend:_AsyncBridge.run", "tools.computer_use.cua_backend:_CuaDriverSession._lifecycle_coro", "tools.computer_use.cua_backend:_CuaDriverSession.start", "tools.computer_use.cua_backend:_CuaDriverSession._attest_runtime_locked", "tools.computer_use.cua_backend:_CuaDriverSession.call_tool", "tools.computer_use.cua_backend:_CuaDriverSession._call_tool_via_cli", "tools.computer_use.cua_backend:_CuaDriverSession._restart_session_locked", "tools.computer_use.cua_backend:_EmbeddedCuaDaemon.start", "tools.computer_use.cua_backend:_EmbeddedCuaDaemon.stop", "tools.computer_use.cua_backend:CuaDriverBackend.set_runtime_attestation_callback", "tools.computer_use.cua_backend:CuaDriverBackend.start", "tools.computer_use.cua_backend:CuaDriverBackend.stop", "tools.computer_use.cua_backend:CuaDriverBackend.capture", "tools.computer_use.cua_backend:CuaDriverBackend._apply_delivery", "tools.computer_use.cua_backend:CuaDriverBackend._run_input_action", "tools.computer_use.cua_backend:CuaDriverBackend._action", "tools.computer_use.cua_backend:CuaDriverBackend._maybe_attach_element_token", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_state", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_prepare", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_action",
    "gateway.run:GatewayRunner._run_agent", "gateway.session:SessionStore.ensure_route_matches", "gateway.session:SessionStore.route_matches", "gateway.session:SessionStore._run_route_transition", "gateway.session:SessionStore.prune_old_entries", "tools.approval:get_current_session_key", "tools.approval:is_approval_bypass_active_for_session",
    "hermes_state:SessionDB._execute_write", "hermes_state:SessionDB.publish_compression_child", "hermes_state:SessionDB.promote_to_session_reset",
)


class VerificationError(RuntimeError):
    pass


def _payload(code: types.CodeType) -> dict[str, Any]:
    def constant(value: Any) -> Any:
        if isinstance(value, types.CodeType): return {"type": "code", "value": _payload(value)}
        if value is None or isinstance(value, (bool, int, str)): return {"type": type(value).__name__, "value": value}
        if isinstance(value, float): return {"type": "float", "value": value.hex()}
        if isinstance(value, complex): return {"type": "complex", "real": value.real.hex(), "imag": value.imag.hex()}
        if isinstance(value, bytes): return {"type": "bytes", "value": value.hex()}
        if isinstance(value, tuple): return {"type": "tuple", "value": [constant(item) for item in value]}
        if isinstance(value, frozenset): return {"type": "frozenset", "value": sorted((constant(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))}
        raise VerificationError(f"unsupported code constant type: {type(value)!r}")
    return {"argcount": code.co_argcount, "posonlyargcount": code.co_posonlyargcount, "kwonlyargcount": code.co_kwonlyargcount, "nlocals": code.co_nlocals, "stacksize": code.co_stacksize, "flags": code.co_flags, "code": code.co_code.hex(), "consts": [constant(value) for value in code.co_consts], "names": list(code.co_names), "varnames": list(code.co_varnames), "freevars": list(code.co_freevars), "cellvars": list(code.co_cellvars), "filename": code.co_filename, "qualname": code.co_qualname, "firstlineno": code.co_firstlineno, "linetable": code.co_linetable.hex(), "exceptiontable": code.co_exceptiontable.hex()}


def _fingerprint(code: types.CodeType) -> str:
    return hashlib.sha256(json.dumps(_payload(code), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _codes(code: types.CodeType, found: dict[str, types.CodeType]) -> None:
    found[code.co_qualname] = code
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType): _codes(constant, found)


def _review_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise VerificationError(f"unsafe review path: {relative!r}")
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or path == root:
        raise VerificationError(f"review path escapes root: {relative!r}")
    if not path.is_file(): raise VerificationError(f"review source missing: {relative}")
    return path


def _verify_process(receipt: dict[str, Any]) -> None:
    try:
        import psutil
        process = psutil.Process(int(receipt["pid"]))
        if abs(process.create_time() - float(receipt["process_create_time"])) > 0.001: raise VerificationError("live process create time mismatch")
        if Path(process.exe()).resolve() != Path(str(receipt["executable"])).resolve(): raise VerificationError("live process executable mismatch")
        if Path(sys.executable).resolve() != Path(str(receipt["launcher"])).resolve(): raise VerificationError("live process launcher mismatch")
        parent = receipt["parent"]
        live_parent = process.parent()
        if (
            not isinstance(parent, dict)
            or live_parent is None
            or live_parent.pid != int(parent["pid"])
            or abs(live_parent.create_time() - float(parent["process_create_time"])) > 0.001
            or Path(live_parent.exe()).resolve() != Path(str(parent["executable"])).resolve()
        ): raise VerificationError("live parent process identity mismatch")
    except VerificationError: raise
    except Exception as exc: raise VerificationError("cannot determine live process identity") from exc


_NATIVE_RUNTIME_ARTIFACTS = (
    "python_runtime",
    "sqlite3_extension",
    "psutil_extension",
    "cua_driver_launcher",
    "cua_driver_executable",
)


def _native_file_identity(path: Path) -> dict[str, Any]:
    try:
        canonical = path.resolve(strict=True)
        if not canonical.is_file():
            raise OSError("not a regular file")
        raw = canonical.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise VerificationError("cannot independently read native runtime artifact") from exc
    return {
        "canonical_path": str(canonical),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _python_runtime_library() -> Path:
    names = [
        value for value in (
            sysconfig.get_config_var("LDLIBRARY"),
            sysconfig.get_config_var("LIBRARY"),
            f"python{sys.version_info.major}{sys.version_info.minor}.dll" if os.name == "nt" else None,
        ) if isinstance(value, str) and value
    ]
    directories = [Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).resolve().parent]
    libdir = sysconfig.get_config_var("LIBDIR")
    if isinstance(libdir, str) and libdir:
        directories.insert(0, Path(libdir))
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve(strict=True)
    raise VerificationError("cannot independently resolve Python runtime library")


def _live_cua_driver_processes(
    *,
    owner_pid: int,
    owner_create_time: float,
    owner_executable: str,
) -> list[dict[str, Any]]:
    """Independently bind the exact direct cua-driver children of one gateway."""
    try:
        import psutil
    except Exception as exc:
        raise VerificationError("cannot import psutil for cua-driver process verification") from exc
    try:
        owner = psutil.Process(int(owner_pid))
        if abs(owner.create_time() - float(owner_create_time)) > 0.001:
            raise VerificationError("gateway owner process create time mismatch")
        if Path(owner.exe()).resolve(strict=True) != Path(owner_executable).resolve(strict=True):
            raise VerificationError("gateway owner executable mismatch")
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError("cannot resolve gateway owner process") from exc

    parent_identity = {
        "pid": int(owner_pid),
        "process_create_time": float(owner_create_time),
        "executable": str(owner_executable),
    }
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if "cua-driver" not in str(process.info.get("name") or "").lower():
                continue
            if process.ppid() != int(owner_pid):
                continue
            parent = process.parent()
            if parent is None or parent.pid != int(owner_pid):
                continue
            if abs(parent.create_time() - float(owner_create_time)) > 0.001:
                continue
            if Path(parent.exe()).resolve(strict=True) != Path(
                owner_executable
            ).resolve(strict=True):
                continue
            argv = process.cmdline()
            if not isinstance(argv, list) or not argv or not all(
                isinstance(value, str) for value in argv
            ):
                raise VerificationError("owned cua-driver argv is unavailable")
            _native_file_identity(Path(argv[0]))
            rows.append({
                "pid": process.pid,
                "process_create_time": process.create_time(),
                "ppid": int(owner_pid),
                "executable": _native_file_identity(Path(process.exe())),
                "parent": dict(parent_identity),
                "argv": argv,
            })
        except VerificationError:
            raise
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    rows.sort(key=lambda row: (row["process_create_time"], row["pid"]))
    if not rows:
        raise VerificationError("no gateway-owned cua-driver process is live")
    executable_identities = {
        (row["executable"]["canonical_path"], row["executable"]["sha256"])
        for row in rows
    }
    launcher_identities = {
        (
            identity["canonical_path"],
            identity["sha256"],
        )
        for identity in (
            _native_file_identity(Path(row["argv"][0])) for row in rows
        )
    }
    if len(executable_identities) != 1:
        raise VerificationError("owned cua-driver processes use ambiguous executable bytes")
    if len(launcher_identities) != 1:
        raise VerificationError("owned cua-driver processes use ambiguous launcher bytes")
    return rows


def _live_native_runtime_artifacts(
    cua_driver_processes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve and hash the verifier runtime's fixed native dependency policy."""
    try:
        import _sqlite3
        import psutil

        extension_name = {
            "win32": "psutil._psutil_windows",
            "darwin": "psutil._psutil_osx",
        }.get(sys.platform, "psutil._psutil_linux")
        psutil_extension = __import__(extension_name, fromlist=["*"])
        if not cua_driver_processes:
            raise VerificationError("active native policy requires a live owned driver")
        launcher = _native_file_identity(Path(cua_driver_processes[0]["argv"][0]))
        artifacts = {
            "python_runtime": _native_file_identity(_python_runtime_library()),
            "sqlite3_extension": _native_file_identity(Path(_sqlite3.__file__)),
            "psutil_extension": _native_file_identity(Path(psutil_extension.__file__)),
            "cua_driver_launcher": launcher,
            "cua_driver_executable": dict(cua_driver_processes[0]["executable"]),
        }
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError("cannot independently resolve native runtime artifacts") from exc
    if set(artifacts) != set(_NATIVE_RUNTIME_ARTIFACTS):
        raise VerificationError("fixed native runtime artifact policy is incomplete")
    return artifacts


def _verify_cua_driver_processes(
    receipt: dict[str, Any],
    live_rows: list[dict[str, Any]],
) -> None:
    if receipt.get("runtime_phase") != "cua_active":
        raise VerificationError("final verification requires cua_active runtime phase")
    recorded = receipt.get("cua_driver_processes")
    if not isinstance(recorded, list) or not recorded:
        raise VerificationError("receipt lacks live cua-driver process identities")
    recorded_keys = [
        (row.get("pid"), row.get("process_create_time"))
        for row in recorded
        if isinstance(row, dict)
    ]
    if len(recorded_keys) != len(recorded):
        raise VerificationError("invalid cua-driver process identity row")
    if len(set(recorded_keys)) != len(recorded_keys):
        raise VerificationError("duplicate cua-driver process identity row")
    live_keys = [(row["pid"], row["process_create_time"]) for row in live_rows]
    if set(recorded_keys) != set(live_keys) or len(recorded_keys) != len(live_keys):
        raise VerificationError("receipt does not match exact live process set")
    expected_parent = {
        "pid": receipt.get("pid"),
        "process_create_time": receipt.get("process_create_time"),
        "executable": receipt.get("executable"),
    }
    by_key = {
        (row["pid"], row["process_create_time"]): row for row in live_rows
    }
    for row in recorded:
        if row.get("ppid") != receipt.get("pid") or row.get("parent") != expected_parent:
            raise VerificationError("cua-driver gateway ancestry mismatch")
        live = by_key[(row["pid"], row["process_create_time"])]
        if row != live:
            if row.get("executable") != live["executable"]:
                raise VerificationError("cua-driver executable identity mismatch")
            if row.get("argv") != live["argv"]:
                raise VerificationError("cua-driver argv mismatch")
            raise VerificationError("cua-driver process identity mismatch")


def _verify_native_runtime_artifacts(
    receipt: dict[str, Any],
    live_processes: list[dict[str, Any]],
) -> None:
    rows = receipt.get("native_runtime_artifacts")
    if not isinstance(rows, dict) or set(rows) != set(_NATIVE_RUNTIME_ARTIFACTS):
        raise VerificationError("fixed native runtime artifact policy mismatch")
    for name, actual in _live_native_runtime_artifacts(live_processes).items():
        row = rows.get(name)
        if not isinstance(row, dict) or set(row) != set(actual):
            raise VerificationError(f"invalid native runtime artifact row: {name}")
        if row.get("canonical_path") != actual["canonical_path"]:
            raise VerificationError(f"native runtime artifact path mismatch: {name}")
        if row.get("size") != actual["size"]:
            raise VerificationError(f"native runtime artifact size mismatch: {name}")
        if row.get("sha256") != actual["sha256"]:
            raise VerificationError(f"native runtime artifact hash mismatch: {name}")


def _verify_live_runtime_snapshot(receipt: dict[str, Any]) -> None:
    """Revalidate the exact gateway/driver/native snapshot at one boundary."""
    _verify_process(receipt)
    live_cua_driver_processes = _live_cua_driver_processes(
        owner_pid=receipt.get("pid"),
        owner_create_time=receipt.get("process_create_time"),
        owner_executable=receipt.get("executable"),
    )
    _verify_cua_driver_processes(receipt, live_cua_driver_processes)
    _verify_native_runtime_artifacts(receipt, live_cua_driver_processes)


def verify(
    receipt: dict[str, Any],
    review_root: Path,
    deployed_root: Path,
    expected_commit: str | None,
) -> str:
    if receipt.get("schema") != 3: raise VerificationError("schema must be 3")
    if set(receipt.get("modules", {})) != set(MODULES): raise VerificationError("receipt module surface does not match fixed policy")
    if set(receipt.get("callables", {})) != set(CALLABLES): raise VerificationError("receipt callable surface does not match fixed callable policy")
    runtime = receipt.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("python_implementation") != platform.python_implementation() or runtime.get("python_version") != list(sys.version_info[:3]) or runtime.get("python_cache_tag") != sys.implementation.cache_tag or runtime.get("optimization") != sys.flags.optimize:
        raise VerificationError("runtime Python semantics mismatch")
    _verify_live_runtime_snapshot(receipt)
    root = review_root.resolve()
    deployment = deployed_root.resolve()
    if not deployment.is_dir():
        raise VerificationError("independently supplied deployed root is not a directory")
    compiled: dict[str, dict[str, types.CodeType]] = {}
    seen: set[Path] = set()
    for relative in MODULES:
        row = receipt["modules"][relative]
        if not isinstance(row, dict): raise VerificationError(f"invalid module row: {relative}")
        path = _review_path(root, relative)
        if path in seen: raise VerificationError("duplicate or symlink-ambiguous review path")
        seen.add(path)
        raw = path.read_bytes()
        if len(raw) != row.get("size") or hashlib.sha256(raw).hexdigest() != row.get("sha256"):
            raise VerificationError(f"module hash mismatch: {relative}")
        receipt_source = row.get("source_path")
        canonical_deployed = (deployment / relative).resolve()
        if deployment not in canonical_deployed.parents:
            raise VerificationError(f"deployed source path escapes root: {relative}")
        if not isinstance(receipt_source, str) or Path(receipt_source).resolve() != canonical_deployed:
            raise VerificationError(f"receipt deployed source path mismatch: {relative}")
        deployed_raw = canonical_deployed.read_bytes()
        if len(deployed_raw) != row.get("size") or hashlib.sha256(deployed_raw).hexdigest() != row.get("sha256"):
            raise VerificationError(f"deployed module hash mismatch: {relative}")
        code = compile(
            raw,
            str(canonical_deployed),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
        found: dict[str, types.CodeType] = {}
        _codes(code, found)
        compiled[relative] = found
    for identity in CALLABLES:
        row = receipt["callables"][identity]
        module, qualname = identity.split(":", 1)
        if not isinstance(row, dict) or row.get("module") != module or row.get("qualname") != qualname:
            raise VerificationError(f"invalid callable identity: {identity}")
        relative = row.get("source_relative_path")
        if relative not in compiled: raise VerificationError(f"callable source not in policy: {identity}")
        code = compiled[relative].get(qualname)
        if code is None: raise VerificationError(f"reviewed callable missing: {identity}")
        if code.co_firstlineno != row.get("first_line") or _fingerprint(code) != row.get("code_sha256"):
            raise VerificationError(f"callable fingerprint mismatch: {identity}")
    source = receipt.get("source_identity", {})
    kind = source.get("kind") if isinstance(source, dict) else None
    if kind not in {"git-clean", "dirty-attested-source", "unversioned"}: raise VerificationError("invalid source identity kind")
    if kind == "git-clean":
        if not isinstance(expected_commit, str) or not expected_commit:
            raise VerificationError("expected commit is required for git-clean verification")
        repository = source.get("repository", {})
        commit = repository.get("head_commit") if isinstance(repository, dict) else None
        attested_tree = repository.get("head_tree") if isinstance(repository, dict) else None
        try: actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError) as exc: raise VerificationError("clean receipt requires review Git repository") from exc
        if actual != commit or (expected_commit is not None and actual != expected_commit): raise VerificationError("review commit mismatch")
        try:
            actual_tree = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            expected_tree = (
                subprocess.check_output(
                    ["git", "-C", str(root), "rev-parse", f"{expected_commit}^{{tree}}"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if expected_commit is not None
                else actual_tree
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise VerificationError("cannot independently resolve review Git tree") from exc
        if not isinstance(attested_tree, str) or attested_tree != actual_tree or actual_tree != expected_tree:
            raise VerificationError("review tree mismatch")
        try:
            deployed_commit = subprocess.check_output(
                ["git", "-C", str(deployment), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            deployed_tree = subprocess.check_output(
                ["git", "-C", str(deployment), "rev-parse", "HEAD^{tree}"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise VerificationError("cannot independently resolve deployed Git tree") from exc
        if deployed_commit != actual or deployed_tree != actual_tree:
            raise VerificationError("deployed Git tree mismatch")
        for relative in MODULES:
            path_row = source.get("attested_paths", {}).get(relative, {})
            if not path_row.get("matches_head"):
                raise VerificationError(f"clean receipt path not matched to HEAD: {relative}")
            try:
                head_blob = subprocess.check_output(
                    ["git", "-C", str(root), "rev-parse", f"HEAD:{relative}"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                subprocess.run(
                    ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", relative],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise VerificationError(f"reviewed path does not match clean HEAD: {relative}") from exc
            if path_row.get("head_blob") != head_blob:
                raise VerificationError(f"receipt HEAD blob mismatch: {relative}")
        _verify_live_runtime_snapshot(receipt)
        return f"verified commit identity: {actual}; verified tree identity: {actual_tree}"
    if expected_commit is not None: raise VerificationError("expected commit cannot verify dirty or unversioned receipt")
    _verify_live_runtime_snapshot(receipt)
    return "verified reviewed content identity (dirty/unversioned source; no commit identity claimed)"


def _durable_replace(temporary: Path, destination: Path) -> None:
    """Replace an evidence receipt only after a platform durability barrier."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH. Concurrent
        # verifier processes may briefly hold the destination metadata open;
        # bounded retries preserve durability without sharing a temp file.
        import time as _time

        for attempt in range(50):
            if move_file(str(temporary), str(destination), 0x1 | 0x8):
                return
            error = ctypes.get_last_error()
            if error not in {5, 32} or attempt == 49:
                raise OSError(error, "MoveFileExW failed")
            _time.sleep(min(0.002 * (attempt + 1), 0.05))
        raise RuntimeError("unreachable durable replacement retry state")
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_verification_receipt(
    path: Path,
    args: argparse.Namespace,
    exit_code: int,
    stdout: bytes,
    stderr: bytes,
) -> None:
    """Atomically persist the exact verifier invocation and byte result."""
    def stream(raw: bytes) -> dict[str, Any]:
        return {
            "base64": base64.b64encode(raw).decode("ascii"),
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    payload = {
        "schema": 2,
        "invocation": {
            "interpreter": str(Path(sys.executable).resolve()),
            "verifier_path": str(Path(__file__).resolve()),
            "cwd": str(Path.cwd().resolve()),
            "argv": [
                str(Path(sys.executable).resolve()),
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            "receipt_path": str(args.receipt),
            "review_root": str(args.review_root),
            "deployed_root": str(args.deployed_root),
            "expected_commit": args.expected_commit,
        },
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "result": {"exit_code": exit_code, "stdout": stream(stdout), "stderr": stream(stderr)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".cua-verify-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(payload, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--deployed-root", type=Path, required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--verification-receipt", type=Path)
    args = parser.parse_args()
    stdout = b""
    stderr = b""
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        stdout = (verify(receipt, args.review_root, args.deployed_root, args.expected_commit) + "\n").encode("utf-8")
        exit_code = 0
    except (OSError, ValueError, KeyError, VerificationError) as exc:
        stderr = f"CUA attestation verification failed: {exc}\n".encode("utf-8")
        exit_code = 1
    if stdout:
        sys.stdout.buffer.write(stdout)
    if stderr:
        sys.stderr.buffer.write(stderr)
    if args.verification_receipt is not None:
        _write_verification_receipt(args.verification_receipt, args, exit_code, stdout, stderr)
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
