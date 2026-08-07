"""Small operator CLI for strict Muncho release identity and status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .completion import (
    ReleaseCompletionError,
    deliver_discord_once,
    hermes_send_discord,
    load_current_production_config,
    prepare_summary_draft,
    record_production_smoke,
    release_health,
    release_status,
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

    announce = subparsers.add_parser("announce-after-smoke")
    announce.add_argument("--release-root", type=Path)
    announce.add_argument("--release-sha", required=True)
    announce.add_argument("--state-dir", type=Path, required=True)
    announce.add_argument("--production-config", type=Path, required=True)
    announce.add_argument("--check", action="append", required=True)

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
            smoke = record_production_smoke(
                arguments.state_dir,
                mapping,
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
            delivery = deliver_discord_once(
                arguments.state_dir,
                draft,
                sender=hermes_send_discord,
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
