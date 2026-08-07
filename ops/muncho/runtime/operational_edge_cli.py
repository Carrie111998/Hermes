#!/usr/bin/env python3
"""Credential-free CLI for explicit Cloud Muncho operational operations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway.operational_edge_catalog import (
    build_operation_argv,
    catalog_public_contract,
    operation_catalog,
)
from gateway.operational_edge_client import (
    AttestedMainPidFileProvider,
    OperationalEdgeClient,
    OperationalEdgeClientConfig,
    OperationalEdgeClientError,
    parse_operational_edge_client_configs,
)
from gateway.operational_edge_protocol import (
    OperationalAccess,
    OperationalIntent,
    operational_command_sha256,
    sha256_json,
)
from scripts.canary import passkey_v2_sensitive_report as sensitive_report
from scripts.canary import passkey_v2_sensitive_report_transport as sensitive_transport


DEFAULT_CLIENT_CONFIG = Path("/etc/muncho/operational-edge-client.json")
_MAX_DISCORD_REPORT_CHARS = 1800


def _stable_json(path: Path, *, maximum: int, allowed_modes: set[int]) -> Mapping[str, Any]:
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
            or not 0 < metadata.st_size <= maximum
        ):
            raise ValueError
        raw = path.read_bytes()
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = item
            return result
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("operational_edge_client_file_invalid") from exc
    if (
        not isinstance(value, Mapping)
        or raw
        != json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    ):
        raise ValueError("operational_edge_client_file_invalid")
    return value


def _config(path: Path, domain: str) -> OperationalEdgeClientConfig:
    value = _stable_json(path, maximum=256 * 1024, allowed_modes={0o400, 0o440, 0o444})
    try:
        configs = parse_operational_edge_client_configs(value)
        config = configs[domain]
    except (KeyError, OperationalEdgeClientError, TypeError, ValueError) as exc:
        raise ValueError("operational_edge_client_config_invalid") from exc
    return config


def _arguments(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("operational_edge_arguments_invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("operational_edge_arguments_invalid")
    return parsed


def _consume_approved_capability(intent: OperationalIntent) -> Mapping[str, Any]:
    """Mechanically consume the exact hash from an existing approved plan."""

    from gateway.canonical_writer_boundary import canonical_writer_call
    from gateway.canonical_writer_protocol import CanonicalWriterOperation

    command_sha256 = operational_command_sha256(intent)
    result = canonical_writer_call(
        CanonicalWriterOperation.CAPABILITY_CONSUME.value,
        {
            "command_sha256": command_sha256,
            "idempotency_key": (
                "operational-edge-consume:" + command_sha256
            ),
            "operational_edge_intent": intent.to_mapping(),
        },
        idempotency_key="operational-edge-consume:" + command_sha256,
    )
    capability = result.get("operational_edge_capability")
    if (
        result.get("authorized") is not True
        or not isinstance(capability, Mapping)
    ):
        raise ValueError("operational_edge_approved_capability_unavailable")
    return capability


def _route_back_execute(**kwargs: Any) -> str:
    from tools.canonical_brain_tool import route_back_execute_tool

    return route_back_execute_tool(**kwargs)


def _sensitive_routeback(
    *,
    intent: OperationalIntent,
    lease: Mapping[str, Any],
    message: str,
    purpose: str,
    idempotency_key: str,
    receipt_sha256: str,
) -> Mapping[str, Any]:
    lease = sensitive_report.validate_step_up_lease(lease)
    if (
        lease["operation_id"] != intent.operation_id
        or lease["arguments_sha256"] != intent.arguments_sha256
        or lease["idempotency_key"] != intent.idempotency_key
    ):
        raise ValueError("sensitive_report_routeback_binding_invalid")
    route = lease["discord_routeback"]
    raw = _route_back_execute(
        case_id=lease["case_id"],
        target_ref={
            "target_type": "guild_channel",
            "guild_id": route["guild_id"],
            "parent_channel_id": route["parent_channel_id"],
            "thread_id": route["thread_id"],
            "channel_id": route["thread_id"],
        },
        message=message,
        message_summary=purpose,
        source_refs={
            "guild_id": route["guild_id"],
            "parent_channel_id": route["parent_channel_id"],
            "thread_id": route["thread_id"],
            "message_id": route["source_message_id"],
            "operational_intent_sha256": sha256_json(intent.to_mapping()),
            "operational_receipt_sha256": receipt_sha256,
        },
        idempotency_key=idempotency_key,
    )
    try:
        result = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("sensitive_report_routeback_invalid") from exc
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise ValueError("sensitive_report_routeback_not_sent")
    return dict(result)


def _route_step_up(
    *, intent: OperationalIntent, state: Mapping[str, Any]
) -> Mapping[str, Any]:
    request_id = str(state["request_id"])
    approval_url = sensitive_transport.validate_approval_url(
        state["approval_url"], request_id
    )
    user_id = str(state["action_envelope"]["requester_discord_user_id"])
    message = (
        f"<@{user_id}> отвори защитеното одобрение от телефона си: "
        f"{approval_url}\nИскането е точно обвързано с тази справка и "
        "след passkey потвърждението Мунчо ще продължи сам."
    )
    return _sensitive_routeback(
        intent=intent,
        lease=state["action_envelope"]["action_payload"]["step_up_lease"],
        message=message,
        purpose="Protected passkey approval link for the exact sensitive report",
        idempotency_key=f"sensitive-step-up:{request_id}",
        receipt_sha256="0" * 64,
    )


def _route_sensitive_result(
    *,
    intent: OperationalIntent,
    receipt: Mapping[str, Any],
    lease: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        raw = base64.b64decode(receipt["stdout_b64"], validate=True)
        result = json.loads(raw.decode("utf-8", errors="strict"))
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("sensitive_report_result_invalid") from exc
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise ValueError("sensitive_report_result_invalid")
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    message = "SkyVision справката е готова:\n```json\n" + rendered + "\n```"
    if len(message) > _MAX_DISCORD_REPORT_CHARS:
        raise ValueError("sensitive_report_result_too_large_for_routeback")
    receipt_sha256 = hashlib.sha256(
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return _sensitive_routeback(
        intent=intent,
        lease=lease,
        message=message,
        purpose="Exact completed sensitive report returned to its source Discord thread",
        idempotency_key=(
            "sensitive-result:" + receipt["idempotency_key"]
        ),
        receipt_sha256=receipt_sha256,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="muncho-ops")
    parser.add_argument("--config", type=Path, default=DEFAULT_CLIENT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    schema = sub.add_parser("schema")
    schema.add_argument("--operation", required=True)
    authorize = sub.add_parser("authorization-hash")
    authorize.add_argument("--operation", required=True)
    authorize.add_argument("--arguments-json", required=True)
    authorize.add_argument("--idempotency-key", required=True)
    invoke = sub.add_parser("invoke")
    invoke.add_argument("--operation", required=True)
    invoke.add_argument("--arguments-json", required=True)
    invoke.add_argument("--idempotency-key", required=True)
    invoke.add_argument("--capability-file", type=Path)
    args = parser.parse_args(argv)

    catalog = operation_catalog()
    if args.command == "catalog":
        print(json.dumps(catalog_public_contract(), ensure_ascii=False, sort_keys=True))
        return 0
    operation = catalog.get(args.operation)
    if operation is None:
        raise SystemExit("unknown operational edge operation")
    if args.command == "schema":
        row = next(
            item
            for item in catalog_public_contract()["operations"]
            if item["operation_id"] == args.operation
        )
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0
    if not operation.available:
        raise SystemExit(operation.blocker_code)
    arguments = _arguments(args.arguments_json)
    # Validate the typed argv contract before any socket is contacted.
    build_operation_argv(operation, arguments)
    if args.command == "authorization-hash":
        if operation.access is not OperationalAccess.MUTATION:
            raise SystemExit("authorization hash is only valid for mutation operations")
        intent = OperationalIntent(
            operation_id=operation.operation_id,
            arguments=arguments,
            arguments_sha256=sha256_json(arguments),
            idempotency_key=args.idempotency_key,
        )
        # Round-trip through the exact protocol validator before exposing the
        # object used by Canonical Writer capability.consume.
        intent = OperationalIntent.from_mapping(intent.to_mapping())
        print(
            json.dumps(
                {
                    "command_sha256": operational_command_sha256(intent),
                    "operational_edge_intent": intent.to_mapping(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    capability = None
    intent = OperationalIntent.from_mapping(
        {
            "operation_id": operation.operation_id,
            "arguments": arguments,
            "arguments_sha256": sha256_json(arguments),
            "idempotency_key": args.idempotency_key,
        }
    )
    if args.capability_file is not None:
        if operation.access is not OperationalAccess.MUTATION:
            raise SystemExit(
                "capability files are only valid for mutation operations"
            )
        capability = _stable_json(
            args.capability_file, maximum=128 * 1024, allowed_modes={0o400, 0o440, 0o444, 0o600, 0o640}
        )
    if operation.access is OperationalAccess.MUTATION and capability is None:
        capability = _consume_approved_capability(intent)
    step_up_authorization = None
    if operation.operation_id == sensitive_report.OPERATION_ID:
        if not isinstance(capability, Mapping):
            raise SystemExit("sensitive report capability unavailable")
        owner_gate = sensitive_transport.SensitiveReportOwnerGateClient()
        create_frame = sensitive_transport.build_frame(
            operation="create",
            capability_envelope=capability,
            intent=intent,
        )
        state = owner_gate.call(create_frame)
        if (
            state.get("schema") != sensitive_transport.RESPONSE_SCHEMA
            or state.get("operation") != "create"
            or state.get("request_id") != create_frame["request_id"]
            or state.get("state") not in {"pending", "granted"}
            or not isinstance(state.get("approval_url"), str)
            or not isinstance(state.get("action_envelope"), Mapping)
        ):
            raise SystemExit("sensitive report owner gate response invalid")
        if state["state"] == "pending":
            routeback = _route_step_up(intent=intent, state=state)
            print(json.dumps({
                "schema": "muncho-sensitive-report-step-up-required.v1",
                "outcome": "step_up_required",
                "request_id": state["request_id"],
                "authenticated_discord_user_id": (
                    state["action_envelope"]["requester_discord_user_id"]
                ),
                "case_id": state["action_envelope"]["case_id"],
                "routeback_status": routeback.get("status"),
                "delivered_to_exact_source_thread": True,
                "secret_material_included": False,
            }, ensure_ascii=False, sort_keys=True))
            return 3
        runtime = sensitive_transport.build_runtime_binding(
            action_envelope=state["action_envelope"],
            capability_envelope=capability,
            intent=intent,
        )
        consume_frame = sensitive_transport.build_frame(
            operation="consume",
            capability_envelope=capability,
            intent=intent,
            runtime_binding=runtime,
        )
        consumed = owner_gate.call(consume_frame)
        if consumed.get("state") != "authorized":
            raise SystemExit("sensitive report passkey approval pending")
        step_up_authorization = sensitive_transport.step_up_bundle(
            response=consumed,
            capability_envelope=capability,
            intent=intent,
        )
    config = _config(args.config, operation.domain)
    provider = AttestedMainPidFileProvider(
        Path("/run/muncho-operational-edge") / operation.domain / "mainpid.json",
        domain=operation.domain,
    )
    client = OperationalEdgeClient(config, main_pid_provider=provider)
    invoke_options = {
        "idempotency_key": args.idempotency_key,
        "capability": capability,
    }
    if step_up_authorization is not None:
        invoke_options["step_up_authorization"] = step_up_authorization
    receipt = client.invoke(
        operation.operation_id,
        arguments,
        **invoke_options,
    )
    if operation.operation_id == sensitive_report.OPERATION_ID:
        if step_up_authorization is None:
            raise SystemExit("sensitive report step-up authorization unavailable")
        routeback = _route_sensitive_result(
            intent=intent,
            receipt=receipt,
            lease=step_up_authorization["action_envelope"][
                "action_payload"
            ]["step_up_lease"],
        )
        print(json.dumps({
            "schema": "muncho-sensitive-report-routeback.v1",
            "outcome": receipt.get("outcome"),
            "routeback_status": routeback.get("status"),
            "delivered_to_exact_source_thread": True,
            "sensitive_result_in_stdout": False,
        }, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt.get("outcome") == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
