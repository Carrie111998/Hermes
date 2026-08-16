"""Canonical-bytes input boundary for the representative-load v2 lane.

The boundary is intentionally inert.  It accepts only two exact builtin
``bytes`` JSON documents, validates the fixed representative-load contract,
and returns a canonical receipt that never authorizes a worker, provider,
credential, tool, network, or external-send operation.
"""

from __future__ import annotations

import hashlib
import json
import re


CONTRACT_VERSION = "hermes.worker_containment.canonical_bytes.v2"
ENVIRONMENT_CONTRACT_VERSION = "hermes.clean_environment.canonical_bytes.v2"
RECEIPT_VERSION = "hermes.worker_containment.canonical_bytes.receipt.v2"

EXPECTED_PROVIDER_ID = "zai"
EXPECTED_MODEL_ID = "glm-5.2"
EXPECTED_PROVIDER_INTERNAL_REVISION = "unknown"
EXPECTED_PROVIDER_INTERNAL_REVISION_OWNER_ACCEPTED = True
EXPECTED_IMMUTABLE_REVISION_CLAIMED = False

MAX_INPUT_TOKENS = 32_768
MAX_OUTPUT_TOKENS = 8_192
MAX_TOTAL_TOKENS = 40_960
MAX_WALL_CLOCK_SECONDS = 900
MAX_OUTPUT_BYTES = 524_288
MAX_COST_USD_MICRODOLLARS = 250_000

_MAX_CANDIDATE_BYTES = 8 * 1024
_MAX_ENVIRONMENT_BYTES = 16 * 1024
_MAX_DEPTH = 4
_MAX_NODES = 96
_MAX_STRING_UTF8_BYTES = 4096
_MAX_INTEGER_DIGITS = 7

_CANDIDATE_KEYS = frozenset(
    {
        "attempts",
        "contract_version",
        "credential_mode",
        "fanout",
        "immutable_revision_claimed",
        "jobs",
        "max_cost_usd_microdollars",
        "max_input_tokens",
        "max_output_bytes",
        "max_output_tokens",
        "max_total_tokens",
        "model_call_limit",
        "model_id",
        "provider_id",
        "provider_internal_revision",
        "provider_internal_revision_owner_accepted",
        "provider_request_limit",
        "repository_mount",
        "retry_count",
        "tool_allowlist",
        "wall_clock_seconds",
    }
)
_ENVIRONMENT_DOCUMENT_KEYS = frozenset({"contract_version", "environment"})
_ALLOWED_ENVIRONMENT_KEYS = frozenset({"HOME", "LANG", "LC_ALL", "TMPDIR", "TZ"})
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_MODEL_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_ALIAS_COMPONENTS = frozenset({"auto", "current", "default", "latest", "stable"})
_ALWAYS_BLOCKING_CODES = (
    "hermes_runtime_binding_required",
    "host_containment_proof_required",
    "owner_approval_required",
)


class _DocumentFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code


def _reject_float(_value: str) -> int:
    raise _DocumentFailure("float_forbidden")


def _reject_nonfinite(_value: str) -> int:
    raise _DocumentFailure("nonfinite_forbidden")


def _strict_integer(value: str) -> int:
    if type(value) is not str:
        raise _DocumentFailure("integer_invalid")
    if value == "-0":
        raise _DocumentFailure("negative_zero_forbidden")
    digits = value[1:] if value.startswith("-") else value
    if not digits or len(digits) > _MAX_INTEGER_DIGITS or not digits.isascii():
        raise _DocumentFailure("integer_digits_exceeded")
    if not digits.isdigit():
        raise _DocumentFailure("integer_invalid")
    return int(value, 10)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str:
            raise _DocumentFailure("object_key_invalid")
        if key in result:
            raise _DocumentFailure("duplicate_key")
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
                raise _DocumentFailure("depth_exceeded")
        elif value in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                raise _DocumentFailure("json_invalid")
    if in_string or depth != 0:
        raise _DocumentFailure("json_invalid")


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_tree(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_NODES:
            raise _DocumentFailure("node_limit_exceeded")
        if depth > _MAX_DEPTH:
            raise _DocumentFailure("depth_exceeded")
        if type(value) is dict:
            node_count += len(value)
            if node_count > _MAX_NODES:
                raise _DocumentFailure("node_limit_exceeded")
            for key, child in value.items():
                if type(key) is not str or _contains_surrogate(key):
                    raise _DocumentFailure("object_key_invalid")
                if len(key.encode("utf-8")) > _MAX_STRING_UTF8_BYTES:
                    raise _DocumentFailure("string_limit_exceeded")
                stack.append((child, depth + 1))
        elif type(value) is list:
            for child in value:
                stack.append((child, depth + 1))
        elif type(value) is str:
            if _contains_surrogate(value):
                raise _DocumentFailure("surrogate_forbidden")
            if len(value.encode("utf-8")) > _MAX_STRING_UTF8_BYTES:
                raise _DocumentFailure("string_limit_exceeded")
        elif type(value) not in (int, bool) and value is not None:
            raise _DocumentFailure("value_type_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _parse_document(raw: bytes, label: str, maximum_bytes: int) -> object:
    if len(raw) > maximum_bytes:
        raise _DocumentFailure(f"{label}_too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _DocumentFailure(f"{label}_bom_forbidden")
    try:
        _preflight_depth(raw)
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_int=_strict_integer,
            parse_constant=_reject_nonfinite,
        )
        _validate_tree(value)
        canonical = _canonical_json_bytes(value)
    except _DocumentFailure as failure:
        if failure.code.startswith(f"{label}_"):
            raise
        raise _DocumentFailure(f"{label}_{failure.code}") from None
    except UnicodeDecodeError:
        raise _DocumentFailure(f"{label}_utf8_invalid") from None
    except json.JSONDecodeError:
        raise _DocumentFailure(f"{label}_json_invalid") from None
    except (MemoryError, RecursionError):
        raise _DocumentFailure(f"{label}_resource_limit_exceeded") from None
    except (OverflowError, UnicodeEncodeError, ValueError):
        raise _DocumentFailure(f"{label}_value_invalid") from None
    if raw != canonical:
        raise _DocumentFailure(f"{label}_noncanonical")
    return value


def _exact_integer(value: object, expected: int | None = None) -> bool:
    return type(value) is int and (expected is None or value == expected)


def _has_alias_component(value: str) -> bool:
    return bool(_ALIAS_COMPONENTS.intersection(re.split(r"[/._-]+", value)))


def _validate_candidate(value: object) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != _CANDIDATE_KEYS:
        raise _DocumentFailure("candidate_shape_invalid")
    if value["contract_version"] != CONTRACT_VERSION:
        raise _DocumentFailure("candidate_contract_version_invalid")
    if value["credential_mode"] != "external_owner_handoff_required":
        raise _DocumentFailure("candidate_credential_mode_invalid")
    for key, expected in (
        ("attempts", 1),
        ("fanout", 0),
        ("jobs", 1),
        ("model_call_limit", 1),
        ("provider_request_limit", 1),
        ("retry_count", 0),
        ("max_input_tokens", MAX_INPUT_TOKENS),
        ("max_output_tokens", MAX_OUTPUT_TOKENS),
        ("max_total_tokens", MAX_TOTAL_TOKENS),
        ("max_output_bytes", MAX_OUTPUT_BYTES),
        ("max_cost_usd_microdollars", MAX_COST_USD_MICRODOLLARS),
        ("wall_clock_seconds", MAX_WALL_CLOCK_SECONDS),
    ):
        if not _exact_integer(value[key], expected):
            raise _DocumentFailure(f"candidate_{key}_invalid")
    if value["max_input_tokens"] + value["max_output_tokens"] > value[
        "max_total_tokens"
    ]:
        raise _DocumentFailure("candidate_token_relation_invalid")
    if type(value["repository_mount"]) is not bool or value["repository_mount"]:
        raise _DocumentFailure("candidate_repository_mount_invalid")
    if type(value["immutable_revision_claimed"]) is not bool or value[
        "immutable_revision_claimed"
    ]:
        raise _DocumentFailure("candidate_immutable_revision_claimed_invalid")
    if (
        type(value["provider_internal_revision_owner_accepted"]) is not bool
        or not value["provider_internal_revision_owner_accepted"]
    ):
        raise _DocumentFailure("candidate_provider_internal_revision_owner_accepted_invalid")
    if value["provider_internal_revision"] != EXPECTED_PROVIDER_INTERNAL_REVISION:
        raise _DocumentFailure("candidate_provider_internal_revision_invalid")
    if type(value["tool_allowlist"]) is not list or value["tool_allowlist"]:
        raise _DocumentFailure("candidate_tool_allowlist_invalid")

    provider_id = value["provider_id"]
    model_id = value["model_id"]
    if (
        type(provider_id) is not str
        or _PROVIDER_ID.fullmatch(provider_id) is None
        or _has_alias_component(provider_id)
        or provider_id != EXPECTED_PROVIDER_ID
    ):
        raise _DocumentFailure("candidate_provider_id_invalid")
    if (
        type(model_id) is not str
        or _MODEL_ID.fullmatch(model_id) is None
        or _has_alias_component(model_id)
        or model_id != EXPECTED_MODEL_ID
    ):
        raise _DocumentFailure("candidate_model_id_invalid")
    return value


def _validate_environment(value: object) -> dict[str, str]:
    if type(value) is not dict or frozenset(value) != _ENVIRONMENT_DOCUMENT_KEYS:
        raise _DocumentFailure("environment_shape_invalid")
    if value["contract_version"] != ENVIRONMENT_CONTRACT_VERSION:
        raise _DocumentFailure("environment_contract_version_invalid")
    environment = value["environment"]
    if type(environment) is not dict:
        raise _DocumentFailure("environment_mapping_invalid")
    if not frozenset(environment).issubset(_ALLOWED_ENVIRONMENT_KEYS):
        raise _DocumentFailure("environment_key_invalid")
    for key, item in environment.items():
        if type(key) is not str or type(item) is not str or not item:
            raise _DocumentFailure("environment_value_invalid")
        if any(character in item for character in ("\x00", "\r", "\n")):
            raise _DocumentFailure("environment_value_invalid")
        if _contains_surrogate(item) or len(item.encode("utf-8")) > _MAX_STRING_UTF8_BYTES:
            raise _DocumentFailure("environment_value_invalid")
    return environment


def _requested_limits(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "attempts": candidate["attempts"],
        "fanout": candidate["fanout"],
        "jobs": candidate["jobs"],
        "max_cost_usd_microdollars": candidate["max_cost_usd_microdollars"],
        "max_input_tokens": candidate["max_input_tokens"],
        "max_output_bytes": candidate["max_output_bytes"],
        "max_output_tokens": candidate["max_output_tokens"],
        "max_total_tokens": candidate["max_total_tokens"],
        "model_call_limit": candidate["model_call_limit"],
        "provider_request_limit": candidate["provider_request_limit"],
        "repository_mount": candidate["repository_mount"],
        "retry_count": candidate["retry_count"],
        "tool_allowlist": candidate["tool_allowlist"],
        "wall_clock_seconds": candidate["wall_clock_seconds"],
    }


def _receipt(
    failure_codes: list[str],
    candidate_raw: bytes | None,
    candidate: dict[str, object] | None,
    environment_raw: bytes | None,
    environment: dict[str, str] | None,
) -> bytes:
    valid = not failure_codes
    blocking_codes = sorted(set((*_ALWAYS_BLOCKING_CODES, *failure_codes)))
    candidate_sha256 = (
        hashlib.sha256(candidate_raw).hexdigest()
        if candidate_raw is not None and candidate is not None
        else None
    )
    environment_commitment = (
        hashlib.sha256(
            ENVIRONMENT_CONTRACT_VERSION.encode("ascii") + b"\x00" + environment_raw
        ).hexdigest()
        if environment_raw is not None and environment is not None
        else None
    )
    requested_identity = (
        {
            "model_id": candidate["model_id"],
            "provider_id": candidate["provider_id"],
            "provider_internal_revision": candidate["provider_internal_revision"],
            "provider_internal_revision_owner_accepted": candidate[
                "provider_internal_revision_owner_accepted"
            ],
            "immutable_revision_claimed": candidate["immutable_revision_claimed"],
        }
        if candidate is not None
        else None
    )
    requested_limits = _requested_limits(candidate) if candidate is not None else None
    receipt = {
        "activation_state": "hold_no_send",
        "blocking_codes": blocking_codes,
        "candidate_document_sha256": candidate_sha256,
        "candidate_input_verified": candidate is not None,
        "clean_environment_commitment_sha256": environment_commitment,
        "clean_environment_input_verified": environment is not None,
        "clean_environment_keys": sorted(environment)
        if environment is not None
        else [],
        "contract_version": RECEIPT_VERSION,
        "credential_scope_verified": False,
        "execution_authorized": False,
        "host_containment_verified": False,
        "model_identity_effective_verified": False,
        "owner_approval_verified": False,
        "provider_internal_revision": (
            candidate["provider_internal_revision"] if candidate is not None else None
        ),
        "provider_internal_revision_owner_accepted": (
            candidate["provider_internal_revision_owner_accepted"]
            if candidate is not None
            else False
        ),
        "immutable_revision_claimed": (
            candidate["immutable_revision_claimed"] if candidate is not None else False
        ),
        "requested_limits": requested_limits,
        "requested_model_identity": requested_identity,
        "safe_to_dispatch": False,
        "status": (
            "canonical_containment_inputs_verified_contract_only"
            if valid
            else "hold_missing_or_invalid"
        ),
        "token_limits_effective_verified": False,
        "tool_allowlist_effective_verified": False,
        "worker_runtime_verified": False,
    }
    return _canonical_json_bytes(receipt)


def assess_worker_containment_canonical_bytes_v2(
    candidate_document: object,
    environment_document: object,
) -> bytes:
    """Validate exact v2 input bytes and return a canonical no-send receipt."""

    failure_codes: list[str] = []
    candidate_raw = candidate_document if type(candidate_document) is bytes else None
    environment_raw = (
        environment_document if type(environment_document) is bytes else None
    )
    if candidate_raw is None:
        failure_codes.append("candidate_document_type_invalid")
    if environment_raw is None:
        failure_codes.append("environment_document_type_invalid")

    candidate: dict[str, object] | None = None
    environment: dict[str, str] | None = None
    if candidate_raw is not None:
        try:
            candidate = _validate_candidate(
                _parse_document(candidate_raw, "candidate_document", _MAX_CANDIDATE_BYTES)
            )
        except _DocumentFailure as failure:
            failure_codes.append(failure.code)
    if environment_raw is not None:
        try:
            environment = _validate_environment(
                _parse_document(
                    environment_raw, "environment_document", _MAX_ENVIRONMENT_BYTES
                )
            )
        except _DocumentFailure as failure:
            failure_codes.append(failure.code)

    return _receipt(
        failure_codes,
        candidate_raw,
        candidate,
        environment_raw,
        environment,
    )


__all__ = [
    "CONTRACT_VERSION",
    "ENVIRONMENT_CONTRACT_VERSION",
    "MAX_COST_USD_MICRODOLLARS",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_TOKENS",
    "MAX_TOTAL_TOKENS",
    "MAX_WALL_CLOCK_SECONDS",
    "RECEIPT_VERSION",
    "assess_worker_containment_canonical_bytes_v2",
]
