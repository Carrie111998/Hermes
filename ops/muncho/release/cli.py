"""Small operator CLI for strict Muncho release identity and status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .completion import (
    DELIVERY_SCHEMA,
    ReleaseCompletionError,
    complete_restart_attestation,
    deliver_discord_via_gateway_once,
    load_current_production_config,
    prepare_summary_draft,
    prepare_restart_attestation,
    record_codex_task_summary_and_finalize,
    record_production_smoke,
    release_health,
    release_status,
    require_restart_attestation,
    reserve_codex_task_summary,
    reserve_release_mapping,
)
from .metadata import (
    ReleaseMetadataError,
    canonical_bytes,
    load_release_bundle,
    require_exact_release_sha,
    resolve_exact_release_sha,
)


def _emit(value: object) -> None:
    print(canonical_bytes(value).decode("ascii"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="muncho-release")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--release-root", type=Path)
    inspect.add_argument("--release-sha")

    reserve = subparsers.add_parser("reserve")
    reserve.add_argument("--release-root", type=Path)
    reserve.add_argument("--release-sha", required=True)
    reserve.add_argument("--version")
    reserve.add_argument("--state-dir", type=Path, required=True)

    restart_prepare = subparsers.add_parser("restart-prepare")
    restart_prepare.add_argument("--release-root", type=Path)
    restart_prepare.add_argument("--release-sha", required=True)
    restart_prepare.add_argument("--state-dir", type=Path, required=True)
    restart_prepare.add_argument("--service", required=True)
    restart_prepare.add_argument("--before-invocation-id", required=True)

    restart_complete = subparsers.add_parser("restart-complete")
    restart_complete.add_argument("--release-root", type=Path)
    restart_complete.add_argument("--release-sha", required=True)
    restart_complete.add_argument("--state-dir", type=Path, required=True)
    restart_complete.add_argument("--service", required=True)
    restart_complete.add_argument("--after-invocation-id", required=True)

    announce = subparsers.add_parser("announce-after-smoke")
    announce.add_argument("--release-root", type=Path)
    announce.add_argument("--release-sha", required=True)
    announce.add_argument("--state-dir", type=Path, required=True)
    announce.add_argument("--production-config", type=Path, required=True)
    announce.add_argument("--check", action="append", required=True)

    coordinator_prepare = subparsers.add_parser("coordinator-prepare")
    coordinator_prepare.add_argument("--version", required=True)
    coordinator_prepare.add_argument("--release-sha", required=True)
    coordinator_prepare.add_argument("--state-dir", type=Path, required=True)
    coordinator_prepare.add_argument("--task-id", required=True)

    coordinator_complete = subparsers.add_parser("coordinator-complete")
    coordinator_complete.add_argument("--version", required=True)
    coordinator_complete.add_argument("--release-sha", required=True)
    coordinator_complete.add_argument("--state-dir", type=Path, required=True)
    coordinator_complete.add_argument("--task-id", required=True)
    coordinator_complete.add_argument("--message-ref", required=True)
    coordinator_complete.add_argument("--summary-sha256", required=True)
    coordinator_complete.add_argument(
        "--attempt-receipt-sha256",
        required=True,
    )

    for name in ("status", "health"):
        status = subparsers.add_parser(name)
        status.add_argument("--version", required=True)
        status.add_argument("--release-sha", required=True)
        status.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "inspect":
            bundle = load_release_bundle(arguments.release_root)
            release_sha = arguments.release_sha or resolve_exact_release_sha(
                arguments.release_root
            )
            if release_sha is None:
                raise ReleaseMetadataError("muncho_release_sha_unavailable")
            release_sha = require_exact_release_sha(release_sha)
            _emit({
                "schema": "muncho-release-inspection.v1",
                "muncho_version": str(bundle.metadata.version),
                "release_sha": release_sha,
                "release_sha_short": release_sha[:8],
                "source_metadata_sha256": bundle.metadata.metadata_sha256,
                "source_history_sha256": bundle.history.history_sha256,
            })
            return 0
        if arguments.command == "reserve":
            bundle = load_release_bundle(arguments.release_root)
            version = arguments.version or str(bundle.metadata.version)
            _emit(
                reserve_release_mapping(
                    arguments.state_dir,
                    bundle,
                    version=version,
                    release_sha=arguments.release_sha,
                )
            )
            return 0
        if arguments.command in {"restart-prepare", "restart-complete"}:
            bundle = load_release_bundle(arguments.release_root)
            version = str(bundle.metadata.version)
            mapping = reserve_release_mapping(
                arguments.state_dir,
                bundle,
                version=version,
                release_sha=arguments.release_sha,
            )
            if arguments.command == "restart-prepare":
                receipt = prepare_restart_attestation(
                    arguments.state_dir,
                    mapping,
                    service_name=arguments.service,
                    before_invocation_id=arguments.before_invocation_id,
                )
                receipt_kind = "attempt"
            else:
                receipt = complete_restart_attestation(
                    arguments.state_dir,
                    mapping,
                    service_name=arguments.service,
                    after_invocation_id=arguments.after_invocation_id,
                )
                receipt_kind = "attestation"
            _emit({
                "schema": "muncho-release-restart-receipt.v1",
                "muncho_version": version,
                "release_sha": mapping["release_sha"],
                "release_sha_short": mapping["release_sha"][:8],
                "receipt_kind": receipt_kind,
                "restart_receipt_sha256": receipt["receipt_sha256"],
            })
            return 0
        if arguments.command == "announce-after-smoke":
            bundle = load_release_bundle(arguments.release_root)
            version = str(bundle.metadata.version)
            observed_sha = resolve_exact_release_sha(arguments.release_root)
            if observed_sha != arguments.release_sha:
                raise ReleaseCompletionError(
                    "muncho_release_deployed_identity_unconfirmed"
                )
            mapping = reserve_release_mapping(
                arguments.state_dir,
                bundle,
                version=version,
                release_sha=arguments.release_sha,
            )
            restart = require_restart_attestation(arguments.state_dir, mapping)
            smoke = record_production_smoke(
                arguments.state_dir,
                mapping,
                restart,
                checks=arguments.check,
            )
            draft = prepare_summary_draft(
                arguments.state_dir,
                bundle,
                mapping=mapping,
                smoke=smoke,
                production_config=load_current_production_config(
                    arguments.production_config
                ),
            )
            delivery = deliver_discord_via_gateway_once(
                arguments.state_dir,
                draft,
            )
            _emit({
                "schema": "muncho-release-automatic-announcement.v1",
                "muncho_version": version,
                "release_sha": mapping["release_sha"],
                "release_sha_short": mapping["release_sha"][:8],
                "mapping_receipt_sha256": mapping["receipt_sha256"],
                "smoke_receipt_sha256": smoke["receipt_sha256"],
                "summary_sha256": draft["summary_sha256"],
                "discord_delivery_receipt_sha256": delivery["receipt_sha256"],
                "summary": draft["summary"],
                "release_completion": "codex_task_summary_pending",
            })
            return 0
        if arguments.command == "coordinator-prepare":
            draft, attempt, created = reserve_codex_task_summary(
                arguments.state_dir,
                version=arguments.version,
                release_sha=arguments.release_sha,
                task_id=arguments.task_id,
            )
            already_delivered = attempt.get("schema") == DELIVERY_SCHEMA
            _emit({
                "schema": "muncho-release-coordinator-summary.v1",
                "muncho_version": draft["muncho_version"],
                "release_sha": draft["release_sha"],
                "release_sha_short": draft["release_sha"][:8],
                "task_id": arguments.task_id,
                "summary": draft["summary"],
                "summary_sha256": draft["summary_sha256"],
                "attempt_receipt_sha256": (
                    attempt["attempt_receipt_sha256"]
                    if already_delivered
                    else attempt["receipt_sha256"]
                ),
                "delivery_state": (
                    "delivered"
                    if already_delivered
                    else "reserved"
                    if created
                    else "reconciliation_required"
                ),
                "message_ref": (
                    attempt["message_ref"] if already_delivered else None
                ),
                "release_completion": (
                    "finalization_pending"
                    if already_delivered
                    else "codex_task_summary_pending"
                ),
            })
            return 0
        if arguments.command == "coordinator-complete":
            codex, completion = record_codex_task_summary_and_finalize(
                arguments.state_dir,
                version=arguments.version,
                release_sha=arguments.release_sha,
                task_id=arguments.task_id,
                message_ref=arguments.message_ref,
                summary_sha256=arguments.summary_sha256,
                attempt_receipt_sha256=arguments.attempt_receipt_sha256,
            )
            health = release_health(
                arguments.state_dir,
                version=completion["muncho_version"],
                release_sha=completion["release_sha"],
            )
            if health["healthy"] is not True:
                raise ReleaseCompletionError(
                    "muncho_release_completion_health_unconfirmed"
                )
            _emit({
                "schema": "muncho-release-coordinator-completion.v1",
                "muncho_version": completion["muncho_version"],
                "release_sha": completion["release_sha"],
                "release_sha_short": completion["release_sha"][:8],
                "summary_sha256": completion["summary_sha256"],
                "codex_task_delivery_receipt_sha256": codex["receipt_sha256"],
                "completion_receipt_sha256": completion["receipt_sha256"],
                "release_completion": "complete",
                "healthy": health["healthy"],
            })
            return 0
        projection = release_status if arguments.command == "status" else release_health
        _emit(
            projection(
                arguments.state_dir,
                version=arguments.version,
                release_sha=arguments.release_sha,
            )
        )
        return 0
    except (ReleaseMetadataError, ReleaseCompletionError) as exc:
        _emit({
            "schema": "muncho-release-error.v1",
            "ok": False,
            "error": str(exc),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
