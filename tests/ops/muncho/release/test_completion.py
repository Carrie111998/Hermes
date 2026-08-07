from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.muncho.release import cli
from ops.muncho.release.completion import (
    ReleaseCompletionError,
    complete_restart_attestation,
    deliver_discord_via_gateway_once,
    finalize_release_completion,
    pending_gateway_discord_deliveries,
    prepare_restart_attestation,
    prepare_summary_draft,
    record_codex_task_summary_and_finalize,
    record_gateway_discord_delivery,
    record_production_smoke,
    record_reserved_summary_delivery,
    release_health,
    release_idempotency_key,
    release_status,
    reserve_codex_task_summary,
    reserve_release_mapping,
    reserve_summary_delivery,
    resolve_discord_destination,
)
from ops.muncho.release.metadata import (
    canonical_bytes,
    load_release_bundle,
    resolve_exact_release_sha,
    sha256_bytes,
)


ROOT = Path(__file__).parents[4]
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40
GUILD_ID = "123456789012345678"
CHANNEL_ID = "223456789012345678"
SERVICE = "hermes-cloud-gateway.service"
BEFORE_INVOCATION_ID = "1" * 32
AFTER_INVOCATION_ID = "2" * 32


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


def _draft(tmp_path: Path, *, release_sha: str = RELEASE_SHA):
    state = _state(tmp_path)
    bundle = load_release_bundle(ROOT)
    mapping = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=release_sha,
        reserved_at=NOW,
    )
    restart_attempt = prepare_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        before_invocation_id=BEFORE_INVOCATION_ID,
        prepared_at=NOW,
    )
    assert restart_attempt["planned_stop_marker_prepared"] is True
    restart = complete_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        after_invocation_id=AFTER_INVOCATION_ID,
        attested_at=NOW,
    )
    smoke = record_production_smoke(
        state,
        mapping,
        restart,
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


def _record_gateway_delivery(
    state: Path,
    draft: dict,
    *,
    sent: list[tuple[str, str]] | None = None,
):
    try:
        return deliver_discord_via_gateway_once(
            state,
            draft,
            timeout_seconds=0,
            reserved_at=NOW,
            queued_at=NOW,
        )
    except ReleaseCompletionError as exc:
        assert str(exc) == "muncho_release_discord_delivery_reconciliation_required"
    request, = pending_gateway_discord_deliveries(state)
    if sent is not None:
        sent.append((request["summary"], request["channel_id"]))
    return record_gateway_discord_delivery(
        state,
        request,
        message_id="423456789012345678",
        published_at=NOW,
    )


def _tamper_and_reseal(path: Path, changes: dict[str, str]) -> None:
    record = json.loads(path.read_text(encoding="ascii"))
    record.update(changes)
    record.pop("receipt_sha256")
    record["receipt_sha256"] = sha256_bytes(canonical_bytes(record))
    path.write_bytes(canonical_bytes(record) + b"\n")


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


def test_restart_cli_records_changed_systemd_invocation_and_replays(
    tmp_path: Path,
    capsys,
):
    state = _state(tmp_path)
    common = [
        "--release-root",
        str(ROOT),
        "--release-sha",
        RELEASE_SHA,
        "--state-dir",
        str(state),
        "--service",
        SERVICE,
    ]
    assert cli.main([
        "restart-prepare",
        *common,
        "--before-invocation-id",
        BEFORE_INVOCATION_ID,
    ]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["receipt_kind"] == "attempt"

    assert cli.main([
        "restart-complete",
        *common,
        "--after-invocation-id",
        BEFORE_INVOCATION_ID,
    ]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "muncho_release_restart_attestation_invalid"
    )

    complete_args = [
        "restart-complete",
        *common,
        "--after-invocation-id",
        AFTER_INVOCATION_ID,
    ]
    assert cli.main(complete_args) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["receipt_kind"] == "attestation"
    assert cli.main(complete_args) == 0
    assert json.loads(capsys.readouterr().out) == completed

    stale_replay = list(complete_args)
    stale_replay[stale_replay.index("--after-invocation-id") + 1] = "3" * 32
    assert cli.main(stale_replay) == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "muncho_release_restart_attestation_conflict"
    )


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

    discord = _record_gateway_delivery(state, draft, sent=sent)
    # Retrying the same (version, SHA) returns the receipt and never sends a
    # duplicate Discord announcement.
    assert _record_gateway_delivery(state, draft, sent=sent) == discord
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


def test_gateway_request_is_mandatory_for_terminal_completion_and_health(
    tmp_path: Path,
):
    state, mapping, smoke, draft = _draft(tmp_path)
    discord = _record_gateway_delivery(state, draft)
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
        message_ref="assistant-release-summary",
        published_at=NOW,
    )
    next(state.glob("gateway-discord-request-*.json")).unlink()

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_state_record_missing",
    ):
        finalize_release_completion(
            state,
            mapping=mapping,
            smoke=smoke,
            draft=draft,
            codex_delivery=codex,
            discord_delivery=discord,
        )
    for projection in (release_status, release_health):
        with pytest.raises(
            ReleaseCompletionError,
            match="muncho_release_status_chain_invalid",
        ):
            projection(state, version="2.3.2", release_sha=RELEASE_SHA)


@pytest.mark.parametrize(
    "changes",
    [
        {"after_invocation_id": "3" * 32},
        {"channel_id": "333456789012345678"},
        {
            "release_sha": "b" * 40,
            "release_idempotency_key": release_idempotency_key(
                "2.3.2",
                "b" * 40,
            ),
        },
        {
            "muncho_version": "9.9.9",
            "release_idempotency_key": release_idempotency_key(
                "9.9.9",
                RELEASE_SHA,
            ),
        },
    ],
)
def test_coordinator_complete_rejects_tampered_gateway_request_chain(
    tmp_path: Path,
    capsys,
    changes: dict[str, str],
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    _record_gateway_delivery(state, draft)
    task_id = "019fa801-52ca-7460-954d-30aee7053618"
    _prepared, attempt, created = reserve_codex_task_summary(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        reserved_at=NOW,
    )
    assert created is True
    _tamper_and_reseal(
        next(state.glob("gateway-discord-request-*.json")),
        changes,
    )

    result = cli.main([
        "coordinator-complete",
        "--version",
        "2.3.2",
        "--release-sha",
        RELEASE_SHA,
        "--state-dir",
        str(state),
        "--task-id",
        task_id,
        "--message-ref",
        "assistant-release-summary",
        "--summary-sha256",
        draft["summary_sha256"],
        "--attempt-receipt-sha256",
        attempt["receipt_sha256"],
    ])

    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "muncho_release_completion_binding_invalid"
    )
    assert not tuple(state.glob("completion-*.json"))


def test_status_and_health_reject_wrong_sha_receipt_under_expected_filename(
    tmp_path: Path,
):
    state, _mapping, _smoke, _summary = _draft(tmp_path / "expected")
    other_state, _other_mapping, _other_smoke, _other_summary = _draft(
        tmp_path / "other",
        release_sha="b" * 40,
    )
    expected_smoke_path = next(state.glob("smoke-*.json"))
    wrong_smoke_path = next(other_state.glob("smoke-*.json"))
    expected_smoke_path.write_bytes(wrong_smoke_path.read_bytes())

    for projection in (release_status, release_health):
        with pytest.raises(
            ReleaseCompletionError,
            match="muncho_release_status_identity_mismatch",
        ):
            projection(
                state,
                version="2.3.2",
                release_sha=RELEASE_SHA,
            )


def test_direct_discord_attempt_cannot_create_a_delivery_receipt(
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

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_state_record_missing",
    ):
        record_reserved_summary_delivery(
            state,
            draft,
            attempt,
            message_ref="423456789012345678",
            published_at=NOW,
        )
    assert not tuple(state.glob("summary-discord-delivery-*.json"))


def test_gateway_queue_reconciles_with_exact_message_id_and_no_duplicate(
    tmp_path: Path,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_discord_delivery_reconciliation_required",
    ):
        deliver_discord_via_gateway_once(
            state,
            draft,
            timeout_seconds=0,
        )

    requests = pending_gateway_discord_deliveries(state)
    assert len(requests) == 1
    request = requests[0]
    assert request["summary"] == draft["summary"]
    assert request["summary_sha256"] == draft["summary_sha256"]
    assert request["channel_id"] == CHANNEL_ID

    delivery = record_gateway_discord_delivery(
        state,
        request,
        message_id="423456789012345678",
        published_at=NOW,
    )
    assert pending_gateway_discord_deliveries(state) == ()
    assert (
        deliver_discord_via_gateway_once(
            state,
            draft,
            timeout_seconds=0,
        )
        == delivery
    )


def test_gateway_queue_refuses_to_adopt_an_uncertain_non_gateway_attempt(
    tmp_path: Path,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    _attempt, created = reserve_summary_delivery(
        state,
        draft,
        kind="discord",
        destination_ref=CHANNEL_ID,
        reserved_at=NOW,
    )
    assert created is True

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_discord_delivery_reconciliation_required",
    ):
        deliver_discord_via_gateway_once(
            state,
            draft,
            timeout_seconds=0,
        )
    assert pending_gateway_discord_deliveries(state) == ()


def test_coordinator_supported_workflow_reserves_records_and_finalizes(
    tmp_path: Path,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    discord = _record_gateway_delivery(state, draft)
    task_id = "019fa801-52ca-7460-954d-30aee7053618"
    prepared, attempt, created = reserve_codex_task_summary(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        reserved_at=NOW,
    )
    assert created is True
    assert prepared["summary"] == draft["summary"]
    assert attempt["network_send_authorized"] is False
    assert attempt["summary_sha256"] == discord["summary_sha256"]

    # A coordinator crash after reservation cannot silently claim publication
    # and cannot create a second attempt on replay.
    replay_draft, replay_attempt, replay_created = reserve_codex_task_summary(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        reserved_at=LATER,
    )
    assert replay_draft == prepared
    assert replay_attempt == attempt
    assert replay_created is False
    assert release_health(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )["healthy"] is False

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_codex_summary_mismatch",
    ):
        record_codex_task_summary_and_finalize(
            state,
            version="2.3.2",
            release_sha=RELEASE_SHA,
            task_id=task_id,
            message_ref="assistant-release-summary",
            summary_sha256="f" * 64,
            attempt_receipt_sha256=attempt["receipt_sha256"],
        )

    codex, completion = record_codex_task_summary_and_finalize(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        message_ref="assistant-release-summary",
        summary_sha256=draft["summary_sha256"],
        attempt_receipt_sha256=attempt["receipt_sha256"],
        published_at=NOW,
        completed_at=NOW,
    )
    assert codex["summary_sha256"] == discord["summary_sha256"]
    assert completion["required_summaries_published"] is True
    assert release_health(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )["healthy"] is True

    assert record_codex_task_summary_and_finalize(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        message_ref="assistant-release-summary",
        summary_sha256=draft["summary_sha256"],
        attempt_receipt_sha256=attempt["receipt_sha256"],
        published_at=LATER,
        completed_at=LATER,
    ) == (codex, completion)


def test_coordinator_crash_after_record_replays_into_one_completion(
    tmp_path: Path,
    monkeypatch,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    _record_gateway_delivery(state, draft)
    task_id = "019fa801-52ca-7460-954d-30aee7053618"
    _prepared, attempt, _created = reserve_codex_task_summary(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
    )

    import ops.muncho.release.completion as completion_module

    real_finalize = completion_module.finalize_release_completion

    def crash_after_codex_receipt(*_args, **_kwargs):
        raise RuntimeError("crash after Codex receipt")

    monkeypatch.setattr(
        completion_module,
        "finalize_release_completion",
        crash_after_codex_receipt,
    )
    with pytest.raises(RuntimeError, match="crash after Codex receipt"):
        record_codex_task_summary_and_finalize(
            state,
            version="2.3.2",
            release_sha=RELEASE_SHA,
            task_id=task_id,
            message_ref="assistant-release-summary",
            summary_sha256=draft["summary_sha256"],
            attempt_receipt_sha256=attempt["receipt_sha256"],
        )
    status = release_status(state, version="2.3.2", release_sha=RELEASE_SHA)
    assert status["codex_task_summary_published"] is True
    assert status["complete"] is False

    monkeypatch.setattr(
        completion_module,
        "finalize_release_completion",
        real_finalize,
    )
    _codex, completion = record_codex_task_summary_and_finalize(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
        task_id=task_id,
        message_ref="assistant-release-summary",
        summary_sha256=draft["summary_sha256"],
        attempt_receipt_sha256=attempt["receipt_sha256"],
    )
    assert completion["required_summaries_published"] is True
    assert release_health(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )["healthy"] is True


def test_coordinator_cli_never_claims_delivery_before_explicit_ack(
    tmp_path: Path,
    capsys,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    _record_gateway_delivery(state, draft)
    task_id = "019fa801-52ca-7460-954d-30aee7053618"
    prepare_args = [
        "coordinator-prepare",
        "--version",
        "2.3.2",
        "--release-sha",
        RELEASE_SHA,
        "--state-dir",
        str(state),
        "--task-id",
        task_id,
    ]
    assert cli.main(prepare_args) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["summary"] == draft["summary"]
    assert prepared["delivery_state"] == "reserved"
    assert prepared["release_completion"] == "codex_task_summary_pending"
    assert release_health(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )["healthy"] is False

    assert cli.main(prepare_args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["delivery_state"] == "reconciliation_required"
    assert replay["attempt_receipt_sha256"] == prepared["attempt_receipt_sha256"]

    complete_args = [
        "coordinator-complete",
        "--version",
        "2.3.2",
        "--release-sha",
        RELEASE_SHA,
        "--state-dir",
        str(state),
        "--task-id",
        task_id,
        "--message-ref",
        "assistant-release-summary",
        "--summary-sha256",
        prepared["summary_sha256"],
        "--attempt-receipt-sha256",
        prepared["attempt_receipt_sha256"],
    ]
    assert cli.main(complete_args) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["release_completion"] == "complete"
    assert completed["healthy"] is True


def test_coordinator_cli_rejects_release_records_copied_under_version_alias(
    tmp_path: Path,
    capsys,
):
    state, _mapping, _smoke, draft = _draft(tmp_path)
    _record_gateway_delivery(state, draft)
    source_version = "2.3.2"
    alias_version = "9.9.9"
    source_suffix = (
        f"v{source_version}-"
        f"{release_idempotency_key(source_version, RELEASE_SHA)[:20]}"
    )
    alias_suffix = (
        f"v{alias_version}-"
        f"{release_idempotency_key(alias_version, RELEASE_SHA)[:20]}"
    )
    alias_mapping = state / f"mapping-v{alias_version}.json"
    alias_mapping.write_bytes(
        (state / f"mapping-v{source_version}.json").read_bytes()
    )
    alias_mapping.chmod(0o600)
    for source in state.glob(f"*{source_suffix}.json"):
        alias = state / source.name.replace(source_suffix, alias_suffix)
        alias.write_bytes(source.read_bytes())
        alias.chmod(0o600)

    task_id = "019fa801-52ca-7460-954d-30aee7053618"
    common = [
        "--version",
        alias_version,
        "--release-sha",
        RELEASE_SHA,
        "--state-dir",
        str(state),
        "--task-id",
        task_id,
    ]
    assert cli.main(["coordinator-prepare", *common]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "muncho_release_completion_binding_invalid"
    )

    _prepared, attempt, created = reserve_codex_task_summary(
        state,
        version=source_version,
        release_sha=RELEASE_SHA,
        task_id=task_id,
        reserved_at=NOW,
    )
    assert created is True
    assert cli.main([
        "coordinator-complete",
        *common,
        "--message-ref",
        "assistant-release-summary",
        "--summary-sha256",
        draft["summary_sha256"],
        "--attempt-receipt-sha256",
        attempt["receipt_sha256"],
    ]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "muncho_release_completion_binding_invalid"
    )
    assert not tuple(state.glob("completion-*.json"))


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


def test_announcement_cli_refuses_release_without_durable_restart_attestation(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    release_sha = resolve_exact_release_sha(ROOT)
    assert release_sha is not None
    state = _state(tmp_path)
    reserve_release_mapping(
        state,
        load_release_bundle(ROOT),
        version="2.3.2",
        release_sha=release_sha,
    )
    monkeypatch.setattr(cli, "load_current_production_config", lambda _path: _config())
    delivered = False

    def impossible_delivery(_state, _draft):
        nonlocal delivered
        delivered = True
        raise AssertionError("announcement must remain unreachable")

    monkeypatch.setattr(cli, "deliver_discord_via_gateway_once", impossible_delivery)
    result = cli.main([
        "announce-after-smoke",
        "--release-root",
        str(ROOT),
        "--release-sha",
        release_sha,
        "--state-dir",
        str(state),
        "--production-config",
        str(tmp_path / "config.yaml"),
        "--check",
        "Gateway is active.",
    ])
    failure = json.loads(capsys.readouterr().out)
    assert result == 2
    assert failure["error"] == "muncho_release_restart_attestation_required"
    assert delivered is False


def test_automatic_announcement_cli_requires_exact_identity_and_sends_once(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    release_sha = resolve_exact_release_sha(ROOT)
    assert release_sha is not None
    sent: list[tuple[str, str]] = []

    monkeypatch.setattr(cli, "load_current_production_config", lambda _path: _config())

    def deliver(state_dir, draft):
        return _record_gateway_delivery(state_dir, draft, sent=sent)

    monkeypatch.setattr(cli, "deliver_discord_via_gateway_once", deliver)
    state = _state(tmp_path)
    bundle = load_release_bundle(ROOT)
    mapping = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=release_sha,
    )
    prepare_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        before_invocation_id=BEFORE_INVOCATION_ID,
    )
    complete_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        after_invocation_id=AFTER_INVOCATION_ID,
    )
    arguments = [
        "announce-after-smoke",
        "--release-root",
        str(ROOT),
        "--release-sha",
        release_sha,
        "--state-dir",
        str(state),
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
