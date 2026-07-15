from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

# The canonical runner uses ``env -i``. Windows' stdlib ignores HOME and needs
# USERPROFILE for ``Path.home()`` during imported-module initialization.
if os.name == "nt" and "USERPROFILE" not in os.environ:
    os.environ["USERPROFILE"] = os.environ["HOME"]

from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.characterize import CharacterizationGateError
from session_bridge.cli import (
    ProductionBackend,
    ProviderDegraded,
    RolloutGateBlocked,
    main,
)
from session_bridge.config import BridgeConfig, CatalogConfig, MirrorsConfig, SidebarConfig
from session_bridge.coordinator import ScanSummary
from session_bridge.mirror import MirrorPolicy, enqueue_mirror_job
from session_bridge.models import ProjectedMessage, Provider, SessionProjection
from session_bridge.store import SessionBridgeStore


@dataclass
class FakeBackend:
    characterization: str = "passed"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    preview: dict[str, Any] = field(
        default_factory=lambda: {
            "session_id": "claude:source",
            "target_provider": "codex",
            "would_enqueue": True,
            "reason": "eligible",
        }
    )
    status_payload: dict[str, Any] = field(
        default_factory=lambda: {"healthy": True, "total_sessions": 3}
    )
    scan_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "provider": "all",
            "discovered": 4,
            "indexed": 4,
            "rebuilt": 0,
            "failed": 0,
        }
    )
    backfill_apply_payload: dict[str, Any] | None = None
    sidebar_status_payload: dict[str, Any] = field(
        default_factory=lambda: {"healthy": True, "counts": {"pending": 0}}
    )
    sidebar_backfill_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "dry_run",
            "days": 30,
            "limit": 10,
            "examined": 0,
            "queued": 0,
            "by_provider": {"claude": 0, "hermes": 0},
            "failed": 0,
        }
    )
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def close(self) -> None:
        self.calls.append(("close",))

    def serve(self) -> None:
        self.calls.append(("serve",))

    def scan(
        self, *, provider: str, all_history: bool, newest_first: bool
    ) -> dict[str, Any]:
        self.calls.append(("scan", provider, all_history, newest_first))
        return dict(self.scan_payload)

    def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return dict(self.status_payload)

    def sidebar_status(self) -> dict[str, Any]:
        self.calls.append(("sidebar_status",))
        return dict(self.sidebar_status_payload)

    def sidebar_backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> dict[str, Any]:
        self.calls.append(("sidebar_backfill", days, limit, apply))
        return {**self.sidebar_backfill_payload, "mode": "apply" if apply else "dry_run"}

    def set_sidebar_continuous(self, *, enabled: bool) -> dict[str, Any]:
        self.calls.append(("set_sidebar_continuous", enabled))
        return {"enabled": enabled, "continuous": enabled}

    def characterize(self, *, provider: str) -> dict[str, Any]:
        self.calls.append(("characterize", provider))
        return {"passed": True, "report": "characterization/report.json"}

    def characterization_status(self) -> str:
        self.calls.append(("characterization_status",))
        return self.characterization

    def backfill_candidates(self, *, days: int) -> list[dict[str, Any]]:
        self.calls.append(("backfill_candidates", days))
        return [dict(candidate) for candidate in self.candidates]

    def apply_backfill(self, *, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append((
            "apply_backfill",
            tuple(item["canonical_id"] for item in candidates),
        ))
        return dict(
            self.backfill_apply_payload
            or {
                "authorized": len(candidates),
                "claimed": len(candidates),
                "succeeded": len(candidates),
                "retried": 0,
                "manual_failure": 0,
                "degraded": False,
                "halted": False,
            }
        )

    def mirror_preview(self, *, session_id: str, target: str) -> dict[str, Any]:
        self.calls.append(("mirror_preview", session_id, target))
        return dict(self.preview)

    def apply_mirror(self, *, session_id: str, target: str) -> dict[str, Any]:
        self.calls.append(("apply_mirror", session_id, target))
        return {
            "session_id": session_id,
            "target_provider": target,
            "state": "queued",
            "degraded": False,
        }


def _run(
    argv: list[str],
    backend: FakeBackend,
    *,
    automatic_creation: bool = False,
) -> int:
    config = BridgeConfig(mirrors=MirrorsConfig(automatic_creation=automatic_creation))
    return main(
        argv,
        config_loader=lambda: config,
        backend_factory=lambda _config: backend,
    )


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_serve_dispatches_and_closes_runtime(capsys):
    backend = FakeBackend()

    assert _run(["serve"], backend) == 0

    assert backend.calls == [("serve",), ("close",)]
    assert _json_output(capsys)["status"] == "stopped"


def test_sidebar_skill_cli_installs_without_loading_bridge_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = tmp_path / "codex" / "skills" / "session-sidebar-sync"
    calls: list[None] = []
    monkeypatch.setattr(
        "session_bridge.cli.install_sidebar_skill",
        lambda: calls.append(None) or installed,
    )

    result = main(
        ["install-sidebar-skill"],
        config_loader=lambda: pytest.fail("installer must not load bridge config"),
        backend_factory=lambda _config: pytest.fail("installer must not start backend"),
    )

    assert result == 0
    assert calls == [None]
    assert _json_output(capsys) == {
        "status": "installed",
        "path": str(installed),
    }


def test_sidebar_skill_cli_sanitizes_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "session_bridge.cli.install_sidebar_skill",
        lambda: (_ for _ in ()).throw(PermissionError("private destination")),
    )

    assert main(["install-sidebar-skill"]) == 2
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {"error": "configuration_error"}
    assert "private destination" not in rendered


def test_sidebar_rollout_commands_are_bounded_and_route_without_mirroring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()

    assert _run(["sidebar-status", "--json"], backend) == 0
    assert _json_output(capsys)["healthy"] is True
    assert _run(
        ["sidebar-backfill", "--days", "30", "--limit", "10", "--dry-run"],
        backend,
    ) == 0
    assert _json_output(capsys)["mode"] == "dry_run"
    assert _run(
        ["sidebar-backfill", "--days", "30", "--limit", "10", "--apply"],
        backend,
    ) == 0
    assert _json_output(capsys)["mode"] == "apply"
    assert _run(["sidebar-continuous", "--enable"], backend) == 0
    assert _json_output(capsys) == {"continuous": True, "enabled": True}
    assert _run(["sidebar-continuous", "--disable"], backend) == 0
    assert _json_output(capsys) == {"continuous": False, "enabled": False}

    assert backend.calls == [
        ("sidebar_status",),
        ("close",),
        ("sidebar_backfill", 30, 10, False),
        ("close",),
        ("sidebar_backfill", 30, 10, True),
        ("close",),
        ("set_sidebar_continuous", True),
        ("close",),
        ("set_sidebar_continuous", False),
        ("close",),
    ]
    assert not any(call[0] in {"apply_backfill", "apply_mirror"} for call in backend.calls)


def test_sidebar_backfill_rejects_a_limit_above_ten() -> None:
    with pytest.raises(SystemExit):
        main(["sidebar-backfill", "--days", "30", "--limit", "11", "--dry-run"])


class _SidebarStatusStore:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def sidebar_delivery_status(self, *, now: float) -> dict[str, Any]:
        assert now == 1_000.0
        return dict(self.payload)


def _production_sidebar_backend(
    payload: dict[str, Any], *, grace_seconds: int = 120
) -> ProductionBackend:
    backend = ProductionBackend(
        replace(
            BridgeConfig(),
            sidebar=replace(
                SidebarConfig(),
                enabled=True,
                heartbeat_grace_seconds=grace_seconds,
            ),
        )
    )
    backend._store = _SidebarStatusStore(payload)  # type: ignore[assignment]
    backend._catalog = object()  # type: ignore[assignment]
    return backend


def test_sidebar_status_is_healthy_when_empty_without_a_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 0, "hermes": 0},
        "counts": {"pending": 0, "leased": 0, "retry": 0, "visible": 0, "failed": 0},
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {"p50": None, "p95": None, "p99": None},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is True
    assert status["degraded_reasons"] == []
    assert status["last_successful_heartbeat_at"] is None

    fresh_pending = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"pending": 1},
        "oldest_pending_age_seconds": 180.0,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
    })
    assert fresh_pending.sidebar_status()["healthy"] is True


def test_sidebar_status_degrades_stale_pending_work_and_redacts_task_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 2, "hermes": 1},
        "counts": {"pending": 1, "leased": 0, "retry": 0, "visible": 2, "failed": 0},
        "oldest_pending_age_seconds": 181.0,
        "last_heartbeat_at": 819.0,
        "last_visible_task_id": "019f-secret-thread-identifier",
        "recent_error_codes": ["broker_time_budget"],
        "delivery_latency_seconds": {"p50": 1.0, "p95": 2.0, "p99": 2.0},
    })

    status = backend.sidebar_status()
    rendered = json.dumps(status, sort_keys=True)

    assert status["healthy"] is False
    assert status["degraded_reasons"] == [
        "broker_heartbeat_stale",
        "oldest_pending_stale",
    ]
    assert status["last_visible_task_id"] == "019f-sec...fier"
    assert "019f-secret-thread-identifier" not in rendered
    assert "lease_token" not in rendered
    assert "marker" not in rendered


@pytest.mark.parametrize(
    ("oldest_age", "heartbeat_at", "healthy", "reasons"),
    (
        (179.0, 1.0, True, []),
        (180.0, None, True, []),
        (
            181.0,
            None,
            False,
            ["broker_heartbeat_stale", "oldest_pending_stale"],
        ),
        (
            181.0,
            819.0,
            False,
            ["broker_heartbeat_stale", "oldest_pending_stale"],
        ),
        (181.0, 999.0, False, ["oldest_pending_stale"]),
    ),
)
def test_sidebar_status_alerts_only_for_work_older_than_threshold(
    monkeypatch: pytest.MonkeyPatch,
    oldest_age: float,
    heartbeat_at: float | None,
    healthy: bool,
    reasons: list[str],
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"pending": 1},
        "oldest_pending_age_seconds": oldest_age,
        "last_heartbeat_at": heartbeat_at,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is healthy
    assert status["degraded_reasons"] == reasons


def test_sidebar_continuous_preserves_unrelated_hermes_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = {
        "theme": "midnight",
        "session_bridge": {
            "sidebar": {"enabled": True, "continuous": False, "backfill_days": 30},
            "future_key": {"keep": "exactly"},
        },
    }
    saved: list[tuple[dict[str, Any], set[tuple[str, ...]] | None]] = []
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: json.loads(json.dumps(loaded)),
    )
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda value, **kwargs: saved.append((value, kwargs.get("preserve_keys"))),
    )
    backend = ProductionBackend(BridgeConfig())

    result = backend.set_sidebar_continuous(enabled=True)

    assert result == {"enabled": False, "continuous": True}
    assert saved == [(
        {
            "theme": "midnight",
            "session_bridge": {
                "sidebar": {"enabled": True, "continuous": True, "backfill_days": 30},
                "future_key": {"keep": "exactly"},
            },
        },
        {("session_bridge", "sidebar", "continuous")},
    )]


def test_production_serve_blocks_automatic_mode_without_passing_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(
        BridgeConfig(mirrors=MirrorsConfig(automatic_creation=True))
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_characterization_gate",
        lambda **_kwargs: (_ for _ in ()).throw(
            CharacterizationGateError("failed", "failed")
        ),
    )
    monkeypatch.setattr(
        backend,
        "_provider_runtime",
        lambda **_kwargs: pytest.fail("provider runtime must not start"),
    )

    with pytest.raises(RolloutGateBlocked) as raised:
        backend.serve()

    assert raised.value.gate == "characterization_failed"


def test_scan_defaults_to_catalog_only_all_history_newest_first(capsys):
    backend = FakeBackend()

    assert _run(["scan"], backend, automatic_creation=True) == 0

    assert backend.calls[:1] == [("scan", "all", True, True)]
    payload = _json_output(capsys)
    assert payload["indexed"] == 4
    assert "automatic_creation" not in json.dumps(payload)


def test_scan_provider_failure_returns_degraded_exit(capsys):
    backend = FakeBackend(
        scan_payload={
            "provider": "codex",
            "discovered": 3,
            "indexed": 2,
            "rebuilt": 0,
            "failed": 1,
        }
    )

    assert _run(["scan", "--provider", "codex"], backend) == 3
    assert _json_output(capsys)["failed"] == 1


def test_claude_only_runtime_never_spawns_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db)
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"m" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.CodexAppServerClient",
        lambda **_kwargs: pytest.fail("Codex must not start for Claude-only scan"),
    )
    try:
        coordinator = backend._provider_runtime(
            targets=False,
            catalog_only=True,
            providers=(Provider.CLAUDE,),
        )
        assert set(coordinator._adapters) == {Provider.CLAUDE}
    finally:
        backend.close()


def test_production_runtime_wires_exact_cwd_permission_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db)
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    captured: dict[str, object] = {}
    sentinel = lambda cwd: cwd == str(tmp_path)

    def coordinator_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"m" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.SessionBridgeCoordinator",
        coordinator_factory,
    )
    monkeypatch.setattr(
        "session_bridge.cli._production_codex_permission_preflight",
        sentinel,
        raising=False,
    )
    try:
        backend._provider_runtime(
            targets=False,
            catalog_only=True,
            providers=(Provider.CLAUDE,),
        )
    finally:
        backend.close()

    assert captured["permission_preflight"] is sentinel


def test_production_all_provider_scan_isolates_provider_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(BridgeConfig())
    runtime_calls: list[Provider] = []
    releases: list[None] = []

    class ClaudeCoordinator:
        async def scan_all_history(self, provider: Provider) -> ScanSummary:
            assert provider is Provider.CLAUDE
            return ScanSummary(
                provider=provider,
                discovered=2,
                indexed=2,
                rebuilt=1,
                failed=0,
                duration_ms=4.0,
            )

    def provider_runtime(**kwargs: Any) -> ClaudeCoordinator:
        selected = kwargs["providers"][0]
        runtime_calls.append(selected)
        if selected is Provider.CODEX:
            raise RuntimeError("synthetic Codex startup failure")
        return ClaudeCoordinator()

    monkeypatch.setattr(backend, "_provider_runtime", provider_runtime)
    monkeypatch.setattr(
        backend,
        "_release_provider_runtime",
        lambda: releases.append(None),
    )

    result = backend.scan(provider="all", all_history=True, newest_first=True)

    assert result == {
        "provider": None,
        "discovered": 2,
        "indexed": 2,
        "rebuilt": 1,
        "failed": 1,
        "duration_ms": 4.0,
    }
    assert runtime_calls == [Provider.CLAUDE, Provider.CODEX]
    assert len(releases) == 2


def test_status_json_is_sanitized_and_degradation_sets_exit_three(capsys):
    backend = FakeBackend(
        status_payload={
            "healthy": False,
            "total_sessions": 3,
            "token": "do-not-print",
            "context_pack": "do-not-print",
            "nested": {"native_path": "C:/private/session.jsonl", "ok": 1},
        }
    )

    assert _run(["status", "--json"], backend) == 3

    rendered = capsys.readouterr().out
    assert "do-not-print" not in rendered
    assert "private/session" not in rendered
    assert json.loads(rendered)["nested"] == {"ok": 1}


def test_characterize_runs_both_providers(capsys):
    backend = FakeBackend()

    assert _run(["characterize", "--provider", "all"], backend) == 0

    assert backend.calls[:1] == [("characterize", "all")]
    assert _json_output(capsys)["passed"] is True


def test_backfill_dry_run_is_newest_first_and_never_applies(capsys):
    backend = FakeBackend(
        candidates=[
            {
                "canonical_id": "claude:older",
                "provider": "claude",
                "target_provider": "codex",
                "last_active": 10.0,
                "eligible": True,
                "reason": "eligible",
                "token": "hidden",
            },
            {
                "canonical_id": "codex:newer",
                "provider": "codex",
                "target_provider": "claude",
                "last_active": 20.0,
                "eligible": True,
                "reason": "eligible",
            },
        ]
    )

    assert _run(["backfill", "--days", "30", "--dry-run"], backend) == 0

    payload = _json_output(capsys)
    assert [item["canonical_id"] for item in payload["candidates"]] == [
        "codex:newer",
        "claude:older",
    ]
    assert "hidden" not in json.dumps(payload)
    assert not any(call[0] == "apply_backfill" for call in backend.calls)


def test_production_backfill_plan_excludes_an_existing_queued_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 200.0)

    def projection(native_id: str, last_active: float) -> SessionProjection:
        return SessionProjection(
            provider=Provider.CLAUDE,
            native_id=native_id,
            title=native_id,
            cwd=str(tmp_path),
            started_at=last_active - 20.0,
            last_active=last_active,
            messages=(
                ProjectedMessage(
                    native_event_id=f"event-{native_id}",
                    ordinal=0,
                    role="user",
                    content="meaningful work",
                    timestamp=last_active - 10.0,
                ),
            ),
            native_cursor=f"cursor-{native_id}",
            native_hash=f"hash-{native_id}",
        )

    store.upsert_projection(projection("queued", 190.0))
    store.upsert_projection(projection("eligible", 180.0))
    enqueue_mirror_job(
        store,
        "claude:queued",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
    )
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 200.0)
    try:
        planned = backend.backfill_candidates(days=30)
    finally:
        backend.close()

    assert [candidate["canonical_id"] for candidate in planned] == ["claude:eligible"]


def test_production_backfill_plan_drains_past_rejected_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 200.0)

    def projection(native_id: str, last_active: float) -> SessionProjection:
        return SessionProjection(
            provider=Provider.CLAUDE,
            native_id=native_id,
            title=native_id,
            cwd=str(tmp_path),
            started_at=last_active - 20.0,
            last_active=last_active,
            messages=(
                ProjectedMessage(
                    native_event_id=f"event-{native_id}",
                    ordinal=0,
                    role="user",
                    content="meaningful work",
                    timestamp=last_active - 10.0,
                ),
            ),
            native_cursor=f"cursor-{native_id}",
            native_hash=f"hash-{native_id}",
        )

    store.upsert_projection(projection("newer", 190.0))
    store.upsert_projection(projection("older", 180.0))
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 200.0)
    monkeypatch.setattr("session_bridge.cli._BACKFILL_PAGE_SIZE", 1)
    original_preview = backend._catalog.mirror_preview
    monkeypatch.setattr(
        backend._catalog,
        "mirror_preview",
        lambda session_id, target: (
            {"would_enqueue": False}
            if session_id == "claude:newer"
            else original_preview(session_id, target)
        ),
    )
    try:
        planned = backend.backfill_candidates(days=30)
    finally:
        backend.close()

    assert [candidate["canonical_id"] for candidate in planned] == ["claude:older"]


def test_production_backfill_plan_fails_visibly_at_planning_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    for native_id, last_active in (("newer", 190.0), ("older", 180.0)):
        store.upsert_projection(
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id=native_id,
                title=native_id,
                cwd=str(tmp_path),
                started_at=last_active - 20.0,
                last_active=last_active,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"event-{native_id}",
                        ordinal=0,
                        role="user",
                        content="meaningful work",
                        timestamp=last_active - 10.0,
                    ),
                ),
                native_cursor=f"cursor-{native_id}",
                native_hash=f"hash-{native_id}",
            )
        )
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 200.0)
    monkeypatch.setattr("session_bridge.cli._BACKFILL_PAGE_SIZE", 1)
    monkeypatch.setattr("session_bridge.cli._MAX_PLANNED_SESSIONS", 1)
    try:
        with pytest.raises(ProviderDegraded, match="backfill_plan_truncated"):
            backend.backfill_candidates(days=30)
    finally:
        backend.close()


@pytest.mark.parametrize("gate", ["missing", "failed", "invalid", "version_drift"])
def test_backfill_apply_refuses_absent_or_failed_characterization(gate, capsys):
    backend = FakeBackend(characterization=gate)

    assert (
        _run(
            [
                "backfill",
                "--days",
                "30",
                "--apply",
                "--confirm-one-shot",
            ],
            backend,
        )
        == 4
    )

    assert _json_output(capsys) == {
        "error": "rollout_gate_blocked",
        "gate": f"characterization_{gate}",
    }
    assert not any(call[0] == "apply_backfill" for call in backend.calls)


def test_backfill_apply_requires_auto_mode_or_explicit_one_shot(capsys):
    backend = FakeBackend()

    assert _run(["backfill", "--days", "30", "--apply"], backend) == 4

    assert _json_output(capsys)["gate"] == "one_shot_confirmation_required"


def test_mutation_refuses_when_catalog_is_disabled(capsys):
    backend = FakeBackend()
    config = BridgeConfig(
        catalog=CatalogConfig(enabled=False),
        mirrors=MirrorsConfig(automatic_creation=True),
    )

    result = main(
        ["backfill", "--days", "30", "--apply"],
        config_loader=lambda: config,
        backend_factory=lambda _config: backend,
    )

    assert result == 4
    assert _json_output(capsys)["gate"] == "catalog_disabled"
    assert not any(call[0] == "apply_backfill" for call in backend.calls)


def test_backfill_apply_caps_and_preserves_newest_first(capsys):
    backend = FakeBackend(
        candidates=[
            {
                "canonical_id": f"claude:{index}",
                "provider": "claude",
                "target_provider": "codex",
                "last_active": float(index),
                "eligible": True,
                "reason": "eligible",
            }
            for index in range(12)
        ]
    )

    assert (
        _run(
            [
                "backfill",
                "--days",
                "30",
                "--apply",
                "--confirm-one-shot",
                "--max-create",
                "10",
            ],
            backend,
        )
        == 0
    )

    apply = next(call for call in backend.calls if call[0] == "apply_backfill")
    # The default durable global creation rate is six per minute.
    assert apply[1] == tuple(f"claude:{index}" for index in range(11, 5, -1))
    assert _json_output(capsys)["authorized"] == 6


def test_backfill_partial_gate_preserves_prior_outcome_in_output(capsys):
    backend = FakeBackend(
        candidates=[
            {
                "canonical_id": "claude:first",
                "provider": "claude",
                "target_provider": "codex",
                "last_active": 20.0,
                "eligible": True,
                "reason": "eligible",
            }
        ],
        backfill_apply_payload={
            "authorized": 1,
            "claimed": 1,
            "succeeded": 1,
            "retried": 0,
            "manual_failure": 0,
            "degraded": False,
            "halted": True,
            "partial": True,
            "gate": "backfill_candidate_invalid",
        },
    )

    result = _run(["backfill", "--apply", "--confirm-one-shot"], backend)

    assert result == 4
    payload = _json_output(capsys)
    assert payload["succeeded"] == 1
    assert payload["gate"] == "backfill_candidate_invalid"


def test_mirror_dry_run_and_apply_use_same_preview_gate(capsys):
    backend = FakeBackend()

    assert (
        _run(
            ["mirror", "claude:source", "--target", "codex", "--dry-run"],
            backend,
        )
        == 0
    )
    assert _json_output(capsys)["would_enqueue"] is True

    backend.calls.clear()
    assert (
        _run(
            [
                "mirror",
                "claude:source",
                "--target",
                "codex",
                "--apply",
                "--confirm-one-shot",
            ],
            backend,
        )
        == 0
    )
    assert ("apply_mirror", "claude:source", "codex") in backend.calls
    _json_output(capsys)


def test_mirror_apply_blocks_ineligible_or_uncharacterized_source(capsys):
    backend = FakeBackend(
        preview={
            "session_id": "claude:source",
            "target_provider": "codex",
            "would_enqueue": False,
            "reason": "already_mapped",
        }
    )

    assert (
        _run(
            [
                "mirror",
                "claude:source",
                "--target",
                "codex",
                "--apply",
                "--confirm-one-shot",
            ],
            backend,
        )
        == 4
    )
    assert _json_output(capsys)["gate"] == "mirror_already_mapped"
    assert not any(call[0] == "apply_mirror" for call in backend.calls)


def test_invalid_unsafe_configuration_returns_two_without_echo(capsys):
    backend = FakeBackend()

    result = main(
        ["status", "--json"],
        config_loader=lambda: (_ for _ in ()).throw(
            ValueError("unsafe config contains bearer do-not-print")
        ),
        backend_factory=lambda _config: backend,
    )

    assert result == 2
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {"error": "configuration_error"}
    assert "do-not-print" not in rendered
    assert backend.calls == []
