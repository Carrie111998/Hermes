"""Pure stateful no-send guard mechanics for the representative-load v2 lane.

The guard binds a valid H5 receipt and exercises only an in-process state
machine.  It never reads configuration or credentials, starts a worker,
opens a network, calls a provider/model, invokes a tool, mounts a repository,
or changes authority.  Positive events are proof-only reservations; the
receipt's actual execution counters remain zero.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sys


INPUT_VERSION = "hermes.strict_runtime_guard.proof.input.v2"
EVENT_VERSION = "hermes.strict_runtime_guard.event.v2"
DECISION_VERSION = "hermes.strict_runtime_guard.decision.v2"
RECEIPT_VERSION = "hermes.strict_runtime_guard.proof.receipt.v2"

_H5_RECEIPT_VERSION = "hermes.strict_no_send_preflight.receipt.v2"
_H5_STATUS = "hermes_strict_no_send_preflight_verified_contract_only"
_H5_CAPSULE_VERSION = "hermes.strict_no_send.request_capsule.v2"

MAX_INPUT_TOKENS = 32_768
MAX_OUTPUT_TOKENS = 8_192
MAX_TOTAL_TOKENS = 40_960
MAX_WALL_CLOCK_SECONDS = 900
MAX_OUTPUT_BYTES = 524_288
MAX_COST_USD_MICRODOLLARS = 250_000

_MAX_PROOF_INPUT_BYTES = 96 * 1024
_MAX_H5_RECEIPT_BYTES = 32 * 1024
_MAX_EVENT_BYTES = 8 * 1024
_MAX_DEPTH = 8
_MAX_NODES = 1024
_MAX_STRING_BYTES = 32 * 1024
_MAX_INTEGER_DIGITS = 10

_H5_BLOCKING_CODES = [
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
]
_ALWAYS_BLOCKING_CODES = [
    "actual_worker_runtime_integration_required",
    "credential_scope_effective_verification_required",
    "effective_provider_transport_verification_required",
    "immutable_model_revision_not_claimed_owner_accepted",
    "owner_approval_required",
]

_H5_RECEIPT_KEYS = {
    "activation_state",
    "actual_cost_usd_microdollars",
    "actual_output_bytes",
    "blocking_codes",
    "candidate_document_sha256",
    "clean_environment_document_sha256",
    "contract_version",
    "credential_environment_boundary_preflight_verified",
    "credential_environment_names",
    "credential_scope_effective_verified",
    "execution_authorized",
    "external_dependency_graph_verified",
    "external_send",
    "filesystem_mutation_effective_verified",
    "h4_candidate_input_verified",
    "h4_environment_input_verified",
    "host_containment_verified",
    "implementation_graph_digest_semantics",
    "implementation_graph_file_count",
    "implementation_graph_sha256",
    "immutable_revision_claimed",
    "job_count",
    "local_implementation_graph_expected_match",
    "local_implementation_graph_trusted_anchor_verified",
    "model_call_count",
    "model_identity_preflight_verified",
    "model_revision_immutable_verified",
    "network_access",
    "no_send_audit_hook_installed",
    "ordinary_runtime_imported",
    "owner_approval_verified",
    "pilot_ready",
    "provider_endpoint_effective_verified",
    "provider_internal_revision",
    "provider_internal_revision_owner_accepted",
    "provider_profile_preflight_verified",
    "provider_request_count",
    "request_capsule",
    "request_capsule_sha256",
    "safe_to_dispatch",
    "status",
    "token_limits_effective_verified",
    "token_limits_preflight_bound",
    "tool_allowlist_preflight_verified",
    "tool_call_count",
    "worker_runtime_verified",
}

_CAPSULE_KEYS = {
    "api_mode",
    "attempt_limit",
    "contract_version",
    "credential_handoff",
    "fallback_model_ids",
    "fallback_provider_ids",
    "fanout",
    "immutable_revision_claimed",
    "job_limit",
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
    "provider_profile_api_mode",
    "provider_profile_declared_base_url",
    "provider_request_limit",
    "repository_mount",
    "retry_count",
    "tool_names",
    "wall_clock_seconds",
}
_BIND_REQUEST_KEYS = {
    "contract_version",
    "credential_material_present",
    "event",
    "fanout",
    "immutable_revision_claimed",
    "model_id",
    "provider_id",
    "provider_internal_revision",
    "provider_internal_revision_owner_accepted",
    "repository_mount",
    "retry_count",
    "tool_names",
}
_COUNT_EVENT_KEYS = {"contract_version", "count", "event"}
_SIMPLE_EVENT_KEYS = {"contract_version", "event"}
_TOOL_EVENT_KEYS = {"contract_version", "event", "tool_name"}
_REPO_EVENT_KEYS = {"contract_version", "event", "mounted"}
_CREDENTIAL_EVENT_KEYS = {
    "contract_version",
    "credential_material_present",
    "event",
}


class _GuardFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _reject_float(_value: str) -> int:
    raise _GuardFailure("json_float_forbidden")


def _reject_nonfinite(_value: str) -> int:
    raise _GuardFailure("json_nonfinite_forbidden")


def _strict_integer(value: str) -> int:
    if not value or not value.isascii():
        raise _GuardFailure("json_integer_invalid")
    if value == "0":
        return 0
    if value.startswith("-"):
        raise _GuardFailure("json_negative_integer_forbidden")
    if value.startswith("0") or not value.isdigit():
        raise _GuardFailure("json_integer_invalid")
    if len(value) > _MAX_INTEGER_DIGITS:
        raise _GuardFailure("json_integer_too_large")
    return int(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _GuardFailure("json_duplicate_key")
        result[key] = value
    return result


def _preflight_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > _MAX_DEPTH:
                raise _GuardFailure("json_depth_exceeded")
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise _GuardFailure("json_structure_invalid")
    if in_string or depth != 0:
        raise _GuardFailure("json_structure_invalid")


def _validate_tree(
    value: object, *, depth: int = 0, nodes: list[int] | None = None
) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > _MAX_NODES:
        raise _GuardFailure("json_node_limit_exceeded")
    if depth > _MAX_DEPTH:
        raise _GuardFailure("json_depth_exceeded")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _GuardFailure("json_surrogate_forbidden") from exc
        if len(encoded) > _MAX_STRING_BYTES:
            raise _GuardFailure("json_string_limit_exceeded")
        return
    if type(value) is list:
        for item in value:
            _validate_tree(item, depth=depth + 1, nodes=nodes)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _GuardFailure("json_key_type_invalid")
            _validate_tree(key, depth=depth + 1, nodes=nodes)
            _validate_tree(item, depth=depth + 1, nodes=nodes)
        return
    raise _GuardFailure("json_value_type_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    _validate_tree(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _GuardFailure("canonical_json_encoding_failed") from exc


def _parse_document(raw: object, label: str, maximum_bytes: int) -> dict[str, object]:
    if type(raw) is not bytes:
        raise _GuardFailure(f"{label}_type_invalid")
    if not raw or len(raw) > maximum_bytes:
        raise _GuardFailure(f"{label}_size_invalid")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _GuardFailure(f"{label}_bom_forbidden")
    _preflight_depth(raw)
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_strict_integer,
            parse_float=_reject_float,
            parse_constant=_reject_nonfinite,
        )
    except _GuardFailure:
        raise
    except UnicodeDecodeError as exc:
        raise _GuardFailure(f"{label}_utf8_invalid") from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _GuardFailure(f"{label}_json_invalid") from exc
    _validate_tree(value)
    if type(value) is not dict:
        raise _GuardFailure(f"{label}_shape_invalid")
    if _canonical_json_bytes(value) != raw:
        raise _GuardFailure(f"{label}_noncanonical")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded_sha256(raw: object, maximum_bytes: int) -> str | None:
    if type(raw) is not bytes or len(raw) > maximum_bytes:
        return None
    return _sha256(raw)


def _is_lower_digest(value: object, *, prefixed: bool = False) -> bool:
    if type(value) is not str:
        return False
    digest = value[7:] if prefixed and value.startswith("sha256:") else value
    if prefixed and not value.startswith("sha256:"):
        return False
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _exact_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid_identifier(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 128 or not value.isascii():
        return False
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return all(character in allowed for character in value)


def _decode_canonical_base64(value: object, maximum_bytes: int) -> bytes:
    if (
        type(value) is not str
        or not value
        or len(value) > ((maximum_bytes + 2) // 3) * 4
    ):
        raise _GuardFailure("h5_receipt_base64_invalid")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise _GuardFailure("h5_receipt_base64_invalid") from exc
    if len(decoded) > maximum_bytes or base64.b64encode(decoded) != encoded:
        raise _GuardFailure("h5_receipt_base64_invalid")
    return decoded


def _validate_capsule(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _CAPSULE_KEYS:
        raise _GuardFailure("h5_request_capsule_shape_invalid")
    capsule = value
    exact_values = {
        "api_mode": "chat_completions",
        "attempt_limit": 1,
        "contract_version": _H5_CAPSULE_VERSION,
        "credential_handoff": "external_owner_handoff_required",
        "fanout": 0,
        "immutable_revision_claimed": False,
        "job_limit": 1,
        "max_cost_usd_microdollars": MAX_COST_USD_MICRODOLLARS,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_total_tokens": MAX_TOTAL_TOKENS,
        "model_call_limit": 1,
        "provider_id": "zai",
        "provider_internal_revision": "unknown",
        "provider_internal_revision_owner_accepted": True,
        "provider_profile_api_mode": "chat_completions",
        "provider_profile_declared_base_url": "https://api.z.ai/api/paas/v4",
        "provider_request_limit": 1,
        "repository_mount": False,
        "retry_count": 0,
        "wall_clock_seconds": MAX_WALL_CLOCK_SECONDS,
    }
    for key, expected in exact_values.items():
        if type(capsule[key]) is not type(expected) or capsule[key] != expected:
            raise _GuardFailure(f"h5_request_capsule_{key}_invalid")
    for key in ("fallback_model_ids", "fallback_provider_ids", "tool_names"):
        if type(capsule[key]) is not list or capsule[key]:
            raise _GuardFailure(f"h5_request_capsule_{key}_invalid")
    if capsule["model_id"] != "glm-5.2" or not _valid_identifier(capsule["model_id"]):
        raise _GuardFailure("h5_request_capsule_model_id_invalid")
    return capsule


def _validate_h5_receipt(
    raw: object,
) -> tuple[dict[str, object], dict[str, object], str]:
    if type(raw) is not bytes:
        raise _GuardFailure("h5_receipt_type_invalid")
    if not raw or len(raw) > _MAX_H5_RECEIPT_BYTES:
        raise _GuardFailure("h5_receipt_size_invalid")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\r" in raw:
        raise _GuardFailure("h5_receipt_transport_invalid")
    receipt = _parse_document(raw[:-1], "h5_receipt", _MAX_H5_RECEIPT_BYTES - 1)
    if set(receipt) != _H5_RECEIPT_KEYS:
        raise _GuardFailure("h5_receipt_shape_invalid")
    if receipt["contract_version"] != _H5_RECEIPT_VERSION:
        raise _GuardFailure("h5_receipt_contract_version_invalid")
    if receipt["status"] != _H5_STATUS or receipt["activation_state"] != "hold_no_send":
        raise _GuardFailure("h5_receipt_status_invalid")
    if receipt["blocking_codes"] != _H5_BLOCKING_CODES:
        raise _GuardFailure("h5_receipt_blocking_codes_invalid")
    for key in ("candidate_document_sha256", "clean_environment_document_sha256"):
        if not _is_lower_digest(receipt[key]):
            raise _GuardFailure(f"h5_{key}_invalid")
    if not _is_lower_digest(receipt["implementation_graph_sha256"], prefixed=True):
        raise _GuardFailure("h5_implementation_graph_invalid")
    if (
        type(receipt["implementation_graph_file_count"]) is not int
        or receipt["implementation_graph_file_count"] < 1
    ):
        raise _GuardFailure("h5_implementation_graph_file_count_invalid")
    if receipt["implementation_graph_digest_semantics"] != "local_python_source_canonical_lf_v2":
        raise _GuardFailure("h5_implementation_graph_semantics_invalid")
    if receipt["credential_environment_names"] != [
        "GLM_API_KEY",
        "ZAI_API_KEY",
        "Z_AI_API_KEY",
    ]:
        raise _GuardFailure("h5_credential_environment_names_invalid")

    true_keys = (
        "credential_environment_boundary_preflight_verified",
        "h4_candidate_input_verified",
        "h4_environment_input_verified",
        "local_implementation_graph_expected_match",
        "model_identity_preflight_verified",
        "no_send_audit_hook_installed",
        "provider_profile_preflight_verified",
        "token_limits_preflight_bound",
        "tool_allowlist_preflight_verified",
    )
    false_keys = (
        "credential_scope_effective_verified",
        "execution_authorized",
        "external_dependency_graph_verified",
        "external_send",
        "filesystem_mutation_effective_verified",
        "host_containment_verified",
        "local_implementation_graph_trusted_anchor_verified",
        "model_revision_immutable_verified",
        "network_access",
        "ordinary_runtime_imported",
        "owner_approval_verified",
        "pilot_ready",
        "provider_endpoint_effective_verified",
        "safe_to_dispatch",
        "token_limits_effective_verified",
        "worker_runtime_verified",
    )
    for key in true_keys:
        if receipt[key] is not True:
            raise _GuardFailure(f"h5_{key}_invalid")
    for key in false_keys:
        if receipt[key] is not False:
            raise _GuardFailure(f"h5_{key}_invalid")
    if receipt["provider_internal_revision"] != "unknown":
        raise _GuardFailure("h5_provider_internal_revision_invalid")
    if receipt["provider_internal_revision_owner_accepted"] is not True:
        raise _GuardFailure("h5_provider_internal_revision_owner_accepted_invalid")
    if receipt["immutable_revision_claimed"] is not False:
        raise _GuardFailure("h5_immutable_revision_claimed_invalid")
    for key in (
        "job_count",
        "model_call_count",
        "provider_request_count",
        "tool_call_count",
        "actual_cost_usd_microdollars",
        "actual_output_bytes",
    ):
        if type(receipt[key]) is not int or receipt[key] != 0:
            raise _GuardFailure(f"h5_{key}_invalid")

    capsule = _validate_capsule(receipt["request_capsule"])
    capsule_sha256 = _sha256(_canonical_json_bytes(capsule))
    if receipt["request_capsule_sha256"] != capsule_sha256:
        raise _GuardFailure("h5_request_capsule_digest_mismatch")
    return receipt, capsule, _sha256(raw)


def _parse_proof_input(raw: object) -> tuple[bytes, str]:
    envelope = _parse_document(raw, "proof_input", _MAX_PROOF_INPUT_BYTES)
    if set(envelope) != {
        "contract_version",
        "expected_h5_receipt_sha256",
        "h5_receipt_b64",
    }:
        raise _GuardFailure("proof_input_shape_invalid")
    if envelope["contract_version"] != INPUT_VERSION:
        raise _GuardFailure("proof_input_contract_version_invalid")
    expected = envelope["expected_h5_receipt_sha256"]
    if not _is_lower_digest(expected):
        raise _GuardFailure("expected_h5_receipt_sha256_invalid")
    h5_raw = _decode_canonical_base64(envelope["h5_receipt_b64"], _MAX_H5_RECEIPT_BYTES)
    if _sha256(h5_raw) != expected:
        raise _GuardFailure("h5_receipt_expected_digest_mismatch")
    return h5_raw, expected


def _event(value: dict[str, object]) -> bytes:
    return _canonical_json_bytes({"contract_version": EVENT_VERSION, **value})


def _valid_binding_event(capsule: dict[str, object]) -> bytes:
    return _event(
        {
            "credential_material_present": False,
            "event": "bind_request",
            "fanout": 0,
            "immutable_revision_claimed": False,
            "model_id": capsule["model_id"],
            "provider_id": capsule["provider_id"],
            "provider_internal_revision": capsule["provider_internal_revision"],
            "provider_internal_revision_owner_accepted": True,
            "repository_mount": False,
            "retry_count": 0,
            "tool_names": [],
        }
    )


class _StrictRuntimeGuard:
    __slots__ = (
        "_attempt_reservations",
        "_capsule",
        "_cost_usd_microdollars",
        "_failure",
        "_held",
        "_h5_sha256",
        "_input_tokens",
        "_job_reservations",
        "_model_call_reservations",
        "_output_bytes",
        "_output_tokens",
        "_provider_request_reservations",
        "_request_bound",
        "_wall_clock_seconds",
    )

    def __init__(self, h5_receipt_document: object):
        self._capsule: dict[str, object] | None = None
        self._failure: str | None = None
        self._held = False
        self._h5_sha256 = _bounded_sha256(
            h5_receipt_document, _MAX_H5_RECEIPT_BYTES
        )
        self._request_bound = False
        self._job_reservations = 0
        self._attempt_reservations = 0
        self._model_call_reservations = 0
        self._provider_request_reservations = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._output_bytes = 0
        self._cost_usd_microdollars = 0
        self._wall_clock_seconds = 0
        try:
            _receipt, self._capsule, self._h5_sha256 = _validate_h5_receipt(
                h5_receipt_document
            )
        except _GuardFailure as failure:
            self._failure = failure.code
        except (MemoryError, RecursionError):
            self._failure = "guard_resource_limit_exceeded"
        except (TypeError, ValueError, UnicodeError):
            self._failure = "h5_receipt_validation_failed"

    def _state(self) -> dict[str, object]:
        return {
            "attempt_reservations": self._attempt_reservations,
            "cost_usd_microdollars_recorded": self._cost_usd_microdollars,
            "input_tokens_recorded": self._input_tokens,
            "job_reservations": self._job_reservations,
            "model_call_reservations": self._model_call_reservations,
            "output_bytes_recorded": self._output_bytes,
            "output_tokens_recorded": self._output_tokens,
            "provider_request_reservations": self._provider_request_reservations,
            "request_bound": self._request_bound,
            "wall_clock_seconds_observed": self._wall_clock_seconds,
        }

    def _decision(
        self,
        *,
        event_sha256: str | None,
        event_name: str | None,
        failure: str | None,
        applied: bool,
    ) -> bytes:
        blockers = sorted(
            set((*_ALWAYS_BLOCKING_CODES, *((failure,) if failure else ())))
        )
        return _canonical_json_bytes(
            {
                "activation_state": "hold_no_send",
                "blocking_codes": blockers,
                "contract_version": DECISION_VERSION,
                "credential_scope_effective_verified": False,
                "event_name": event_name,
                "event_sha256": event_sha256,
                "execution_authorized": False,
                "external_send": False,
                "guard_event_applied": applied,
                "guard_state": self._state(),
                "h5_receipt_sha256": self._h5_sha256,
                "job_count": 0,
                "model_call_count": 0,
                "network_access": False,
                "pilot_ready": False,
                "provider_request_count": 0,
                "runtime_guard_integrated_with_worker": False,
                "safe_to_dispatch": False,
                "status": (
                    "guard_event_accepted_contract_only_no_send"
                    if failure is None and applied
                    else "hold_missing_or_invalid"
                ),
                "tool_call_count": 0,
            }
        )

    def __call__(self, event_document: object) -> bytes:
        event_sha256 = _bounded_sha256(event_document, _MAX_EVENT_BYTES)
        event_name: str | None = None
        if self._failure is not None:
            return self._decision(
                event_sha256=event_sha256,
                event_name=None,
                failure=self._failure,
                applied=False,
            )
        if self._held:
            return self._decision(
                event_sha256=event_sha256,
                event_name=None,
                failure="guard_terminal_hold",
                applied=False,
            )
        try:
            event = _parse_document(event_document, "guard_event", _MAX_EVENT_BYTES)
            event_name = event.get("event") if type(event.get("event")) is str else None
            self._apply(event)
        except _GuardFailure as failure:
            self._held = True
            return self._decision(
                event_sha256=event_sha256,
                event_name=event_name,
                failure=failure.code,
                applied=False,
            )
        except (MemoryError, RecursionError):
            self._held = True
            return self._decision(
                event_sha256=event_sha256,
                event_name=event_name,
                failure="guard_resource_limit_exceeded",
                applied=False,
            )
        except (TypeError, ValueError, UnicodeError):
            self._held = True
            return self._decision(
                event_sha256=event_sha256,
                event_name=event_name,
                failure="guard_event_validation_failed",
                applied=False,
            )
        return self._decision(
            event_sha256=event_sha256,
            event_name=event_name,
            failure=None,
            applied=True,
        )

    def _apply(self, event: dict[str, object]) -> None:
        if event.get("contract_version") != EVENT_VERSION:
            raise _GuardFailure("guard_event_contract_version_invalid")
        event_name = event.get("event")
        if type(event_name) is not str:
            raise _GuardFailure("guard_event_name_invalid")
        if event_name == "bind_request":
            self._bind_request(event)
            return
        if not self._request_bound:
            raise _GuardFailure("request_binding_required")
        if event_name in {
            "reserve_job",
            "reserve_attempt",
            "reserve_model_call",
            "reserve_provider_request",
            "retry",
        }:
            if set(event) != _SIMPLE_EVENT_KEYS:
                raise _GuardFailure("guard_event_shape_invalid")
            self._reserve(event_name)
            return
        if event_name in {
            "record_input_tokens",
            "record_output_tokens",
            "record_output_bytes",
            "record_cost_usd_microdollars",
            "observe_wall_clock",
        }:
            if set(event) != _COUNT_EVENT_KEYS:
                raise _GuardFailure("guard_event_shape_invalid")
            count = event["count"]
            if not _exact_nonnegative_integer(count):
                raise _GuardFailure("guard_event_count_invalid")
            self._record(event_name, count)
            return
        if event_name == "record_tool_call":
            if set(event) != _TOOL_EVENT_KEYS:
                raise _GuardFailure("guard_event_shape_invalid")
            raise _GuardFailure("tool_call_forbidden")
        if event_name == "mount_repository":
            if set(event) != _REPO_EVENT_KEYS or event.get("mounted") is not True:
                raise _GuardFailure("repository_mount_forbidden")
            raise _GuardFailure("repository_mount_forbidden")
        if event_name == "supply_credential_material":
            if set(event) != _CREDENTIAL_EVENT_KEYS:
                raise _GuardFailure("guard_event_shape_invalid")
            raise _GuardFailure("credential_material_forbidden_in_no_send_proof")
        raise _GuardFailure("guard_event_name_invalid")

    def _bind_request(self, event: dict[str, object]) -> None:
        if set(event) != _BIND_REQUEST_KEYS:
            raise _GuardFailure("guard_event_shape_invalid")
        if self._request_bound:
            raise _GuardFailure("request_already_bound")
        assert self._capsule is not None
        for key in (
            "provider_id",
            "model_id",
            "provider_internal_revision",
            "provider_internal_revision_owner_accepted",
            "immutable_revision_claimed",
        ):
            if type(event[key]) is not type(self._capsule[key]) or event[key] != self._capsule[key]:
                raise _GuardFailure(f"requested_{key}_mismatch")
        if type(event["retry_count"]) is not int or event["retry_count"] != 0:
            raise _GuardFailure("retry_forbidden")
        if type(event["fanout"]) is not int or event["fanout"] != 0:
            raise _GuardFailure("fanout_forbidden")
        if type(event["tool_names"]) is not list or event["tool_names"]:
            raise _GuardFailure("tool_allowlist_violation")
        if type(event["repository_mount"]) is not bool or event["repository_mount"]:
            raise _GuardFailure("repository_mount_forbidden")
        if (
            type(event["credential_material_present"]) is not bool
            or event["credential_material_present"]
        ):
            raise _GuardFailure("credential_material_forbidden_in_no_send_proof")
        self._request_bound = True

    def _reserve(self, event_name: str) -> None:
        assert self._capsule is not None
        if event_name == "retry":
            raise _GuardFailure("retry_forbidden")
        mapping = {
            "reserve_job": ("_job_reservations", "job_limit", "job_limit_exceeded"),
            "reserve_attempt": (
                "_attempt_reservations",
                "attempt_limit",
                "attempt_limit_exceeded",
            ),
            "reserve_model_call": (
                "_model_call_reservations",
                "model_call_limit",
                "model_call_limit_exceeded",
            ),
            "reserve_provider_request": (
                "_provider_request_reservations",
                "provider_request_limit",
                "provider_request_limit_exceeded",
            ),
        }
        attribute, limit_key, failure_code = mapping[event_name]
        current = getattr(self, attribute)
        limit = self._capsule[limit_key]
        if type(limit) is not int or current + 1 > limit:
            raise _GuardFailure(failure_code)
        setattr(self, attribute, current + 1)

    def _record(self, event_name: str, count: int) -> None:
        assert self._capsule is not None
        if event_name == "observe_wall_clock":
            if count < self._wall_clock_seconds:
                raise _GuardFailure("wall_clock_not_monotonic")
            if count > self._capsule["wall_clock_seconds"]:
                raise _GuardFailure("wall_clock_limit_exceeded")
            self._wall_clock_seconds = count
            return
        if event_name == "record_input_tokens":
            new_input = self._input_tokens + count
            if new_input > self._capsule["max_input_tokens"]:
                raise _GuardFailure("input_token_limit_exceeded")
            if new_input + self._output_tokens > self._capsule["max_total_tokens"]:
                raise _GuardFailure("total_token_limit_exceeded")
            self._input_tokens = new_input
            return
        if event_name == "record_output_tokens":
            new_output = self._output_tokens + count
            if new_output > self._capsule["max_output_tokens"]:
                raise _GuardFailure("output_token_limit_exceeded")
            if self._input_tokens + new_output > self._capsule["max_total_tokens"]:
                raise _GuardFailure("total_token_limit_exceeded")
            self._output_tokens = new_output
            return
        if event_name == "record_output_bytes":
            new_bytes = self._output_bytes + count
            if new_bytes > self._capsule["max_output_bytes"]:
                raise _GuardFailure("output_bytes_limit_exceeded")
            self._output_bytes = new_bytes
            return
        new_cost = self._cost_usd_microdollars + count
        if new_cost > self._capsule["max_cost_usd_microdollars"]:
            raise _GuardFailure("cost_limit_exceeded")
        self._cost_usd_microdollars = new_cost


def new_strict_runtime_guard_v2(h5_receipt_document: object) -> object:
    """Return a pure stateful guard with no dispatch or callback surface."""

    return _StrictRuntimeGuard(h5_receipt_document)


def _decision_object(raw: bytes) -> dict[str, object]:
    value = _parse_document(raw, "guard_decision", 32_768)
    if value.get("contract_version") != DECISION_VERSION:
        raise _GuardFailure("guard_decision_contract_version_invalid")
    return value


def _expect_applied(guard: object, event_raw: bytes) -> dict[str, object]:
    decision = _decision_object(guard(event_raw))
    if (
        decision.get("status") != "guard_event_accepted_contract_only_no_send"
        or decision.get("guard_event_applied") is not True
    ):
        raise _GuardFailure("expected_guard_event_not_applied")
    _verify_decision_non_authorizing(decision)
    return decision


def _expect_hold(guard: object, event_raw: bytes, code: str) -> None:
    decision = _decision_object(guard(event_raw))
    if decision.get("status") != "hold_missing_or_invalid":
        raise _GuardFailure("expected_guard_hold_missing")
    blockers = decision.get("blocking_codes")
    if type(blockers) is not list or code not in blockers:
        raise _GuardFailure("expected_guard_hold_code_missing")
    _verify_decision_non_authorizing(decision)


def _verify_decision_non_authorizing(decision: dict[str, object]) -> None:
    for key in (
        "credential_scope_effective_verified",
        "execution_authorized",
        "external_send",
        "network_access",
        "pilot_ready",
        "runtime_guard_integrated_with_worker",
        "safe_to_dispatch",
    ):
        if decision.get(key) is not False:
            raise _GuardFailure("guard_decision_authority_violation")
    for key in (
        "job_count",
        "model_call_count",
        "provider_request_count",
        "tool_call_count",
    ):
        if decision.get(key) != 0 or type(decision.get(key)) is not int:
            raise _GuardFailure("guard_decision_execution_count_violation")


def _mutated_binding(capsule: dict[str, object], **changes: object) -> bytes:
    value = json.loads(_valid_binding_event(capsule))
    value.update(changes)
    return _canonical_json_bytes(value)


def _run_fixed_guard_proof(
    h5_raw: bytes, capsule: dict[str, object]
) -> tuple[dict[str, object], list[str]]:
    positive_guard = new_strict_runtime_guard_v2(h5_raw)
    positive_events = [
        _valid_binding_event(capsule),
        _event({"event": "reserve_job"}),
        _event({"event": "reserve_attempt"}),
        _event({"event": "reserve_model_call"}),
        _event({"event": "reserve_provider_request"}),
        _event({"count": capsule["max_input_tokens"], "event": "record_input_tokens"}),
        _event({"count": capsule["max_output_tokens"], "event": "record_output_tokens"}),
        _event({"count": capsule["max_output_bytes"], "event": "record_output_bytes"}),
        _event(
            {
                "count": capsule["max_cost_usd_microdollars"],
                "event": "record_cost_usd_microdollars",
            }
        ),
        _event({"count": capsule["wall_clock_seconds"], "event": "observe_wall_clock"}),
    ]
    final_decision: dict[str, object] | None = None
    for event_raw in positive_events:
        final_decision = _expect_applied(positive_guard, event_raw)
    if final_decision is None:
        raise _GuardFailure("positive_guard_sequence_missing")

    probes: list[tuple[str, bytes, list[bytes]]] = [
        ("requested_provider_id_mismatch", _mutated_binding(capsule, provider_id="other"), []),
        ("requested_model_id_mismatch", _mutated_binding(capsule, model_id="other"), []),
        (
            "requested_provider_internal_revision_mismatch",
            _mutated_binding(capsule, provider_internal_revision="claimed"),
            [],
        ),
        (
            "requested_provider_internal_revision_owner_accepted_mismatch",
            _mutated_binding(capsule, provider_internal_revision_owner_accepted=False),
            [],
        ),
        (
            "requested_immutable_revision_claimed_mismatch",
            _mutated_binding(capsule, immutable_revision_claimed=True),
            [],
        ),
        ("retry_forbidden", _mutated_binding(capsule, retry_count=1), []),
        ("fanout_forbidden", _mutated_binding(capsule, fanout=1), []),
        ("tool_allowlist_violation", _mutated_binding(capsule, tool_names=["terminal"]), []),
        ("repository_mount_forbidden", _mutated_binding(capsule, repository_mount=True), []),
        (
            "credential_material_forbidden_in_no_send_proof",
            _mutated_binding(capsule, credential_material_present=True),
            [],
        ),
        ("request_binding_required", _event({"event": "reserve_job"}), []),
        (
            "job_limit_exceeded",
            _event({"event": "reserve_job"}),
            [_valid_binding_event(capsule), _event({"event": "reserve_job"})],
        ),
        (
            "attempt_limit_exceeded",
            _event({"event": "reserve_attempt"}),
            [_valid_binding_event(capsule), _event({"event": "reserve_attempt"})],
        ),
        (
            "model_call_limit_exceeded",
            _event({"event": "reserve_model_call"}),
            [_valid_binding_event(capsule), _event({"event": "reserve_model_call"})],
        ),
        (
            "provider_request_limit_exceeded",
            _event({"event": "reserve_provider_request"}),
            [_valid_binding_event(capsule), _event({"event": "reserve_provider_request"})],
        ),
        (
            "input_token_limit_exceeded",
            _event({"count": capsule["max_input_tokens"] + 1, "event": "record_input_tokens"}),
            [_valid_binding_event(capsule)],
        ),
        (
            "output_token_limit_exceeded",
            _event({"count": capsule["max_output_tokens"] + 1, "event": "record_output_tokens"}),
            [_valid_binding_event(capsule)],
        ),
        (
            "output_bytes_limit_exceeded",
            _event({"count": capsule["max_output_bytes"] + 1, "event": "record_output_bytes"}),
            [_valid_binding_event(capsule)],
        ),
        (
            "cost_limit_exceeded",
            _event(
                {
                    "count": capsule["max_cost_usd_microdollars"] + 1,
                    "event": "record_cost_usd_microdollars",
                }
            ),
            [_valid_binding_event(capsule)],
        ),
        (
            "wall_clock_limit_exceeded",
            _event({"count": capsule["wall_clock_seconds"] + 1, "event": "observe_wall_clock"}),
            [_valid_binding_event(capsule)],
        ),
        (
            "tool_call_forbidden",
            _event({"event": "record_tool_call", "tool_name": "terminal"}),
            [_valid_binding_event(capsule)],
        ),
        (
            "repository_mount_forbidden",
            _event({"event": "mount_repository", "mounted": True}),
            [_valid_binding_event(capsule)],
        ),
        (
            "credential_material_forbidden_in_no_send_proof",
            _event(
                {
                    "credential_material_present": True,
                    "event": "supply_credential_material",
                }
            ),
            [_valid_binding_event(capsule)],
        ),
        ("retry_forbidden", _event({"event": "retry"}), [_valid_binding_event(capsule)]),
    ]
    verified_codes: list[str] = []
    for code, violating_event, prefix in probes:
        guard = new_strict_runtime_guard_v2(h5_raw)
        for event_raw in prefix:
            _expect_applied(guard, event_raw)
        _expect_hold(guard, violating_event, code)
        verified_codes.append(code)
    return final_decision["guard_state"], verified_codes


def _proof_receipt(
    *,
    failures: list[str],
    h5_sha256: str | None,
    capsule: dict[str, object] | None,
    positive_state: dict[str, object] | None,
    verified_codes: list[str],
) -> bytes:
    valid = not failures
    return _canonical_json_bytes(
        {
            "activation_state": "hold_no_send",
            "actual_cost_usd_microdollars": 0,
            "actual_output_bytes": 0,
            "actual_worker_runtime_verified": False,
            "blocking_codes": sorted(set((*_ALWAYS_BLOCKING_CODES, *failures))),
            "contract_version": RECEIPT_VERSION,
            "credential_scope_effective_verified": False,
            "execution_authorized": False,
            "external_send": False,
            "guard_event_api_verified": valid,
            "guard_probe_codes": verified_codes if valid else [],
            "guard_probe_count": len(verified_codes) if valid else 0,
            "h5_preflight_receipt_verified": valid,
            "h5_receipt_sha256": h5_sha256,
            "h5_receipt_trusted_anchor_verified": False,
            "immutable_model_revision_effective_verified": False,
            "job_count": 0,
            "model_call_count": 0,
            "network_access": False,
            "pilot_ready": False,
            "proof_only_guard_state": positive_state if valid else None,
            "provider_id_requested": capsule["provider_id"] if valid and capsule else None,
            "provider_internal_revision": (
                capsule["provider_internal_revision"] if valid and capsule else None
            ),
            "provider_internal_revision_owner_accepted": (
                capsule["provider_internal_revision_owner_accepted"]
                if valid and capsule
                else False
            ),
            "immutable_revision_claimed": (
                capsule["immutable_revision_claimed"] if valid and capsule else False
            ),
            "provider_request_count": 0,
            "provider_transport_effective_verified": False,
            "requested_model_id": capsule["model_id"] if valid and capsule else None,
            "runtime_guard_decision_mechanics_verified_no_send": valid,
            "runtime_guard_integrated_with_worker": False,
            "safe_to_dispatch": False,
            "status": (
                "hermes_strict_runtime_guard_mechanics_verified_no_send"
                if valid
                else "hold_missing_or_invalid"
            ),
            "token_limits_effective_verified": False,
            "tool_call_count": 0,
        }
    )


def prove_strict_runtime_guard_v2(raw_input: object) -> bytes:
    """Run the fixed no-send mechanics proof and return canonical receipt bytes."""

    failures: list[str] = []
    h5_sha256: str | None = None
    capsule: dict[str, object] | None = None
    positive_state: dict[str, object] | None = None
    verified_codes: list[str] = []
    try:
        h5_raw, h5_sha256 = _parse_proof_input(raw_input)
        _receipt, capsule, actual_sha256 = _validate_h5_receipt(h5_raw)
        if actual_sha256 != h5_sha256:
            raise _GuardFailure("h5_receipt_digest_binding_failed")
        positive_state, verified_codes = _run_fixed_guard_proof(h5_raw, capsule)
    except _GuardFailure as failure:
        failures.append(failure.code)
    except (MemoryError, RecursionError):
        failures.append("proof_resource_limit_exceeded")
    except (TypeError, ValueError, UnicodeError):
        failures.append("runtime_guard_proof_failed")
    return _proof_receipt(
        failures=failures,
        h5_sha256=h5_sha256,
        capsule=capsule,
        positive_state=positive_state,
        verified_codes=verified_codes,
    )


def main() -> int:
    if sys.argv[1:]:
        sys.stderr.buffer.write(
            b"usage: hermes-strict-runtime-guard-v2 < canonical-proof-input.json\n"
        )
        return 64
    raw = sys.stdin.buffer.read(_MAX_PROOF_INPUT_BYTES + 1)
    receipt = prove_strict_runtime_guard_v2(raw)
    sys.stdout.buffer.write(receipt + b"\n")
    try:
        status = json.loads(receipt).get("status")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 64
    return 0 if status == "hermes_strict_runtime_guard_mechanics_verified_no_send" else 64


__all__ = [
    "EVENT_VERSION",
    "INPUT_VERSION",
    "RECEIPT_VERSION",
    "new_strict_runtime_guard_v2",
    "prove_strict_runtime_guard_v2",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
