from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from dataclasses import dataclass, field, replace
from datetime import timezone
from pathlib import Path
from threading import Event, Thread
import time
from typing import Any, Mapping

import pytest

# The canonical runner uses ``env -i``. Windows' stdlib ignores HOME and needs
# USERPROFILE for ``Path.home()`` during imported-module initialization.
if os.name == "nt" and "USERPROFILE" not in os.environ:
    os.environ["USERPROFILE"] = os.environ["HOME"]

import session_bridge.cli as cli_module
from agent.transports.codex_app_server import CodexAppServerError

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.characterize import (
    CharacterizationGateError,
    _write_characterization_record,
    characterize_claude_visibility,
)
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    derive_claude_visibility_identity,
)
from session_bridge.cli import (
    ConfigurationFailure,
    ProductionBackend,
    ProviderDegraded,
    RolloutGateBlocked,
    _run_continuous_visibility_worker,
    _claude_characterization_open_work_allowed,
    _sync_claude_characterization_records,
    build_parser,
    main,
)
from session_bridge.codex_adapter import SidebarThreadVerifier
from session_bridge.codex_client import RecoveringCodexAppServerClient
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
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarHydrationState,
    SidebarJobState,
    canonical_session_id,
    encode_bridge_marker,
)
from session_bridge.preview import build_session_preview
from session_bridge.sidebar import (
    SidebarCandidate,
    build_registration_prompt,
    sidebar_bridge_id,
    sidebar_create_recovery_key,
)
from session_bridge.sidebar_hydration_executor import (
    SidebarHydrationExecutor,
)
from session_bridge.sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
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
            "scope": "days",
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
    sidebar_run_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "idle",
            "job_id": None,
            "thread_id": None,
            "error_code": None,
        }
    )
    sidebar_terminal_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_thread_unrecoverable",
        }
    )
    sidebar_precreate_terminal_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "precutover_create_unrecoverable",
        }
    )
    sidebar_unbound_terminal_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_create_unrecoverable",
        }
    )
    sidebar_bound_retry_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "requeued",
            "job_id": "sidebar-job:" + "a" * 64,
            "codex_thread_id": "019f-bound-retry-thread",
            "error_code": "native_task_not_indexed",
            "state": "sidebar_retry",
        }
    )
    sidebar_hydration_status_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "counts": {
                "hydration_pending": 1,
                "hydration_leased": 0,
                "hydration_retry": 0,
                "hydration_visible": 0,
                "hydration_failed": 0,
            },
            "oldest_pending_age_seconds": 12.5,
            "recent_error_codes": ["hydration_send_ambiguous"],
        }
    )
    sidebar_hydration_backfill_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "dry_run",
            "scope": "days",
            "days": 30,
            "limit": 10,
            "examined": 4,
            "eligible": 3,
            "already_readable": 1,
            "seeded": 0,
            "blocked": 0,
            "blocked_codes": {},
            "candidates": [],
        }
    )
    claude_visibility_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "continuous": False,
            "counts": {
                "claude_pending": 0,
                "claude_leased": 0,
                "claude_retry": 0,
                "claude_visible": 0,
                "claude_failed": 0,
            },
            "retry_codes": {},
            "failed_codes": {},
            "usage": {
                "local_day": "2026-07-17",
                "attempts": 0,
                "reserved_cost_usd": "0",
            },
            "lineage": {
                "unlinked_visible": 0,
                "repairable": 0,
                "blocked": 0,
                "blocker_codes": {},
            },
            "candidates": [],
            "exclusions": [],
            "open_reasons": [],
            "fatal_reasons": [],
            "degraded_reasons": [],
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

    def configure_sidebar_broker(
        self,
        *,
        thread_id: str,
        project_id: str,
        cwd: str,
        inbox_cwd: str,
    ) -> dict[str, Any]:
        self.calls.append(("configure_sidebar_broker", thread_id, project_id, cwd, inbox_cwd))
        return {
            "delivery_mode": "desktop_broker",
            "broker_thread_id": thread_id,
            "broker_project_id": project_id,
            "broker_cwd": cwd,
            "inbox_cwd": inbox_cwd,
            "heartbeat_interval_seconds": 60,
            "heartbeat_grace_seconds": 120,
            "oldest_job_alert_seconds": 300,
            "readable_preview_enabled": True,
        }

    def sidebar_backfill(
        self, *, days: int | None, limit: int, apply: bool
    ) -> dict[str, Any]:
        self.calls.append(("sidebar_backfill", days, limit, apply))
        return {
            **self.sidebar_backfill_payload,
            "mode": "apply" if apply else "dry_run",
            "scope": "all_history" if days is None else "days",
            "days": days,
            "limit": limit,
        }

    def set_sidebar_continuous(self, *, enabled: bool) -> dict[str, Any]:
        self.calls.append(("set_sidebar_continuous", enabled))
        return {"enabled": enabled, "continuous": enabled}

    def set_sidebar_readable_preview(self, *, enabled: bool) -> dict[str, Any]:
        self.calls.append(("set_sidebar_readable_preview", enabled))
        return {"readable_preview_enabled": enabled}

    def set_sidebar_hydration(self, *, enabled: bool) -> dict[str, Any]:
        self.calls.append(("set_sidebar_hydration", enabled))
        return {"legacy_hydration_enabled": enabled}

    def sidebar_hydration_seed(
        self,
        *,
        source_session_id: str,
        codex_thread_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_hydration_seed",
            source_session_id,
            codex_thread_id,
            confirmation,
        ))
        return {
            "job_id": "sidebar-hydration:" + "b" * 64,
            "source_session_id": source_session_id,
            "codex_thread_id": codex_thread_id,
            "state": "hydration_pending",
            "preview_version": 1,
            "preview_digest": "a" * 64,
        }

    def sidebar_hydration_status(self) -> dict[str, Any]:
        self.calls.append(("sidebar_hydration_status",))
        return dict(self.sidebar_hydration_status_payload)

    def sidebar_hydration_seed_backfill(
        self,
        *,
        days: int | None,
        limit: int,
        apply: bool,
        confirmation: str | None,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_hydration_seed_backfill",
            days,
            limit,
            apply,
            confirmation,
        ))
        return {
            **self.sidebar_hydration_backfill_payload,
            "mode": "apply" if apply else "dry_run",
            "scope": "all_history" if days is None else "days",
            "days": days,
            "limit": limit,
            "seeded": (
                int(self.sidebar_hydration_backfill_payload["eligible"])
                if apply
                else 0
            ),
        }

    def sidebar_run_once(self) -> dict[str, Any]:
        self.calls.append(("sidebar_run_once",))
        return dict(self.sidebar_run_payload)

    def sidebar_acknowledge_unrecoverable(
        self,
        *,
        job_id: str,
        codex_thread_id: str,
        expected_error_code: str,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_acknowledge_unrecoverable",
            job_id,
            codex_thread_id,
            expected_error_code,
        ))
        return dict(self.sidebar_terminal_payload)

    def sidebar_acknowledge_precreate_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_acknowledge_precreate_unrecoverable",
            job_id,
            expected_error_code,
        ))
        return dict(self.sidebar_precreate_terminal_payload)

    def sidebar_acknowledge_unbound_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_acknowledge_unbound_unrecoverable",
            job_id,
            expected_error_code,
        ))
        return dict(self.sidebar_unbound_terminal_payload)

    def sidebar_retry_bound(
        self,
        *,
        job_id: str,
        source_session_id: str,
        codex_thread_id: str,
        expected_error_code: str,
        confirmation: str,
    ) -> dict[str, Any]:
        self.calls.append((
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            codex_thread_id,
            expected_error_code,
            confirmation,
        ))
        return dict(self.sidebar_bound_retry_payload)

    def claude_visibility_status(self) -> dict[str, Any]:
        self.calls.append(("claude_visibility_status",))
        return dict(self.claude_visibility_payload)

    def claude_visibility_backfill(self, *, days: int, limit: int, apply: bool):
        self.calls.append(("claude_visibility_backfill", days, limit, apply))
        return {
            **self.claude_visibility_payload,
            "mode": "apply" if apply else "dry_run",
            "dry_run": not apply,
            "applied": apply,
            "enqueued": 0,
        }

    def reconcile_claude_visibility_lineage(
        self, *, limit: int, apply: bool, cursor: Mapping[str, Any] | None = None
    ):
        self.calls.append(("reconcile_claude_visibility_lineage", limit, apply, cursor))
        return {
            "mode": "apply" if apply else "dry_run",
            "scanned": 1,
            "repairable": 1,
            "repaired": 1 if apply else 0,
            "remaining": 0 if apply else 1,
            "blocker_codes": {},
            "next_cursor": None,
            "has_more": False,
            "complete": apply,
        }

    def set_claude_visibility_continuous(self, *, enabled: bool):
        self.calls.append(("set_claude_visibility_continuous", enabled))
        return {"enabled": False, "continuous": enabled}

    def claude_visibility_run_once(self):
        self.calls.append(("claude_visibility_run_once",))
        return {
            "enabled": True,
            "status": "no_due_job",
            "degraded": False,
            "fatal": False,
        }

    def abort_claude_visibility_characterization(
        self, *, expected_job_id: str, expected_reserved_claude_uuid: str
    ):
        self.calls.append((
            "abort_claude_visibility_characterization",
            expected_job_id,
            expected_reserved_claude_uuid,
        ))
        return {
            "status": "aborted_exact_absence",
            "job_id": expected_job_id,
            "reserved_claude_uuid": expected_reserved_claude_uuid,
            "replacement_created": False,
            "active_record_retired": True,
        }

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


def test_serve_uses_explicit_root_for_loader_and_restores_after_return(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    observed: list[Path] = []

    class Backend:
        def serve(self) -> None:
            assert get_hermes_home() == root

        def close(self) -> None:
            return None

    exit_code = main(
        ["serve", "--config-home", str(root)],
        config_loader=lambda: observed.append(get_hermes_home()) or BridgeConfig(),
        backend_factory=lambda _config: Backend(),
    )

    assert exit_code == 0
    assert observed == [root]
    assert get_hermes_home() != root
    assert json.loads(capsys.readouterr().out) == {"status": "stopped"}


def test_serve_defaults_explicit_state_database_under_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "root"
    config_home.mkdir()
    state_db = config_home / "state.db"
    backend = ProductionBackend(BridgeConfig(), db_path=state_db)
    opened_paths: list[Path | None] = []

    class Database:
        def close(self) -> None:
            return None

    class Store:
        def __init__(self, database: Database) -> None:
            assert database is backend._db

    class Catalog:
        def __init__(self, database: Database, store: Store) -> None:
            assert database is backend._db
            assert store is backend._store

    def open_database(db_path: Path | None = None, **_kwargs: object) -> Database:
        opened_paths.append(db_path)
        return Database()

    monkeypatch.setattr("session_bridge.cli.SessionDB", open_database)
    monkeypatch.setattr("session_bridge.cli.SessionBridgeStore", Store)
    monkeypatch.setattr("session_bridge.cli.UnifiedCatalog", Catalog)
    try:
        backend._require_catalog()
        assert opened_paths == [state_db]
    finally:
        backend.close()


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


def test_claude_skill_cli_installs_without_loading_bridge_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = tmp_path / "claude" / "skills" / "session-bridge"
    calls: list[None] = []
    monkeypatch.setattr(
        "session_bridge.cli.install_claude_skill",
        lambda: calls.append(None) or installed,
    )

    result = main(
        ["install-claude-skill"],
        config_loader=lambda: pytest.fail("installer must not load bridge config"),
        backend_factory=lambda _config: pytest.fail("installer must not start backend"),
    )

    assert result == 0
    assert calls == [None]
    assert _json_output(capsys) == {"status": "installed", "path": str(installed)}


def test_sidebar_rollout_commands_are_bounded_and_route_without_mirroring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()

    assert _run(["sidebar-status", "--json"], backend) == 0
    assert _json_output(capsys)["healthy"] is True
    assert (
        _run(
            ["sidebar-backfill", "--days", "30", "--limit", "10", "--dry-run"],
            backend,
        )
        == 0
    )
    assert _json_output(capsys)["mode"] == "dry_run"
    assert (
        _run(
            ["sidebar-backfill", "--days", "30", "--limit", "10", "--apply"],
            backend,
        )
        == 0
    )
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
    assert not any(
        call[0] in {"apply_backfill", "apply_mirror"} for call in backend.calls
    )


def test_sidebar_backfill_all_history_is_dry_run_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()

    assert (
        _run(
            [
                "sidebar-backfill",
                "--all-history",
                "--limit",
                "10",
                "--dry-run",
            ],
            backend,
        )
        == 0
    )

    payload = _json_output(capsys)
    assert payload["scope"] == "all_history"
    assert payload["queued"] == 0
    assert backend.calls == [
        ("sidebar_backfill", None, 10, False),
        ("close",),
    ]


def test_sidebar_backfill_rejects_days_with_all_history() -> None:
    backend = FakeBackend()

    with pytest.raises(SystemExit):
        _run(
            [
                "sidebar-backfill",
                "--days",
                "30",
                "--all-history",
                "--limit",
                "10",
                "--dry-run",
            ],
            backend,
        )

    assert backend.calls == []


def test_sidebar_broker_configure_persists_only_canonical_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()
    thread_id = "019f9b71-7109-7ed0-943a-d7291190245c"
    project_id = "local-453ac85f86839c6d001817cb8480b8ca"
    cwd = "C:\\Users\\diego\\Developer\\session-sidebar-broker"
    inbox_cwd = "C:\\Users\\diego\\.hermes"

    assert _run(
        [
            "sidebar-broker-configure",
            "--thread-id", thread_id,
            "--project-id", project_id,
            "--cwd", cwd,
            "--inbox-cwd", inbox_cwd,
        ],
        backend,
    ) == 0

    assert _json_output(capsys)["delivery_mode"] == "desktop_broker"
    assert backend.calls == [
        ("configure_sidebar_broker", thread_id, project_id, cwd, inbox_cwd),
        ("close",),
    ]


def test_production_sidebar_broker_configuration_persists_exact_leaves_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "delivery_mode": "desktop_broker",
        "broker_thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "broker_project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "broker_cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
        "heartbeat_interval_seconds": 60,
        "heartbeat_grace_seconds": 120,
        "oldest_job_alert_seconds": 300,
        "readable_preview_enabled": True,
    }
    original = {
        "theme": "midnight",
        "session_bridge": {
            "sidebar": {
                "enabled": False,
                "continuous": True,
                "legacy_hydration_enabled": True,
                "unrelated": "preserve",
            },
        },
    }
    persisted: list[tuple[dict[str, Any], set[tuple[str, ...]] | None]] = []

    def mutate_config(mutator, **kwargs):
        document = json.loads(json.dumps(original))
        mutator(document)
        persisted.append((document, kwargs.get("preserve_keys")))
        return document

    reloaded = BridgeConfig(
        sidebar=replace(
            SidebarConfig(),
            continuous=True,
            legacy_hydration_enabled=True,
            **expected,
        )
    )
    reloads: list[None] = []
    monkeypatch.setattr("hermes_cli.config.mutate_config", mutate_config)
    monkeypatch.setattr(
        "session_bridge.cli.BridgeConfig.load",
        lambda: reloads.append(None) or reloaded,
    )
    backend = ProductionBackend(BridgeConfig())

    result = backend.configure_sidebar_broker(
        thread_id=expected["broker_thread_id"],
        project_id=expected["broker_project_id"],
        cwd=expected["broker_cwd"],
        inbox_cwd=expected["inbox_cwd"],
    )

    assert result == expected
    assert set(result) == set(expected)
    assert reloads == [None]
    assert backend.config is reloaded
    assert persisted == [
        (
            {
                "theme": "midnight",
                "session_bridge": {
                    "sidebar": {
                        "enabled": False,
                        "continuous": True,
                        "legacy_hydration_enabled": True,
                        "unrelated": "preserve",
                        **expected,
                    },
                },
            },
            {("session_bridge", "sidebar", key) for key in expected},
        )
    ]


def test_production_sidebar_broker_configuration_fails_closed_on_reload_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
    }

    def mutate_config(mutator, **_kwargs):
        document: dict[str, Any] = {}
        mutator(document)
        return document

    monkeypatch.setattr("hermes_cli.config.mutate_config", mutate_config)
    monkeypatch.setattr(
        "session_bridge.cli.BridgeConfig.load",
        lambda: BridgeConfig(
            sidebar=replace(
                SidebarConfig(),
                broker_thread_id="different-thread",
            )
        ),
    )

    with pytest.raises(ConfigurationFailure, match="sidebar_broker_reload_mismatch"):
        ProductionBackend(BridgeConfig()).configure_sidebar_broker(**expected)


@pytest.mark.parametrize("field", ("thread_id", "project_id", "cwd", "inbox_cwd"))
@pytest.mark.parametrize("unsafe", ("bad\x00value", "bad\x85value", "bad\u2028value", "bad\u2029value"))
def test_production_sidebar_broker_configuration_rejects_unsafe_identity_text(
    field: str,
    unsafe: str,
) -> None:
    values = {
        "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
    }
    values[field] = unsafe

    with pytest.raises(ConfigurationFailure, match="invalid_sidebar_broker_identity"):
        ProductionBackend(BridgeConfig()).configure_sidebar_broker(**values)


def test_sidebar_readable_hydration_commands_are_explicit_and_never_schedule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()
    source_id = "claude:2a786924-8093-4a9f-a371-6e27ca66be32"
    thread_id = "019f8927-8012-77d0-beb0-4cd5f8cc21f9"

    assert _run(["sidebar-readable-preview", "--enable"], backend) == 0
    assert _json_output(capsys) == {"readable_preview_enabled": True}
    assert _run(["sidebar-readable-preview", "--disable"], backend) == 0
    assert _json_output(capsys) == {"readable_preview_enabled": False}
    assert _run(["sidebar-hydration", "--enable"], backend) == 0
    assert _json_output(capsys) == {"legacy_hydration_enabled": True}
    assert (
        _run(
            [
                "sidebar-hydration-seed",
                "--source-session-id",
                source_id,
                "--codex-thread-id",
                thread_id,
                "--confirm",
                "HYDRATE_EXACT_EXISTING_TASK",
            ],
            backend,
        )
        == 0
    )
    seed = _json_output(capsys)
    assert set(seed) == {
        "job_id",
        "source_session_id",
        "codex_thread_id",
        "state",
        "preview_version",
        "preview_digest",
    }
    assert _run(["sidebar-hydration-status"], backend) == 0
    status = _json_output(capsys)
    assert status == backend.sidebar_hydration_status_payload

    assert backend.calls == [
        ("set_sidebar_readable_preview", True),
        ("close",),
        ("set_sidebar_readable_preview", False),
        ("close",),
        ("set_sidebar_hydration", True),
        ("close",),
        (
            "sidebar_hydration_seed",
            source_id,
            thread_id,
            "HYDRATE_EXACT_EXISTING_TASK",
        ),
        ("close",),
        ("sidebar_hydration_status",),
        ("close",),
    ]
    assert not any("automation" in str(call).casefold() for call in backend.calls)


def test_sidebar_hydration_seed_requires_exact_confirmation_and_has_no_bulk_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()
    parser = build_parser()
    action = next(
        action
        for action in parser._subparsers._group_actions[0].choices[
            "sidebar-hydration-seed"
        ]._actions
        if action.dest == "confirm"
    )

    assert action.required is True
    assert action.choices == ("HYDRATE_EXACT_EXISTING_TASK",)
    with pytest.raises(SystemExit):
        parser.parse_args([
            "sidebar-hydration-seed",
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "thread-1",
            "--all",
            "--confirm",
            "HYDRATE_EXACT_EXISTING_TASK",
        ])
    assert capsys.readouterr().out == ""
    assert backend.calls == []


def test_sidebar_hydration_seed_backfill_defaults_to_dry_run_and_requires_apply_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()

    assert _run(["sidebar-hydration-seed-backfill", "--days", "30"], backend) == 0
    assert _json_output(capsys) == backend.sidebar_hydration_backfill_payload
    assert (
        _run(
            [
                "sidebar-hydration-seed-backfill",
                "--days",
                "30",
                "--apply",
                "--confirm",
                "HYDRATE_ALL_EXACT_EXISTING_TASKS",
            ],
            backend,
        )
        == 0
    )
    applied = _json_output(capsys)
    assert applied["mode"] == "apply"
    assert applied["seeded"] == 3
    assert backend.calls == [
        ("sidebar_hydration_seed_backfill", 30, 10, False, None),
        ("close",),
        (
            "sidebar_hydration_seed_backfill",
            30,
            10,
            True,
            "HYDRATE_ALL_EXACT_EXISTING_TASKS",
        ),
        ("close",),
    ]


def test_sidebar_hydration_seed_backfill_all_history_is_bounded_and_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeBackend()

    assert (
        _run(
            [
                "sidebar-hydration-seed-backfill",
                "--all-history",
                "--limit",
                "2",
            ],
            backend,
        )
        == 0
    )

    payload = _json_output(capsys)
    assert payload["scope"] == "all_history"
    assert payload["limit"] == 2
    assert payload["seeded"] == 0
    assert backend.calls == [
        ("sidebar_hydration_seed_backfill", None, 2, False, None),
        ("close",),
    ]


def test_sidebar_hydration_seed_backfill_rejects_days_with_all_history() -> None:
    backend = FakeBackend()

    with pytest.raises(SystemExit):
        _run(
            [
                "sidebar-hydration-seed-backfill",
                "--days",
                "30",
                "--all-history",
            ],
            backend,
        )

    assert backend.calls == []


def test_production_sidebar_hydration_seed_requires_one_exact_visible_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    thread_id = "019f8927-8012-77d0-beb0-4cd5f8cc21f9"
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: "exact-hydration-seed-lease",
    )
    store.upsert_projection(
        SessionProjection(
            provider=Provider.CLAUDE,
            native_id="exact-hydration-source",
            title="Exact hydration source",
            cwd=str(tmp_path),
            started_at=900.0,
            last_active=950.0,
            messages=(
                ProjectedMessage(
                    native_event_id="exact-hydration-message",
                    ordinal=0,
                    role="user",
                    content="Restore the readable session history",
                    timestamp=950.0,
                ),
            ),
            native_cursor="cursor-exact-hydration",
            native_hash="hash-exact-hydration",
        )
    )
    source_id = "claude:exact-hydration-source"
    bridge_id = sidebar_bridge_id(source_id)
    candidate = SidebarCandidate(
        source_session_id=source_id,
        provider=Provider.CLAUDE,
        bridge_id=bridge_id,
        title="[Claude] Exact hydration source",
        cwd=str(tmp_path),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=950.0,
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=now, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=now + 1,
    )
    store.upsert_projection(
        SessionProjection(
            provider=Provider.CODEX,
            native_id=thread_id,
            title="[Claude] Exact hydration source",
            cwd=str(tmp_path),
            started_at=950.0,
            last_active=951.0,
            messages=(
                ProjectedMessage(
                    native_event_id="registration",
                    ordinal=0,
                    role="user",
                    content="signed registration",
                    timestamp=951.0,
                ),
            ),
            native_cursor="target-cursor",
            native_hash="target-hash",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
    )
    store.create_link(
        SessionLink(
            id="exact-hydration-link",
            from_session_id=source_id,
            to_session_id=f"codex:{thread_id}",
            relation=Relation.MIRRORS,
            bridge_id=bridge_id,
            source_cursor=None,
            source_hash=None,
            created_at=now + 2,
        )
    )
    backend = ProductionBackend(
        BridgeConfig(sidebar=SidebarConfig(preview_budget_chars=24_000))
    )
    backend._store = store
    backend._catalog = object()  # type: ignore[assignment]
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)

    seeded = backend.sidebar_hydration_seed(
        source_session_id=source_id,
        codex_thread_id=thread_id,
        confirmation="HYDRATE_EXACT_EXISTING_TASK",
    )

    assert set(seeded) == {
        "job_id",
        "source_session_id",
        "codex_thread_id",
        "state",
        "preview_version",
        "preview_digest",
    }
    assert seeded["source_session_id"] == source_id
    assert seeded["codex_thread_id"] == thread_id
    assert seeded["state"] == "hydration_pending"
    with pytest.raises(RolloutGateBlocked, match="target_mismatch"):
        backend.sidebar_hydration_seed(
            source_session_id=source_id,
            codex_thread_id="different-thread",
            confirmation="HYDRATE_EXACT_EXISTING_TASK",
        )
    with pytest.raises(RolloutGateBlocked, match="confirmation"):
        backend.sidebar_hydration_seed(
            source_session_id=source_id,
            codex_thread_id=thread_id,
            confirmation="",
        )
    backend.close()


def test_production_sidebar_hydration_backfill_dry_runs_then_seeds_only_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    marker_secret = b"k" * 32
    db = SessionDB(tmp_path / "hydration-backfill.db")
    tokens = iter(("backfill-legacy-token", "backfill-readable-token"))
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: next(tokens),
    )
    prompts: dict[str, str] = {}
    candidates: dict[str, SidebarCandidate] = {}
    for index, kind in enumerate(("legacy", "readable")):
        native_id = f"hydration-backfill-{kind}"
        thread_id = f"019f8927-8012-77d0-beb0-4cd5f8cc22{index}"
        store.upsert_projection(
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id=native_id,
                title=f"{kind.title()} hydration source",
                cwd=str(tmp_path),
                started_at=900.0 + index,
                last_active=950.0 + index,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"{kind}-message",
                        ordinal=0,
                        role="user",
                        content=f"Restore the {kind} readable session history",
                        timestamp=950.0 + index,
                    ),
                ),
                native_cursor=f"cursor-{kind}",
                native_hash=f"hash-{kind}",
            )
        )
        source_id = f"claude:{native_id}"
        bridge_id = sidebar_bridge_id(source_id)
        candidate = SidebarCandidate(
            source_session_id=source_id,
            provider=Provider.CLAUDE,
            bridge_id=bridge_id,
            title=f"[Claude] {kind.title()} hydration source",
            cwd=str(tmp_path),
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=950.0 + index,
        )
        candidates[kind] = candidate
        store.enqueue_sidebar_job(candidate)
        lease = store.claim_sidebar_jobs(now=now, limit=1)[0]
        store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id=thread_id,
            now=now + index + 1,
        )
        store.upsert_projection(
            SessionProjection(
                provider=Provider.CODEX,
                native_id=thread_id,
                title=candidate.title,
                cwd=str(tmp_path),
                started_at=950.0 + index,
                last_active=951.0 + index,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"{kind}-registration",
                        ordinal=0,
                        role="user",
                        content="registered",
                        timestamp=951.0 + index,
                    ),
                ),
                native_cursor=f"target-cursor-{kind}",
                native_hash=f"target-hash-{kind}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=bridge_id,
            )
        )
        store.create_link(
            SessionLink(
                id=f"hydration-backfill-link-{kind}",
                from_session_id=source_id,
                to_session_id=f"codex:{thread_id}",
                relation=Relation.MIRRORS,
                bridge_id=bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=now + index + 2,
            )
        )
        marker = encode_bridge_marker(
            BridgeMarkerPayload(
                bridge_id=bridge_id,
                source_session_id=source_id,
                target_provider=Provider.CODEX,
                policy_generation=1,
            ),
            marker_secret,
        )
        if kind == "legacy":
            prompts[thread_id] = build_registration_prompt(candidate, marker)
        else:
            preview = build_session_preview(
                source_session_id=source_id,
                source_cursor=f"cursor-{kind}",
                source_hash=f"hash-{kind}",
                title=candidate.title,
                provider=Provider.CLAUDE.value,
                cwd=candidate.cwd,
                captured_at=951.0 + index,
                messages=[],
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
            )
            prompts[thread_id] = build_registration_prompt(
                candidate,
                marker,
                preview=preview,
            )

    class ExactReader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def read_thread_initial_prompt(
            self,
            *,
            thread_id: str,
            deadline: float,
        ) -> str:
            assert deadline > 0
            self.calls.append(thread_id)
            return prompts[thread_id]

    reader = ExactReader()
    backend = ProductionBackend(
        BridgeConfig(
            sidebar=SidebarConfig(
                legacy_hydration_enabled=True,
                preview_budget_chars=24_000,
            )
        )
    )
    backend._store = store
    backend._catalog = object()  # type: ignore[assignment]
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: now)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_delivery",
        lambda: reader,
    )
    legacy_job = store.get_sidebar_job_for_source(
        candidates["legacy"].source_session_id
    )
    assert legacy_job is not None

    dry_run = backend.sidebar_hydration_seed_backfill(
        days=30,
        limit=10,
        apply=False,
        confirmation=None,
    )

    assert dry_run == {
        "mode": "dry_run",
        "scope": "days",
        "days": 30,
        "limit": 10,
        "examined": 2,
        "eligible": 1,
        "already_readable": 1,
        "seeded": 0,
        "blocked": 0,
        "blocked_codes": {},
        "candidates": [
            {
                "source_session_id": candidates["legacy"].source_session_id,
                "codex_thread_id": str(legacy_job["codex_thread_id"]),
                "visible_at": 1_001.0,
                "hydration_state": "not_seeded",
            }
        ],
    }
    assert store.sidebar_hydration_status(now)["counts"]["hydration_pending"] == 0
    with pytest.raises(RolloutGateBlocked, match="confirmation"):
        backend.sidebar_hydration_seed_backfill(
            days=30,
            limit=10,
            apply=True,
            confirmation=None,
        )

    applied = backend.sidebar_hydration_seed_backfill(
        days=30,
        limit=10,
        apply=True,
        confirmation="HYDRATE_ALL_EXACT_EXISTING_TASKS",
    )

    assert applied == {
        **dry_run,
        "mode": "apply",
        "seeded": 1,
        "candidates": [
            {
                **dry_run["candidates"][0],
                "hydration_state": SidebarHydrationState.PENDING.value,
            }
        ],
    }
    assert store.sidebar_hydration_status(now)["counts"]["hydration_pending"] == 1
    readable_job = store.get_sidebar_job_for_source(
        candidates["readable"].source_session_id
    )
    prompts[str(readable_job["codex_thread_id"])] = prompts[
        str(legacy_job["codex_thread_id"])
    ]

    blocked = backend.sidebar_hydration_seed_backfill(
        days=30,
        limit=10,
        apply=True,
        confirmation="HYDRATE_ALL_EXACT_EXISTING_TASKS",
    )

    assert blocked == {
        "mode": "apply",
        "scope": "days",
        "days": 30,
        "limit": 10,
        "examined": 1,
        "eligible": 0,
        "already_readable": 0,
        "seeded": 0,
        "blocked": 1,
        "blocked_codes": {"hydration_target_identity_mismatch": 1},
        "candidates": [],
    }
    assert store.sidebar_hydration_status(now)["counts"]["hydration_pending"] == 1
    backend.close()


def test_sidebar_run_once_requires_desktop_broker_before_runtime_startup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def forbidden_config_loader() -> BridgeConfig:
        calls.append("config_loader")
        raise AssertionError("sidebar diagnostic must not load config")

    def forbidden_backend_factory(_config: BridgeConfig) -> FakeBackend:
        calls.append("backend_factory")
        raise AssertionError("sidebar diagnostic must not construct a backend")

    assert (
        main(
            ["sidebar-run-once"],
            config_loader=forbidden_config_loader,
            backend_factory=forbidden_backend_factory,
        )
        == 3
    )

    assert _json_output(capsys) == {"error": "desktop_broker_required"}
    assert calls == []


def test_sidebar_retry_bound_requires_exact_authority_and_preserves_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "a" * 64
    source_session_id = "claude:2a23388a-5d72-4e0d-bbad-5e655ef4c8a3"
    thread_id = "019f8bed-2a9f-7353-a841-551fc1c8b68e"
    backend = FakeBackend(
        sidebar_bound_retry_payload={
            "status": "requeued",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "error_code": "native_task_not_indexed",
            "state": "sidebar_retry",
            "private_detail": "C:/private/provider-detail",
        }
    )

    assert (
        _run(
            [
                "sidebar-retry-bound",
                "--job-id",
                job_id,
                "--source-session-id",
                source_session_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "native_task_not_indexed",
                "--confirm",
                "PRESERVE_EXACT_BOUND_TASK",
            ],
            backend,
        )
        == 0
    )

    assert backend.calls == [
        (
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            thread_id,
            "native_task_not_indexed",
            "PRESERVE_EXACT_BOUND_TASK",
        ),
        ("close",),
    ]
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": "sidebar_retry",
    }
    assert "private/provider-detail" not in rendered


def test_sidebar_retry_bound_accepts_exact_project_drift_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "b" * 64
    source_session_id = "claude:1b04a5be-69ff-4c08-afa4-0a0e534cede6"
    thread_id = "019fa02c-8d73-7a73-a04b-e838e26b88c2"
    backend = FakeBackend(
        sidebar_bound_retry_payload={
            "status": "requeued",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "error_code": "codex_thread_conflict",
            "state": "sidebar_retry",
        }
    )

    assert (
        _run(
            [
                "sidebar-retry-bound",
                "--job-id",
                job_id,
                "--source-session-id",
                source_session_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "codex_thread_conflict",
                "--confirm",
                "PRESERVE_EXACT_BOUND_TASK",
            ],
            backend,
        )
        == 0
    )

    assert backend.calls == [
        (
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            thread_id,
            "codex_thread_conflict",
            "PRESERVE_EXACT_BOUND_TASK",
        ),
        ("close",),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": "sidebar_retry",
    }


def test_sidebar_retry_bound_accepts_exact_idempotent_marker_replay(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "c" * 64
    source_session_id = "claude:2775e933-7684-486f-a681-e50647dbf17c"
    thread_id = "019fa687-7ee0-79d1-92d9-17294ea810ab"
    backend = FakeBackend(
        sidebar_bound_retry_payload={
            "status": "requeued",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "error_code": "marker_conflict",
            "state": "sidebar_retry",
        }
    )

    assert (
        _run(
            [
                "sidebar-retry-bound",
                "--job-id",
                job_id,
                "--source-session-id",
                source_session_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "marker_conflict",
                "--confirm",
                "PRESERVE_EXACT_BOUND_TASK",
            ],
            backend,
        )
        == 0
    )

    assert backend.calls == [
        (
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            thread_id,
            "marker_conflict",
            "PRESERVE_EXACT_BOUND_TASK",
        ),
        ("close",),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": "sidebar_retry",
    }


def test_sidebar_retry_bound_parser_accepts_exact_transient_bridge_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "d" * 64
    source_session_id = "claude:transient-bridge-failure"
    thread_id = "019f-transient-bridge-failure"
    backend = FakeBackend(
        sidebar_bound_retry_payload={
            "status": "requeued",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "error_code": "bridge_temporarily_unavailable",
            "state": "sidebar_retry",
        }
    )

    assert (
        _run(
            [
                "sidebar-retry-bound",
                "--job-id",
                job_id,
                "--source-session-id",
                source_session_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "bridge_temporarily_unavailable",
                "--confirm",
                "PRESERVE_EXACT_BOUND_TASK",
            ],
            backend,
        )
        == 0
    )
    assert backend.calls == [
        (
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            thread_id,
            "bridge_temporarily_unavailable",
            "PRESERVE_EXACT_BOUND_TASK",
        ),
        ("close",),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": "sidebar_retry",
    }


def test_sidebar_retry_bound_parser_accepts_repaired_source_identity_with_narrow_authority(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "e" * 64
    source_session_id = "claude:repaired-source-identity"
    thread_id = "019f-repaired-source-identity"
    confirmation = "PRESERVE_EXACT_BOUND_TASK_AFTER_SOURCE_CWD_REPAIR"
    backend = FakeBackend(
        sidebar_bound_retry_payload={
            "status": "requeued",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "error_code": "source_identity_mismatch",
            "state": "sidebar_retry",
        }
    )

    assert (
        _run(
            [
                "sidebar-retry-bound",
                "--job-id",
                job_id,
                "--source-session-id",
                source_session_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "source_identity_mismatch",
                "--confirm",
                confirmation,
            ],
            backend,
        )
        == 0
    )
    assert backend.calls == [
        (
            "sidebar_retry_bound",
            job_id,
            source_session_id,
            thread_id,
            "source_identity_mismatch",
            confirmation,
        ),
        ("close",),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": "sidebar_retry",
    }


@pytest.mark.parametrize(
    "argv",
    (
        [
            "sidebar-retry-bound",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "019f-bound-thread",
            "--expected-error-code",
            "native_task_not_indexed",
        ],
        [
            "sidebar-retry-bound",
            "--job-id",
            "not-a-full-job-id",
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "019f-bound-thread",
            "--expected-error-code",
            "native_task_not_indexed",
            "--confirm",
            "PRESERVE_EXACT_BOUND_TASK",
        ],
        [
            "sidebar-retry-bound",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "019f-bound-thread",
            "--expected-error-code",
            "source_cwd_missing",
            "--confirm",
            "PRESERVE_EXACT_BOUND_TASK",
        ],
        [
            "sidebar-retry-bound",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "019f-bound-thread",
            "--expected-error-code",
            "native_task_not_indexed",
            "--confirm",
            "REPLACE_BOUND_TASK",
        ],
    ),
)
def test_sidebar_retry_bound_parser_rejects_incomplete_authority(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit):
        main(argv)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "already_requeued"),
        ("state", "sidebar_pending"),
        ("error_code", "marker_conflict"),
        ("job_id", "not-a-job-id"),
        ("codex_thread_id", "invalid thread id"),
    ),
)
def test_sidebar_retry_bound_rejects_invalid_backend_results(
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    payload = {
        "status": "requeued",
        "job_id": "sidebar-job:" + "a" * 64,
        "codex_thread_id": "019f-bound-retry-thread",
        "error_code": "native_task_not_indexed",
        "state": "sidebar_retry",
    }
    payload[field] = value
    backend = FakeBackend(sidebar_bound_retry_payload=payload)

    result = _run(
        [
            "sidebar-retry-bound",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--source-session-id",
            "claude:source",
            "--codex-thread-id",
            "019f-bound-retry-thread",
            "--expected-error-code",
            "native_task_not_indexed",
            "--confirm",
            "PRESERVE_EXACT_BOUND_TASK",
        ],
        backend,
    )

    assert result == 3
    assert _json_output(capsys) == {"error": "provider_degraded"}


def test_sidebar_terminal_acknowledgement_requires_exact_operator_authority_and_sanitizes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "a" * 64
    thread_id = "019f-operator-terminal-thread"
    backend = FakeBackend(
        sidebar_terminal_payload={
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_thread_unrecoverable",
            "job_id": job_id,
            "codex_thread_id": thread_id,
            "evidence_digest": "e" * 64,
            "private_detail": "C:/private/provider-detail",
        }
    )

    assert (
        _run(
            [
                "sidebar-acknowledge-unrecoverable",
                "--job-id",
                job_id,
                "--codex-thread-id",
                thread_id,
                "--expected-error-code",
                "native_create_ambiguous",
                "--confirm",
                "native-thread-unrecoverable",
            ],
            backend,
        )
        == 0
    )

    assert backend.calls == [
        (
            "sidebar_acknowledge_unrecoverable",
            job_id,
            thread_id,
            "native_create_ambiguous",
        ),
        ("close",),
    ]
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {
        "status": "acknowledged",
        "error_code": "native_create_ambiguous",
        "resolution_code": "native_thread_unrecoverable",
    }
    for private in (job_id, thread_id, "e" * 64, "private/provider-detail"):
        assert private not in rendered


@pytest.mark.parametrize(
    "argv",
    (
        [
            "sidebar-acknowledge-unrecoverable",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--codex-thread-id",
            "019f-terminal-thread",
            "--expected-error-code",
            "native_create_ambiguous",
        ],
        [
            "sidebar-acknowledge-unrecoverable",
            "--job-id",
            "not-a-full-job-id",
            "--codex-thread-id",
            "019f-terminal-thread",
            "--expected-error-code",
            "native_create_ambiguous",
            "--confirm",
            "native-thread-unrecoverable",
        ],
        [
            "sidebar-acknowledge-unrecoverable",
            "--job-id",
            "sidebar-job:" + "a" * 64,
            "--codex-thread-id",
            "019f-terminal-thread",
            "--expected-error-code",
            "marker_conflict",
            "--confirm",
            "native-thread-unrecoverable",
        ],
    ),
)
def test_sidebar_terminal_acknowledgement_parser_rejects_incomplete_authority(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit):
        main(argv)


def test_sidebar_precreate_acknowledgement_requires_exact_operator_authority_and_sanitizes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "b" * 64
    backend = FakeBackend(
        sidebar_precreate_terminal_payload={
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "precutover_create_unrecoverable",
            "job_id": job_id,
            "recovery_key": "hermes-session-bridge-create-v1:private",
            "evidence_digest": "f" * 64,
            "private_detail": "C:/private/provider-detail",
        }
    )

    assert (
        _run(
            [
                "sidebar-acknowledge-precreate-unrecoverable",
                "--job-id",
                job_id,
                "--expected-error-code",
                "native_create_ambiguous",
                "--confirm",
                "precutover-create-unrecoverable",
            ],
            backend,
        )
        == 0
    )

    assert backend.calls == [
        (
            "sidebar_acknowledge_precreate_unrecoverable",
            job_id,
            "native_create_ambiguous",
        ),
        ("close",),
    ]
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {
        "status": "acknowledged",
        "error_code": "native_create_ambiguous",
        "resolution_code": "precutover_create_unrecoverable",
    }
    for private in (job_id, "hermes-session-bridge-create-v1", "f" * 64, "private"):
        assert private not in rendered


@pytest.mark.parametrize(
    "argv",
    (
        [
            "sidebar-acknowledge-precreate-unrecoverable",
            "--job-id",
            "sidebar-job:" + "b" * 64,
            "--expected-error-code",
            "native_create_ambiguous",
        ],
        [
            "sidebar-acknowledge-precreate-unrecoverable",
            "--job-id",
            "not-a-full-job-id",
            "--expected-error-code",
            "native_create_ambiguous",
            "--confirm",
            "precutover-create-unrecoverable",
        ],
        [
            "sidebar-acknowledge-precreate-unrecoverable",
            "--job-id",
            "sidebar-job:" + "b" * 64,
            "--expected-error-code",
            "marker_conflict",
            "--confirm",
            "precutover-create-unrecoverable",
        ],
    ),
)
def test_sidebar_precreate_acknowledgement_parser_rejects_incomplete_authority(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit):
        main(argv)


def test_sidebar_unbound_acknowledgement_requires_exact_operator_authority_and_sanitizes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = "sidebar-job:" + "c" * 64
    backend = FakeBackend(
        sidebar_unbound_terminal_payload={
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_create_unrecoverable",
            "job_id": job_id,
            "recovery_key": "hermes-session-bridge-create-v1:private",
            "evidence_digest": "e" * 64,
        }
    )

    assert (
        _run(
            [
                "sidebar-acknowledge-unbound-unrecoverable",
                "--job-id",
                job_id,
                "--expected-error-code",
                "native_create_ambiguous",
                "--confirm",
                "unbound-create-unrecoverable",
            ],
            backend,
        )
        == 0
    )
    assert backend.calls == [
        (
            "sidebar_acknowledge_unbound_unrecoverable",
            job_id,
            "native_create_ambiguous",
        ),
        ("close",),
    ]
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {
        "status": "acknowledged",
        "error_code": "native_create_ambiguous",
        "resolution_code": "native_create_unrecoverable",
    }
    for private in (job_id, "hermes-session-bridge-create-v1", "e" * 64, "private"):
        assert private not in rendered


class _TerminalProbeClient:
    def __init__(self, *, scenario: str, thread_id: str) -> None:
        self.scenario = scenario
        self.thread_id = thread_id
        self._initialized = False
        self.initialize_calls = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def initialize(self, *, timeout: float, **kwargs: object) -> dict[str, object]:
        assert timeout > 0
        assert kwargs == {"capabilities": {"experimentalApi": True}}
        self.initialize_calls += 1
        self._initialized = True
        return {}

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, object]:
        assert timeout > 0
        exact_params = dict(params or {})
        self.calls.append((method, exact_params))
        assert exact_params["threadId"] == self.thread_id
        if method == "thread/read":
            assert exact_params == {
                "threadId": self.thread_id,
                "includeTurns": True,
            }
            if self.scenario in {"unrecoverable", "resume_transient"}:
                raise CodexAppServerError(
                    code=-32600,
                    message=f"thread not loaded: {self.thread_id}",
                )
            if self.scenario == "read_transient":
                raise TimeoutError("private provider timeout")
            observed = (
                "019f-different-thread"
                if self.scenario == "malformed"
                else self.thread_id
            )
            return {
                "thread": {
                    "id": observed,
                    "cwd": "C:/workspace/project",
                    "turns": [],
                    "status": {"type": "idle"},
                }
            }
        if method == "thread/resume":
            assert exact_params == {"threadId": self.thread_id}
            if self.scenario == "resume_transient":
                raise TimeoutError("private resume timeout")
            if self.scenario == "unrecoverable":
                raise CodexAppServerError(
                    code=-32600,
                    message=f"no rollout found for thread id {self.thread_id}",
                )
        raise AssertionError(f"forbidden provider method: {method}")

    def close(self, timeout: float = 3.0) -> None:
        del timeout
        self.closed = True


def _record_cli_absence_proof(
    store: SessionBridgeStore,
    lease_token: str,
    *,
    completed_at: float = 100.0,
) -> dict[str, Any]:
    evidence = SidebarReconciliationEvidence.create(
        state=SidebarReconciliationState.ABSENCE_PROVEN,
        generation=f"cli-scan:{completed_at}",
        completed_at=completed_at,
        expires_at=completed_at + 1_000.0,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=0,
        recovered_thread_id=None,
        fixed_reason=None,
    )
    return store.record_sidebar_reconciliation_proof(
        lease_token=lease_token,
        evidence=evidence,
        marker_digest=evidence.marker_digest,
        placement_generation=1,
        delivery_generation=1,
        now=completed_at,
    )


def _production_terminal_resolution_backend(
    tmp_path: Path,
    *,
    scenario: str,
) -> tuple[
    ProductionBackend,
    SessionBridgeStore,
    dict[str, Any],
    dict[str, Any],
    _TerminalProbeClient,
]:
    db = SessionDB(tmp_path / f"terminal-{scenario}.db")
    tokens = iter((f"terminal-{scenario}-lease",))
    store = SessionBridgeStore(db, sidebar_token_factory=lambda: next(tokens))
    source_session_id = f"hermes:terminal-{scenario}"
    db.ensure_session(source_session_id, source="cli")
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title="[Hermes] terminal evidence",
        cwd=str(tmp_path),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_cli_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key=f"hermes-session-bridge-create-v1:terminal-{scenario}",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    thread_id = f"019f-terminal-provider-{scenario}"
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=150.0,
    )
    client = _TerminalProbeClient(scenario=scenario, thread_id=thread_id)
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    backend._sidebar_codex_client = client  # type: ignore[assignment]
    return backend, store, failed, reservation, client


def _production_bound_retry_backend(
    tmp_path: Path,
) -> tuple[
    ProductionBackend,
    SessionBridgeStore,
    dict[str, Any],
    dict[str, Any],
]:
    db = SessionDB(tmp_path / "bound-retry.db")
    tokens = iter(f"bound-retry-token-{attempt}" for attempt in range(1, 7))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    source_session_id = "hermes:bound-retry"
    db.ensure_session(source_session_id, source="cli")
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title="[Hermes] bound retry",
        cwd=str(tmp_path),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_cli_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key="hermes-session-bridge-create-v1:bound-retry",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    thread_id = "019f-production-bound-retry"
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )
    failed = None
    for _attempt in range(5):
        failed = store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="native_task_not_indexed",
            codex_thread_id=thread_id,
            now=150.0 if failed is None else failed["next_attempt_at"],
        )
        if failed["state"] == SidebarJobState.RETRY.value:
            lease = store.claim_sidebar_jobs(
                now=failed["next_attempt_at"],
                limit=1,
            )[0]
    assert failed is not None
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    return backend, store, failed, reservation


def test_production_sidebar_retry_bound_preserves_exact_task_and_is_single_use(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    try:
        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="native_task_not_indexed",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "native_task_not_indexed",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
        with pytest.raises(
            RolloutGateBlocked,
            match="sidebar_bound_retry_snapshot_mismatch",
        ):
            backend.sidebar_retry_bound(
                job_id=failed["id"],
                source_session_id=source_session_id,
                codex_thread_id=thread_id,
                expected_error_code="native_task_not_indexed",
                confirmation="PRESERVE_EXACT_BOUND_TASK",
            )
    finally:
        backend.close()


def test_production_sidebar_retry_bound_accepts_exact_project_drift_conflict(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("codex_thread_conflict", failed["id"]),
        )
    )
    try:
        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="codex_thread_conflict",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "codex_thread_conflict",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
    finally:
        backend.close()


def test_production_sidebar_retry_bound_accepts_exact_ambiguous_create(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("native_create_ambiguous", failed["id"]),
        )
    )
    try:
        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="native_create_ambiguous",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "native_create_ambiguous",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
    finally:
        backend.close()


def test_production_sidebar_retry_bound_accepts_exact_idempotent_marker_replay(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("marker_conflict", failed["id"]),
        )
    )
    try:
        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="marker_conflict",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "marker_conflict",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
    finally:
        backend.close()


def test_production_sidebar_retry_bound_accepts_exact_transient_bridge_failure(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("bridge_temporarily_unavailable", failed["id"]),
        )
    )
    try:
        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="bridge_temporarily_unavailable",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "bridge_temporarily_unavailable",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
    finally:
        backend.close()


def test_production_sidebar_retry_bound_accepts_repaired_source_identity_only_with_narrow_authority(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation = _production_bound_retry_backend(tmp_path)
    source_session_id = failed["source_session_id"]
    thread_id = failed["codex_thread_id"]
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("source_identity_mismatch", failed["id"]),
        )
    )
    try:
        with pytest.raises(
            RolloutGateBlocked,
            match="sidebar_bound_retry_snapshot_mismatch",
        ):
            backend.sidebar_retry_bound(
                job_id=failed["id"],
                source_session_id=source_session_id,
                codex_thread_id=thread_id,
                expected_error_code="source_identity_mismatch",
                confirmation="PRESERVE_EXACT_BOUND_TASK",
            )

        result = backend.sidebar_retry_bound(
            job_id=failed["id"],
            source_session_id=source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="source_identity_mismatch",
            confirmation="PRESERVE_EXACT_BOUND_TASK_AFTER_SOURCE_CWD_REPAIR",
        )

        assert result == {
            "status": "requeued",
            "job_id": failed["id"],
            "codex_thread_id": thread_id,
            "error_code": "source_identity_mismatch",
            "state": SidebarJobState.RETRY.value,
        }
        assert store.get_sidebar_create_reservation(source_session_id) == reservation
    finally:
        backend.close()


def test_production_terminal_acknowledgement_derives_exact_evidence_and_replays(
    tmp_path: Path,
) -> None:
    backend, store, failed, reservation, client = (
        _production_terminal_resolution_backend(tmp_path, scenario="unrecoverable")
    )
    source_session_id = failed["source_session_id"]
    before_job = store.get_sidebar_job_for_source(source_session_id)
    before_reservation = store.get_sidebar_create_reservation(source_session_id)
    try:
        first = backend.sidebar_acknowledge_unrecoverable(
            job_id=failed["id"],
            codex_thread_id=failed["codex_thread_id"],
            expected_error_code="native_create_ambiguous",
        )
        replay = backend.sidebar_acknowledge_unrecoverable(
            job_id=failed["id"],
            codex_thread_id=failed["codex_thread_id"],
            expected_error_code="native_create_ambiguous",
        )

        assert first == {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_thread_unrecoverable",
        }
        assert replay == {**first, "status": "already_acknowledged"}
        assert store.get_sidebar_job_for_source(source_session_id) == before_job
        assert (
            store.get_sidebar_create_reservation(source_session_id)
            == before_reservation
            == reservation
        )
        [audit] = store.db._conn.execute(
            "SELECT * FROM session_sidebar_terminal_resolutions"
        ).fetchall()
        assert len(audit["evidence_digest"]) == 64
        assert set(audit["evidence_digest"]) <= set("0123456789abcdef")
        assert audit["codex_thread_id"] == failed["codex_thread_id"]
        assert client.initialize_calls == 1
        assert [method for method, _params in client.calls] == [
            "thread/read",
            "thread/resume",
            "thread/read",
            "thread/resume",
        ]
    finally:
        backend.close()
    assert client.closed is True


@pytest.mark.parametrize(
    ("scenario", "expected_methods", "expected_exception"),
    (
        ("materialized", ["thread/read"], RolloutGateBlocked),
        ("read_transient", ["thread/read"], ProviderDegraded),
        ("resume_transient", ["thread/read", "thread/resume"], ProviderDegraded),
        ("malformed", ["thread/read"], ProviderDegraded),
    ),
)
def test_production_terminal_acknowledgement_never_writes_without_exact_evidence(
    tmp_path: Path,
    scenario: str,
    expected_methods: list[str],
    expected_exception: type[Exception],
) -> None:
    backend, store, failed, _reservation, client = (
        _production_terminal_resolution_backend(tmp_path, scenario=scenario)
    )
    before = store.get_sidebar_job_for_source(failed["source_session_id"])
    try:
        with pytest.raises(expected_exception):
            backend.sidebar_acknowledge_unrecoverable(
                job_id=failed["id"],
                codex_thread_id=failed["codex_thread_id"],
                expected_error_code="native_create_ambiguous",
            )
        assert [method for method, _params in client.calls] == expected_methods
        assert store.get_sidebar_job_for_source(failed["source_session_id"]) == before
        assert (
            store.db._conn.execute(
                "SELECT COUNT(*) FROM session_sidebar_terminal_resolutions"
            ).fetchone()[0]
            == 0
        )
    finally:
        backend.close()


def test_production_terminal_acknowledgement_rejects_noncanonical_job_before_probe(
    tmp_path: Path,
) -> None:
    backend, store, failed, _reservation, client = (
        _production_terminal_resolution_backend(tmp_path, scenario="unrecoverable")
    )
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET idempotency_key = ? WHERE id = ?",
            ("corrupt-terminal-idempotency", failed["id"]),
        )
    )
    try:
        with pytest.raises(
            RolloutGateBlocked, match="sidebar_terminal_snapshot_mismatch"
        ):
            backend.sidebar_acknowledge_unrecoverable(
                job_id=failed["id"],
                codex_thread_id=failed["codex_thread_id"],
                expected_error_code="native_create_ambiguous",
            )
        assert client.calls == []
        assert (
            store.db._conn.execute(
                "SELECT COUNT(*) FROM session_sidebar_terminal_resolutions"
            ).fetchone()[0]
            == 0
        )
    finally:
        backend.close()


class _PrecreateProbeVerifier:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.marker_calls: list[BridgeMarkerPayload] = []
        self.terminal_marker_calls: list[BridgeMarkerPayload] = []
        self.recovery_calls: list[tuple[str, str, float]] = []
        self.create_calls: list[object] = []

    def find_by_marker(self, expected: BridgeMarkerPayload) -> object | None:
        self.marker_calls.append(expected)
        if self.scenario == "marker_error":
            raise TimeoutError("private marker probe timeout")
        if self.scenario == "marker_match":
            return object()
        return None

    def find_by_marker_including_archived(
        self,
        expected: BridgeMarkerPayload,
    ) -> object | None:
        self.terminal_marker_calls.append(expected)
        if self.scenario == "marker_error":
            raise TimeoutError("private marker probe timeout")
        if self.scenario in {"marker_match", "archived_marker_match"}:
            return object()
        return None

    @property
    def all_marker_calls(self) -> list[BridgeMarkerPayload]:
        return [*self.marker_calls, *self.terminal_marker_calls]

    def find_by_recovery_key(
        self,
        recovery_key: str,
        *,
        expected_cwd: str,
        deadline: float,
    ) -> str | None:
        self.recovery_calls.append((recovery_key, expected_cwd, deadline))
        if self.scenario == "recovery_error":
            raise TimeoutError("private recovery probe timeout")
        if self.scenario == "recovery_match":
            return "019f-private-recovered-thread"
        return None

    def create_thread(self, *_args: object, **_kwargs: object) -> None:
        self.create_calls.append(object())
        raise AssertionError("precreate terminal proof must never create")


def _production_precreate_resolution_backend(
    tmp_path: Path,
    *,
    marker_secret: bytes,
) -> tuple[
    ProductionBackend,
    SessionBridgeStore,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    SidebarCandidate,
]:
    db = SessionDB(tmp_path / "precreate-terminal.db")
    tokens = iter(("precreate-terminal-token",))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    source_session_id = "hermes:precreate-terminal"
    db.ensure_session(source_session_id, source="cli")
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title="[Hermes] precreate terminal evidence",
        cwd=str(tmp_path),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    queued = store.enqueue_sidebar_job(candidate)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET state = ?, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (SidebarJobState.RETRY.value, 105.0, 105.0, queued["id"]),
        )
    )
    store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=110.0,
    )
    lease = store.claim_sidebar_jobs(now=120.0, limit=1)[0]
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=130.0,
    )
    reservation = store.get_sidebar_create_reservation(source_session_id)
    cutover = store.get_state("session-bridge:sidebar:create-reservation-cutover:v1")
    assert reservation is not None
    assert cutover is not None
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    return backend, store, failed, reservation, cutover, candidate


def _production_unbound_resolution_backend(
    tmp_path: Path,
    *,
    marker_secret: bytes,
) -> tuple[
    ProductionBackend,
    SessionBridgeStore,
    dict[str, Any],
    dict[str, Any],
    SidebarCandidate,
]:
    db = SessionDB(tmp_path / "unbound-terminal.db")
    tokens = iter(("unbound-terminal-token-1", "unbound-terminal-token-2"))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    source_session_id = "hermes:unbound-terminal"
    db.ensure_session(source_session_id, source="cli")
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title="[Hermes] unbound terminal evidence",
        cwd=str(tmp_path),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=candidate.bridge_id,
            source_session_id=candidate.source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_secret,
    )
    proof = _record_cli_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key=sidebar_create_recovery_key(marker, marker_secret),
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=105.0,
    )
    retry = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="bridge_temporarily_unavailable",
        now=110.0,
    )
    lease = store.claim_sidebar_jobs(now=retry["next_attempt_at"], limit=1)[0]
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=retry["next_attempt_at"] + 1.0,
    )
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    return backend, store, failed, reservation, candidate


def test_production_unbound_acknowledgement_probes_exact_identities_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_secret = b"unbound-cli-marker-secret"
    backend, store, failed, reservation, candidate = (
        _production_unbound_resolution_backend(
            tmp_path,
            marker_secret=marker_secret,
        )
    )
    verifier = _PrecreateProbeVerifier("zero")
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_verifier",
        lambda *, marker_secret: verifier,
        raising=False,
    )
    before_job = store.get_sidebar_job_for_source(candidate.source_session_id)
    try:
        first = backend.sidebar_acknowledge_unbound_unrecoverable(
            job_id=failed["id"],
            expected_error_code="native_create_ambiguous",
        )
        replay = backend.sidebar_acknowledge_unbound_unrecoverable(
            job_id=failed["id"],
            expected_error_code="native_create_ambiguous",
        )

        assert first == {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "native_create_unrecoverable",
        }
        assert replay == {**first, "status": "already_acknowledged"}
        assert len(verifier.all_marker_calls) == 2
        assert [
            (recovery_key, cwd)
            for recovery_key, cwd, _deadline in verifier.recovery_calls
        ] == [(reservation["recovery_key"], candidate.cwd)] * 2
        assert verifier.create_calls == []
        assert (
            store.get_sidebar_job_for_source(candidate.source_session_id) == before_job
        )
        [audit] = store.db._conn.execute(
            "SELECT * FROM session_sidebar_unbound_resolutions"
        ).fetchall()
        assert audit["resolution_code"] == "native_create_unrecoverable"
        assert len(audit["evidence_digest"]) == 64
    finally:
        backend.close()


def test_production_precreate_acknowledgement_probes_exact_identities_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_secret = b"precreate-cli-marker-secret"
    backend, store, failed, reservation, _cutover, candidate = (
        _production_precreate_resolution_backend(
            tmp_path,
            marker_secret=marker_secret,
        )
    )
    verifier = _PrecreateProbeVerifier("zero")
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_verifier",
        lambda *, marker_secret: verifier,
        raising=False,
    )
    before_job = store.get_sidebar_job_for_source(candidate.source_session_id)
    try:
        first = backend.sidebar_acknowledge_precreate_unrecoverable(
            job_id=failed["id"],
            expected_error_code="native_create_ambiguous",
        )
        replay = backend.sidebar_acknowledge_precreate_unrecoverable(
            job_id=failed["id"],
            expected_error_code="native_create_ambiguous",
        )

        assert first == {
            "status": "acknowledged",
            "error_code": "native_create_ambiguous",
            "resolution_code": "precutover_create_unrecoverable",
        }
        assert replay == {**first, "status": "already_acknowledged"}
        assert (
            verifier.all_marker_calls
            == [
                BridgeMarkerPayload(
                    bridge_id=candidate.bridge_id,
                    source_session_id=candidate.source_session_id,
                    target_provider=Provider.CODEX,
                    policy_generation=1,
                )
            ]
            * 2
        )
        assert [
            (recovery_key, cwd)
            for recovery_key, cwd, _deadline in verifier.recovery_calls
        ] == [(reservation["recovery_key"], candidate.cwd)] * 2
        assert all(
            isinstance(deadline, float) and deadline > 0
            for _recovery_key, _cwd, deadline in verifier.recovery_calls
        )
        assert verifier.create_calls == []
        assert (
            store.get_sidebar_job_for_source(candidate.source_session_id) == before_job
        )
        [audit] = store.db._conn.execute(
            "SELECT * FROM session_sidebar_precreate_resolutions"
        ).fetchall()
        assert audit["resolution_code"] == "precutover_create_unrecoverable"
        assert len(audit["evidence_digest"]) == 64
    finally:
        backend.close()


@pytest.mark.parametrize(
    ("scenario", "expected_exception", "expected_recovery_calls"),
    (
        ("marker_match", RolloutGateBlocked, 0),
        ("recovery_match", RolloutGateBlocked, 1),
        ("marker_error", ProviderDegraded, 0),
        ("recovery_error", ProviderDegraded, 1),
    ),
)
def test_production_precreate_acknowledgement_never_writes_without_two_zero_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_exception: type[Exception],
    expected_recovery_calls: int,
) -> None:
    marker_secret = b"precreate-cli-marker-secret"
    backend, store, failed, _reservation, _cutover, candidate = (
        _production_precreate_resolution_backend(
            tmp_path,
            marker_secret=marker_secret,
        )
    )
    verifier = _PrecreateProbeVerifier(scenario)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_verifier",
        lambda *, marker_secret: verifier,
        raising=False,
    )
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    try:
        with pytest.raises(expected_exception):
            backend.sidebar_acknowledge_precreate_unrecoverable(
                job_id=failed["id"],
                expected_error_code="native_create_ambiguous",
            )
        assert len(verifier.all_marker_calls) == 1
        assert len(verifier.recovery_calls) == expected_recovery_calls
        assert verifier.create_calls == []
        assert store.get_sidebar_job_for_source(candidate.source_session_id) == before
        assert (
            store.db._conn.execute(
                "SELECT COUNT(*) FROM session_sidebar_precreate_resolutions"
            ).fetchone()[0]
            == 0
        )
    finally:
        backend.close()


def test_production_precreate_acknowledgement_archived_marker_blocks_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_secret = b"precreate-cli-marker-secret"
    backend, store, failed, _reservation, _cutover, candidate = (
        _production_precreate_resolution_backend(
            tmp_path,
            marker_secret=marker_secret,
        )
    )
    verifier = _PrecreateProbeVerifier("archived_marker_match")
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_verifier",
        lambda *, marker_secret: verifier,
    )
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    try:
        with pytest.raises(RolloutGateBlocked, match="native_thread_materialized"):
            backend.sidebar_acknowledge_precreate_unrecoverable(
                job_id=failed["id"],
                expected_error_code="native_create_ambiguous",
            )
        assert verifier.marker_calls == []
        assert len(verifier.terminal_marker_calls) == 1
        assert verifier.recovery_calls == []
        assert verifier.create_calls == []
        assert store.get_sidebar_job_for_source(candidate.source_session_id) == before
        assert (
            store.db._conn.execute(
                "SELECT COUNT(*) FROM session_sidebar_precreate_resolutions"
            ).fetchone()[0]
            == 0
        )
    finally:
        backend.close()


def test_production_precreate_acknowledgement_rejects_cutover_drift_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_secret = b"precreate-cli-marker-secret"
    backend, store, failed, _reservation, cutover, _candidate = (
        _production_precreate_resolution_backend(
            tmp_path,
            marker_secret=marker_secret,
        )
    )
    cutover["quarantined_job_ids"] = []
    store.set_state(
        "session-bridge:sidebar:create-reservation-cutover:v1",
        cutover,
    )
    verifier = _PrecreateProbeVerifier("zero")
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_secret)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_verifier",
        lambda *, marker_secret: verifier,
        raising=False,
    )
    try:
        with pytest.raises(
            RolloutGateBlocked,
            match="sidebar_precreate_snapshot_mismatch",
        ):
            backend.sidebar_acknowledge_precreate_unrecoverable(
                job_id=failed["id"],
                expected_error_code="native_create_ambiguous",
            )
        assert verifier.marker_calls == []
        assert verifier.recovery_calls == []
    finally:
        backend.close()


def test_claude_visibility_exact_commands_route_with_safe_defaults(capsys) -> None:
    backend = FakeBackend()

    assert _run(["claude-visibility-status", "--json"], backend) == 0
    assert _json_output(capsys)["counts"]["claude_pending"] == 0
    assert (
        _run(
            [
                "claude-visibility-backfill",
                "--days",
                "30",
                "--limit",
                "10",
                "--dry-run",
            ],
            backend,
        )
        == 0
    )
    dry_run = _json_output(capsys)
    assert dry_run["mode"] == "dry_run"
    assert dry_run["dry_run"] is True
    assert dry_run["applied"] is False
    assert (
        _run(
            ["claude-visibility-backfill", "--days", "30", "--limit", "10", "--apply"],
            backend,
        )
        == 0
    )
    assert _json_output(capsys)["mode"] == "apply"
    assert _run(["claude-visibility-continuous", "--enable"], backend) == 0
    assert _json_output(capsys) == {"continuous": True, "enabled": False}
    assert _run(["claude-visibility-continuous", "--disable"], backend) == 0
    assert _json_output(capsys) == {"continuous": False, "enabled": False}
    assert _run(["claude-visibility-run-once"], backend) == 0
    assert _json_output(capsys)["status"] == "no_due_job"

    assert backend.calls == [
        ("claude_visibility_status",),
        ("close",),
        ("claude_visibility_backfill", 30, 10, False),
        ("close",),
        ("claude_visibility_backfill", 30, 10, True),
        ("close",),
        ("set_claude_visibility_continuous", True),
        ("close",),
        ("set_claude_visibility_continuous", False),
        ("close",),
        ("claude_visibility_run_once",),
        ("close",),
    ]


def test_claude_visibility_backfill_defaults_to_explicit_dry_run_json(capsys) -> None:
    backend = FakeBackend()

    assert (
        _run(["claude-visibility-backfill", "--days", "30", "--limit", "10"], backend)
        == 0
    )

    assert _json_output(capsys) == {
        **backend.claude_visibility_payload,
        "mode": "dry_run",
        "applied": False,
        "dry_run": True,
        "enqueued": 0,
    }
    assert backend.calls == [("claude_visibility_backfill", 30, 10, False), ("close",)]


def test_claude_visibility_backfill_validation_precedes_backend_mutation(
    capsys,
) -> None:
    backend = FakeBackend()

    with pytest.raises(SystemExit):
        _run(["claude-visibility-backfill", "--days", "0", "--limit", "10"], backend)
    with pytest.raises(SystemExit):
        _run(["claude-visibility-backfill", "--days", "30", "--limit", "11"], backend)
    with pytest.raises(SystemExit):
        _run(
            [
                "claude-visibility-backfill",
                "--days",
                "30",
                "--limit",
                "10",
                "--dry-run",
                "--apply",
            ],
            backend,
        )

    assert backend.calls == []


def test_claude_visibility_lineage_reconcile_requires_explicit_apply_confirmation(
    capsys,
) -> None:
    backend = FakeBackend()

    assert (
        _run(
            [
                "claude-visibility-reconcile-lineage",
                "--limit",
                "25",
                "--dry-run",
            ],
            backend,
        )
        != 0
    )
    assert _json_output(capsys) == {
        "mode": "dry_run",
        "scanned": 1,
        "repairable": 1,
        "repaired": 0,
        "remaining": 1,
        "blocker_codes": {},
        "has_more": False,
        "complete": False,
    }
    assert (
        _run(
            [
                "claude-visibility-reconcile-lineage",
                "--limit",
                "25",
                "--apply",
            ],
            backend,
        )
        != 0
    )
    assert _json_output(capsys) == {
        "error": "rollout_gate_blocked",
        "gate": "historical_lineage_repair_confirmation_required",
    }
    assert (
        _run(
            [
                "claude-visibility-reconcile-lineage",
                "--limit",
                "25",
                "--apply",
                "--confirm-historical-repair",
            ],
            backend,
        )
        == 0
    )
    assert _json_output(capsys)["mode"] == "apply"
    assert backend.calls == [
        ("reconcile_claude_visibility_lineage", 25, False, None),
        ("close",),
        ("close",),
        ("reconcile_claude_visibility_lineage", 25, True, None),
        ("close",),
    ]


def test_claude_visibility_lineage_reconcile_passes_cursor_and_never_succeeds_partial_apply(
    capsys,
) -> None:
    cursor = {
        "after_visible_at": 100.0,
        "after_job_id": "job-a",
        "high_water_visible_at": 101.0,
        "high_water_job_id": "job-b",
    }

    class PartialBackend(FakeBackend):
        def reconcile_claude_visibility_lineage(
            self,
            *,
            limit: int,
            apply: bool,
            cursor: Mapping[str, Any] | None = None,
        ):
            self.calls.append((
                "reconcile_claude_visibility_lineage",
                limit,
                apply,
                cursor,
            ))
            return {
                "mode": "apply",
                "scanned": 1,
                "repairable": 1,
                "repaired": 1,
                "remaining": 2,
                "blocker_codes": {},
                "next_cursor": cursor,
                "has_more": True,
                "complete": False,
            }

    backend = PartialBackend()
    result = _run(
        [
            "claude-visibility-reconcile-lineage",
            "--limit",
            "1",
            "--cursor",
            json.dumps(cursor),
            "--apply",
            "--confirm-historical-repair",
        ],
        backend,
    )

    assert result != 0
    assert _json_output(capsys)["complete"] is False
    assert backend.calls[0] == (
        "reconcile_claude_visibility_lineage",
        1,
        True,
        cursor,
    )


def test_claude_visibility_lineage_reconcile_rejects_non_object_cursor_before_backend(
    capsys,
) -> None:
    backend = FakeBackend()

    with pytest.raises(SystemExit):
        _run(
            [
                "claude-visibility-reconcile-lineage",
                "--cursor",
                "[]",
                "--dry-run",
            ],
            backend,
        )

    assert backend.calls == []


def test_production_lineage_cursor_auth_rejects_mode_replay_and_forgery_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker_key = b"c" * 32
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    identities = []
    for index in range(3):
        candidate = ClaudeVisibilityCandidate(
            source_session_id=f"codex:cli-lineage-source-{index}",
            source_provider=Provider.CODEX,
            native_name=f"[Codex] CLI lineage {index}",
            source_cwd=str(tmp_path),
            git_root=str(tmp_path),
            git_branch="main",
            git_head=f"head-{index}",
            worktree_id=f"worktree-{index}",
            eligible_at=100.0,
        )
        identity = derive_claude_visibility_identity(candidate, marker_key)
        identities.append(identity)
        store.upsert_projection(
            SessionProjection(
                provider=Provider.CODEX,
                native_id=f"cli-lineage-source-{index}",
                title=candidate.native_name,
                cwd=str(tmp_path),
                started_at=90.0,
                last_active=100.0,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"source-{index}",
                        ordinal=0,
                        role="user",
                        content="meaningful source request",
                        timestamp=100.0,
                    ),
                ),
                native_cursor=f"source-cursor-{index}",
                native_hash=f"source-hash-{index}",
            )
        )
        store.enqueue_claude_visibility_job(candidate, identity, marker_key)
        store.upsert_projection(
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id=identity.claude_uuid,
                title=f"Claude lineage target {index}",
                cwd=str(tmp_path),
                started_at=100.0,
                last_active=100.0,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"target-{index}",
                        ordinal=0,
                        role="user",
                        content="signed registration",
                        timestamp=100.0,
                    ),
                ),
                native_cursor=f"target-cursor-{index}",
                native_hash=f"target-hash-{index}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=identity.bridge_id,
            )
        )
        db._execute_write(
            lambda conn, job_id=identity.job_id: conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', completion_digest = ?,
                       visible_at = 100, updated_at = 100 WHERE id = ?""",
                ("a" * 64, job_id),
            )
        )

    def link_rows() -> list[tuple[str, str]]:
        with db._lock:
            conn = db._conn
            assert conn is not None
            return [
                (str(row["id"]), str(row["bridge_id"]))
                for row in conn.execute(
                    "SELECT id, bridge_id FROM session_links ORDER BY id"
                ).fetchall()
            ]

    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: marker_key)
    monkeypatch.setattr(backend, "close", lambda: None)
    try:
        dry_run = backend.reconcile_claude_visibility_lineage(
            limit=1,
            apply=False,
        )
        before_mode_replay = link_rows()
        assert (
            _run(
                [
                    "claude-visibility-reconcile-lineage",
                    "--limit",
                    "1",
                    "--cursor",
                    json.dumps(dry_run["next_cursor"]),
                    "--apply",
                    "--confirm-historical-repair",
                ],
                backend,
            )
            != 0
        )
        assert _json_output(capsys) == {"error": "configuration_error"}
        assert link_rows() == before_mode_replay == []

        first_apply = backend.reconcile_claude_visibility_lineage(
            limit=1,
            apply=True,
        )
        cursor = dict(first_apply["next_cursor"])
        ordered_job_ids = sorted(identity.job_id for identity in identities)
        forged = {**cursor, "after_job_id": ordered_job_ids[1]}
        before_forgery = link_rows()
        assert (
            _run(
                [
                    "claude-visibility-reconcile-lineage",
                    "--limit",
                    "1",
                    "--cursor",
                    json.dumps(forged),
                    "--apply",
                    "--confirm-historical-repair",
                ],
                backend,
            )
            != 0
        )
        assert _json_output(capsys) == {"error": "configuration_error"}
        assert link_rows() == before_forgery
    finally:
        db.close()


def test_claude_visibility_status_blocks_on_unlinked_visible_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    state: 0
                    for state in (
                        "claude_pending",
                        "claude_leased",
                        "claude_retry",
                        "claude_visible",
                        "claude_failed",
                    )
                },
                "retry_codes": {},
                "failed_codes": {},
                "fatal": [],
                "usage": {
                    "local_day": "2026-07-21",
                    "attempts": 0,
                    "reserved_cost_usd": "0",
                },
                "lineage": {
                    "unlinked_visible": 2,
                    "repairable": 1,
                    "blocked": 1,
                    "blocker_codes": {"claude_lineage_missing_source": 1},
                },
            }

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())

    result = backend.claude_visibility_status()

    assert result["lineage"] == {
        "unlinked_visible": 2,
        "repairable": 1,
        "blocked": 1,
        "blocker_codes": {"claude_lineage_missing_source": 1},
    }
    assert result["open_reasons"] == ["unlinked_visible_lineage"]
    assert result["fatal_reasons"] == ["claude_lineage_missing_source"]
    assert result["degraded_reasons"] == [
        "claude_lineage_missing_source",
        "unlinked_visible_lineage",
    ]


def test_claude_visibility_apply_and_run_once_use_typed_nonzero_contract(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(
        claude_visibility_payload={
            "enabled": True,
            "continuous": False,
            "counts": {},
            "retry_codes": {},
            "failed_codes": {"unknown_error_code": 1},
            "usage": {},
            "candidates": [],
            "exclusions": [],
            "open_reasons": [],
            "fatal_reasons": ["unknown_retry"],
            "degraded_reasons": [],
        }
    )
    assert (
        _run(
            ["claude-visibility-backfill", "--days", "30", "--limit", "10", "--apply"],
            backend,
        )
        != 0
    )
    _json_output(capsys)

    monkeypatch.setattr(
        backend,
        "claude_visibility_run_once",
        lambda: {
            "enabled": True,
            "status": "degraded",
            "degraded": True,
            "fatal": False,
        },
    )
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

    assert (
        _run(
            ["sidebar-backfill", "--days", "30", "--limit", "10", mode],
            backend,
        )
        == 3
    )
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

    assert (
        _run(
            ["sidebar-backfill", "--days", "30", "--limit", "10", mode],
            backend,
        )
        == 0
    )
    assert _json_output(capsys)["excluded_by_reason"] == {"source_cwd_missing": 1}


def test_sidebar_backfill_rejects_a_limit_above_ten() -> None:
    with pytest.raises(SystemExit):
        main(["sidebar-backfill", "--days", "30", "--limit", "11", "--dry-run"])


class _SidebarStatusStore:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def sidebar_delivery_status(
        self,
        *,
        now: float,
        inbox_cwd: str | None,
        placement_generation: int,
    ) -> dict[str, Any]:
        assert now == 1_000.0
        assert inbox_cwd in {None, "C:\\Users\\diego\\.hermes"}
        assert placement_generation == 1
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
        "stage_latency_seconds": {
            "source_to_index": {"p50": 1.0, "p95": 2.0},
            "index_to_queue": {"p50": 3.0, "p95": 4.0},
            "queue_to_visible": {"p50": 5.0, "p95": 6.0},
            "source_to_visible": {"p50": 9.0, "p95": 12.0},
        },
    })

    status = backend.sidebar_status()

    assert status["healthy"] is True
    assert status["degraded_reasons"] == []
    assert status["last_successful_heartbeat_at"] is None
    assert status["stage_latency_seconds"] == {
        "source_to_index": {"p50": 1.0, "p95": 2.0},
        "index_to_queue": {"p50": 3.0, "p95": 4.0},
        "queue_to_visible": {"p50": 5.0, "p95": 6.0},
        "source_to_visible": {"p50": 9.0, "p95": 12.0},
    }

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


def test_sidebar_status_exposes_fixed_health_counts_without_private_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    private = "C:/private/source.jsonl HERMES_SESSION_BRIDGE_V1:secret"
    backend = _production_sidebar_backend({
        "counts": {
            "sidebar_pending": 1,
            "sidebar_leased": 2,
            "sidebar_retry": 3,
            "sidebar_visible": 4,
            "sidebar_failed": 5,
            "ambiguous": 2,
            "needs_attention": 1,
            "projectless_legacy_count": 3,
            "source_payload": private,
        },
        "oldest_eligible_age_seconds": 10.0,
        "oldest_pending_age_seconds": 5.0,
        "last_heartbeat_at": 999.0,
        "last_visible_task_id": "private-task-identity",
        "delivery_latency_seconds": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
    })

    status = backend.sidebar_status()

    assert status["counts"] == {
        "sidebar_pending": 1,
        "sidebar_leased": 2,
        "sidebar_visible": 4,
        "sidebar_retry": 3,
        "sidebar_failed": 5,
        "sidebar_excluded": 0,
        "ambiguous": 2,
        "needs_attention": 5,
        "projectless_legacy_count": 3,
    }
    rendered = json.dumps(status)
    assert private not in rendered
    assert "private-task-identity" not in rendered
    assert "HERMES_SESSION_BRIDGE_V1" not in rendered


def test_sidebar_status_exposes_only_bounded_reconciliation_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "counts": {},
        "reconciliation_counts": {
            "recovered": 1,
            "absence_proven": 2,
            "blocked": 3,
            "private": 99,
        },
        "reconciliation_blocked_codes": {
            "marker_conflict": 1,
            "native_create_ambiguous": 2,
            "bridge_temporarily_unavailable": 0,
            "provider-private": 99,
        },
        "oldest_reconciliation_wait_age_seconds": 40.0,
        "reconciliation_scan_age_seconds": 10.0,
        "recovered_existing_total": 4,
        "created_new_total": 5,
        "signed_marker": "HERMES_SESSION_BRIDGE_V1:secret",
        "proof_digest": "a" * 64,
        "reconciliation_generation": "private-generation",
    })

    status = backend.sidebar_status()

    assert status["reconciliation_counts"] == {
        "recovered": 1,
        "absence_proven": 2,
        "blocked": 3,
    }
    assert status["reconciliation_blocked_codes"] == {
        "marker_conflict": 1,
        "native_create_ambiguous": 2,
        "bridge_temporarily_unavailable": 0,
    }
    assert status["oldest_reconciliation_wait_age_seconds"] == 40.0
    assert status["reconciliation_scan_age_seconds"] == 10.0
    assert status["recovered_existing_total"] == 4
    assert status["created_new_total"] == 5
    rendered = json.dumps(status)
    for private in (
        "HERMES_SESSION_BRIDGE_V1",
        "proof_digest",
        "private-generation",
        "provider-private",
    ):
        assert private not in rendered


def test_sidebar_status_exposes_only_sanitized_placement_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "counts": {},
        "placement": {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 12,
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": 1234.0},
        },
    })

    assert backend.sidebar_status()["placement"] == {
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
        "generation": 1,
        "verified_visible": 12,
        "mismatch_count": 0,
        "canary": {"status": "passed", "verified_at": 1234.0},
    }


@pytest.mark.parametrize(
    "placement",
    (
        {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": float("nan"),
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": 1234.0},
        },
        {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 1,
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": -0.001},
        },
        {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 1,
            "mismatch_count": 0,
            "source_cwd": "C:\\secret\\source",
            "canary": {"status": "passed", "verified_at": 1234.0},
        },
        {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 1,
            "mismatch_count": 0,
            "canary": {
                "status": "passed",
                "verified_at": 1234.0,
                "canary_identity_digest": "a" * 64,
            },
        },
    ),
)
def test_sidebar_status_rejects_unsanitized_placement_health(
    placement: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({"counts": {}, "placement": placement})

    with pytest.raises(ConfigurationFailure, match="invalid_sidebar_status"):
        backend.sidebar_status()


def test_sidebar_status_preserves_raw_failures_but_waives_exact_terminal_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"sidebar_failed": 1},
        "blocking_failed_count": 0,
        "terminally_resolved_failed_count": 1,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": 1,
            "effective": 1,
            "ineffective": 0,
            "by_resolution_code": {"native_thread_unrecoverable": 1},
        },
        "execution_blockers": [],
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": ["native_create_ambiguous"],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is True
    assert status["degraded_reasons"] == []
    assert status["counts"]["sidebar_failed"] == 1
    assert status["blocking_failed_count"] == 0
    assert status["terminally_resolved_failed_count"] == 1
    assert status["ineffective_terminal_resolution_count"] == 0
    assert status["terminal_resolution_ledger_valid"] is True
    assert status["terminal_resolutions"] == {
        "total": 1,
        "effective": 1,
        "ineffective": 0,
        "by_resolution_code": {
            "native_thread_unrecoverable": 1,
            "precutover_create_unrecoverable": 0,
            "native_create_unrecoverable": 0,
        },
    }
    assert status["execution_blockers"] == []


def test_sidebar_status_accepts_and_preserves_precreate_terminal_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 0, "hermes": 1},
        "counts": {"sidebar_failed": 1},
        "blocking_failed_count": 0,
        "terminally_resolved_failed_count": 1,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": 1,
            "effective": 1,
            "ineffective": 0,
            "by_resolution_code": {
                "native_thread_unrecoverable": 0,
                "precutover_create_unrecoverable": 1,
            },
        },
        "execution_blockers": [],
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": ["native_create_ambiguous"],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is True
    assert status["terminal_resolutions"]["by_resolution_code"] == {
        "native_thread_unrecoverable": 0,
        "precutover_create_unrecoverable": 1,
        "native_create_unrecoverable": 0,
    }

def test_sidebar_status_accepts_and_preserves_unbound_terminal_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"sidebar_failed": 1},
        "blocking_failed_count": 0,
        "terminally_resolved_failed_count": 1,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": 1,
            "effective": 1,
            "ineffective": 0,
            "by_resolution_code": {
                "native_thread_unrecoverable": 0,
                "precutover_create_unrecoverable": 0,
                "native_create_unrecoverable": 1,
            },
        },
        "execution_blockers": [],
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": ["native_create_ambiguous"],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is True
    assert status["terminal_resolutions"]["by_resolution_code"] == {
        "native_thread_unrecoverable": 0,
        "precutover_create_unrecoverable": 0,
        "native_create_unrecoverable": 1,
    }


@pytest.mark.parametrize(
    ("ledger_valid", "ineffective", "blockers"),
    (
        (
            True,
            1,
            ["sidebar_failed", "sidebar_terminal_resolution_mismatch"],
        ),
        (
            False,
            0,
            ["sidebar_failed", "sidebar_terminal_resolution_ledger_invalid"],
        ),
    ),
)
def test_sidebar_status_fails_closed_for_ineffective_or_invalid_terminal_ledger(
    monkeypatch: pytest.MonkeyPatch,
    ledger_valid: bool,
    ineffective: int,
    blockers: list[str],
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"sidebar_failed": 1},
        "blocking_failed_count": 1,
        "terminally_resolved_failed_count": 0,
        "ineffective_terminal_resolution_count": ineffective,
        "terminal_resolution_ledger_valid": ledger_valid,
        "terminal_resolutions": {
            "total": ineffective,
            "effective": 0,
            "ineffective": ineffective,
            "by_resolution_code": {"native_thread_unrecoverable": 0},
        },
        "execution_blockers": blockers,
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": ["native_create_ambiguous"],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is False
    assert status["degraded_reasons"] == blockers
    assert status["execution_blockers"] == blockers
    assert status["counts"]["sidebar_failed"] == 1


def test_sidebar_status_degrades_for_blocking_failure_without_halting_other_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 1, "hermes": 0},
        "counts": {"sidebar_failed": 1},
        "blocking_failed_count": 1,
        "terminally_resolved_failed_count": 0,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": 0,
            "effective": 0,
            "ineffective": 0,
            "by_resolution_code": {"native_thread_unrecoverable": 0},
        },
        "execution_blockers": [],
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": ["native_create_ambiguous"],
        "delivery_latency_seconds": {},
    })

    status = backend.sidebar_status()

    assert status["healthy"] is False
    assert status["degraded_reasons"] == ["sidebar_failed"]
    assert status["execution_blockers"] == []


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
        "oldest_eligible_age_seconds": 301.0,
        "oldest_pending_age_seconds": 181.0,
        "last_heartbeat_at": 819.0,
        "last_visible_task_id": "019f-secret-thread-identifier",
        "recent_error_codes": ["broker_time_budget"],
        "delivery_latency_seconds": {"p50": 1.0, "p95": 2.0, "p99": 2.0},
        "scheduler": {
            "fresh_claims_since_oldest": 3,
            "next_lane": "oldest",
        },
        "recovery": {
            "lane": "hydration",
            "status": "visible",
            "last_cycle_at": 999.0,
        },
    })

    status = backend.sidebar_status()
    rendered = json.dumps(status, sort_keys=True)

    assert status["healthy"] is False
    assert status["degraded_reasons"] == [
        "broker_heartbeat_stale",
        "oldest_pending_stale",
    ]
    expected_tag = hashlib.sha256(b"019f-secret-thread-identifier").hexdigest()[:16]
    assert status["last_visible_task_id"] == f"task:{expected_tag}"
    assert status["scheduler"] == {
        "fresh_claims_since_oldest": 3,
        "next_lane": "oldest",
    }
    assert status["recovery"] == {
        "lane": "hydration",
        "status": "visible",
        "last_cycle_at": 999.0,
    }
    assert "019f-secret-thread-identifier" not in rendered
    assert "lease_token" not in rendered
    assert "HERMES_SESSION_BRIDGE_V1:" not in rendered


@pytest.mark.parametrize(
    ("heartbeat_at", "heartbeat_stale", "reasons"),
    (
        (821.0, False, ["oldest_pending_stale"]),
        (819.0, True, ["broker_heartbeat_stale", "oldest_pending_stale"]),
    ),
)
def test_sidebar_status_uses_broker_thresholds_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_at: float,
    heartbeat_stale: bool,
    reasons: list[str],
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    sidebar = replace(
        SidebarConfig(),
        enabled=True,
        continuous=True,
        inbox_cwd="C:\\Users\\diego\\.hermes",
        broker_thread_id="019f9b71-7109-7ed0-943a-d7291190245c",
        broker_project_id="local-453ac85f86839c6d001817cb8480b8ca",
        broker_cwd="C:\\Users\\diego\\Developer\\session-sidebar-broker",
    )
    backend = ProductionBackend(replace(BridgeConfig(), sidebar=sidebar))
    backend._store = _SidebarStatusStore({  # type: ignore[assignment]
        "counts": {"pending": 1},
        "oldest_eligible_age_seconds": 301.0,
        "oldest_pending_age_seconds": 12.0,
        "last_heartbeat_at": heartbeat_at,
    })
    backend._catalog = object()  # type: ignore[assignment]

    status = backend.sidebar_status()

    assert status["heartbeat_stale"] is heartbeat_stale
    assert status["oldest_job_overdue"] is True
    assert status["degraded_reasons"] == reasons
    assert status["broker"] == {
        "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
    }


@pytest.mark.parametrize(
    "unsafe",
    ("C:\\unsafe\x00path", "C:\\unsafe\x85path", "C:\\unsafe\u2028path", "C:\\unsafe\u2029path"),
)
def test_sidebar_status_omits_placement_with_unsafe_inbox_path(
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "counts": {},
        "placement": {
            "inbox_cwd": unsafe,
            "generation": 1,
            "verified_visible": 1,
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": 1234.0},
        },
    })

    status = backend.sidebar_status()

    assert "placement" not in status
    assert unsafe not in json.dumps(status)
    assert "messages" not in repr(status)


def test_production_sidebar_status_uses_real_eligible_age_for_overdue_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    source_session_id = canonical_session_id(Provider.HERMES, "eligible-age")
    db.create_session(source_session_id, "cli", cwd="C:/workspace/project")
    store = SessionBridgeStore(db, sidebar_token_factory=lambda: "eligible-age-lease")
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title="[hermes] eligible age",
        cwd="C:/workspace/project",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=600.0,
    )
    store.enqueue_sidebar_job(candidate)
    assert len(store.claim_sidebar_jobs(now=900.0, limit=1)) == 1
    backend = ProductionBackend(BridgeConfig())
    backend._store = store
    backend._catalog = object()  # type: ignore[assignment]
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_001.0)

    status = backend.sidebar_status()

    assert status["oldest_eligible_age_seconds"] == 401.0
    assert status["oldest_pending_age_seconds"] == 101.0
    assert status["oldest_job_overdue"] is True


@pytest.mark.parametrize(
    "payload",
    (
        {"scheduler": {"fresh_claims_since_oldest": 4, "next_lane": "fresh"}},
        {"scheduler": {"fresh_claims_since_oldest": 0, "next_lane": "secret"}},
        {"recovery": {"lane": "secret", "status": "idle", "last_cycle_at": 1.0}},
        {
            "recovery": {
                "lane": "hydration",
                "status": "secret",
                "last_cycle_at": 1.0,
            }
        },
        {"stage_latency_seconds": {"secret_stage": {"p50": 1.0}}},
        {"stage_latency_seconds": {"source_to_index": {"p99": 1.0}}},
        {"stage_latency_seconds": {"source_to_index": "private timing"}},
    ),
)
def test_sidebar_status_rejects_invalid_scheduler_or_recovery_progress(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr("session_bridge.cli.time.time", lambda: 1_000.0)
    backend = _production_sidebar_backend({
        "eligible_by_provider": {"claude": 0, "hermes": 0},
        "counts": {},
        "oldest_pending_age_seconds": None,
        "last_heartbeat_at": None,
        "last_visible_task_id": None,
        "recent_error_codes": [],
        "delivery_latency_seconds": {},
        **payload,
    })

    with pytest.raises(ConfigurationFailure, match="invalid_sidebar_status"):
        backend.sidebar_status()


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
        (299.0, 999.0, True, []),
        (300.0, None, True, []),
        (
            301.0,
            None,
            False,
            ["oldest_pending_stale"],
        ),
        (
            301.0,
            819.0,
            False,
            ["broker_heartbeat_stale", "oldest_pending_stale"],
        ),
        (301.0, 999.0, False, ["oldest_pending_stale"]),
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
            "oldest_eligible_age_seconds": oldest_age,
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
    ((0.0, True), (301.0, False)),
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
            "oldest_eligible_age_seconds": age,
            "oldest_pending_age_seconds": age,
            "last_heartbeat_at": 999.0,
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
    assert saved == [
        (
            {
                "theme": "midnight",
                "session_bridge": {
                    "sidebar": {
                        "enabled": True,
                        "continuous": True,
                        "backfill_days": 30,
                    },
                    "future_key": {"keep": "exactly"},
                },
            },
            {("session_bridge", "sidebar", "continuous")},
        )
    ]


@pytest.mark.parametrize(
    ("method_name", "key"),
    (
        ("set_sidebar_readable_preview", "readable_preview_enabled"),
        ("set_sidebar_hydration", "legacy_hydration_enabled"),
    ),
)
def test_sidebar_readable_feature_flags_persist_only_the_exact_leaf(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    key: str,
) -> None:
    loaded = {
        "theme": "midnight",
        "session_bridge": {
            "sidebar": {
                "enabled": True,
                "continuous": False,
                "backfill_days": 30,
            },
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

    result = getattr(backend, method_name)(enabled=True)

    assert result == {key: True}
    assert saved[0][0]["session_bridge"]["sidebar"] == {
        "enabled": True,
        "continuous": False,
        "backfill_days": 30,
        key: True,
    }
    assert saved[0][0]["session_bridge"]["future_key"] == {"keep": "exactly"}
    assert saved[0][0]["theme"] == "midnight"
    assert saved[0][1] == {("session_bridge", "sidebar", key)}


def test_claude_visibility_continuous_preserves_unrelated_config_and_enabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = {
        "theme": "midnight",
        "session_bridge": {
            "claude_visibility": {
                "enabled": False,
                "continuous": False,
                "backfill_days": 30,
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
    assert saved == [
        (
            {
                "theme": "midnight",
                "session_bridge": {
                    "claude_visibility": {
                        "enabled": False,
                        "continuous": True,
                        "backfill_days": 30,
                    },
                    "future_key": {"keep": "exactly"},
                },
            },
            {("session_bridge", "claude_visibility", "continuous")},
        )
    ]


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

    with pytest.raises(
        ConfigurationFailure, match="claude_visibility_continuous_not_persisted"
    ):
        backend.set_claude_visibility_continuous(enabled=True)

    assert backend.config.claude_visibility.continuous is False


def test_claude_visibility_status_does_not_construct_delivery_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    state: 0
                    for state in (
                        "claude_pending",
                        "claude_leased",
                        "claude_retry",
                        "claude_visible",
                        "claude_failed",
                    )
                },
                "retry_codes": {},
                "failed_codes": {},
                "usage": {
                    "local_day": "2026-07-17",
                    "attempts": 0,
                    "reserved_cost_usd": "0",
                },
            }

    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config,
            claude_visibility=replace(config.claude_visibility, enabled=True),
        )
    )
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
    assert result["last_cycle"] == {"tracked": False, "value": None}
    assert result["last_empty_cycle"] == {"tracked": False, "value": None}
    assert result["last_registrar_result"] == {"tracked": False, "value": None}


def test_claude_visibility_preflight_blocks_before_any_runtime_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config, claude_visibility=replace(config.claude_visibility, enabled=True)
        )
    )
    events: list[object] = []

    def resolve_executable(_name: str) -> tuple[str, ...]:
        events.append("resolve_executable")
        return ("claude",)

    def preflight(command: tuple[str, ...]) -> Any:
        events.append(("preflight", command))
        return cli_module._ClaudeVisibilityPreflight(
            None, "claude_visibility_preflight_failed_not_logged_in"
        )

    monkeypatch.setattr("session_bridge.cli.resolve_cli_executable", resolve_executable)
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight_detail", preflight
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_marker_key",
        lambda: (_ for _ in ()).throw(AssertionError("marker resolution")),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("source construction")
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registrar construction")
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeVisibilityCoordinator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coordinator construction")
        ),
    )
    monkeypatch.setattr(
        backend,
        "_require_store",
        lambda: (_ for _ in ()).throw(AssertionError("store/claim/cost access")),
    )
    monkeypatch.setattr(
        backend,
        "_claude_visibility_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory access")
        ),
    )

    with pytest.raises(ProviderDegraded, match="claude_visibility_preflight_failed"):
        backend.claude_visibility_run_once()

    assert events == ["resolve_executable", ("preflight", ("claude",))]
    assert backend._claude_visibility_coordinator is None


def test_claude_visibility_runtime_passes_only_preflight_theme_to_registrar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config, claude_visibility=replace(config.claude_visibility, enabled=True)
        )
    )
    events: list[object] = []
    store = object()
    source = object()
    coordinator = object()

    def resolve_executable(_name: str) -> tuple[str, ...]:
        events.append("resolve_executable")
        return ("claude",)

    def preflight(command: tuple[str, ...]) -> Any:
        events.append(("preflight", command))
        return cli_module._ClaudeVisibilityPreflight(
            {
                "version": "2.1.216",
                "authentication": "available",
                "theme": "light",
            },
            None,
        )

    def marker_key() -> bytes:
        events.append("marker")
        return b"m" * 32

    def source_factory(*_args: Any, **_kwargs: Any) -> object:
        events.append("source")
        return source

    def store_factory() -> object:
        events.append("store")
        return store

    def registrar_factory(*args: Any, **kwargs: Any) -> object:
        events.append(("registrar", args, kwargs))
        assert kwargs["startup_theme"] == "light"
        return object()

    def coordinator_factory(**kwargs: Any) -> object:
        events.append(("coordinator", kwargs))
        return coordinator

    monkeypatch.setattr("session_bridge.cli.resolve_cli_executable", resolve_executable)
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight_detail", preflight
    )
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", marker_key)
    monkeypatch.setattr("session_bridge.cli.ClaudeSourceAdapter", source_factory)
    monkeypatch.setattr("session_bridge.cli.ClaudeNativeRegistrar", registrar_factory)
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeVisibilityCoordinator", coordinator_factory
    )
    monkeypatch.setattr(backend, "_require_store", store_factory)

    assert backend._claude_visibility_runtime() is coordinator

    assert events[:5] == [
        "resolve_executable",
        ("preflight", ("claude",)),
        "marker",
        "source",
        "store",
    ]
    registrar_event = events[5]
    assert isinstance(registrar_event, tuple)
    assert registrar_event[0] == "registrar"
    assert events[6][0] == "coordinator"  # type: ignore[index]

    events.clear()
    assert backend._claude_visibility_runtime() is coordinator
    assert events == ["resolve_executable", ("preflight", ("claude",))]


def test_claude_visibility_inventory_reuses_indexed_codex_and_fast_state_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig()
    backend = ProductionBackend(config)
    indexed_projection = SessionProjection(
        provider=Provider.CODEX,
        native_id="indexed-codex",
        title="Indexed Codex",
        cwd="C:/work/indexed",
        started_at=100.0,
        last_active=200.0,
        messages=(
            ProjectedMessage(
                native_event_id="indexed-user",
                ordinal=0,
                role="user",
                content="Reuse indexed projection",
                timestamp=110.0,
            ),
        ),
        origin_kind=OriginKind.NATIVE,
    )
    indexed = cli_module.SidebarSource(
        source_session_id="codex:indexed-codex",
        projection=indexed_projection,
        git_root=None,
        git_head=None,
        worktree_id=None,
        automation_only=False,
        subagent_only=False,
    )

    class Store:
        def list_claude_visibility_hermes_sources(
            self, after: float, limit: int | None
        ) -> tuple[()]:
            assert after == 150.0
            assert limit is None
            return ()

        def list_claude_visibility_codex_sources(
            self, after: float, limit: int | None
        ) -> tuple[cli_module.SidebarSource, ...]:
            assert after == 150.0
            assert limit is None
            return (indexed,)

        def list_claude_visibility_source_ids(self) -> frozenset[str]:
            return frozenset({"codex:indexed-codex"})

    captured: dict[str, object] = {}

    class Adapter:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def list_claude_visibility_sources(
            self,
            *,
            after: float,
            state_db_only: bool,
            indexed_sources: Mapping[str, cli_module.SidebarSource],
            known_visibility_source_ids: frozenset[str],
        ) -> tuple[cli_module.SidebarSource, ...]:
            captured.update({
                "after": after,
                "state_db_only": state_db_only,
                "indexed_sources": indexed_sources,
                "known_visibility_source_ids": known_visibility_source_ids,
            })
            return (indexed,)

    backend._codex_client = object()
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr("session_bridge.cli.CodexSourceAdapter", Adapter)

    sources = backend._claude_visibility_inventory(
        150.0,
        marker_secret=b"m" * 32,
        state_db_only=True,
    )

    assert sources == (indexed,)
    assert captured == {
        "after": 150.0,
        "state_db_only": True,
        "indexed_sources": {"indexed-codex": indexed},
        "known_visibility_source_ids": frozenset({"codex:indexed-codex"}),
    }


def test_characterization_preflight_blocks_before_store_or_registrar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ProductionBackend(BridgeConfig())
    events: list[object] = []
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable", lambda _name: ("claude",)
    )

    def preflight(command: tuple[str, ...]) -> Any:
        events.append(("preflight", command))
        return cli_module._ClaudeVisibilityPreflight(
            None, "claude_visibility_preflight_failed_not_logged_in"
        )

    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight_detail", preflight
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_marker_key",
        lambda: (_ for _ in ()).throw(AssertionError("marker resolution")),
    )
    monkeypatch.setattr(
        backend,
        "_require_store",
        lambda: (_ for _ in ()).throw(AssertionError("store/claim/cost access")),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registrar construction")
        ),
    )

    with pytest.raises(ProviderDegraded, match="claude_visibility_preflight_failed"):
        backend.characterize_claude_visibility()

    assert events == [("preflight", ("claude",))]


@pytest.mark.parametrize("sync_fails", [False, True])
def test_characterization_cleanup_syncs_exact_terminal_record_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_fails: bool,
) -> None:
    operation_id = "16161616-1616-4616-8616-161616161616"
    source_root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    completed_root = source_root / ".cleanup-completed"
    completed_root.mkdir(parents=True)
    completed_state = _characterization_state(
        operation_id, phase="completed", source_root=source_root
    )
    calls: list[dict[str, object]] = []

    class Store:
        def record_claude_visibility_characterization(self, **kwargs):
            calls.append(kwargs)
            if sync_fails:
                raise ValueError("synthetic terminal append failure")
            return {"status": "cleanup_completed"}

    def cleanup(**_kwargs):
        _write_characterization_record(
            completed_root / f"{operation_id}.json",
            completed_state,
            b"k" * 32,
        )
        return {
            "passed": True,
            "cleanup": "removed_exact_characterization",
        }

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.cleanup_characterized_claude_visibility", cleanup
    )

    token = {"id": operation_id, "capability": "x" * 43}
    if sync_fails:
        with pytest.raises(
            ConfigurationFailure, match="characterization_record_invalid"
        ):
            backend.characterize_claude_visibility(cleanup_token=token)
    else:
        result = backend.characterize_claude_visibility(cleanup_token=token)
        assert result["cleanup"] == "removed_exact_characterization"
    assert (completed_root / f"{operation_id}.json").exists()
    assert len(calls) == 1
    assert calls[0]["operation_id"] == operation_id
    assert calls[0]["cleanup_completed"] is True


@pytest.mark.parametrize("fatal_kind", ["unknown_retry", "raw_fatal"])
def test_characterization_status_fatal_blocks_before_native_or_low_level_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_kind: str,
) -> None:
    source_root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    active_state: dict[str, object] | None = None
    if fatal_kind == "unknown_retry":
        source_root.mkdir(parents=True)
        active_state = _characterization_state(
            "15151515-1515-4515-8515-151515151515",
            phase="launching",
            source_root=source_root,
        )
        _write_characterization_record(
            source_root / ".claude-visibility-operation.json",
            active_state,
            b"k" * 32,
        )

    class Store:
        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            assert active_state is not None
            return {"status": "registered"}

        def claude_visibility_status(self, _now):
            state = "claude_retry" if fatal_kind == "unknown_retry" else None
            return {
                "counts": {
                    "claude_pending": 0,
                    "claude_leased": 0,
                    "claude_retry": int(state == "claude_retry"),
                    "claude_visible": 0,
                    "claude_failed": 0,
                },
                "retry_codes": (
                    {"future_unknown_retry": 1} if fatal_kind == "unknown_retry" else {}
                ),
                "failed_codes": {},
                "fatal": (
                    [{"code": "unknown_job_state"}] if fatal_kind == "raw_fatal" else []
                ),
                "lineage": {
                    "unlinked_visible": 0,
                    "repairable": 0,
                    "blocked": 0,
                    "blocker_codes": {},
                },
                "characterizations": (
                    []
                    if active_state is None
                    else [{"job_id": active_state["job_id"], "state": state}]
                ),
            }

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable", lambda _name: ("claude",)
    )
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight_detail",
        lambda _command: cli_module._ClaudeVisibilityPreflight(
            {"theme": "light"}, None
        ),
    )
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: pytest.fail("fatal status must block native source"),
    )
    monkeypatch.setattr(
        "session_bridge.cli.characterize_claude_visibility",
        lambda **_kwargs: pytest.fail("fatal status must block low-level work"),
    )

    with pytest.raises(RolloutGateBlocked, match="claude_visibility_not_idle"):
        backend.characterize_claude_visibility()


def test_characterization_replays_terminal_abort_before_native_or_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    source_root.mkdir(parents=True)
    operation_id = "19191919-1919-4919-8919-191919191919"
    active = _characterization_state(
        operation_id, phase="launching", source_root=source_root
    )
    _write_characterization_record(
        source_root / ".claude-visibility-operation.json", active, b"k" * 32
    )
    calls: list[object] = []

    class Store:
        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            return {"status": "registered"}

        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    "claude_pending": 0,
                    "claude_leased": 0,
                    "claude_retry": 0,
                    "claude_visible": 0,
                    "claude_failed": 0,
                },
                "retry_codes": {},
                "failed_codes": {},
                "fatal": [],
                "lineage": {
                    "unlinked_visible": 0,
                    "repairable": 0,
                    "blocked": 0,
                    "blocker_codes": {},
                },
                "characterizations": [],
            }

        def record_claude_visibility_characterization(self, **kwargs):
            calls.append(("terminal", kwargs))
            assert kwargs["launch_aborted"] is True
            return {
                "status": "already_aborted",
                "job_id": active["job_id"],
                "reserved_claude_uuid": active["reserved_claude_uuid"],
            }

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda _name: pytest.fail("terminal abort replay must not resolve Claude"),
    )
    monkeypatch.setattr(
        "session_bridge.cli.retire_aborted_claude_visibility_characterization",
        lambda **kwargs: calls.append(("retire", kwargs)) or {"status": "retired"},
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: pytest.fail(
            "terminal abort must not read native data"
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.characterize_claude_visibility",
        lambda **_kwargs: pytest.fail("terminal abort must not start new work"),
    )

    result = backend.characterize_claude_visibility()

    assert result == {
        "status": "aborted_exact_absence",
        "job_id": active["job_id"],
        "reserved_claude_uuid": active["reserved_claude_uuid"],
        "replacement_created": False,
        "active_record_retired": True,
        "replayed": True,
    }
    assert [call[0] for call in calls] == ["terminal", "retire"]


def _characterization_state(
    operation_id: str, *, phase: str, source_root: Path
) -> dict[str, object]:
    source_session_id = f"codex:{operation_id}"
    candidate = ClaudeVisibilityCandidate(
        source_session_id=source_session_id,
        source_provider=Provider.CODEX,
        native_name="[Codex] Verify native Claude session visibility and exact-ID resume metadata.",
        source_cwd=str(source_root / f"claude-visibility-{operation_id}"),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    identity = derive_claude_visibility_identity(candidate, b"k" * 32)
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "phase": phase,
        "created_at": 100.0,
        "expires_at": 200.0,
        "source_provider": "codex",
        "source_session_id": source_session_id,
        "bridge_id": identity.bridge_id,
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
        "native_name": candidate.native_name,
        "source_cwd": candidate.source_cwd,
        "signed_marker": identity.signed_marker,
        "transcript_path": None,
        "transcript_identity": None,
        "sentinel_nonce": "nonce",
        "cleanup_authorized_at": 150.0 if phase == "completed" else None,
        "cleanup_capability_hash": "a" * 64,
    }


def test_characterization_record_sync_is_authenticated_bounded_and_phase_aware(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude-visibility-sources"
    root.mkdir()
    completed = root / ".cleanup-completed"
    completed.mkdir()
    active_id = "11111111-1111-4111-8111-111111111111"
    completed_id = "22222222-2222-4222-8222-222222222222"
    _write_characterization_record(
        root / ".claude-visibility-operation.json",
        _characterization_state(active_id, phase="launching", source_root=root),
        b"k" * 32,
    )
    _write_characterization_record(
        completed / f"{completed_id}.json",
        _characterization_state(completed_id, phase="completed", source_root=root),
        b"k" * 32,
    )

    class Store:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def enqueue_claude_visibility_characterization(
            self,
            _candidate,
            _identity,
            _marker_secret,
            *,
            operation_id,
            evidence_digest,
        ):
            self.calls.append({
                "operation_id": operation_id,
                "evidence_digest": evidence_digest,
                "cleanup_completed": False,
            })
            return {"status": "registered"}

        def record_claude_visibility_characterization(self, **kwargs):
            self.calls.append(kwargs)
            return {"status": "recorded"}

    store = Store()
    result = _sync_claude_characterization_records(
        store=store,
        source_root=root,
        marker_secret=b"k" * 32,
        include_active=True,
        include_completed=True,
    )

    assert result == {"registered": 2, "cleanup_completed": 1}
    assert [call["operation_id"] for call in store.calls] == [
        active_id,
        completed_id,
    ]
    assert store.calls[0]["cleanup_completed"] is False
    assert store.calls[1]["cleanup_completed"] is True
    assert all(len(str(call["evidence_digest"])) == 64 for call in store.calls)


def test_characterization_restart_sync_atomically_recovers_missing_job_before_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude-visibility-sources"
    root.mkdir()
    operation_id = "14141414-1414-4414-8414-141414141414"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)

    synced = _sync_claude_characterization_records(
        store=store,
        source_root=root,
        marker_secret=b"k" * 32,
        include_active=True,
        include_completed=False,
    )
    raw = store.claude_visibility_status(100.0)

    assert synced == {"registered": 1, "cleanup_completed": 0}
    assert raw["counts"]["claude_pending"] == 1
    assert raw["characterizations"] == [
        {"job_id": state["job_id"], "state": "claude_pending"}
    ]
    assert _claude_characterization_open_work_allowed(
        raw,
        active_operation=True,
        active_job_id=str(state["job_id"]),
    )
    claim = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        expected_job_id=str(state["job_id"]),
    )
    assert claim.claimed
    assert claim.reserved_claude_uuid == state["reserved_claude_uuid"]
    db.close()


def test_characterization_restart_sync_backfills_exact_preledger_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude-visibility-sources"
    root.mkdir()
    operation_id = "16161616-1616-4616-8616-161616161616"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    candidate = ClaudeVisibilityCandidate(
        source_session_id=str(state["source_session_id"]),
        source_provider=Provider.CODEX,
        native_name=str(state["native_name"]),
        source_cwd=str(state["source_cwd"]),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=float(state["created_at"]),
    )
    identity = derive_claude_visibility_identity(candidate, b"k" * 32)
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    store.enqueue_claude_visibility_job(candidate, identity, b"k" * 32)
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_retry', attempts = 7,
                   next_attempt_at = 100, error_code = 'creation_ambiguous',
                   error_detail = 'legacy ambiguous create', updated_at = 99
               WHERE id = ?""",
            (identity.job_id,),
        )
    )
    before = dict(
        db._conn.execute(
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        ).fetchone()
    )

    synced = _sync_claude_characterization_records(
        store=store,
        source_root=root,
        marker_secret=b"k" * 32,
        include_active=True,
        include_completed=False,
    )

    assert synced == {"registered": 1, "cleanup_completed": 0}
    assert (
        dict(
            db._conn.execute(
                "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                (identity.job_id,),
            ).fetchone()
        )
        == before
    )
    assert store.claude_visibility_status(100.0)["characterizations"] == [
        {"job_id": identity.job_id, "state": "claude_retry"}
    ]
    db.close()


def test_characterization_restart_sync_refuses_to_create_second_open_job(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude-visibility-sources"
    root.mkdir()
    operation_id = "18181818-1818-4818-8818-181818181818"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate = ClaudeVisibilityCandidate(
        source_session_id="codex:unrelated-open",
        source_provider=Provider.CODEX,
        native_name="[Codex] unrelated",
        source_cwd="C:/work/unrelated",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    identity = derive_claude_visibility_identity(candidate, b"k" * 32)
    store.enqueue_claude_visibility_job(candidate, identity, b"k" * 32)

    with pytest.raises(ConfigurationFailure, match="characterization_record_invalid"):
        _sync_claude_characterization_records(
            store=store,
            source_root=root,
            marker_secret=b"k" * 32,
            include_active=True,
            include_completed=False,
        )

    raw = store.claude_visibility_status(100.0)
    assert raw["counts"]["claude_pending"] == 1
    assert raw["characterizations"] == []
    db.close()


def test_lineage_apply_syncs_completed_characterization_before_store_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []

    class Store:
        def reconcile_claude_visibility_lineage(self, **kwargs):
            events.append(("reconcile", kwargs))
            return {
                "scanned": 0,
                "repairable": 0,
                "repaired": 0,
                "remaining": 0,
                "blocker_codes": {},
                "next_cursor": None,
                "has_more": False,
                "complete": True,
            }

    backend = ProductionBackend(BridgeConfig())
    store = Store()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        "session_bridge.cli._sync_claude_characterization_records",
        lambda **kwargs: (
            events.append(("sync", kwargs)) or {"registered": 1, "cleanup_completed": 1}
        ),
    )

    result = backend.reconcile_claude_visibility_lineage(limit=25, apply=True)

    assert result["complete"] is True
    assert events[0][0] == "sync"  # type: ignore[index]
    assert events[0][1]["include_active"] is False  # type: ignore[index]
    assert events[0][1]["include_completed"] is True  # type: ignore[index]
    assert events[1][0] == "reconcile"  # type: ignore[index]


def test_characterization_abort_cli_requires_explicit_confirmation(capsys) -> None:
    backend = FakeBackend()

    job_id = "claude-visibility-job:test"
    reserved_uuid = "11111111-1111-4111-8111-111111111111"
    assert (
        _run(
            [
                "claude-visibility-abort-characterization",
                "--job-id",
                job_id,
                "--reserved-claude-uuid",
                reserved_uuid,
            ],
            backend,
        )
        == 4
    )
    assert _json_output(capsys) == {
        "error": "rollout_gate_blocked",
        "gate": "characterization_exact_absence_confirmation_required",
    }
    assert not any(
        call[0] == "abort_claude_visibility_characterization" for call in backend.calls
    )

    assert (
        _run(
            [
                "claude-visibility-abort-characterization",
                "--confirm-exact-absence",
                "--job-id",
                job_id,
                "--reserved-claude-uuid",
                reserved_uuid,
            ],
            backend,
        )
        == 0
    )
    assert _json_output(capsys) == {
        "status": "aborted_exact_absence",
        "job_id": job_id,
        "reserved_claude_uuid": reserved_uuid,
        "replacement_created": False,
        "active_record_retired": True,
    }


@pytest.mark.parametrize("phase", ["launched", "ready"])
def test_characterization_abort_rejects_unretirable_active_phase_before_store_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    root.mkdir(parents=True)
    operation_id = "55555555-5555-4555-8555-555555555555"
    state = _characterization_state(operation_id, phase=phase, source_root=root)
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        backend,
        "_require_store",
        lambda: (_ for _ in ()).throw(AssertionError("store must remain untouched")),
    )

    with pytest.raises(RolloutGateBlocked, match="characterization_abort_not_active"):
        backend.abort_claude_visibility_characterization(
            expected_job_id=str(state["job_id"]),
            expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        )


@pytest.mark.parametrize(
    ("registrar_status", "expected_gate"),
    [
        ("absent", None),
        ("visible", "characterization_native_session_materialized"),
        ("failed", "characterization_exact_id_conflict"),
    ],
)
def test_characterization_abort_reconciles_only_exact_uuid_and_never_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registrar_status: str,
    expected_gate: str | None,
) -> None:
    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    root.mkdir(parents=True)
    operation_id = "66666666-6666-4666-8666-666666666666"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    calls: list[tuple[object, ...]] = []

    class Claim:
        claimed = True
        status = "claimed"
        job_id = state["job_id"]
        reserved_claude_uuid = state["reserved_claude_uuid"]
        lease_kind = "reconciliation"
        launch_permitted = False
        registration_reserved = False
        requires_exact_id_reconciliation = True

    class Store:
        abort_calls = 0

        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            calls.append(("register",))
            return {
                "status": "registered",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
            }

        def record_claude_visibility_characterization(self, **kwargs):
            calls.append(("record", kwargs))
            assert kwargs["launch_aborted"] is True
            self.abort_calls += 1
            if self.abort_calls == 1:
                return {
                    "status": "reconciliation_required",
                    "job_id": state["job_id"],
                    "reserved_claude_uuid": state["reserved_claude_uuid"],
                }
            return {
                "status": "launch_aborted",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
            }

        def claim_claude_visibility_reconciliation(self, *args, **kwargs):
            calls.append(("claim", args, kwargs))
            assert kwargs == {"expected_job_id": state["job_id"]}
            return Claim()

    class Registrar:
        def process(self, claim):
            calls.append(("process", claim))
            assert claim is not None
            return type(
                "Outcome",
                (),
                {
                    "status": registrar_status,
                    "job_id": state["job_id"],
                    "reserved_claude_uuid": state["reserved_claude_uuid"],
                    "error_code": None,
                },
            )()

    store = Store()
    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: calls.append(("source",)) or object(),
    )

    def registrar_factory(*args, **kwargs):
        calls.append(("registrar", args, kwargs))
        assert kwargs["claude_command"] == ()
        return Registrar()

    monkeypatch.setattr("session_bridge.cli.ClaudeNativeRegistrar", registrar_factory)
    monkeypatch.setattr(
        "session_bridge.cli.claim_claude_visibility_characterization_abort",
        lambda **kwargs: (
            calls.append(("abort-intent", kwargs))
            or {
                "status": "claimed",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
                "operation": state,
            }
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda *_args, **_kwargs: pytest.fail("abort must not resolve a launcher"),
    )
    monkeypatch.setattr(
        "session_bridge.cli.retire_aborted_claude_visibility_characterization",
        lambda **kwargs: calls.append(("retire", kwargs)) or {"status": "retired"},
    )

    with pytest.raises(
        RolloutGateBlocked, match="characterization_abort_identity_mismatch"
    ):
        backend.abort_claude_visibility_characterization(
            expected_job_id="claude-visibility-job:reviewed-other-job",
            expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        )
    assert store.abort_calls == 0

    if expected_gate is not None:
        with pytest.raises(RolloutGateBlocked, match=expected_gate):
            backend.abort_claude_visibility_characterization(
                expected_job_id=str(state["job_id"]),
                expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
            )
        assert store.abort_calls == 1
    else:
        result = backend.abort_claude_visibility_characterization(
            expected_job_id=str(state["job_id"]),
            expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        )
        assert result == {
            "status": "aborted_exact_absence",
            "job_id": state["job_id"],
            "reserved_claude_uuid": state["reserved_claude_uuid"],
            "replacement_created": False,
            "active_record_retired": True,
        }
        assert store.abort_calls == 2
        assert len([call for call in calls if call[0] == "retire"]) == 1
    [claim_call] = [call for call in calls if call[0] == "claim"]
    assert claim_call[2] == {"expected_job_id": state["job_id"]}
    assert len([call for call in calls if call[0] == "process"]) == 1


def test_characterization_abort_registers_crash_before_enqueue_then_proves_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    root.mkdir(parents=True)
    operation_id = "67676767-6767-4767-8767-676767676767"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    disposable = Path(str(state["source_cwd"]))
    disposable.mkdir()
    (disposable / ".session-bridge-characterization.json").write_text(
        json.dumps({"operation_id": operation_id, "nonce": "nonce"}),
        encoding="utf-8",
    )
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        database, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    claims: list[object] = []

    class Registrar:
        def process(self, claim):
            assert not (root / ".claude-visibility-operation.json").exists()
            assert (root / ".abort-claims" / f"{operation_id}.json").exists()
            claims.append(claim)
            store.record_claude_visibility_exact_id_absent(
                claim.job_id,
                claim.lease_digest,
                claim.reserved_claude_uuid,
                claim.attempt_ordinal,
                "b" * 64,
            )
            return type(
                "Outcome",
                (),
                {
                    "status": "absent",
                    "job_id": claim.job_id,
                    "reserved_claude_uuid": claim.reserved_claude_uuid,
                    "error_code": None,
                },
            )()

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: Registrar(),
    )
    monkeypatch.setattr(
        "session_bridge.cli.retire_aborted_claude_visibility_characterization",
        lambda **_kwargs: {"status": "retired"},
    )

    result = backend.abort_claude_visibility_characterization(
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
    )

    assert result == {
        "status": "aborted_exact_absence",
        "job_id": state["job_id"],
        "reserved_claude_uuid": state["reserved_claude_uuid"],
        "replacement_created": False,
        "active_record_retired": True,
    }
    assert len(claims) == 1
    assert claims[0].lease_kind == "reconciliation"
    assert claims[0].attempt_ordinal == 0
    assert (
        database._conn.execute(
            "SELECT COUNT(*) FROM session_claude_registration_usage"
        ).fetchone()[0]
        == 0
    )
    assert [
        row[0]
        for row in database._conn.execute(
            """SELECT event_kind
               FROM session_claude_visibility_characterization_events
               WHERE job_id = ? ORDER BY event_kind""",
            (state["job_id"],),
        ).fetchall()
    ] == ["launch_aborted", "registered"]
    database.close()


def test_characterization_abort_claim_survives_crash_before_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import session_bridge.cli as cli_module

    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    projects = tmp_path / "projects"
    root.mkdir(parents=True)
    projects.mkdir()
    operation_id = "68686868-6868-4868-8868-686868686868"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    disposable = Path(str(state["source_cwd"]))
    disposable.mkdir()
    (disposable / ".session-bridge-characterization.json").write_text(
        json.dumps({"operation_id": operation_id, "nonce": "nonce"}),
        encoding="utf-8",
    )
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        database, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    original_record = cli_module._record_claude_characterization_payload
    crash_once = True

    def crash_before_registration(**kwargs):
        nonlocal crash_once
        if crash_once and kwargs.get("ensure_registered") is True:
            crash_once = False
            raise RuntimeError("simulated crash before registration")
        return original_record(**kwargs)

    class Registrar:
        def process(self, claim):
            store.record_claude_visibility_exact_id_absent(
                claim.job_id,
                claim.lease_digest,
                claim.reserved_claude_uuid,
                claim.attempt_ordinal,
                "b" * 64,
            )
            return type(
                "Outcome",
                (),
                {
                    "status": "absent",
                    "job_id": claim.job_id,
                    "reserved_claude_uuid": claim.reserved_claude_uuid,
                    "error_code": None,
                },
            )()

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr(
        "session_bridge.cli._record_claude_characterization_payload",
        crash_before_registration,
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: Registrar(),
    )

    with pytest.raises(RuntimeError, match="simulated crash before registration"):
        backend.abort_claude_visibility_characterization(
            expected_job_id=str(state["job_id"]),
            expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        )

    assert not (root / ".claude-visibility-operation.json").exists()
    assert (root / ".abort-claims" / f"{operation_id}.json").exists()
    assert (
        database._conn.execute(
            "SELECT COUNT(*) FROM session_claude_visibility_jobs"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(RuntimeError, match="characterization_abort_in_progress"):
        characterize_claude_visibility(
            source_root=root,
            projects_root=projects,
            reserve=lambda _projection: pytest.fail("abort claim must block reserve"),
            registrar=object(),
            restarted_source=lambda: pytest.fail(
                "abort claim must block source discovery"
            ),
            marker_secret=b"k" * 32,
        )

    result = backend.abort_claude_visibility_characterization(
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
    )

    assert result["status"] == "aborted_exact_absence"
    assert result["replacement_created"] is False
    assert (root / ".abort-completed" / f"{operation_id}.json").exists()
    database.close()


def test_characterization_abort_replays_exact_absence_after_crash_before_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    root.mkdir(parents=True)
    operation_id = "69696969-6969-4969-8969-696969696969"
    state = _characterization_state(operation_id, phase="launching", source_root=root)
    disposable = Path(str(state["source_cwd"]))
    disposable.mkdir()
    (disposable / ".session-bridge-characterization.json").write_text(
        json.dumps({"operation_id": operation_id, "nonce": "nonce"}),
        encoding="utf-8",
    )
    _write_characterization_record(
        root / ".claude-visibility-operation.json", state, b"k" * 32
    )
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        database, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    process_calls = 0

    class Registrar:
        def process(self, claim):
            nonlocal process_calls
            process_calls += 1
            store.record_claude_visibility_exact_id_absent(
                claim.job_id,
                claim.lease_digest,
                claim.reserved_claude_uuid,
                claim.attempt_ordinal,
                "b" * 64,
            )
            raise RuntimeError("simulated crash after exact absence")

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: Registrar(),
    )

    with pytest.raises(RuntimeError, match="simulated crash after exact absence"):
        backend.abort_claude_visibility_characterization(
            expected_job_id=str(state["job_id"]),
            expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        )

    result = backend.abort_claude_visibility_characterization(
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
    )

    assert result["status"] == "aborted_exact_absence"
    assert process_calls == 1
    assert (
        database._conn.execute(
            """SELECT COUNT(*)
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ? AND event_kind = 'launch_aborted'""",
            (state["job_id"],),
        ).fetchone()[0]
        == 1
    )
    database.close()


def test_characterization_abort_replays_claimed_filesystem_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    claims = root / ".abort-claims"
    claims.mkdir(parents=True)
    operation_id = "88888888-8888-4888-8888-888888888888"
    state = _characterization_state(
        operation_id, phase="abort_disposable_removing", source_root=root
    )
    _write_characterization_record(claims / f"{operation_id}.json", state, b"k" * 32)
    events: list[tuple[str, object]] = []

    class Store:
        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            events.append(("register", None))
            return {
                "status": "registered",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
            }

        def record_claude_visibility_characterization(self, **kwargs):
            events.append(("record", kwargs))
            return {
                "status": "already_aborted",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
            }

        def claim_claude_visibility_reconciliation(self, *_args, **_kwargs):
            raise AssertionError("replay must not acquire another lease")

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.claim_claude_visibility_characterization_abort",
        lambda **kwargs: (
            events.append(("abort-intent", kwargs))
            or {
                "status": "claimed",
                "job_id": state["job_id"],
                "reserved_claude_uuid": state["reserved_claude_uuid"],
                "operation": state,
            }
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.retire_aborted_claude_visibility_characterization",
        lambda **kwargs: events.append(("retire", kwargs)) or {"status": "retired"},
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda *_args, **_kwargs: pytest.fail("replay must not resolve a launcher"),
    )

    result = backend.abort_claude_visibility_characterization(
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
    )

    assert result == {
        "status": "aborted_exact_absence",
        "job_id": state["job_id"],
        "reserved_claude_uuid": state["reserved_claude_uuid"],
        "replacement_created": False,
        "active_record_retired": True,
        "replayed": True,
    }
    assert [event[0] for event in events] == [
        "abort-intent",
        "register",
        "record",
        "retire",
    ]
    assert events[3][1]["expected_operation_id"] == operation_id  # type: ignore[index]


def test_claude_visibility_status_reports_durable_open_work_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    "claude_pending": 0,
                    "claude_leased": 0,
                    "claude_retry": 1,
                    "claude_visible": 0,
                    "claude_failed": 0,
                },
                "retry_codes": {"creation_ambiguous": 1},
                "failed_codes": {},
                "fatal": [],
                "usage": {
                    "local_day": "2026-07-18",
                    "attempts": 1,
                    "reserved_cost_usd": "0.02",
                },
            }

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())

    result = backend.claude_visibility_status()

    assert result["enabled"] is False
    assert result["counts"]["claude_retry"] == 1
    assert result["retry_codes"] == {"creation_ambiguous": 1}
    assert result["usage"]["attempts"] == 1
    assert result["open_reasons"] == ["open_visibility_work"]


@pytest.mark.parametrize(
    ("counts", "active_operation", "allowed"),
    [
        ({"claude_retry": 1}, True, False),
        ({"claude_retry": 1}, False, False),
        ({"claude_retry": 2}, True, False),
        ({"claude_leased": 1}, True, False),
        ({"claude_failed": 1}, True, False),
        ({"claude_pending": 1}, True, False),
    ],
)
def test_characterization_recovery_allows_only_one_owned_open_job(
    counts: dict[str, int], active_operation: bool, allowed: bool
) -> None:
    complete = {
        state: counts.get(state, 0)
        for state in (
            "claude_pending",
            "claude_leased",
            "claude_retry",
            "claude_failed",
        )
    }

    assert (
        _claude_characterization_open_work_allowed(
            {"counts": complete}, active_operation=active_operation
        )
        is allowed
    )


@pytest.mark.parametrize(
    "state",
    ["claude_pending", "claude_leased", "claude_retry"],
)
def test_characterization_recovery_allows_only_exact_authenticated_open_job(
    state: str,
) -> None:
    raw = {
        "counts": {
            candidate: int(candidate == state)
            for candidate in (
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_failed",
            )
        },
        "characterizations": [
            {"job_id": "claude-visibility-job:exact", "state": state}
        ],
    }

    assert _claude_characterization_open_work_allowed(
        raw,
        active_operation=True,
        active_job_id="claude-visibility-job:exact",
    )
    assert not _claude_characterization_open_work_allowed(
        raw,
        active_operation=True,
        active_job_id="claude-visibility-job:other",
    )


@pytest.mark.parametrize(
    ("failed_codes", "allowed"),
    [({"bridge_conflict": 1}, True), ({"future_failure": 1}, False)],
)
def test_characterization_recovery_allows_only_exact_auth_recoverable_failed_job(
    failed_codes: dict[str, int], allowed: bool
) -> None:
    raw = {
        "counts": {
            "claude_pending": 0,
            "claude_leased": 0,
            "claude_retry": 0,
            "claude_failed": 1,
        },
        "characterizations": [
            {
                "job_id": "claude-visibility-job:exact",
                "state": "claude_failed",
            }
        ],
        "failed_codes": failed_codes,
        "retry_codes": {},
    }

    assert (
        _claude_characterization_open_work_allowed(
            raw,
            active_operation=True,
            active_job_id="claude-visibility-job:exact",
        )
        is allowed
    )


def test_claude_visibility_status_exposes_durable_cycle_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = {
        "tracked": True,
        "value": {
            "at": 100.0,
            "sequence": 1,
            "status": "no_due_job",
            "error_code": None,
            "empty_verified": True,
        },
    }

    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    state: 0
                    for state in (
                        "claude_pending",
                        "claude_leased",
                        "claude_retry",
                        "claude_visible",
                        "claude_failed",
                    )
                },
                "retry_codes": {},
                "failed_codes": {},
                "fatal": [],
                "usage": {
                    "local_day": "2026-07-17",
                    "attempts": 0,
                    "reserved_cost_usd": "0",
                },
                "last_cycle": tracked,
                "last_empty_cycle": {"tracked": True, "value": 100.0},
                "last_registrar_result": {"tracked": False, "value": None},
            }

    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config, claude_visibility=replace(config.claude_visibility, enabled=True)
        )
    )
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())

    result = backend.claude_visibility_status()

    assert result["last_cycle"] == tracked
    assert result["last_empty_cycle"] == {"tracked": True, "value": 100.0}


def test_claude_visibility_status_exposes_sanitized_unknown_state_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    state: 0
                    for state in (
                        "claude_pending",
                        "claude_leased",
                        "claude_retry",
                        "claude_visible",
                        "claude_failed",
                    )
                },
                "retry_codes": {},
                "failed_codes": {},
                "usage": {
                    "local_day": "2026-07-17",
                    "attempts": 0,
                    "reserved_cost_usd": "0",
                },
                "fatal": [
                    {
                        "code": "unknown_job_state",
                        "state": "future_state",
                        "error_code": "future-code",
                        "count": 1,
                    }
                ],
            }

    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config, claude_visibility=replace(config.claude_visibility, enabled=True)
        )
    )
    monkeypatch.setattr(backend, "_require_store", lambda: ReadOnlyStore())

    result = backend.claude_visibility_status()

    assert result["fatal_reasons"] == ["unknown_job_state"]
    assert result["fatal"] == [
        {
            "code": "unknown_job_state",
            "state": "future_state",
            "error_code": "future-code",
            "count": 1,
        }
    ]


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


def test_production_serve_applies_sidebar_create_cutover_before_public_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(BridgeConfig())
    events: list[str] = []

    monkeypatch.setattr(
        backend,
        "_apply_sidebar_create_reservation_cutover",
        lambda **_kwargs: events.append("cutover"),
        raising=False,
    )

    def fail_provider_runtime(**_kwargs: Any) -> None:
        assert events == ["cutover"]
        events.append("provider_runtime")
        raise RuntimeError("stop before serving")

    monkeypatch.setattr(backend, "_provider_runtime", fail_provider_runtime)

    with pytest.raises(ProviderDegraded, match="service_start_failed"):
        backend.serve()

    assert events == ["cutover", "provider_runtime"]


def test_production_serve_preserves_sidebar_cutover_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setattr(
        backend,
        "_apply_sidebar_create_reservation_cutover",
        lambda **_kwargs: (_ for _ in ()).throw(
            ConfigurationFailure("sidebar_create_reservation_cutover_invalid")
        ),
    )
    monkeypatch.setattr(
        backend,
        "_provider_runtime",
        lambda **_kwargs: pytest.fail("provider runtime must not start"),
    )

    with pytest.raises(
        ConfigurationFailure,
        match="^sidebar_create_reservation_cutover_invalid$",
    ):
        backend.serve()


def test_continuous_visibility_worker_keeps_start_to_start_interval() -> None:
    calls: list[str] = []
    waits: list[float] = []
    moments = iter((10.0, 46.5))

    class StopAfterOneCycle:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return True

    _run_continuous_visibility_worker(
        run_once=lambda: calls.append("run"),
        close=lambda: calls.append("close"),
        stop=StopAfterOneCycle(),
        interval_seconds=60.0,
        monotonic=lambda: next(moments),
    )

    assert calls == ["run", "close"]
    assert waits == [pytest.approx(23.5)]


def test_continuous_sidebar_recovery_worker_drains_then_uses_idle_wait() -> None:
    calls: list[str] = []
    waits: list[float] = []
    results = iter((
        {"lane": "hydration", "status": "visible"},
        {"lane": "registration", "status": "idle"},
    ))

    class StopAfterIdle:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return len(waits) == 2

    cli_module._run_continuous_sidebar_recovery_worker(
        run_once=lambda: calls.append("run") or next(results),
        close=lambda: calls.append("close"),
        stop=StopAfterIdle(),
        actionable_interval_seconds=0.05,
        idle_interval_seconds=2.0,
    )

    assert calls == ["run", "run", "close"]
    assert waits == [0.05, 2.0]


def test_production_sidebar_recovery_once_requires_desktop_broker() -> None:
    backend = ProductionBackend(BridgeConfig())

    with pytest.raises(RolloutGateBlocked, match="desktop_broker_required"):
        backend.run_sidebar_recovery_once()

    assert backend._sidebar_executor is None


def test_sidebar_hydration_claim_runtime_does_not_start_a_provider_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(
        BridgeConfig(sidebar=SidebarConfig(enabled=True, continuous=True))
    )
    provider_calls: list[str] = []
    store = object()
    native = object()

    def reject_provider_runtime(**kwargs: object) -> object:
        del kwargs
        provider_calls.append("provider")
        raise AssertionError("hydration claims are store-backed")

    monkeypatch.setattr(
        backend,
        "_provider_runtime",
        reject_provider_runtime,
    )
    monkeypatch.setattr(backend, "_require_store", lambda: store)
    monkeypatch.setattr(
        backend,
        "_require_sidebar_terminal_delivery",
        lambda: native,
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_marker_key",
        lambda: b"h" * 32,
    )

    executor = backend._require_sidebar_hydration_executor()

    assert isinstance(executor, SidebarHydrationExecutor)
    assert provider_calls == []
    assert backend._codex_client is None


class _RecordedServeTransport:
    """What serve() built instead of actually binding a port."""

    def __init__(self) -> None:
        self.config_kwargs: dict[str, object] = {}
        self.ran = False
        self.should_exit = False
        self.watchdogs: list[_FakeListenerWatchdog] = []


class _FakeListenerWatchdog:
    def __init__(self, *, host: str, port: int, on_deaf: object) -> None:
        self.host = host
        self.port = port
        self.on_deaf = on_deaf
        self.started = False
        self.stopped = False
        self.fired = False

    def start(self) -> object:
        self.started = True
        return self

    def stop(self, **_kwargs: object) -> None:
        self.stopped = True


def _stub_serve_transport(
    monkeypatch: pytest.MonkeyPatch, *, fires: bool = False
) -> _RecordedServeTransport:
    """Keep serve() off a real socket.

    serve() no longer calls uvicorn.run(); it builds Config+Server so the
    listener watchdog has a shutdown handle. A test that only stubbed
    uvicorn.run would therefore bind the real service port -- 7484 on this
    box, where the live bridge is listening.
    """
    import uvicorn

    record = _RecordedServeTransport()

    class _FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            record.config_kwargs = {"app": app, **kwargs}

    class _FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config
            self.should_exit = False

        def run(self) -> None:
            record.ran = True
            record.should_exit = self.should_exit

    def _build_watchdog(**kwargs: object) -> _FakeListenerWatchdog:
        watchdog = _FakeListenerWatchdog(**kwargs)  # type: ignore[arg-type]
        watchdog.fired = fires
        record.watchdogs.append(watchdog)
        return watchdog

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr("session_bridge.cli.ListenerWatchdog", _build_watchdog)
    return record


def test_production_serve_does_not_start_local_sidebar_recovery_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig(
        sidebar=SidebarConfig(enabled=True, continuous=True),
    )
    state_db = Path("C:/hermetic/session-bridge.db")
    backend = ProductionBackend(
        replace(
            config,
            claude_visibility=replace(
                config.claude_visibility,
                enabled=True,
                continuous=True,
            ),
        ),
        db_path=state_db,
    )
    threads: list[object] = []
    visibility_db_paths: list[Path | None] = []

    class VisibilityBackend:
        def claude_visibility_run_once(self) -> dict[str, object]:
            return {"status": "idle"}

        def close(self) -> None:
            return None

    visibility_backend = VisibilityBackend()

    class FakeThread:
        def __init__(
            self,
            *,
            target: object,
            kwargs: dict[str, object],
            name: str,
            daemon: bool,
        ) -> None:
            self.target = target
            self.kwargs = kwargs
            self.name = name
            self.daemon = daemon
            self.started = False
            self.join_timeout: float | None = None
            threads.append(self)

        def start(self) -> None:
            self.started = True

        def join(self, timeout: float) -> None:
            self.join_timeout = timeout

    monkeypatch.setattr(
        backend,
        "_apply_sidebar_create_reservation_cutover",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(backend, "_provider_runtime", lambda **_kwargs: object())
    monkeypatch.setattr(backend, "_require_catalog", lambda: object())
    monkeypatch.setattr(backend, "_require_store", lambda: object())
    monkeypatch.setattr("session_bridge.cli.resolve_bearer_token", lambda: "token")
    monkeypatch.setattr("session_bridge.cli.create_app", lambda **_kwargs: object())
    def build_visibility_backend(
        _config: BridgeConfig, *, db_path: Path | None = None
    ) -> VisibilityBackend:
        visibility_db_paths.append(db_path)
        return visibility_backend

    monkeypatch.setattr(
        "session_bridge.cli.ProductionBackend",
        build_visibility_backend,
    )
    monkeypatch.setattr("session_bridge.cli.threading.Thread", FakeThread)
    _stub_serve_transport(monkeypatch)

    backend.serve()

    started = [thread for thread in threads if thread.started]
    thread = next(
        thread
        for thread in started
        if thread.name == "session-bridge-claude-visibility"
    )
    assert thread.name == "session-bridge-claude-visibility"
    assert thread.daemon is True
    assert thread.started is True
    assert thread.join_timeout == 5.0
    assert thread.target is cli_module._run_continuous_visibility_worker
    assert thread.kwargs["run_once"] == visibility_backend.claude_visibility_run_once
    assert thread.kwargs["close"] == visibility_backend.close
    assert thread.kwargs["stop"].is_set() is True
    assert visibility_db_paths == [state_db]
    assert all(thread.name != "session-bridge-sidebar-recovery" for thread in started)
    assert all(
        thread.target is not cli_module._run_continuous_sidebar_recovery_worker
        for thread in started
    )


def _serve_backend(monkeypatch: pytest.MonkeyPatch, **config_kwargs) -> ProductionBackend:
    """A ProductionBackend whose serve() has every dependency stubbed but the transport."""
    config = BridgeConfig(**config_kwargs) if config_kwargs else BridgeConfig()
    backend = ProductionBackend(config, db_path=Path("C:/hermetic/session-bridge.db"))
    monkeypatch.setattr(
        backend,
        "_apply_sidebar_create_reservation_cutover",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(backend, "_provider_runtime", lambda **_kwargs: object())
    monkeypatch.setattr(backend, "_require_catalog", lambda: object())
    monkeypatch.setattr(backend, "_require_store", lambda: object())
    monkeypatch.setattr("session_bridge.cli.resolve_bearer_token", lambda: "token")
    monkeypatch.setattr("session_bridge.cli.create_app", lambda **_kwargs: object())
    return backend


def test_production_serve_watches_the_port_it_actually_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog pointed at the wrong port would restart the service forever."""
    record = _stub_serve_transport(monkeypatch)
    backend = _serve_backend(
        monkeypatch, service=ServiceConfig(host="127.0.0.1", port=7484)
    )

    backend.serve()

    assert record.ran is True
    assert record.config_kwargs["host"] == "127.0.0.1"
    assert record.config_kwargs["port"] == 7484
    assert len(record.watchdogs) == 1
    watchdog = record.watchdogs[0]
    assert (watchdog.host, watchdog.port) == (
        record.config_kwargs["host"],
        record.config_kwargs["port"],
    )
    assert watchdog.started is True
    assert watchdog.stopped is True


def test_production_serve_stops_the_watchdog_even_when_serving_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_serve_transport(monkeypatch)
    backend = _serve_backend(monkeypatch)

    import uvicorn

    class _ExplodingServer:
        def __init__(self, config: object) -> None:
            self.should_exit = False

        def run(self) -> None:
            raise OSError("bind failed")

    monkeypatch.setattr(uvicorn, "Server", _ExplodingServer)

    with pytest.raises(ConfigurationFailure):
        backend.serve()

    assert record.watchdogs[0].stopped is True


def test_production_serve_reports_a_deaf_listener_as_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not service_start_failed: the service started fine and then went deaf."""
    _stub_serve_transport(monkeypatch, fires=True)
    backend = _serve_backend(monkeypatch)

    with pytest.raises(ProviderDegraded) as excinfo:
        backend.serve()

    assert str(excinfo.value) == "service_listener_deaf"


def test_production_serve_returns_cleanly_when_the_watchdog_never_fired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_serve_transport(monkeypatch, fires=False)
    backend = _serve_backend(monkeypatch)

    assert backend.serve() is None


def test_a_deaf_listener_exits_degraded_not_one(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Exit 1 is reserved for an uncaught BaseException; the supervisor keys on this."""

    class DeafBackend(FakeBackend):
        def serve(self) -> None:
            raise ProviderDegraded("service_listener_deaf")

    assert _run(["serve"], DeafBackend(), automatic_creation=True) == 3


def test_deaf_handler_asks_for_shutdown_then_insists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_bridge.listener_watchdog import make_deaf_listener_handler

    requested: list[bool] = []
    exited: list[int] = []
    timers: list[tuple[float, object]] = []

    class _FakeTimer:
        def __init__(self, interval: float, function) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False
            timers.append((interval, function))

        def start(self) -> None:
            return None

    handler = make_deaf_listener_handler(
        lambda: requested.append(True),
        grace=20.0,
        hard_exit=exited.append,
        timer_factory=_FakeTimer,
        stream=io.StringIO(),
    )
    handler(3)

    assert requested == [True]
    assert exited == [], "the polite shutdown must get its grace period first"
    assert len(timers) == 1
    interval, function = timers[0]
    assert interval == 20.0
    function()
    assert exited == [3]


def test_deaf_handler_still_insists_when_the_polite_path_raises() -> None:
    from session_bridge.listener_watchdog import make_deaf_listener_handler

    exited: list[int] = []
    timers: list[object] = []

    class _FakeTimer:
        def __init__(self, interval: float, function) -> None:
            self.function = function
            self.daemon = False
            timers.append(function)

        def start(self) -> None:
            return None

    def _explode() -> None:
        raise RuntimeError("uvicorn is wedged")

    handler = make_deaf_listener_handler(
        _explode,
        hard_exit=exited.append,
        timer_factory=_FakeTimer,
        stream=io.StringIO(),
    )
    handler(3)

    assert len(timers) == 1
    timers[0]()
    assert exited == [3]


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


def test_production_codex_runtime_owns_a_recovering_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(db)
    backend = ProductionBackend(BridgeConfig())
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)

    class Client:
        def close(self) -> None:
            pass

    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"m" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda name: (name,),
    )
    monkeypatch.setattr(
        "session_bridge.cli.CodexAppServerClient",
        lambda **_kwargs: Client(),
    )
    try:
        backend._provider_runtime(
            targets=False,
            catalog_only=True,
            providers=(Provider.CODEX,),
        )

        assert isinstance(
            backend._codex_client,
            RecoveringCodexAppServerClient,
        )
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
                    return {
                        "data": [
                            {
                                "id": thread_id,
                                "title": "Native task",
                                "cwd": str(tmp_path),
                                "createdAt": now,
                                "updatedAt": now,
                                "revision": "revision-1",
                            }
                        ]
                    }
                return {"data": []}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": thread_id,
                        "turns": [
                            {
                                "id": "registration-turn",
                                "status": "completed",
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "registration-item",
                                        "content": [{"type": "text", "text": marker}],
                                    }
                                ],
                            }
                        ],
                    }
                }
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
            sidebar=replace(SidebarConfig(), enabled=True, continuous=True),
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
        assert coordinator._sidebar_executor is None

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


def test_production_sidebar_executor_is_disabled_without_construction() -> None:
    backend = ProductionBackend(BridgeConfig())

    with pytest.raises(RolloutGateBlocked, match="desktop_broker_required"):
        backend._require_sidebar_executor()

    assert backend._sidebar_executor is None
    assert backend._sidebar_codex_client is None
    assert backend._sidebar_registration_codex_client is None


@pytest.mark.parametrize(
    ("sidebar", "expected_gate"),
    [
        (SidebarConfig(enabled=False, continuous=False), "desktop_broker_required"),
        (
            SidebarConfig(enabled=True, continuous=True),
            "desktop_broker_required",
        ),
    ],
)
def test_production_sidebar_run_once_refuses_before_executor_construction(
    sidebar: SidebarConfig,
    expected_gate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(replace(BridgeConfig(), sidebar=sidebar))
    executor_constructions: list[str] = []

    def unexpected_executor_construction() -> None:
        executor_constructions.append("constructed")
        raise AssertionError("sidebar executor must not be constructed")

    monkeypatch.setattr(
        backend,
        "_require_sidebar_executor",
        unexpected_executor_construction,
    )

    with pytest.raises(RolloutGateBlocked) as exc_info:
        backend.sidebar_run_once()

    assert exc_info.value.gate == expected_gate
    assert executor_constructions == []


def test_production_backend_close_attempts_all_cleanup_when_first_client_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ProductionBackend(BridgeConfig())

    class CloseProbe:
        def __init__(self, *, failure: Exception | None = None) -> None:
            self.failure = failure
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1
            if self.failure is not None:
                raise self.failure

    provider_client = CloseProbe(failure=RuntimeError("provider close failed"))
    sidebar_client = CloseProbe()
    registration_client = CloseProbe()
    database = CloseProbe()
    monkeypatch.setattr(backend, "_codex_client", provider_client)
    monkeypatch.setattr(backend, "_sidebar_codex_client", sidebar_client)
    monkeypatch.setattr(
        backend,
        "_sidebar_registration_codex_client",
        registration_client,
    )
    monkeypatch.setattr(backend, "_db", database)

    with pytest.raises(RuntimeError, match="provider close failed"):
        backend.close()
    backend.close()

    assert provider_client.close_count == 1
    assert sidebar_client.close_count == 1
    assert registration_client.close_count == 1
    assert database.close_count == 1
    assert backend._codex_client is None
    assert backend._sidebar_codex_client is None
    assert backend._sidebar_registration_codex_client is None
    assert backend._db is None


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


# ---------------------------------------------------------------------------
# The preflight gate that refused must reach the exception message, which is
# what _run_continuous_visibility_worker logs. Public CLI output is unaffected:
# main() still collapses ProviderDegraded to {"error": "provider_degraded"}.
# ---------------------------------------------------------------------------


def _refusing_preflight(code: str):
    from session_bridge.cli import _ClaudeVisibilityPreflight

    def preflight(*_args: object, **_kwargs: object) -> object:
        return _ClaudeVisibilityPreflight(None, code)

    return preflight


def test_claude_visibility_runtime_raises_the_specific_preflight_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig()
    backend = ProductionBackend(
        replace(
            config, claude_visibility=replace(config.claude_visibility, enabled=True)
        )
    )
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable", lambda _name: ("claude",)
    )
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight_detail",
        _refusing_preflight("claude_visibility_preflight_failed_auth_unavailable"),
    )

    with pytest.raises(
        ProviderDegraded, match="claude_visibility_preflight_failed_auth_unavailable"
    ):
        backend.claude_visibility_run_once()


def test_claude_visibility_preflight_gate_codes_stay_out_of_public_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A diagnostic code must never widen the public error contract."""
    from session_bridge.cli import EXIT_DEGRADED, main

    code = "claude_visibility_preflight_failed_not_logged_in"

    class _RaisingBackend:
        def claude_visibility_run_once(self) -> Mapping[str, Any]:
            raise ProviderDegraded(code)

        def close(self) -> None:
            return None

    exit_code = main(
        ["claude-visibility-run-once"],
        config_loader=BridgeConfig,
        backend_factory=lambda _config: _RaisingBackend(),
    )
    captured = capsys.readouterr().out

    assert exit_code == EXIT_DEGRADED
    assert json.loads(captured.strip().splitlines()[-1]) == {
        "error": "provider_degraded"
    }
    assert "not_logged_in" not in captured
    assert code not in captured
