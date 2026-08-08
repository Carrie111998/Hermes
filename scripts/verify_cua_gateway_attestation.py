#!/usr/bin/env python
"""Independently verify schema-v3 CUA gateway runtime attestation receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import types
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
    "tools.computer_use.cua_backend:_AsyncBridge.run", "tools.computer_use.cua_backend:_CuaDriverSession._lifecycle_coro", "tools.computer_use.cua_backend:_CuaDriverSession.call_tool", "tools.computer_use.cua_backend:_CuaDriverSession._call_tool_via_cli", "tools.computer_use.cua_backend:_CuaDriverSession._restart_session_locked", "tools.computer_use.cua_backend:_EmbeddedCuaDaemon.start", "tools.computer_use.cua_backend:_EmbeddedCuaDaemon.stop", "tools.computer_use.cua_backend:CuaDriverBackend.start", "tools.computer_use.cua_backend:CuaDriverBackend.stop", "tools.computer_use.cua_backend:CuaDriverBackend.capture", "tools.computer_use.cua_backend:CuaDriverBackend._apply_delivery", "tools.computer_use.cua_backend:CuaDriverBackend._run_input_action", "tools.computer_use.cua_backend:CuaDriverBackend._action", "tools.computer_use.cua_backend:CuaDriverBackend._maybe_attach_element_token", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_state", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_prepare", "tools.computer_use.cua_backend:CuaDriverBackend.typed_browser_action",
    "gateway.run:GatewayRunner._run_agent", "gateway.session:SessionStore.route_matches", "gateway.session:SessionStore._run_route_transition", "gateway.session:SessionStore.prune_old_entries", "tools.approval:get_current_session_key", "tools.approval:is_approval_bypass_active_for_session",
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
    _verify_process(receipt)
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
        repository = source.get("repository", {})
        commit = repository.get("head_commit") if isinstance(repository, dict) else None
        try: actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError) as exc: raise VerificationError("clean receipt requires review Git repository") from exc
        if actual != commit or (expected_commit is not None and actual != expected_commit): raise VerificationError("review commit mismatch")
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
        return f"verified commit identity: {actual}"
    if expected_commit is not None: raise VerificationError("expected commit cannot verify dirty or unversioned receipt")
    return "verified reviewed content identity (dirty/unversioned source; no commit identity claimed)"


def _write_verification_receipt(
    path: Path,
    args: argparse.Namespace,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> None:
    """Persist the exact verifier invocation and its complete result."""
    payload = {
        "schema": 1,
        "command": list(sys.argv),
        "receipt_path": str(args.receipt),
        "review_root": str(args.review_root),
        "deployed_root": str(args.deployed_root),
        "expected_commit": args.expected_commit,
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--deployed-root", type=Path, required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--verification-receipt", type=Path)
    args = parser.parse_args()
    stdout = ""
    stderr = ""
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        stdout = verify(receipt, args.review_root, args.deployed_root, args.expected_commit)
        exit_code = 0
    except (OSError, ValueError, KeyError, VerificationError) as exc:
        stderr = f"CUA attestation verification failed: {exc}"
        exit_code = 1
    if stdout:
        print(stdout)
        stdout += "\n"
    if stderr:
        print(stderr, file=sys.stderr)
        stderr += "\n"
    if args.verification_receipt is not None:
        _write_verification_receipt(args.verification_receipt, args, exit_code, stdout, stderr)
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
