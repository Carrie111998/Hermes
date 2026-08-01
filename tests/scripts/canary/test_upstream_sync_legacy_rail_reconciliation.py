from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import upstream_sync_legacy_rail_reconciliation as legacy
from scripts.canary import upstream_sync_rail_cutover as cutover


REVISION = "9" * 40
SENDER_REVISION = "8" * 40


def _write(path: Path, raw: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _legacy_job() -> dict[str, Any]:
    return {
        "id": cutover.LEGACY_CRON_JOB_ID,
        "name": "Fork upstream daily sync monitor",
        "prompt": "arbitrary prose is not an authority",
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
        "next_run_at": "2026-07-31T06:30:00+00:00",
        "last_run_at": "2026-07-30T06:30:00+00:00",
        "last_status": "completed",
        "last_error": None,
        "last_delivery_error": None,
        "last_delivery_status": "delivered",
        "last_delivery_confirmed_at": "2026-07-30T06:31:00+00:00",
        "deliver": "origin",
        "origin": None,
        "enabled_toolsets": None,
        "workdir": None,
        "fire_claim": None,
        "run_claim": None,
    }


def _jobs_raw(job: dict[str, Any] | None = None) -> bytes:
    value = {
        "schema_version": 2,
        "jobs": [copy.deepcopy(job or _legacy_job())],
        "updated_at": "2026-07-30T06:30:24+00:00",
    }
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _pointer_raw(value: dict[str, Any] | None = None) -> bytes:
    selected = value or dict(legacy.EXACT_STALE_POINTER)
    return (
        json.dumps(selected, ensure_ascii=True, indent=2, sort_keys=True).encode(
            "ascii"
        )
        + b"\n"
    )


def _stage_package(
    staged_root: Path,
    systemd_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, bytes]:
    releases = staged_root.parent / "releases"
    monkeypatch.setattr(rail, "RELEASES_ROOT", releases)
    artifacts = {
        name: f"reviewed legacy bytes for {name}\n".encode("ascii")
        for name in legacy.UNIT_NAMES
    }
    jobs = [
        {
            "job_id": job_id,
            "argv": ["--execute"],
            "fork_repository": "lomliev/hermes-agent",
            "upstream_repository_read_only": "NousResearch/hermes-agent",
            "auto_merge_or_deploy_enabled": False,
        }
        for job_id in rail.JOB_IDS
    ]
    unsigned = {
        "schema": rail.MANIFEST_SCHEMA,
        "rail_schema": rail.RAIL_SCHEMA,
        "release_revision": REVISION,
        "release_root": str(rail.release_root(REVISION)),
        "sender_revision": SENDER_REVISION,
        "sender_release_root": str(rail.release_root(SENDER_REVISION)),
        "sender_interpreter_sha256": "a" * 64,
        "jobs": jobs,
        "source_digests": {},
        "host_binary_digests": {},
        "artifacts": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in artifacts.items()
        },
        "github_credential_path": "/root/.config/gh/hosts.yml",
        "github_credential_value_recorded": False,
        "sync_service_model_or_provider_dependency": False,
        "sync_service_discord_dependency": False,
        "reporter_github_credential_dependency": False,
        "package_installs_or_starts_units": False,
    }
    manifest = {
        **unsigned,
        "manifest_sha256": rail.sha256(rail.canonical(unsigned)),
    }
    _write(
        staged_root / "manifest.json",
        rail.canonical(manifest) + b"\n",
        mode=0o444,
    )
    for name, raw in artifacts.items():
        _write(staged_root / name, raw, mode=0o444)
        _write(systemd_root / name, raw, mode=0o644)
    return artifacts


class FakeSystemd:
    def __init__(
        self,
        *,
        systemd_root: Path,
        artifacts: dict[str, bytes],
    ) -> None:
        self.systemd_root = systemd_root
        self.artifacts = artifacts
        self.states = {
            name: legacy.UnitObservation(
                unit=name,
                loaded=True,
                active=name in legacy.TIMER_NAMES,
                unit_file_state=("enabled" if name in legacy.TIMER_NAMES else "static"),
                main_pid=0,
                fragment_path=str(systemd_root / name),
                fragment_sha256=hashlib.sha256(raw).hexdigest(),
            )
            for name, raw in artifacts.items()
        }
        self.calls: list[tuple[str, ...]] = []
        self.fail_after_first_disable = False

    def observe(self, name: str) -> legacy.UnitObservation:
        return self.states[name]

    def mutate(self, *arguments: str) -> None:
        self.calls.append(tuple(arguments))
        if arguments == ("daemon-reload",):
            for name in legacy.UNIT_NAMES:
                path = self.systemd_root / name
                if path.exists():
                    continue
                self.states[name] = legacy.UnitObservation.absent(name)
            return
        if (
            len(arguments) == 3
            and arguments[:2] == ("disable", "--now")
            and arguments[2] in legacy.TIMER_NAMES
        ):
            name = arguments[2]
            self.states[name] = legacy.UnitObservation(
                unit=name,
                loaded=True,
                active=False,
                unit_file_state="disabled",
                main_pid=0,
                fragment_path=str(self.systemd_root / name),
                fragment_sha256=hashlib.sha256(self.artifacts[name]).hexdigest(),
            )
            if (
                self.fail_after_first_disable
                and sum(call[:2] == ("disable", "--now") for call in self.calls) == 1
            ):
                raise legacy.LegacyRailReconciliationError("injected_systemd_failure")
            return
        raise AssertionError(f"unexpected systemctl mutation: {arguments!r}")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.staged_root = tmp_path / "staged"
        self.systemd_root = tmp_path / "systemd"
        self.jobs_path = tmp_path / "cron/jobs.json"
        self.pointer_path = tmp_path / "state/auto-sync-pr-state.json"
        self.plan_path = tmp_path / "plan.json"
        self.preflight_path = tmp_path / "preflight.json"
        self.evidence_root = tmp_path / "evidence"
        self.artifacts = _stage_package(
            self.staged_root,
            self.systemd_root,
            monkeypatch,
        )
        _write(self.jobs_path, _jobs_raw(), mode=0o600)
        _write(self.pointer_path, _pointer_raw(), mode=0o600)
        self.controller = FakeSystemd(
            systemd_root=self.systemd_root,
            artifacts=self.artifacts,
        )
        self.lock_states = {"authority": False, "cron": False}

        @contextmanager
        def cron_lock(_path: Path) -> Iterator[None]:
            assert self.lock_states["authority"] is True
            self.lock_states["cron"] = True
            try:
                yield
            finally:
                self.lock_states["cron"] = False

        monkeypatch.setattr(legacy, "_cron_jobs_lock", cron_lock)

    @contextmanager
    def authority_lock(self) -> Iterator[None]:
        self.lock_states["authority"] = True
        try:
            yield
        finally:
            assert self.lock_states["cron"] is False
            self.lock_states["authority"] = False

    def plan(self) -> dict[str, Any]:
        return legacy.build_plan(
            staged_root=self.staged_root,
            jobs_path=self.jobs_path,
            pointer_path=self.pointer_path,
            systemd_root=self.systemd_root,
            root_owned=False,
            require_root=False,
            unit_observer=self.controller.observe,
            activation_lock_factory=self.authority_lock,
        )

    def preflight(self, plan: dict[str, Any]) -> dict[str, Any]:
        _write(
            self.plan_path,
            legacy.canonical(plan) + b"\n",
            mode=0o600,
        )
        return legacy.preflight(
            expected_plan_sha256=plan["plan_sha256"],
            plan_path=self.plan_path,
            staged_root=self.staged_root,
            jobs_path=self.jobs_path,
            pointer_path=self.pointer_path,
            systemd_root=self.systemd_root,
            root_owned=False,
            require_root=False,
            unit_observer=self.controller.observe,
            activation_lock_factory=self.authority_lock,
        )

    def reconcile(
        self,
        plan: dict[str, Any],
        preflight: dict[str, Any],
    ) -> dict[str, Any]:
        _write(
            self.preflight_path,
            legacy.canonical(preflight) + b"\n",
            mode=0o600,
        )
        return legacy.reconcile(
            expected_plan_sha256=plan["plan_sha256"],
            expected_preflight_sha256=preflight["receipt_sha256"],
            plan_path=self.plan_path,
            preflight_path=self.preflight_path,
            staged_root=self.staged_root,
            jobs_path=self.jobs_path,
            pointer_path=self.pointer_path,
            systemd_root=self.systemd_root,
            evidence_root=self.evidence_root,
            root_owned=False,
            require_root=False,
            unit_observer=self.controller.observe,
            systemctl_mutator=self.controller.mutate,
            activation_lock_factory=self.authority_lock,
        )


def test_reconciliation_is_exact_evidence_backed_and_preserves_cron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    original_jobs = harness.jobs_path.read_bytes()

    plan = harness.plan()
    preflight = harness.preflight(plan)
    terminal = harness.reconcile(plan, preflight)

    assert terminal["schema"] == legacy.TERMINAL_SCHEMA
    assert terminal["legacy_cron_preserved_exactly"] is True
    assert harness.jobs_path.read_bytes() == original_jobs
    assert not harness.pointer_path.exists()
    assert not harness.pointer_path.with_name(
        f"{harness.pointer_path.name}.ledger"
    ).exists()
    assert all(not (harness.systemd_root / name).exists() for name in legacy.UNIT_NAMES)
    assert [
        call for call in harness.controller.calls if call[:2] == ("disable", "--now")
    ] == [("disable", "--now", name) for name in legacy.TIMER_NAMES]
    assert set(terminal["removed_unit_files"]) == set(legacy.UNIT_NAMES)
    assert terminal["removed_pointer"]["pr_number"] == 95
    assert terminal["merged_pr_evidence"] == legacy.EXACT_MERGED_PR_EVIDENCE
    assert harness.lock_states == {"authority": False, "cron": False}


def test_reconcile_fails_before_mutation_when_cron_bytes_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    plan = harness.plan()
    preflight = harness.preflight(plan)
    changed = _jobs_raw()
    changed = changed.replace(b"delivered", b"confirmed")
    _write(harness.jobs_path, changed, mode=0o600)

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="legacy_rail_cron_drifted",
    ):
        harness.reconcile(plan, preflight)

    assert harness.controller.calls == []
    assert harness.pointer_path.exists()
    assert all((harness.systemd_root / name).exists() for name in legacy.UNIT_NAMES)


def test_plan_rejects_pointer_that_only_looks_related_by_keywords(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    impostor = dict(legacy.EXACT_STALE_POINTER)
    impostor["pr_number"] = 96
    impostor["pr_url"] = "https://github.com/lomliev/hermes-agent/pull/96"
    _write(harness.pointer_path, _pointer_raw(impostor), mode=0o600)

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="legacy_rail_pointer_identity_invalid",
    ):
        harness.plan()

    assert harness.controller.calls == []


def test_plan_rejects_any_pointer_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    ledger = harness.pointer_path.with_name(f"{harness.pointer_path.name}.ledger")
    _write(ledger, b"{}\n", mode=0o600)

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="legacy_rail_pointer_ledger_present",
    ):
        harness.plan()

    assert harness.controller.calls == []


def test_plan_rejects_running_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    name = rail.SYNC_SERVICE_UNIT
    state = harness.controller.states[name]
    harness.controller.states[name] = legacy.UnitObservation(
        unit=name,
        loaded=True,
        active=True,
        unit_file_state=state.unit_file_state,
        main_pid=442,
        fragment_path=state.fragment_path,
        fragment_sha256=state.fragment_sha256,
    )

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="legacy_rail_service_not_quiescent",
    ):
        harness.plan()


def test_systemctl_mutation_scope_rejects_service_units_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def capture(*_arguments: str) -> tuple[int, bytes]:
        nonlocal called
        called = True
        return 0, b""

    monkeypatch.setattr(legacy, "_systemctl_capture", capture)

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="legacy_rail_systemd_mutation_scope_invalid",
    ):
        legacy.systemctl_mutate(
            "disable",
            "--now",
            rail.SYNC_SERVICE_UNIT,
        )

    assert called is False


def test_started_reconciliation_recovers_forward_after_timer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    plan = harness.plan()
    preflight = harness.preflight(plan)
    harness.controller.fail_after_first_disable = True

    with pytest.raises(
        legacy.LegacyRailReconciliationError,
        match="injected_systemd_failure",
    ):
        harness.reconcile(plan, preflight)

    started = (
        harness.evidence_root / plan["plan_sha256"] / "reconciliation-started.json"
    )
    assert started.is_file()
    harness.controller.fail_after_first_disable = False

    terminal = harness.reconcile(plan, preflight)

    assert terminal["forward_recovery_only"] is True
    assert terminal["legacy_cron_preserved_exactly"] is True
    assert all(
        harness.controller.states[name] == legacy.UnitObservation.absent(name)
        for name in legacy.UNIT_NAMES
    )


def test_verify_is_read_only_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    plan = harness.plan()
    preflight = harness.preflight(plan)
    terminal = harness.reconcile(plan, preflight)
    harness.controller.calls.clear()

    verified = legacy.verify(
        expected_plan_sha256=plan["plan_sha256"],
        plan_path=harness.plan_path,
        staged_root=harness.staged_root,
        jobs_path=harness.jobs_path,
        pointer_path=harness.pointer_path,
        systemd_root=harness.systemd_root,
        evidence_root=harness.evidence_root,
        root_owned=False,
        require_root=False,
        unit_observer=harness.controller.observe,
        activation_lock_factory=harness.authority_lock,
    )

    assert verified == terminal
    assert harness.controller.calls == []
