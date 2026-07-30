from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from cron import jobs as cron_jobs
from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_upstream_sync as owner_gate
from scripts.canary import upstream_sync_rail_cutover as cutover
from scripts.canary.passkey_v2_signer import ReceiptSigner


ROOT = Path(__file__).parents[3]
REVISION = "a" * 40
UPSTREAM_CANDIDATE = "b" * 40
UPSTREAM_TAIL = "c" * 40
FORK_BEFORE = "d" * 40
FORK_AFTER = "e" * 40
AUTHORIZATION_NOW = 2_000_000_000


def _write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _write_canonical(
    path: Path,
    value: dict[str, Any],
    *,
    mode: int = 0o444,
) -> None:
    _write(path, cutover._canonical(value) + b"\n", mode=mode)


def _legacy_job() -> dict[str, Any]:
    return {
        "id": cutover.LEGACY_CRON_JOB_ID,
        "name": "Fork upstream daily sync monitor",
        "prompt": "review the exact upstream sync observation",
        "skills": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "no_agent": False,
        "context_from": None,
        "schedule": {
            "kind": "interval",
            "minutes": 1440,
            "display": "every 24h",
        },
        "schedule_display": "every 24h",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": "2026-07-01T00:00:00+00:00",
        "next_run_at": "2020-01-01T00:00:00+00:00",
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "last_delivery_status": "none",
        "last_delivery_confirmed_at": None,
        "deliver": "origin",
        "origin": None,
        "enabled_toolsets": None,
        "workdir": None,
        "fire_claim": None,
        "run_claim": None,
    }


def _jobs_store(job: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "jobs": [copy.deepcopy(job or _legacy_job())],
        "updated_at": "2026-07-30T06:30:24+00:00",
    }


def _owner_authorization(
    plan: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    action = owner_gate.build_upstream_sync_action_envelope(
        activation_plan=plan,
        authorization_nonce_sha256="4" * 64,
        authority_manifest_sha256="5" * 64,
        authority_host_receipt_sha256="6" * 64,
        external_iam_receipt_sha256="7" * 64,
        prior_authoritative_receipt_sha256="8" * 64,
        prior_event_head_sha256=protocol.GENESIS_JOURNAL_HEAD_SHA256,
        issued_at_unix=AUTHORIZATION_NOW,
    )
    challenge = protocol.build_challenge_record(
        envelope=action,
        challenge_id="C" * 32,
        challenge_b64url=base64.urlsafe_b64encode(b"x" * 32)
        .rstrip(b"=")
        .decode("ascii"),
        rp_id=protocol.PRODUCTION_RP_ID,
        origin=protocol.PRODUCTION_ORIGIN,
        created_at_unix=AUTHORIZATION_NOW + 1,
    )
    grant = protocol.build_passkey_grant(
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        approver_discord_user_id=owner_gate.OWNER_DISCORD_USER_ID,
        credential_id_sha256="9" * 64,
        credential_record_sha256="a" * 64,
        credential_migration_receipt_sha256="b" * 64,
        assertion_verification_sha256="c" * 64,
        credential_sign_count=3,
        credential_backed_up=True,
        granted_at_unix=AUTHORIZATION_NOW + 2,
    )
    runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["activation_plan_sha256"],
        executor_binary_sha256=plan["activation_runtime_sha256"],
        mutation_wrapper_sha256=plan["package_manifest_sha256"],
        remote_transport_sha256="d" * 64,
    )
    signer = ReceiptSigner(Ed25519PrivateKey.generate())
    receipt = signer.sign(
        protocol.build_authorization_receipt_unsigned(
            envelope=action,
            grant=grant,
            challenge=challenge,
            runtime_binding=runtime,
            consume_attempt_id="e" * 64,
            consumed_at_unix=AUTHORIZATION_NOW + 3,
            prior_journal_head_sha256="f" * 64,
            receipt_public_key_id=signer.key_id,
        )
    )
    bundle = owner_gate.build_authorization_bundle(
        activation_plan=plan,
        action_envelope=action,
        challenge_record=challenge,
        grant_record=grant,
        authorization_receipt=receipt,
        receipt_public_key=signer.public_key,
    )
    pem = signer.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return dict(bundle), pem


class FakeSystemd:
    def __init__(
        self,
        *,
        systemd_root: Path,
        package: cutover.PackageContext,
        legacy: cutover.TimerObservation,
    ) -> None:
        self.systemd_root = systemd_root
        self.package = package
        self.states = {
            cutover.LEGACY_COLLECTOR_TIMER_UNIT: legacy,
            **{
                name: cutover.TimerObservation(
                    unit=name,
                    fragment_path=None,
                    fragment_sha256=None,
                    loaded=False,
                    enabled=False,
                    active=False,
                )
                for name in cutover.TIMER_NAMES
            },
        }
        self.calls: list[tuple[str, ...]] = []
        self.fail_old_disable = False
        self.leave_inactive_on_enable: str | None = None

    def observe(self, unit: str) -> cutover.TimerObservation:
        return self.states[unit]

    def _reload(self) -> None:
        for name in cutover.TIMER_NAMES:
            path = self.systemd_root / name
            previous = self.states[name]
            if path.exists():
                self.states[name] = cutover.TimerObservation(
                    unit=name,
                    fragment_path=str(path),
                    fragment_sha256=hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest(),
                    loaded=True,
                    enabled=previous.enabled,
                    active=previous.active,
                )
            else:
                self.states[name] = cutover.TimerObservation(
                    unit=name,
                    fragment_path=None,
                    fragment_sha256=None,
                    loaded=False,
                    enabled=False,
                    active=False,
                )

    def mutate(self, *arguments: str) -> None:
        self.calls.append(tuple(arguments))
        if arguments == ("daemon-reload",):
            self._reload()
            return
        if len(arguments) == 3 and arguments[:2] == ("enable", "--now"):
            name = arguments[2]
            current = self.states[name]
            self.states[name] = cutover.TimerObservation(
                unit=name,
                fragment_path=current.fragment_path,
                fragment_sha256=current.fragment_sha256,
                loaded=True,
                enabled=True,
                active=name != self.leave_inactive_on_enable,
            )
            return
        if len(arguments) == 3 and arguments[:2] == ("disable", "--now"):
            name = arguments[2]
            if (
                name == cutover.LEGACY_COLLECTOR_TIMER_UNIT
                and self.fail_old_disable
            ):
                raise cutover.UpstreamSyncRailCutoverError(
                    "injected_legacy_timer_disable_failure"
                )
            current = self.states[name]
            self.states[name] = cutover.TimerObservation(
                unit=name,
                fragment_path=current.fragment_path,
                fragment_sha256=current.fragment_sha256,
                loaded=current.loaded,
                enabled=False,
                active=False,
            )
            return
        if len(arguments) == 2 and arguments[0] == "enable":
            name = arguments[1]
            current = self.states[name]
            self.states[name] = cutover.TimerObservation(
                unit=name,
                fragment_path=current.fragment_path,
                fragment_sha256=current.fragment_sha256,
                loaded=current.loaded,
                enabled=True,
                active=False,
            )
            return
        raise AssertionError(f"unexpected systemctl mutation: {arguments!r}")


@dataclass
class Harness:
    staged_root: Path
    authority_path: Path
    catch_up_path: Path
    owner_path: Path
    owner_public_key_path: Path
    preflight_path: Path
    runtime_path: Path
    jobs_path: Path
    candidate_state_paths: tuple[Path, ...]
    systemd_root: Path
    evidence_root: Path
    package: cutover.PackageContext
    authority: dict[str, Any]
    preflight: dict[str, Any]
    controller: FakeSystemd

    def activate(
        self,
        *,
        authorization_now_unix: int = AUTHORIZATION_NOW + 4,
    ) -> dict[str, Any]:
        return cutover.activate(
            expected_authority_sha256=self.authority["authority_sha256"],
            expected_preflight_sha256=self.preflight["receipt_sha256"],
            staged_root=self.staged_root,
            authority_path=self.authority_path,
            catch_up_path=self.catch_up_path,
            owner_authorization_path=self.owner_path,
            owner_receipt_public_key_path=self.owner_public_key_path,
            preflight_path=self.preflight_path,
            runtime_path=self.runtime_path,
            jobs_path=self.jobs_path,
            systemd_root=self.systemd_root,
            evidence_root=self.evidence_root,
            root_owned=False,
            require_root=False,
            authorization_now_unix=authorization_now_unix,
            timer_observer=self.controller.observe,
            activation_lock_factory=lambda: nullcontext(),
            candidate_state_paths=self.candidate_state_paths,
        )

    def rollback(self) -> dict[str, Any]:
        return cutover.rollback_inert(
            expected_authority_sha256=self.authority["authority_sha256"],
            expected_preflight_sha256=self.preflight["receipt_sha256"],
            staged_root=self.staged_root,
            authority_path=self.authority_path,
            catch_up_path=self.catch_up_path,
            owner_authorization_path=self.owner_path,
            owner_receipt_public_key_path=self.owner_public_key_path,
            preflight_path=self.preflight_path,
            runtime_path=self.runtime_path,
            jobs_path=self.jobs_path,
            systemd_root=self.systemd_root,
            evidence_root=self.evidence_root,
            root_owned=False,
            require_root=False,
            authorization_now_unix=AUTHORIZATION_NOW + 4,
            timer_observer=self.controller.observe,
            activation_lock_factory=lambda: nullcontext(),
        )


def _build_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    preexisting_units: tuple[str, ...] = (),
) -> Harness:
    releases = tmp_path / "releases"
    monkeypatch.setattr(rail, "RELEASES_ROOT", releases)
    release = rail.release_root(REVISION)
    for relative in (
        rail.RAIL_RELATIVE,
        rail.MUNCHO_ROUTINE_RELATIVE,
        rail.HARDENING_RELATIVE,
        rail.SKYAI_ROUTINE_RELATIVE,
        rail.REPORTER_RELATIVE,
    ):
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    _write(
        release / rail.SOURCE_MARKER_RELATIVE,
        rail.exact_revision_marker(REVISION),
        mode=0o444,
    )
    _write(
        release / ".venv/bin/python",
        b"reviewed-python-placeholder\n",
        mode=0o755,
    )
    monkeypatch.setattr(rail, "validate_credential_metadata", lambda: None)
    monkeypatch.setattr(
        rail,
        "host_binary_fact",
        lambda path: "1" * 64 if path == rail.GH_PATH else "2" * 64,
    )
    built = rail.build_package(REVISION, REVISION)
    staged_root = tmp_path / "staged"
    rail.stage_package(built, output_root=staged_root)
    package = cutover._validate_package_context(
        staged_root=staged_root,
        release_revision=REVISION,
        sender_revision=REVISION,
        expected_manifest_sha256=built.manifest_sha256,
        root_owned=False,
        staged_trust_root=staged_root,
        release_trust_root=releases,
    )

    systemd_root = tmp_path / "systemd"
    systemd_root.mkdir()
    legacy_path = systemd_root / cutover.LEGACY_COLLECTOR_TIMER_UNIT
    _write(
        legacy_path,
        b"[Timer]\nOnCalendar=daily\n",
        mode=0o644,
    )
    legacy = cutover.TimerObservation(
        unit=cutover.LEGACY_COLLECTOR_TIMER_UNIT,
        fragment_path=str(
            cutover.SYSTEMD_ROOT
            / cutover.LEGACY_COLLECTOR_TIMER_UNIT
        ),
        fragment_sha256=hashlib.sha256(
            legacy_path.read_bytes()
        ).hexdigest(),
        loaded=True,
        enabled=True,
        active=True,
    )
    for name in preexisting_units:
        _write(systemd_root / name, package.artifacts[name], mode=0o644)

    controller = FakeSystemd(
        systemd_root=systemd_root,
        package=package,
        legacy=legacy,
    )
    controller._reload()
    monkeypatch.setattr(cutover, "_systemctl_mutate", controller.mutate)

    runtime_path = ROOT / cutover.CUTOVER_RUNTIME_RELATIVE
    catch_up = cutover.build_first_catch_up_receipt(
        candidate_upstream_sha=UPSTREAM_CANDIDATE,
        fork_main_before_sha=FORK_BEFORE,
        fork_main_after_sha=FORK_AFTER,
        observed_upstream_tail_sha=UPSTREAM_TAIL,
        release_checks_sha256="3" * 64,
        completed_at="2026-07-30T08:00:00Z",
    )
    catch_up_path = staged_root / "first-catch-up-receipt.json"
    _write_canonical(catch_up_path, catch_up)
    store = _jobs_store()
    jobs_path = tmp_path / "hermes-home/cron/jobs.json"
    _write(
        jobs_path,
        json.dumps(store, indent=2).encode("utf-8") + b"\n",
        mode=0o600,
    )
    activation_runtime_sha256 = hashlib.sha256(
        runtime_path.read_bytes()
    ).hexdigest()
    plan = owner_gate.build_activation_plan(
        release_revision=package.manifest["release_revision"],
        sender_revision=package.manifest["sender_revision"],
        package_manifest_sha256=package.manifest["manifest_sha256"],
        activation_runtime_sha256=activation_runtime_sha256,
        first_catch_up_receipt_sha256=catch_up["receipt_sha256"],
        candidate_upstream_sha=catch_up["candidate_upstream_sha"],
        fork_main_after_sha=catch_up["fork_main_after_sha"],
        unit_digests=package.manifest["artifacts"],
        legacy_cron_source_definition_sha256=(
            cutover._static_definition_sha256(_legacy_job())
        ),
        legacy_cron_retired_definition_sha256=(
            cutover._static_definition_sha256(
                cutover._retired_job(_legacy_job())
            )
        ),
        legacy_collector_timer_prestate=legacy.state,
        legacy_collector_timer_fragment_path=legacy.fragment_path,
        legacy_collector_timer_fragment_sha256=(
            legacy.fragment_sha256
        ),
    )
    owner_authorization, owner_public_key_pem = (
        _owner_authorization(dict(plan))
    )
    owner_path = staged_root / "owner-authorization.receipt"
    _write_canonical(owner_path, owner_authorization)
    owner_public_key_path = tmp_path / "owner-gate/authority-receipt.pem"
    _write(owner_public_key_path, owner_public_key_pem, mode=0o444)
    authority = cutover.build_activation_authority(
        package=package,
        first_catch_up_receipt=catch_up,
        owner_authorization=owner_authorization,
        owner_receipt_public_key_pem=owner_public_key_pem,
        authorization_now_unix=AUTHORIZATION_NOW + 4,
        jobs_store=store,
        legacy_timer=legacy,
        activation_runtime_sha256=activation_runtime_sha256,
    )
    authority_path = staged_root / "activation-authority.json"
    _write_canonical(authority_path, authority)
    candidate_state_paths = (
        tmp_path / "candidate-state/legacy/auto-sync-pr-state.json",
        tmp_path / "candidate-state/muncho/auto-sync-pr-state.json",
        tmp_path / "candidate-state/skyai/skyai-sync-candidate-state.json",
    )
    preflight = cutover.preflight(
        expected_authority_sha256=authority["authority_sha256"],
        staged_root=staged_root,
        authority_path=authority_path,
        catch_up_path=catch_up_path,
        owner_authorization_path=owner_path,
        owner_receipt_public_key_path=owner_public_key_path,
        runtime_path=runtime_path,
        jobs_path=jobs_path,
        systemd_root=systemd_root,
        root_owned=False,
        authorization_now_unix=AUTHORIZATION_NOW + 4,
        timer_observer=controller.observe,
        candidate_state_paths=candidate_state_paths,
    )
    preflight_path = staged_root / "activation-preflight.json"
    _write_canonical(preflight_path, preflight)
    return Harness(
        staged_root=staged_root,
        authority_path=authority_path,
        catch_up_path=catch_up_path,
        owner_path=owner_path,
        owner_public_key_path=owner_public_key_path,
        preflight_path=preflight_path,
        runtime_path=runtime_path,
        jobs_path=jobs_path,
        candidate_state_paths=candidate_state_paths,
        systemd_root=systemd_root,
        evidence_root=tmp_path / "evidence",
        package=package,
        authority=authority,
        preflight=preflight,
        controller=controller,
    )


def _stored_job(harness: Harness) -> dict[str, Any]:
    value = json.loads(harness.jobs_path.read_text(encoding="utf-8"))
    return next(
        item
        for item in value["jobs"]
        if item["id"] == cutover.LEGACY_CRON_JOB_ID
    )


def _evidence_file(harness: Harness, name: str) -> Path:
    return cutover._evidence_path(
        harness.authority["authority_sha256"],
        name,
        evidence_root=harness.evidence_root,
    )


def test_first_catch_up_freezes_candidate_and_treats_later_tail_as_drift() -> None:
    receipt = cutover.build_first_catch_up_receipt(
        candidate_upstream_sha=UPSTREAM_CANDIDATE,
        fork_main_before_sha=FORK_BEFORE,
        fork_main_after_sha=FORK_AFTER,
        observed_upstream_tail_sha=UPSTREAM_TAIL,
        release_checks_sha256="3" * 64,
        completed_at="2026-07-30T08:00:00Z",
    )
    assert receipt["candidate_upstream_sha"] == UPSTREAM_CANDIDATE
    assert receipt["observed_upstream_tail_sha"] == UPSTREAM_TAIL
    assert receipt["tail_drift_rebinds_candidate"] is False

    tampered = dict(receipt)
    tampered["tail_drift_rebinds_candidate"] = True
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = hashlib.sha256(
        cutover._canonical(unsigned)
    ).hexdigest()
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="first_catch_up_invalid",
    ):
        cutover.validate_first_catch_up_receipt(
            tampered,
            expected_sha256=tampered["receipt_sha256"],
        )


@pytest.mark.parametrize("authority_kind", ("pointer", "ledger"))
def test_candidate_state_blocks_activation_before_timer_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_kind: str,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    selected = harness.candidate_state_paths[0]
    if authority_kind == "ledger":
        selected = selected.with_name(f"{selected.name}.ledger")
        selected.mkdir(parents=True)
    else:
        _write(selected, b"legacy-candidate-state\n", mode=0o600)
    harness.controller.calls.clear()

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="candidate_state_requires_reconciliation",
    ):
        harness.activate()

    assert harness.controller.calls == []
    assert not _evidence_file(harness, "activation-started.json").exists()


def test_expired_authorization_cannot_start_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.controller.calls.clear()

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="owner_authorization_invalid",
    ):
        harness.activate(
            authorization_now_unix=AUTHORIZATION_NOW + 100_000
        )

    assert not _evidence_file(
        harness, "activation-started.json"
    ).exists()
    assert harness.controller.calls == []
    assert all(
        not (harness.systemd_root / name).exists()
        for name in cutover.UNIT_NAMES
    )
    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"


def test_activation_started_receipt_recovers_after_expiry_before_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_install = cutover._install_units

    def fail_install(*_args: Any, **_kwargs: Any) -> None:
        raise cutover.UpstreamSyncRailCutoverError(
            "injected_install_failure"
        )

    monkeypatch.setattr(cutover, "_install_units", fail_install)
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_install_failure",
    ):
        harness.activate()

    started_path = _evidence_file(
        harness, "activation-started.json"
    )
    assert started_path.is_file()
    assert started_path.stat().st_mode & 0o777 == 0o600
    assert all(
        not (harness.systemd_root / name).exists()
        for name in cutover.UNIT_NAMES
    )
    assert harness.controller.calls == []
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="forward_recovery_only",
    ):
        harness.rollback()

    monkeypatch.setattr(cutover, "_install_units", real_install)
    terminal = harness.activate(
        authorization_now_unix=AUTHORIZATION_NOW + 100_000
    )
    started = json.loads(started_path.read_text(encoding="ascii"))
    timers = json.loads(
        _evidence_file(
            harness, "timers-active.json"
        ).read_text(encoding="ascii")
    )
    assert (
        timers["activation_started_receipt_sha256"]
        == started["receipt_sha256"]
    )
    assert (
        terminal["activation_started_receipt_sha256"]
        == started["receipt_sha256"]
    )
    assert terminal["legacy_scheduler_claimable"] is False


def test_activation_started_receipt_recovers_after_first_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_mutate = cutover._systemctl_mutate

    def fail_before_second_timer(*arguments: str) -> None:
        if arguments == (
            "enable",
            "--now",
            rail.REPORT_TIMER_UNIT,
        ):
            raise cutover.UpstreamSyncRailCutoverError(
                "injected_second_timer_failure"
            )
        real_mutate(*arguments)

    monkeypatch.setattr(
        cutover,
        "_systemctl_mutate",
        fail_before_second_timer,
    )
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_second_timer_failure",
    ):
        harness.activate()

    assert _evidence_file(
        harness, "activation-started.json"
    ).is_file()
    assert not _evidence_file(
        harness, "timers-active.json"
    ).exists()
    assert harness.controller.observe(rail.SYNC_TIMER_UNIT).active
    assert not harness.controller.observe(rail.REPORT_TIMER_UNIT).active
    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"

    monkeypatch.setattr(cutover, "_systemctl_mutate", real_mutate)
    terminal = harness.activate(
        authorization_now_unix=AUTHORIZATION_NOW + 100_000
    )
    assert terminal["new_timers_active"] is True
    assert terminal["legacy_scheduler_claimable"] is False


def test_activation_started_receipt_recovers_after_both_timers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_prove = cutover._prove_new_timers_active

    def fail_after_both_timers(
        *_args: Any,
        **_kwargs: Any,
    ) -> list[cutover.TimerObservation]:
        raise cutover.UpstreamSyncRailCutoverError(
            "injected_post_timer_failure"
        )

    monkeypatch.setattr(
        cutover,
        "_prove_new_timers_active",
        fail_after_both_timers,
    )
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_post_timer_failure",
    ):
        harness.activate()

    assert all(
        harness.controller.observe(name).active
        for name in cutover.TIMER_NAMES
    )
    assert _evidence_file(
        harness, "activation-started.json"
    ).is_file()
    assert not _evidence_file(
        harness, "timers-active.json"
    ).exists()
    assert _stored_job(harness)["enabled"] is True

    monkeypatch.setattr(
        cutover,
        "_prove_new_timers_active",
        real_prove,
    )
    terminal = harness.activate(
        authorization_now_unix=AUTHORIZATION_NOW + 100_000
    )
    assert terminal["new_timers_active"] is True
    assert terminal["legacy_scheduler_claimable"] is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("forward_recovery_only", False),
        ("preflight_receipt_sha256", "0" * 64),
    ),
)
def test_activation_started_tamper_or_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: Any,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_install = cutover._install_units
    monkeypatch.setattr(
        cutover,
        "_install_units",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.UpstreamSyncRailCutoverError(
                "injected_install_failure"
            )
        ),
    )
    with pytest.raises(cutover.UpstreamSyncRailCutoverError):
        harness.activate()
    monkeypatch.setattr(cutover, "_install_units", real_install)

    started_path = _evidence_file(
        harness, "activation-started.json"
    )
    tampered = json.loads(started_path.read_text(encoding="ascii"))
    tampered[field] = replacement
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = hashlib.sha256(
        cutover._canonical(unsigned)
    ).hexdigest()
    _write_canonical(started_path, tampered, mode=0o600)
    harness.controller.calls.clear()

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="activation_started_receipt_invalid",
    ):
        harness.activate(
            authorization_now_unix=AUTHORIZATION_NOW + 100_000
        )

    assert harness.controller.calls == []
    assert all(
        not (harness.systemd_root / name).exists()
        for name in cutover.UNIT_NAMES
    )
    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"


def test_crash_before_cron_retirement_leaves_legacy_collector_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_retire = cutover._retire_legacy_cron
    monkeypatch.setattr(
        cutover,
        "_retire_legacy_cron",
        lambda **_kwargs: (_ for _ in ()).throw(
            cutover.UpstreamSyncRailCutoverError(
                "injected_cron_retirement_failure"
            )
        ),
    )

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_cron_retirement_failure",
    ):
        harness.activate()

    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"
    assert (
        "disable",
        "--now",
        cutover.LEGACY_COLLECTOR_TIMER_UNIT,
    ) not in harness.controller.calls
    timers_path = cutover._evidence_path(
        harness.authority["authority_sha256"],
        "timers-active.json",
        evidence_root=harness.evidence_root,
    )
    assert timers_path.is_file()

    monkeypatch.setattr(cutover, "_retire_legacy_cron", real_retire)
    terminal = harness.activate()
    assert terminal["legacy_scheduler_claimable"] is False
    assert _stored_job(harness)["state"] == "paused"


def test_timers_active_receipt_allows_forward_recovery_after_auth_window_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_retire = cutover._retire_legacy_cron
    monkeypatch.setattr(
        cutover,
        "_retire_legacy_cron",
        lambda **_kwargs: (_ for _ in ()).throw(
            cutover.UpstreamSyncRailCutoverError(
                "injected_cron_retirement_failure"
            )
        ),
    )
    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_cron_retirement_failure",
    ):
        harness.activate()

    timers_path = cutover._evidence_path(
        harness.authority["authority_sha256"],
        "timers-active.json",
        evidence_root=harness.evidence_root,
    )
    assert timers_path.is_file()

    monkeypatch.setattr(cutover, "_retire_legacy_cron", real_retire)
    terminal = harness.activate(
        authorization_now_unix=AUTHORIZATION_NOW + 100_000
    )
    assert terminal["legacy_scheduler_claimable"] is False
    assert _stored_job(harness)["state"] == "paused"


def test_crash_after_cron_retirement_retries_old_timer_disable_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.controller.fail_old_disable = True

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="injected_legacy_timer_disable_failure",
    ):
        harness.activate()

    retired = _stored_job(harness)
    assert retired["enabled"] is False
    assert retired["state"] == "paused"
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"

    harness.controller.fail_old_disable = False
    terminal = harness.activate()
    assert terminal["legacy_cron_state"] == "retired"
    assert terminal["legacy_collector_timer_state"] == "disabled_inactive"
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "disabled_inactive"


def test_no_legacy_retirement_occurs_until_both_new_timers_are_proven_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.controller.leave_inactive_on_enable = rail.REPORT_TIMER_UNIT

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="new_timer_activation_unconfirmed",
    ):
        harness.activate()

    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"
    timers_path = cutover._evidence_path(
        harness.authority["authority_sha256"],
        "timers-active.json",
        evidence_root=harness.evidence_root,
    )
    assert not timers_path.exists()


def test_retired_record_is_rejected_by_real_scheduler_claim_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.activate()

    hermes_home = harness.jobs_path.parent.parent
    with cron_jobs.use_cron_store(hermes_home):
        assert (
            cron_jobs.claim_job_for_fire(cutover.LEGACY_CRON_JOB_ID)
            is False
        )
        assert cutover.LEGACY_CRON_JOB_ID not in {
            item["id"] for item in cron_jobs.get_due_jobs()
        }


def test_inert_rollback_removes_only_units_absent_at_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preexisting = (rail.SYNC_SERVICE_UNIT,)
    harness = _build_harness(
        tmp_path,
        monkeypatch,
        preexisting_units=preexisting,
    )
    before = (harness.systemd_root / rail.SYNC_SERVICE_UNIT).read_bytes()
    cutover._install_units(
        harness.package,
        systemd_root=harness.systemd_root,
        root_owned=False,
    )

    receipt = harness.rollback()

    assert receipt["preserved_preexisting_unit_files"] == list(preexisting)
    assert (harness.systemd_root / rail.SYNC_SERVICE_UNIT).read_bytes() == before
    assert all(
        not (harness.systemd_root / name).exists()
        for name in cutover.UNIT_NAMES
        if name not in preexisting
    )
    assert _stored_job(harness)["enabled"] is True
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"


def test_rollback_is_forbidden_after_any_new_timer_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    cutover._install_units(
        harness.package,
        systemd_root=harness.systemd_root,
        root_owned=False,
    )
    harness.controller.mutate("enable", "--now", rail.SYNC_TIMER_UNIT)

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="forward_recovery_only",
    ):
        harness.rollback()

    assert all(
        (harness.systemd_root / name).is_file()
        for name in cutover.UNIT_NAMES
    )
    assert _stored_job(harness)["enabled"] is True


def test_existing_timers_active_receipt_is_validated_before_forward_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    real_retire = cutover._retire_legacy_cron
    monkeypatch.setattr(
        cutover,
        "_retire_legacy_cron",
        lambda **_kwargs: (_ for _ in ()).throw(
            cutover.UpstreamSyncRailCutoverError(
                "injected_cron_retirement_failure"
            )
        ),
    )
    with pytest.raises(cutover.UpstreamSyncRailCutoverError):
        harness.activate()
    monkeypatch.setattr(cutover, "_retire_legacy_cron", real_retire)

    timers_path = cutover._evidence_path(
        harness.authority["authority_sha256"],
        "timers-active.json",
        evidence_root=harness.evidence_root,
    )
    tampered = json.loads(timers_path.read_text(encoding="ascii"))
    tampered["forward_recovery_only"] = False
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = hashlib.sha256(
        cutover._canonical(unsigned)
    ).hexdigest()
    _write_canonical(timers_path, tampered, mode=0o600)

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="timers_active_receipt_invalid",
    ):
        harness.activate()
    assert _stored_job(harness)["enabled"] is True


def test_untrusted_staged_parent_mode_blocks_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.controller.calls.clear()
    harness.staged_root.chmod(0o777)

    with pytest.raises(
        cutover.UpstreamSyncRailCutoverError,
        match="parent_chain_untrusted",
    ):
        harness.activate()

    assert harness.controller.calls == []
    assert _stored_job(harness)["enabled"] is True
    assert all(
        not harness.controller.observe(name).active
        for name in cutover.TIMER_NAMES
    )
    assert harness.controller.observe(
        cutover.LEGACY_COLLECTOR_TIMER_UNIT
    ).state == "enabled_active"
