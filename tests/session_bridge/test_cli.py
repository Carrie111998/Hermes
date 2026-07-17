from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Thread
import time
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
    ConfigurationFailure,
    ProductionBackend,
    ProviderDegraded,
    RolloutGateBlocked,
    main,
)
from session_bridge.codex_adapter import SidebarThreadVerifier
from session_bridge.config import (
    BridgeConfig,
    CatalogConfig,
    MirrorsConfig,
    ServiceConfig,
    SidebarConfig,
)
from session_bridge.coordinator import ScanSummary
from session_bridge.mirror import MirrorPolicy, enqueue_mirror_job
from session_bridge.models import (
    BridgeMarkerPayload,
    ProjectedMessage,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.sidebar import SidebarCandidate, sidebar_bridge_id
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
            "excluded": 0,
            "excluded_by_reason": {"source_cwd_missing": 0},
        }
    )
    claude_visibility_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "continuous": False,
            "counts": {"claude_pending": 0, "claude_leased": 0,
                       "claude_retry": 0, "claude_visible": 0,
                       "claude_failed": 0},
            "retry_codes": {}, "failed_codes": {},
            "usage": {"local_day": "2026-07-17", "attempts": 0,
                      "reserved_cost_usd": "0"},
            "candidates": [], "exclusions": [], "open_reasons": [],
            "fatal_reasons": [], "degraded_reasons": [],
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

    def claude_visibility_status(self) -> dict[str, Any]:
        self.calls.append(("claude_visibility_status",))
        return dict(self.claude_visibility_payload)

    def claude_visibility_backfill(self, *, days: int, limit: int, apply: bool):
        self.calls.append(("claude_visibility_backfill", days, limit, apply))
        return {**self.claude_visibility_payload,
                "mode": "apply" if apply else "dry_run",
                "dry_run": not apply, "applied": apply, "enqueued": 0}

    def set_claude_visibility_continuous(self, *, enabled: bool):
        self.calls.append(("set_claude_visibility_continuous", enabled))
        return {"enabled": False, "continuous": enabled}

    def claude_visibility_run_once(self):
        self.calls.append(("claude_visibility_run_once",))
        return {"enabled": True, "status": "no_due_job", "degraded": False,
                "fatal": False}

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


def test_claude_visibility_exact_commands_route_with_safe_defaults(capsys) -> None:
    backend = FakeBackend()

    assert _run(["claude-visibility-status", "--json"], backend) == 0
    assert _json_output(capsys)["counts"]["claude_pending"] == 0
    assert _run(
        ["claude-visibility-backfill", "--days", "30", "--limit", "10", "--dry-run"],
        backend,
    ) == 0
    dry_run = _json_output(capsys)
    assert dry_run["mode"] == "dry_run"
    assert dry_run["dry_run"] is True
    assert dry_run["applied"] is False
    assert _run(
        ["claude-visibility-backfill", "--days", "30", "--limit", "10", "--apply"],
        backend,
    ) == 0
    assert _json_output(capsys)["mode"] == "apply"
    assert _run(["claude-visibility-continuous", "--enable"], backend) == 0
    assert _json_output(capsys) == {"continuous": True, "enabled": False}
    assert _run(["claude-visibility-continuous", "--disable"], backend) == 0
    assert _json_output(capsys) == {"continuous": False, "enabled": False}
    assert _run(["claude-visibility-run-once"], backend) == 0
    assert _json_output(capsys)["status"] == "no_due_job"

    assert backend.calls == [
        ("claude_visibility_status",), ("close",),
        ("claude_visibility_backfill", 30, 10, False), ("close",),
        ("claude_visibility_backfill", 30, 10, True), ("close",),
        ("set_claude_visibility_continuous", True), ("close",),
        ("set_claude_visibility_continuous", False), ("close",),
        ("claude_visibility_run_once",), ("close",),
    ]


def test_claude_visibility_backfill_defaults_to_explicit_dry_run_json(capsys) -> None:
    backend = FakeBackend()

    assert _run(
        ["claude-visibility-backfill", "--days", "30", "--limit", "10"], backend
    ) == 0

    assert _json_output(capsys) == {
        **backend.claude_visibility_payload,
        "mode": "dry_run",
        "applied": False,
        "dry_run": True,
        "enqueued": 0,
    }
    assert backend.calls == [
        ("claude_visibility_backfill", 30, 10, False), ("close",)
    ]


def test_claude_visibility_backfill_validation_precedes_backend_mutation(capsys) -> None:
    backend = FakeBackend()

    with pytest.raises(SystemExit):
        _run(["claude-visibility-backfill", "--days", "0", "--limit", "10"], backend)
    with pytest.raises(SystemExit):
        _run(["claude-visibility-backfill", "--days", "30", "--limit", "11"], backend)
    with pytest.raises(SystemExit):
        _run([
            "claude-visibility-backfill", "--days", "30", "--limit", "10",
            "--dry-run", "--apply",
        ], backend)

    assert backend.calls == []


def test_claude_visibility_apply_and_run_once_use_typed_nonzero_contract(capsys) -> None:
    backend = FakeBackend(claude_visibility_payload={
        "enabled": True, "continuous": False, "counts": {}, "retry_codes": {},
        "failed_codes": {"unknown_error_code": 1}, "usage": {},
        "candidates": [], "exclusions": [], "open_reasons": [],
        "fatal_reasons": ["unknown_retry"], "degraded_reasons": [],
    })
    assert _run([
        "claude-visibility-backfill", "--days", "30", "--limit", "10", "--apply"
    ], backend) != 0
    _json_output(capsys)

    backend.claude_visibility_run_once = lambda: {
        "enabled": True, "status": "degraded", "degraded": True, "fatal": False
    }
    assert _run(["claude-visibility-run-once"], backend) != 0
    _json_output(capsys)


@pytest.mark.parametrize("apply", (False, True))
def test_sidebar_backfill_candidate_failures_exit_degraded(
    capsys: pytest.CaptureFixture[str],
    apply: bool,
) -> None:
    backend = FakeBackend(
        sidebar_backfill_payload={
            "mode": "apply" if apply else "dry_run",
            "days": 30,
            "limit": 10,
            "examined": 1,
            "queued": 0,
            "by_provider": {"claude": 0, "hermes": 0},
            "failed": 1,
        }
    )
    mode = "--apply" if apply else "--dry-run"

    assert _run(
        ["sidebar-backfill", "--days", "30", "--limit", "10", mode],
        backend,
    ) == 3
    assert _json_output(capsys)["failed"] == 1


@pytest.mark.parametrize("apply", (False, True))
def test_sidebar_backfill_exclusions_do_not_exit_degraded(
    capsys: pytest.CaptureFixture[str],
    apply: bool,
) -> None:
    backend = FakeBackend(
        sidebar_backfill_payload={
            "mode": "apply" if apply else "dry_run",
            "days": 30,
            "limit": 10,
            "examined": 1,
            "queued": 0,
            "by_provider": {"claude": 0, "hermes": 0},
            "failed": 0,
            "excluded": 1,
            "excluded_by_reason": {"source_cwd_missing": 1},
        }
    )
    mode = "--apply" if apply else "--dry-run"

    assert _run(
        ["sidebar-backfill", "--days", "30", "--limit", "10", mode],
        backend,
    ) == 0
    assert _json_output(capsys)["excluded_by_reason"] == {
        "source_cwd_missing": 1
    }


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


def test_sidebar_status_preserves_exclusion_count_without_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 0, "hermes": 0},
        "counts": {"sidebar_excluded": 7},
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["counts"]["sidebar_excluded"] == 7
    assert status["healthy"] is True
    assert status["degraded_reasons"] == []


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
    expected_tag = hashlib.sha256(
        b"019f-secret-thread-identifier"
    ).hexdigest()[:16]
    assert status["last_visible_task_id"] == f"task:{expected_tag}"
    assert "019f-secret-thread-identifier" not in rendered
    assert "lease_token" not in rendered
    assert "marker" not in rendered


@pytest.mark.parametrize(
    "hostile_id",
    (
        "C:/private/task",
        "../private-task",
        "secret\nsecond-line",
        "\x00control",
        "_leading-symbol",
        "a" * 513,
        "",
    ),
)
def test_sidebar_status_never_redacts_hostile_task_ids_by_fragment(
    monkeypatch: pytest.MonkeyPatch,
    hostile_id: str,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 0, "hermes": 0},
        "counts": {},
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": hostile_id,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
    })

    assert backend.sidebar_status()["last_visible_task_id"] is None


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


@pytest.mark.parametrize(
    ("age", "healthy"),
    ((0.0, True), (181.0, False)),
)
def test_sidebar_status_includes_leased_only_actionable_work(
    monkeypatch: pytest.MonkeyPatch,
    age: float,
    healthy: bool,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"leased": 1},
        "oldest_pending_age_seconds": age,
        "last_heartbeat_at": 1.0,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
    })

    assert backend.sidebar_status()["healthy"] is healthy


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
    def mutate_config(mutator, **kwargs):
        value = json.loads(json.dumps(loaded))
        mutator(value)
        saved.append((value, kwargs.get("preserve_keys")))
        return value

    monkeypatch.setattr("hermes_cli.config.mutate_config", mutate_config)
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


def test_claude_visibility_continuous_preserves_unrelated_config_and_enabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = {
        "theme": "midnight",
        "session_bridge": {
            "claude_visibility": {
                "enabled": False, "continuous": False, "backfill_days": 30
            },
            "future_key": {"keep": "exactly"},
        },
    }
    saved = []

    def mutate_config(mutator, **kwargs):
        value = json.loads(json.dumps(loaded))
        mutator(value)
        saved.append((value, kwargs.get("preserve_keys")))
        return value

    monkeypatch.setattr("hermes_cli.config.mutate_config", mutate_config)
    backend = ProductionBackend(BridgeConfig())

    result = backend.set_claude_visibility_continuous(enabled=True)

    assert result == {"enabled": False, "continuous": True}
    assert saved == [(
        {
            "theme": "midnight",
            "session_bridge": {
                "claude_visibility": {
                    "enabled": False, "continuous": True, "backfill_days": 30
                },
                "future_key": {"keep": "exactly"},
            },
        },
        {("session_bridge", "claude_visibility", "continuous")},
    )]


def test_claude_visibility_continuous_postwrite_mismatch_keeps_runtime_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.mutate_config",
        lambda _mutator, **_kwargs: {
            "session_bridge": {"claude_visibility": {"continuous": False}}
        },
    )
    backend = ProductionBackend(BridgeConfig())

    with pytest.raises(ConfigurationFailure, match="claude_visibility_continuous_not_persisted"):
        backend.set_claude_visibility_continuous(enabled=True)

    assert backend.config.claude_visibility.continuous is False


def test_claude_visibility_status_does_not_construct_delivery_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {state: 0 for state in (
                    "claude_pending", "claude_leased", "claude_retry",
                    "claude_visible", "claude_failed",
                )},
                "retry_codes": {}, "failed_codes": {},
                "usage": {"local_day": "2026-07-17", "attempts": 0,
                          "reserved_cost_usd": "0"},
            }

    config = BridgeConfig()
    backend = ProductionBackend(replace(
        config,
        claude_visibility=replace(config.claude_visibility, enabled=True),
    ))
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())
    monkeypatch.setattr(
        "session_bridge.cli.resolve_marker_key",
        lambda: (_ for _ in ()).throw(AssertionError("marker key")),
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda _name: (_ for _ in ()).throw(AssertionError("delivery executable")),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("registrar")),
    )

    result = backend.claude_visibility_status()

    assert result["enabled"] is True
    assert result["candidates"] == []
    assert result["degraded_reasons"] == []
    assert result["last_empty_cycle"] == {"tracked": False, "value": None}
    assert result["last_registrar_result"] == {"tracked": False, "value": None}


def test_claude_visibility_status_exposes_sanitized_unknown_state_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {state: 0 for state in (
                    "claude_pending", "claude_leased", "claude_retry",
                    "claude_visible", "claude_failed",
                )},
                "retry_codes": {}, "failed_codes": {},
                "usage": {"local_day": "2026-07-17", "attempts": 0,
                          "reserved_cost_usd": "0"},
                "fatal": [{"code": "unknown_job_state", "state": "future_state",
                           "error_code": "future-code", "count": 1}],
            }

    config = BridgeConfig()
    backend = ProductionBackend(replace(
        config, claude_visibility=replace(config.claude_visibility, enabled=True)
    ))
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())

    result = backend.claude_visibility_status()

    assert result["fatal_reasons"] == ["unknown_job_state"]
    assert result["fatal"] == [{
        "code": "unknown_job_state", "state": "future_state",
        "error_code": "future-code", "count": 1,
    }]


def test_claude_visibility_json_is_one_sanitized_stdout_document(capsys) -> None:
    backend = FakeBackend()
    backend.claude_visibility_payload["secret_detail"] = "must-not-leak"
    backend.claude_visibility_payload["tuple_value"] = ("stable", 1)

    assert _run(["claude-visibility-status", "--json"], backend) == 0

    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1
    payload = json.loads(stdout)
    assert "secret_detail" not in payload
    assert payload["tuple_value"] == ["stable", 1]


def test_sidebar_continuous_full_managed_rejects_without_runtime_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.config import save_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_config(
        {"session_bridge": {"sidebar": {"continuous": False}}},
        strip_defaults=False,
    )
    original = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED", "homebrew")
    backend = ProductionBackend(BridgeConfig())

    exit_code = main(
        ["sidebar-continuous", "--enable"],
        config_loader=lambda: backend.config,
        backend_factory=lambda _config: backend,
    )

    rendered = capsys.readouterr().out
    assert exit_code == 2
    assert json.loads(rendered) == {"error": "configuration_error"}
    assert "sensitive" not in rendered
    assert backend.config.sidebar.continuous is False
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == original


def test_sidebar_continuous_managed_leaf_reports_effective_value_without_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / "config.yaml").write_text(
        "session_bridge:\n  sidebar:\n    continuous: false\n",
        encoding="utf-8",
    )
    managed_scope.invalidate_managed_cache()
    backend = ProductionBackend(BridgeConfig())

    with pytest.raises(ConfigurationFailure, match="sidebar_continuous_not_persisted"):
        backend.set_sidebar_continuous(enabled=True)

    assert backend.config.sidebar.continuous is False


def test_sidebar_continuous_mutation_exception_does_not_change_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setattr(
        "hermes_cli.config.mutate_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("sensitive persistence failure")
        ),
    )

    with pytest.raises(OSError, match="sensitive persistence failure"):
        backend.set_sidebar_continuous(enabled=True)

    assert backend.config.sidebar.continuous is False


def test_mutate_config_serializes_competing_updates_without_lost_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as config_module

    durable: dict[str, Any] = {"existing": {"keep": True}}
    first_inside = Event()
    release_first = Event()
    second_inside = Event()
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: json.loads(json.dumps(durable)),
    )

    def save_config(value, **_kwargs):
        durable.clear()
        durable.update(json.loads(json.dumps(value)))
        return True

    monkeypatch.setattr(config_module, "save_config", save_config)

    def first_mutation(value: dict[str, Any]) -> None:
        value["sidebar_writer"] = True
        first_inside.set()
        assert release_first.wait(timeout=5)

    def second_mutation(value: dict[str, Any]) -> None:
        second_inside.set()
        value["concurrent_writer"] = True

    first = Thread(target=lambda: config_module.mutate_config(first_mutation))
    second = Thread(target=lambda: config_module.mutate_config(second_mutation))
    first.start()
    assert first_inside.wait(timeout=5)
    second.start()
    assert not second_inside.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert durable == {
        "existing": {"keep": True},
        "sidebar_writer": True,
        "concurrent_writer": True,
    }


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


def test_production_runtime_wires_real_sidebar_verifier_claim_and_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_key = b"m" * 32
    now = time.time()
    thread_id = "task.native-123"
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: "production-composition-lease",
    )
    source = SessionProjection(
        provider=Provider.CLAUDE,
        native_id="production-composition",
        title="Production composition",
        cwd=str(tmp_path),
        started_at=now - 20,
        last_active=now - 10,
        messages=(
            ProjectedMessage(
                native_event_id="production-composition-request",
                ordinal=0,
                role="user",
                content="Prove the production sidebar verifier path",
                timestamp=now - 10,
            ),
        ),
        native_cursor="cursor-production-composition",
        native_hash="hash-production-composition",
    )
    store.upsert_projection(source)
    source_id = "claude:production-composition"
    bridge_id = sidebar_bridge_id(source_id)
    store.enqueue_sidebar_job(
        SidebarCandidate(
            source_session_id=source_id,
            provider=Provider.CLAUDE,
            bridge_id=bridge_id,
            title="[Claude] Production composition",
            cwd=str(tmp_path),
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=now - 10,
        )
    )
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_key,
    )

    class ProtocolCodexClient:
        def __init__(self) -> None:
            self.published = False
            self.calls: list[str] = []
            self.closed = False

        def request(
            self, method: str, params: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            self.calls.append(method)
            if method == "thread/list":
                if self.published and params.get("archived") is False:
                    return {"data": [{
                        "id": thread_id,
                        "title": "Native task",
                        "cwd": str(tmp_path),
                        "createdAt": now,
                        "updatedAt": now,
                        "revision": "revision-1",
                    }]}
                return {"data": []}
            if method == "thread/read":
                return {"thread": {
                    "id": thread_id,
                    "turns": [{
                        "id": "registration-turn",
                        "status": "completed",
                        "items": [{
                            "type": "userMessage",
                            "id": "registration-item",
                            "content": [{"type": "text", "text": marker}],
                        }],
                    }],
                }}
            raise AssertionError(f"unexpected production request: {method}")

        def take_notification(self, timeout: float = 0.0) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    client = ProtocolCodexClient()
    backend = ProductionBackend(
        replace(
            BridgeConfig(),
            service=replace(ServiceConfig(), reconcile_seconds=0.0),
            sidebar=replace(SidebarConfig(), enabled=True),
        )
    )
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_key)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda name: (name,),
    )
    monkeypatch.setattr(
        "session_bridge.cli.CodexAppServerClient",
        lambda **_kwargs: client,
    )
    try:
        coordinator = backend._provider_runtime(
            targets=True,
            catalog_only=False,
            providers=(Provider.CODEX,),
        )
        assert isinstance(coordinator._sidebar_verifier, SidebarThreadVerifier)

        claim = asyncio.run(
            coordinator.claim_sidebar_jobs_for_delivery(now=now, limit=1)
        )[0]
        assert store.sidebar_delivery_status(now=now)["last_heartbeat_at"] == now
        client.published = True
        committed = asyncio.run(
            coordinator.commit_sidebar_job(
                lease_token=claim.lease_token,
                codex_thread_id=thread_id,
            )
        )
    finally:
        backend.close()

    assert committed["state"] == "sidebar_visible"
    assert "thread/read" in client.calls
    assert client.closed is True


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
