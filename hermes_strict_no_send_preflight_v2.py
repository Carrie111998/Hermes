"""Strict, installed-source no-send preflight for one GLM representative load.

This entrypoint deliberately avoids Hermes' normal startup path.  It consumes
one canonical H4 envelope, checks only the installed provider/model/tool
resolution surfaces and a read-only implementation graph, and emits a
canonical non-authorizing receipt.  It never reads config, dotenv files, or
credential values, and it never creates a job or calls a provider, model,
network, tool, or external-send surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


CONTRACT_VERSION = "hermes.strict_no_send_preflight.v2"
INPUT_VERSION = "hermes.strict_no_send_preflight.input.v2"
REQUEST_CAPSULE_VERSION = "hermes.strict_no_send.request_capsule.v2"
RECEIPT_VERSION = "hermes.strict_no_send_preflight.receipt.v2"

_H4_STATUS = "canonical_containment_inputs_verified_contract_only"
_MAX_ENVELOPE_BYTES = 48 * 1024
_MAX_NESTED_DOCUMENT_BYTES = 16 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_GRAPH_BYTES = 32 * 1024 * 1024
_MAX_PROVIDER_PLUGIN_COUNT = 64
_MAX_DEPTH = 4
_MAX_NODES = 64
_MAX_STRING_UTF8_BYTES = 24 * 1024
_EXPECTED_PROVIDER_ID = "zai"
_EXPECTED_MODEL_ID = "glm-5.2"
_EXPECTED_API_MODE = "chat_completions"
_EXPECTED_PROFILE_BASE_URL = "https://api.z.ai/api/paas/v4"
_EXPECTED_PROVIDER_INTERNAL_REVISION = "unknown"
_EXPECTED_PROVIDER_INTERNAL_REVISION_OWNER_ACCEPTED = True
_EXPECTED_IMMUTABLE_REVISION_CLAIMED = False
_MAX_INPUT_TOKENS = 32_768
_MAX_OUTPUT_TOKENS = 8_192
_MAX_TOTAL_TOKENS = 40_960
_MAX_WALL_CLOCK_SECONDS = 900
_MAX_OUTPUT_BYTES = 524_288
_MAX_COST_USD_MICRODOLLARS = 250_000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

_ENVELOPE_KEYS = frozenset(
    {
        "candidate_document_b64",
        "contract_version",
        "environment_document_b64",
        "expected_implementation_graph_sha256",
    }
)
_ALLOWED_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "HERMES_HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
        "__CF_USER_TEXT_ENCODING",
    }
)
_FORBIDDEN_IMPORTED_MODULES = frozenset(
    {
        "cli",
        "model_tools",
        "run_agent",
        "hermes_cli.config",
        "hermes_cli.env_loader",
        "hermes_cli.main",
        "dotenv",
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        "auth.json",
        "config.yaml",
        "credentials.json",
        "secrets.json",
    }
)
_ALWAYS_BLOCKING_CODES = (
    "credential_handoff_required",
    "credential_scope_effective_verification_required",
    "effective_provider_endpoint_verification_required",
    "external_dependency_graph_verification_required",
    "host_containment_proof_required",
    "interpreter_bootstrap_filesystem_side_effect_verification_required",
    "owner_approval_required",
    "runtime_token_enforcement_required",
    "strict_worker_runner_required",
    "trusted_implementation_graph_anchor_required",
)

# Resolver files are intentionally named, while provider plugin files are
# enumerated below.  The v2 entrypoints are part of the graph so a wheel cannot
# silently pair a preflight with a different H4/H6 contract.
_STATIC_GRAPH_PATHS = (
    "agent/models_dev.py",
    "agent/portal_tags.py",
    "hermes_cli/__init__.py",
    "hermes_cli/codex_models.py",
    "hermes_cli/model_normalize.py",
    "hermes_cli/models.py",
    "hermes_constants.py",
    "hermes_strict_no_send_preflight_v2.py",
    "hermes_strict_runtime_guard_v2.py",
    "hermes_worker_containment_canonical_bytes_v2.py",
    "plugins/__init__.py",
    "providers/__init__.py",
    "providers/base.py",
    "toolsets.py",
    "utils.py",
)
_AUDIT_HOOK_INSTALLED = False


class _PreflightFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code


def _reject_float(_value: str) -> int:
    raise _PreflightFailure("input_float_forbidden")


def _reject_nonfinite(_value: str) -> int:
    raise _PreflightFailure("input_nonfinite_forbidden")


def _reject_integer(_value: str) -> int:
    raise _PreflightFailure("input_integer_forbidden")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _PreflightFailure("input_duplicate_or_invalid_key")
        result[key] = value
    return result


def _preflight_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for value in raw:
        if in_string:
            if escaped:
                escaped = False
            elif value == 0x5C:
                escaped = True
            elif value == 0x22:
                in_string = False
            continue
        if value == 0x22:
            in_string = True
        elif value in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_DEPTH:
                raise _PreflightFailure("input_depth_exceeded")
        elif value in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                raise _PreflightFailure("input_json_invalid")
    if in_string or depth != 0:
        raise _PreflightFailure("input_json_invalid")


def _validate_tree(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES:
            raise _PreflightFailure("input_node_limit_exceeded")
        if depth > _MAX_DEPTH:
            raise _PreflightFailure("input_depth_exceeded")
        if type(value) is dict:
            nodes += len(value)
            if nodes > _MAX_NODES:
                raise _PreflightFailure("input_node_limit_exceeded")
            for key, child in value.items():
                if type(key) is not str or any(
                    0xD800 <= ord(character) <= 0xDFFF for character in key
                ):
                    raise _PreflightFailure("input_key_invalid")
                if len(key.encode("utf-8")) > _MAX_STRING_UTF8_BYTES:
                    raise _PreflightFailure("input_string_limit_exceeded")
                stack.append((child, depth + 1))
        elif type(value) is list:
            for child in value:
                stack.append((child, depth + 1))
        elif type(value) is str:
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise _PreflightFailure("input_surrogate_forbidden")
            if len(value.encode("utf-8")) > _MAX_STRING_UTF8_BYTES:
                raise _PreflightFailure("input_string_limit_exceeded")
        elif type(value) not in (bool,) and value is not None:
            raise _PreflightFailure("input_value_type_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _parse_input(raw: object) -> dict[str, str]:
    if type(raw) is not bytes:
        raise _PreflightFailure("input_type_invalid")
    if len(raw) > _MAX_ENVELOPE_BYTES:
        raise _PreflightFailure("input_too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _PreflightFailure("input_bom_forbidden")
    try:
        _preflight_depth(raw)
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_int=_reject_integer,
            parse_constant=_reject_nonfinite,
        )
        _validate_tree(value)
        canonical = _canonical_json_bytes(value)
    except _PreflightFailure:
        raise
    except UnicodeDecodeError:
        raise _PreflightFailure("input_utf8_invalid") from None
    except json.JSONDecodeError:
        raise _PreflightFailure("input_json_invalid") from None
    except (MemoryError, RecursionError):
        raise _PreflightFailure("input_resource_limit_exceeded") from None
    except (OverflowError, UnicodeEncodeError, ValueError):
        raise _PreflightFailure("input_value_invalid") from None
    if raw != canonical:
        raise _PreflightFailure("input_noncanonical")
    if type(value) is not dict or frozenset(value) != _ENVELOPE_KEYS:
        raise _PreflightFailure("input_shape_invalid")
    if value["contract_version"] != INPUT_VERSION:
        raise _PreflightFailure("input_contract_version_invalid")
    for key in _ENVELOPE_KEYS:
        if type(value[key]) is not str:
            raise _PreflightFailure("input_field_type_invalid")
    if _DIGEST.fullmatch(value["expected_implementation_graph_sha256"]) is None:
        raise _PreflightFailure("expected_implementation_graph_sha256_invalid")
    return value


def _decode_document(value: str, label: str) -> bytes:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        raise _PreflightFailure(f"{label}_base64_invalid") from None
    if base64.b64encode(raw).decode("ascii") != value:
        raise _PreflightFailure(f"{label}_base64_noncanonical")
    if len(raw) > _MAX_NESTED_DOCUMENT_BYTES:
        raise _PreflightFailure(f"{label}_too_large")
    return raw


def _walk_without_symlinks(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError:
            raise _PreflightFailure("filesystem_boundary_invalid") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _PreflightFailure("filesystem_symlink_forbidden")


def _safe_read_source(root: Path, relative_name: str) -> bytes:
    relative = PurePosixPath(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise _PreflightFailure("implementation_graph_path_invalid")
    target = root.joinpath(*relative.parts)
    _walk_without_symlinks(target)
    try:
        before = os.lstat(target)
    except OSError:
        raise _PreflightFailure("implementation_graph_read_failed") from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _PreflightFailure("implementation_graph_file_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_nlink,
                opened.st_size,
            ) != (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
                before.st_size,
            ):
                raise _PreflightFailure("implementation_graph_identity_changed")
            if opened.st_size < 1 or opened.st_size > _MAX_SOURCE_BYTES:
                raise _PreflightFailure("implementation_graph_size_invalid")
            chunks: list[bytes] = []
            remaining = opened.st_size + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = os.lstat(target)
    except _PreflightFailure:
        raise
    except OSError:
        raise _PreflightFailure("implementation_graph_read_failed") from None
    if len(raw) != before.st_size:
        raise _PreflightFailure("implementation_graph_short_read")
    if (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    ) != (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise _PreflightFailure("implementation_graph_identity_changed")
    return raw


def _implementation_graph_paths(root: Path | None = None) -> tuple[str, ...]:
    root = root or Path(os.path.abspath(os.path.dirname(__file__)))
    plugin_root = root / "plugins" / "model-providers"
    _walk_without_symlinks(plugin_root)
    try:
        collected_names: list[str] = []
        with os.scandir(plugin_root) as entries:
            for entry in entries:
                if (
                    not entry.is_dir(follow_symlinks=False)
                    or entry.name.startswith((".", "_"))
                ):
                    continue
                if len(collected_names) >= _MAX_PROVIDER_PLUGIN_COUNT:
                    raise _PreflightFailure("implementation_graph_plugin_scan_failed")
                collected_names.append(entry.name)
    except OSError:
        raise _PreflightFailure("implementation_graph_plugin_scan_failed") from None
    if not collected_names:
        raise _PreflightFailure("implementation_graph_plugin_scan_failed")
    plugin_names = tuple(sorted(collected_names))
    for name in plugin_names:
        try:
            encoded = name.encode("ascii")
        except UnicodeEncodeError:
            raise _PreflightFailure("implementation_graph_plugin_name_invalid") from None
        if (
            len(encoded) > 80
            or re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None
        ):
            raise _PreflightFailure("implementation_graph_plugin_name_invalid")
    plugin_paths = tuple(
        f"plugins/model-providers/{name}/__init__.py" for name in plugin_names
    )
    return tuple(sorted((*_STATIC_GRAPH_PATHS, *plugin_paths)))


def _canonical_source_bytes(relative_name: str, raw: bytes) -> bytes:
    if not relative_name.endswith(".py"):
        raise _PreflightFailure("implementation_graph_source_type_invalid")
    canonical = raw.replace(b"\r\n", b"\n")
    if b"\r" in canonical:
        raise _PreflightFailure("implementation_graph_source_line_endings_invalid")
    return canonical


def _implementation_graph_sha256() -> tuple[str, int]:
    root = Path(os.path.abspath(os.path.dirname(__file__)))
    _walk_without_symlinks(root)
    digest = hashlib.sha256()
    paths = _implementation_graph_paths(root)
    total_bytes = 0
    for relative_name in paths:
        raw = _canonical_source_bytes(
            relative_name, _safe_read_source(root, relative_name)
        )
        total_bytes += len(raw)
        if total_bytes > _MAX_GRAPH_BYTES:
            raise _PreflightFailure("implementation_graph_total_size_invalid")
        encoded_name = relative_name.encode("ascii")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return "sha256:" + digest.hexdigest(), len(paths)


def _validate_process_environment(logical_environment: dict[str, str]) -> None:
    normalized: dict[str, str] = {}
    for key in os.environ:
        if type(key) is not str:
            raise _PreflightFailure("process_environment_key_invalid")
        folded = key.upper()
        if folded in normalized and normalized[folded] != key:
            raise _PreflightFailure("process_environment_case_collision")
        normalized[folded] = key
    unknown = frozenset(normalized).difference(_ALLOWED_PROCESS_ENVIRONMENT_KEYS)
    if unknown:
        # Deliberately report only the class, never the unknown key/value.  In
        # particular a credential variable is rejected without reading it.
        raise _PreflightFailure("process_environment_key_forbidden")
    for required in (
        "HOME",
        "HERMES_HOME",
        "LANG",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "TZ",
    ):
        if required not in normalized:
            raise _PreflightFailure("process_environment_key_missing")
    for key, expected in (
        ("LANG", "C.UTF-8"),
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PYTHONHASHSEED", "0"),
        ("TZ", "UTC"),
    ):
        if os.environ[normalized[key]] != expected:
            raise _PreflightFailure("process_environment_value_invalid")
    for key in ("HOME", "HERMES_HOME", "PATH"):
        if not os.environ[normalized[key]]:
            raise _PreflightFailure("process_environment_value_invalid")

    for key, expected_value in logical_environment.items():
        actual_name = normalized.get(key.upper())
        if actual_name is None or os.environ.get(actual_name) != expected_value:
            raise _PreflightFailure("logical_environment_mismatch")

    home = os.environ[normalized["HOME"]]
    hermes_home = os.environ[normalized["HERMES_HOME"]]
    expected_hermes_home = os.path.join(home, ".hermes")
    if os.path.normcase(os.path.abspath(hermes_home)) != os.path.normcase(
        os.path.abspath(expected_hermes_home)
    ):
        raise _PreflightFailure("hermes_home_boundary_invalid")
    hermes_home_path = Path(hermes_home)
    _walk_without_symlinks(hermes_home_path)
    try:
        if not stat.S_ISDIR(os.lstat(hermes_home_path).st_mode):
            raise _PreflightFailure("hermes_home_boundary_invalid")
        with os.scandir(hermes_home_path) as entries:
            if next(entries, None) is not None:
                raise _PreflightFailure("hermes_home_not_empty")
    except _PreflightFailure:
        raise
    except OSError:
        raise _PreflightFailure("hermes_home_boundary_invalid") from None


def _install_no_send_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return

    def _audit(event: str, arguments: tuple[object, ...]) -> None:
        if event.startswith("socket.") or event.startswith("subprocess."):
            raise _PreflightFailure("forbidden_runtime_event")
        if event in {
            "os.exec",
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
            "os.posix_spawnp",
            "os.spawn",
            "os.startfile",
            "os.system",
        }:
            raise _PreflightFailure("forbidden_runtime_event")
        if event in {
            "os.chdir",
            "os.chmod",
            "os.chown",
            "os.link",
            "os.mkdir",
            "os.putenv",
            "os.remove",
            "os.removexattr",
            "os.rename",
            "os.rmdir",
            "os.setxattr",
            "os.symlink",
            "os.truncate",
            "os.unsetenv",
            "os.utime",
        }:
            raise _PreflightFailure("filesystem_mutation_forbidden")
        if event == "open" and arguments:
            candidate = arguments[0]
            mode = arguments[1] if len(arguments) > 1 else None
            flags = arguments[2] if len(arguments) > 2 else 0
            write_flags = (
                os.O_APPEND
                | os.O_CREAT
                | os.O_RDWR
                | os.O_TRUNC
                | os.O_WRONLY
            )
            if (
                type(mode) is str
                and any(marker in mode for marker in ("+", "a", "w", "x"))
            ) or (type(flags) is int and flags & write_flags):
                raise _PreflightFailure("filesystem_mutation_forbidden")
            if type(candidate) is str:
                name = os.path.basename(candidate).lower()
                if name in _SENSITIVE_FILENAMES:
                    raise _PreflightFailure("sensitive_file_read_forbidden")
            elif type(candidate) is bytes:
                name = os.path.basename(candidate).lower()
                if name in {item.encode("ascii") for item in _SENSITIVE_FILENAMES}:
                    raise _PreflightFailure("sensitive_file_read_forbidden")

    sys.addaudithook(_audit)
    _AUDIT_HOOK_INSTALLED = True


def _resolve_hermes_surfaces(
    candidate: dict[str, object],
) -> tuple[object, str, list[str]]:
    # These imports are the bounded installed provider/tool graph.  They do
    # not load normal Hermes runtime/config/dotenv/credential paths.  The full
    # model normalizer is deliberately not executed here: it imports the
    # ordinary model/config surface, which is outside this no-send preflight.
    from providers import _REGISTRY, _import_plugin_dir
    from toolsets import resolve_multiple_toolsets

    provider_id = candidate["provider_id"]
    model_id = candidate["model_id"]
    if provider_id != _EXPECTED_PROVIDER_ID or model_id != _EXPECTED_MODEL_ID:
        raise _PreflightFailure("requested_model_identity_out_of_scope")

    # ``providers.get_provider_profile`` performs the full provider discovery
    # path.  That path also probes the general plugin manager, which imports
    # ``hermes_cli.config`` even when no plugin is enabled.  H5 is expressly
    # not a normal runtime/config bootstrap, so load only the installed,
    # bundled provider profile needed by this exact capsule.  The profile
    # still comes from the current Hermes source tree/wheel and registers in
    # the same provider registry; user plugins and entry points are outside
    # this no-send preflight boundary.
    module_root = Path(os.path.abspath(os.path.dirname(__file__)))
    provider_dir = module_root / "plugins" / "model-providers" / provider_id
    _walk_without_symlinks(provider_dir)
    try:
        provider_metadata = os.lstat(provider_dir)
    except OSError:
        raise _PreflightFailure("provider_profile_resolution_failed") from None
    if not stat.S_ISDIR(provider_metadata.st_mode):
        raise _PreflightFailure("provider_profile_resolution_failed")
    _import_plugin_dir(provider_dir, "bundled")
    profile = _REGISTRY.get(provider_id)
    if profile is None or profile.name != provider_id:
        raise _PreflightFailure("provider_profile_resolution_failed")
    if tuple(profile.env_vars) != ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"):
        raise _PreflightFailure("provider_credential_surface_changed")
    if profile.api_mode != _EXPECTED_API_MODE:
        raise _PreflightFailure("provider_api_mode_changed")
    if profile.base_url != _EXPECTED_PROFILE_BASE_URL:
        raise _PreflightFailure("provider_profile_base_url_changed")
    if model_id not in tuple(profile.fallback_models):
        raise _PreflightFailure("provider_model_catalog_mismatch")
    # The owner-approved identity is the provider-native, unqualified model
    # id.  H4 has already required the exact built-in string values; repeat
    # the delimiter check here so this receipt cannot be read as proof that a
    # provider-qualified alias was resolved by ordinary Hermes startup.
    if "/" in model_id or model_id != _EXPECTED_MODEL_ID:
        raise _PreflightFailure("requested_model_identity_out_of_scope")
    resolved_model_id = model_id
    resolved_tools = resolve_multiple_toolsets([])
    if resolved_tools:
        raise _PreflightFailure("empty_tool_resolution_drift")
    if _FORBIDDEN_IMPORTED_MODULES.intersection(sys.modules):
        raise _PreflightFailure("ordinary_runtime_imported")
    return profile, resolved_model_id, resolved_tools


def _request_capsule(
    candidate: dict[str, object],
    profile: object,
    normalized_model: str,
    resolved_tools: list[str],
) -> dict[str, object]:
    return {
        "api_mode": _EXPECTED_API_MODE,
        "attempt_limit": candidate["attempts"],
        "contract_version": REQUEST_CAPSULE_VERSION,
        "credential_handoff": candidate["credential_mode"],
        "fallback_model_ids": [],
        "fallback_provider_ids": [],
        "fanout": candidate["fanout"],
        "immutable_revision_claimed": candidate["immutable_revision_claimed"],
        "job_limit": candidate["jobs"],
        "max_cost_usd_microdollars": candidate["max_cost_usd_microdollars"],
        "max_input_tokens": candidate["max_input_tokens"],
        "max_output_bytes": candidate["max_output_bytes"],
        "max_output_tokens": candidate["max_output_tokens"],
        "max_total_tokens": candidate["max_total_tokens"],
        "model_call_limit": candidate["model_call_limit"],
        "model_id": normalized_model,
        "provider_id": candidate["provider_id"],
        "provider_internal_revision": candidate["provider_internal_revision"],
        "provider_internal_revision_owner_accepted": candidate[
            "provider_internal_revision_owner_accepted"
        ],
        "provider_profile_api_mode": profile.api_mode,
        "provider_profile_declared_base_url": profile.base_url,
        "provider_request_limit": candidate["provider_request_limit"],
        "repository_mount": candidate["repository_mount"],
        "retry_count": candidate["retry_count"],
        "tool_names": resolved_tools,
        "wall_clock_seconds": candidate["wall_clock_seconds"],
    }


def _receipt(
    *,
    failures: list[str],
    candidate_raw: bytes | None,
    environment_raw: bytes | None,
    graph_sha256: str | None,
    graph_file_count: int,
    h4_receipt: dict[str, object] | None,
    profile: object | None,
    capsule: dict[str, object] | None,
    ordinary_runtime_imported: bool,
) -> bytes:
    valid = not failures
    capsule_raw = _canonical_json_bytes(capsule) if capsule is not None else None
    credential_names = sorted(profile.env_vars) if profile is not None else []
    result = {
        "activation_state": "hold_no_send",
        "blocking_codes": sorted(set((*_ALWAYS_BLOCKING_CODES, *failures))),
        "candidate_document_sha256": (
            hashlib.sha256(candidate_raw).hexdigest() if candidate_raw is not None else None
        ),
        "clean_environment_document_sha256": (
            hashlib.sha256(environment_raw).hexdigest()
            if environment_raw is not None
            else None
        ),
        "contract_version": RECEIPT_VERSION,
        "credential_environment_names": credential_names,
        "credential_environment_boundary_preflight_verified": valid,
        "credential_scope_effective_verified": False,
        "execution_authorized": False,
        "external_send": False,
        "host_containment_verified": False,
        "implementation_graph_file_count": graph_file_count,
        "implementation_graph_digest_semantics": "local_python_source_canonical_lf_v2",
        "implementation_graph_sha256": graph_sha256,
        "external_dependency_graph_verified": False,
        "filesystem_mutation_effective_verified": False,
        "local_implementation_graph_expected_match": valid,
        "local_implementation_graph_trusted_anchor_verified": False,
        "job_count": 0,
        "model_call_count": 0,
        "model_identity_preflight_verified": valid,
        "model_revision_immutable_verified": False,
        "provider_internal_revision": (
            capsule["provider_internal_revision"] if capsule is not None else None
        ),
        "provider_internal_revision_owner_accepted": (
            capsule["provider_internal_revision_owner_accepted"]
            if capsule is not None
            else False
        ),
        "immutable_revision_claimed": (
            capsule["immutable_revision_claimed"] if capsule is not None else False
        ),
        "network_access": False,
        "no_send_audit_hook_installed": _AUDIT_HOOK_INSTALLED,
        "ordinary_runtime_imported": ordinary_runtime_imported,
        "owner_approval_verified": False,
        "pilot_ready": False,
        "provider_endpoint_effective_verified": False,
        "provider_profile_preflight_verified": valid,
        "provider_request_count": 0,
        "request_capsule": capsule,
        "request_capsule_sha256": (
            hashlib.sha256(capsule_raw).hexdigest() if capsule_raw is not None else None
        ),
        "safe_to_dispatch": False,
        "status": (
            "hermes_strict_no_send_preflight_verified_contract_only"
            if valid
            else "hold_missing_or_invalid"
        ),
        "token_limits_effective_verified": False,
        "token_limits_preflight_bound": valid,
        "tool_allowlist_preflight_verified": valid,
        "tool_call_count": 0,
        "actual_cost_usd_microdollars": 0,
        "actual_output_bytes": 0,
        "worker_runtime_verified": False,
        "h4_candidate_input_verified": bool(
            h4_receipt and h4_receipt.get("candidate_input_verified") is True
        ),
        "h4_environment_input_verified": bool(
            h4_receipt and h4_receipt.get("clean_environment_input_verified") is True
        ),
    }
    return _canonical_json_bytes(result)


def assess_strict_no_send_preflight_v2(raw_input: object) -> bytes:
    """Run the bounded v2 preflight and return canonical receipt bytes."""

    _install_no_send_audit_hook()
    failures: list[str] = []
    candidate_raw: bytes | None = None
    environment_raw: bytes | None = None
    graph_sha256: str | None = None
    graph_file_count = 0
    h4_receipt: dict[str, object] | None = None
    profile: object | None = None
    capsule: dict[str, object] | None = None
    try:
        envelope = _parse_input(raw_input)
        candidate_raw = _decode_document(
            envelope["candidate_document_b64"], "candidate_document"
        )
        environment_raw = _decode_document(
            envelope["environment_document_b64"], "environment_document"
        )
        _validate_process_environment({})
        graph_sha256, graph_file_count = _implementation_graph_sha256()
        if graph_sha256 != envelope["expected_implementation_graph_sha256"]:
            raise _PreflightFailure("implementation_graph_mismatch")

        from hermes_worker_containment_canonical_bytes_v2 import (
            assess_worker_containment_canonical_bytes_v2,
        )

        h4_raw = assess_worker_containment_canonical_bytes_v2(
            candidate_raw, environment_raw
        )
        h4_receipt = json.loads(h4_raw)
        if h4_receipt.get("status") != _H4_STATUS:
            raise _PreflightFailure("h4_candidate_or_environment_invalid")
        candidate = json.loads(candidate_raw)
        environment_document = json.loads(environment_raw)
        logical_environment = environment_document.get("environment")
        if type(candidate) is not dict or type(logical_environment) is not dict:
            raise _PreflightFailure("h4_document_shape_invalid")
        _validate_process_environment(logical_environment)
        profile, normalized_model, resolved_tools = _resolve_hermes_surfaces(candidate)
        capsule = _request_capsule(
            candidate, profile, normalized_model, resolved_tools
        )
    except _PreflightFailure as failure:
        failures.append(failure.code)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        failures.append("hermes_preflight_resolution_failed")
    except (MemoryError, RecursionError):
        failures.append("preflight_resource_limit_exceeded")
    return _receipt(
        failures=failures,
        candidate_raw=candidate_raw,
        environment_raw=environment_raw,
        graph_sha256=graph_sha256,
        graph_file_count=graph_file_count,
        h4_receipt=h4_receipt,
        profile=profile,
        capsule=capsule,
        ordinary_runtime_imported=bool(
            _FORBIDDEN_IMPORTED_MODULES.intersection(sys.modules)
        ),
    )


def main() -> int:
    if sys.argv[1:]:
        sys.stderr.buffer.write(
            b"usage: hermes-strict-no-send-preflight-v2 < canonical-envelope.json\n"
        )
        return 64
    raw = sys.stdin.buffer.read(_MAX_ENVELOPE_BYTES + 1)
    receipt = assess_strict_no_send_preflight_v2(raw)
    sys.stdout.buffer.write(receipt + b"\n")
    try:
        status = json.loads(receipt).get("status")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 64
    return 0 if status == "hermes_strict_no_send_preflight_verified_contract_only" else 64


__all__ = [
    "CONTRACT_VERSION",
    "INPUT_VERSION",
    "RECEIPT_VERSION",
    "REQUEST_CAPSULE_VERSION",
    "_implementation_graph_sha256",
    "assess_strict_no_send_preflight_v2",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
