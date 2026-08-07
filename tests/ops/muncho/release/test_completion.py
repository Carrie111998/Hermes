from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.muncho.release import cli
from ops.muncho.release.completion import (
    ReleaseCompletionError,
    deliver_discord_once,
    finalize_release_completion,
    prepare_summary_draft,
    record_production_smoke,
    record_reserved_summary_delivery,
    release_health,
    release_status,
    reserve_release_mapping,
    reserve_summary_delivery,
    resolve_discord_destination,
)
from ops.muncho.release.metadata import load_release_bundle, resolve_exact_release_sha


ROOT = Path(__file__).parents[4]
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40
GUILD_ID = "123456789012345678"
CHANNEL_ID = "223456789012345678"


def _config() -> dict:
    return {
        "approvals": {
            "gateway_owner_escalation": {
                "enabled": True,
                "owner_user_id": "323456789012345678",
                "owner_guild_id": GUILD_ID,
                "owner_channel_id": CHANNEL_ID,
                "owner_target_type": "guild_channel",
            }
        }
    }


def _state(tmp_path: Path) -> Path:
    return (tmp_path / "release-state").resolve()


def _draft(tmp_path: Path):
    state = _state(tmp_path)
    bundle = load_release_bundle(ROOT)
    mapping = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        reserved_at=NOW,
    )
    smoke = record_production_smoke(
        state,
        mapping,
        checks=(
            "Gateway service is active on the exact release SHA.",
            "CLI and Discord version replies report the same identity.",
            "Rollback target remains available and unchanged.",
        ),
        completed_at=NOW,
    )
    draft = prepare_summary_draft(
        state,
        bundle,
        mapping=mapping,
        smoke=smoke,
        production_config=_config(),
        created_at=NOW,
    )
    return state, mapping, smoke, draft


def test_retrospective_r1_mapping_is_append_only_without_source_metadata(
    tmp_path: Path,
):
    bundle = load_release_bundle(ROOT)
    mapping = reserve_release_mapping(
        _state(tmp_path),
        bundle,
        version="2.3.1",
        release_sha="5564ec24a48d819e8ba0dd924bdb82ca5064ed4c",
        reserved_at=NOW,
    )

    assert mapping["muncho_version"] == "2.3.1"
    assert mapping["metadata_present_at_source"] is False
    assert mapping["source_metadata_sha256"] is None


def test_reservation_is_idempotent_and_refuses_version_reuse(tmp_path: Path):
    state = _state(tmp_path)
    bundle = load_release_bundle(ROOT)
    first = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        reserved_at=NOW,
    )
    second = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        reserved_at=NOW,
    )
    assert second == first

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_version_reused",
    ):
        reserve_release_mapping(
            state,
            bundle,
            version="2.3.2",
            release_sha="b" * 40,
            reserved_at=NOW,
        )


def test_destination_is_discovered_from_typed_config_not_hardcoded():
    assert resolve_discord_destination(_config()) == {
        "platform": "discord",
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "target_type": "guild_channel",
        "config_source": "approvals.gateway_owner_escalation",
    }


def test_full_completion_requires_same_summary_in_codex_and_discord(
    tmp_path: Path,
):
    state, mapping, smoke, draft = _draft(tmp_path)
    assert (
        prepare_summary_draft(
            state,
            load_release_bundle(ROOT),
            mapping=mapping,
            smoke=smoke,
            production_config=_config(),
            created_at=LATER,
        )
        == draft
    )
    sent: list[tuple[str, str]] = []

    def sender(message: str, channel_id: str):
        sent.append((message, channel_id))
        return {"success": True, "message_id": "423456789012345678"}

    discord = deliver_discord_once(
        state,
        draft,
        sender=sender,
        reserved_at=NOW,
        published_at=NOW,
    )
    # Retrying the same (version, SHA) returns the receipt and never sends a
    # duplicate Discord announcement.
    assert deliver_discord_once(state, draft, sender=sender) == discord
    assert sent == [(draft["summary"], CHANNEL_ID)]

    codex_attempt, created = reserve_summary_delivery(
        state,
        draft,
        kind="codex_task",
        destination_ref="019fa801-52ca-7460-954d-30aee7053618",
        reserved_at=NOW,
    )
    assert created is True
    codex = record_reserved_summary_delivery(
        state,
        draft,
        codex_attempt,
        message_ref="assistant-final-release-summary",
        published_at=NOW,
    )
    assert (
        record_reserved_summary_delivery(
            state,
            draft,
            codex_attempt,
            message_ref="assistant-final-release-summary",
            published_at=LATER,
        )
        == codex
    )
    assert codex["summary_sha256"] == discord["summary_sha256"]
    assert codex["summary_sha256"] == draft["summary_sha256"]

    completion = finalize_release_completion(
        state,
        mapping=mapping,
        smoke=smoke,
        draft=draft,
        codex_delivery=codex,
        discord_delivery=discord,
        completed_at=NOW,
    )
    assert (
        finalize_release_completion(
            state,
            mapping=mapping,
            smoke=smoke,
            draft=draft,
            codex_delivery=codex,
            discord_delivery=discord,
            completed_at=LATER,
        )
        == completion
    )
    assert completion["muncho_version"] == "2.3.2"
    assert completion["release_sha"] == RELEASE_SHA
    assert completion["required_summaries_published"] is True

    status = release_status(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )
    health = release_health(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )
    assert status["phase"] == "complete"
    assert status["release_sha"] == RELEASE_SHA
    assert health["healthy"] is True
    assert health["muncho_version"] == "2.3.2"


def test_completion_is_not_healthy_after_smoke_but_before_both_summaries(
    tmp_path: Path,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    codex_attempt, created = reserve_summary_delivery(
        state,
        draft,
        kind="codex_task",
        destination_ref="019fa801-52ca-7460-954d-30aee7053618",
        reserved_at=NOW,
    )
    assert created is True
    record_reserved_summary_delivery(
        state,
        draft,
        codex_attempt,
        message_ref="assistant-final-release-summary",
        published_at=NOW,
    )

    status = release_status(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )
    assert status["production_smoke_passed"] is True
    assert status["codex_task_summary_published"] is True
    assert status["discord_summary_published"] is False
    assert status["complete"] is False


def test_reserved_discord_attempt_never_retries_an_uncertain_send(
    tmp_path: Path,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    attempt, created = reserve_summary_delivery(
        state,
        draft,
        kind="discord",
        destination_ref=CHANNEL_ID,
        reserved_at=NOW,
    )
    assert created is True
    assert attempt["network_send_authorized"] is True

    called = False

    def sender(_message: str, _channel: str):
        nonlocal called
        called = True
        return {"success": True, "message_id": "423456789012345678"}

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_discord_delivery_reconciliation_required",
    ):
        deliver_discord_once(state, draft, sender=sender)
    assert called is False


def test_delivery_receipt_requires_a_persisted_matching_attempt(tmp_path: Path):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    attempt, created = reserve_summary_delivery(
        state,
        draft,
        kind="codex_task",
        destination_ref="019fa801-52ca-7460-954d-30aee7053618",
        reserved_at=NOW,
    )
    assert created is True
    attempt_path = next(state.glob("summary-codex_task-attempt-*.json"))
    attempt_path.unlink()

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_state_record_missing",
    ):
        record_reserved_summary_delivery(
            state,
            draft,
            attempt,
            message_ref="assistant-final-release-summary",
            published_at=NOW,
        )


def test_automatic_announcement_cli_requires_exact_identity_and_sends_once(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    release_sha = resolve_exact_release_sha(ROOT)
    assert release_sha is not None
    sent: list[tuple[str, str]] = []

    monkeypatch.setattr(cli, "load_current_production_config", lambda _path: _config())

    def sender(message: str, channel_id: str):
        sent.append((message, channel_id))
        return {"success": True, "message_id": "423456789012345678"}

    monkeypatch.setattr(cli, "hermes_send_discord", sender)
    arguments = [
        "announce-after-smoke",
        "--release-root",
        str(ROOT),
        "--release-sha",
        release_sha,
        "--state-dir",
        str(_state(tmp_path)),
        "--production-config",
        str(tmp_path / "config.yaml"),
        "--check",
        "Exact deployed identity is active.",
        "--check",
        "Gateway health and production smoke passed.",
    ]

    assert cli.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["muncho_version"] == "2.3.2"
    assert first["release_sha"] == release_sha
    assert first["release_completion"] == "codex_task_summary_pending"
    assert sent == [(first["summary"], CHANNEL_ID)]

    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert sent == [(first["summary"], CHANNEL_ID)]

    mismatched = list(arguments)
    mismatched[mismatched.index("--release-sha") + 1] = "b" * 40
    assert cli.main(mismatched) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"] == "muncho_release_deployed_identity_unconfirmed"
    assert sent == [(first["summary"], CHANNEL_ID)]
