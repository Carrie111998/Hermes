"""IdleChipArchiveWorker: recurring auto-archive of idle automation-chip records.

Chip sessions started in the Claude Code desktop app (verb-first titles under
bypassPermissions, or worktree/profile cwds) each leave an unarchived
``local_*.json`` registry record that sits in the sidebar forever. This worker
archives them once every store copy of the session has been idle past a
threshold. It must never touch bridge mirror records (the mirror_float recency
rule owns those), user-typed sessions, or anything still live in ANY harness
store — the current-account store is the only one the app updates live, so
per-record idleness on a union-synced copy lies about a running session.
"""
from __future__ import annotations

import json
import os
from datetime import timezone
from pathlib import Path

import pytest

from session_bridge.mirror_float import IdleChipArchiveWorker

NOW = 2_000_000_000.0
DAY = 86_400.0


def _write_record(
    root: Path,
    session_id: str,
    *,
    cli_session_id: str | None = None,
    title: str = "Fix the widget frobnicator",
    permission_mode: str = "bypassPermissions",
    cwd: str = "C:\\Users\\diego",
    is_archived: bool = False,
    last_activity_at_ms: int | None = None,
    mtime: float | None = None,
    raw: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"local_{session_id}.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        record = {
            "sessionId": f"local_{session_id}",
            "cliSessionId": cli_session_id or f"cli-{session_id}",
            "cwd": cwd,
            "originCwd": cwd,
            "createdAt": int((NOW - 30 * DAY) * 1000),
            "lastActivityAt": (
                last_activity_at_ms
                if last_activity_at_ms is not None
                else int((NOW - 2 * DAY) * 1000)
            ),
            "model": "claude-fable-5",
            "isArchived": is_archived,
            "title": title,
            "permissionMode": permission_mode,
            "extraField": {"must": "survive"},
        }
        path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    stamp = mtime if mtime is not None else NOW - 2 * DAY
    os.utime(path, (stamp, stamp))
    return path


def _worker(*roots: Path, **overrides) -> IdleChipArchiveWorker:
    options = {
        "registry_roots": roots,
        "idle_seconds": 1 * DAY,
        "lookback_seconds": 14 * DAY,
        "run_min_interval_seconds": 3600.0,
        "monotonic": iter(range(0, 10_000_000, 100_000)).__next__,
        "wall_clock": lambda: NOW,
    }
    options.update(overrides)
    return IdleChipArchiveWorker(**options)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_archives_idle_verb_title_bypass_chip(tmp_path) -> None:
    path = _write_record(tmp_path / "a", "chip1")
    worker = _worker(tmp_path / "a")

    result = worker.run_once()

    assert result == {"examined": 1, "archived": 1, "skipped": 0, "throttled": 0}
    record = _load(path)
    assert record["isArchived"] is True
    # every other field survives the rewrite byte-for-byte in value terms
    assert record["extraField"] == {"must": "survive"}
    assert record["title"] == "Fix the widget frobnicator"
    assert record["permissionMode"] == "bypassPermissions"


def test_spares_recently_active_chip(tmp_path) -> None:
    path = _write_record(
        tmp_path / "a",
        "chip1",
        last_activity_at_ms=int((NOW - 3600) * 1000),
        mtime=NOW - 3600,
    )
    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 0
    assert _load(path)["isArchived"] is False


def test_spares_chip_live_in_another_store_copy(tmp_path) -> None:
    """B/C union-synced copies carry stale timestamps for a session that is
    live in the current-account store; group liveness must span all roots."""
    stale = _write_record(
        tmp_path / "b", "chip1", cli_session_id="cli-shared"
    )
    _write_record(
        tmp_path / "a",
        "chip1",
        cli_session_id="cli-shared",
        last_activity_at_ms=int((NOW - 600) * 1000),
        mtime=NOW - 600,
    )
    result = _worker(tmp_path / "a", tmp_path / "b").run_once()

    assert result["archived"] == 0
    assert _load(stale)["isArchived"] is False


def test_spares_user_permission_modes(tmp_path) -> None:
    for index, mode in enumerate(("default", "acceptEdits", "plan", "auto")):
        _write_record(tmp_path / "a", f"user{index}", permission_mode=mode)

    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 0


def test_spares_bypass_record_with_conversational_title(tmp_path) -> None:
    path = _write_record(
        tmp_path / "a", "diego1", title="AI usage monitor email mismatch"
    )
    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 0
    assert _load(path)["isArchived"] is False


def test_archives_bypass_record_with_worktree_cwd_regardless_of_title(
    tmp_path,
) -> None:
    path = _write_record(
        tmp_path / "a",
        "wt1",
        title="AI usage monitor email mismatch",
        cwd="C:\\Users\\diego\\.hermes\\.claude\\worktrees\\jovial-x",
    )
    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 1
    assert _load(path)["isArchived"] is True


def test_never_touches_tagged_mirror_records(tmp_path) -> None:
    for index, title in enumerate(
        ("[Codex] Fix the importer", "[Hermes] Verify gateway", "[Bridge] abc123")
    ):
        _write_record(tmp_path / "a", f"mirror{index}", title=title)

    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 0


def test_skips_already_archived_without_rewrite(tmp_path) -> None:
    path = _write_record(tmp_path / "a", "done1", is_archived=True)
    before = path.stat().st_mtime

    result = _worker(tmp_path / "a").run_once()

    assert result["archived"] == 0
    assert path.stat().st_mtime == before


def test_lookback_bounds_the_scan(tmp_path) -> None:
    """Records untouched for longer than the lookback are not even read —
    the scan cost must stay proportional to recent activity, not store size."""
    _write_record(
        tmp_path / "a",
        "old1",
        last_activity_at_ms=int((NOW - 60 * DAY) * 1000),
        mtime=NOW - 60 * DAY,
    )
    result = _worker(tmp_path / "a").run_once()

    assert result == {"examined": 0, "archived": 0, "skipped": 0, "throttled": 0}


def test_unreadable_record_counts_skipped(tmp_path) -> None:
    _write_record(tmp_path / "a", "bad1", raw="{not json")

    result = _worker(tmp_path / "a").run_once()

    assert result["skipped"] == 1
    assert result["archived"] == 0


def test_second_run_within_interval_throttles(tmp_path) -> None:
    _write_record(tmp_path / "a", "chip1")
    clock_values = iter((0.0, 100.0))
    worker = _worker(
        tmp_path / "a", monotonic=clock_values.__next__, run_min_interval_seconds=3600.0
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first["archived"] == 1
    assert second == {"examined": 0, "archived": 0, "skipped": 0, "throttled": 1}


def test_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError):
        IdleChipArchiveWorker(registry_roots=(), idle_seconds=0.0)
    with pytest.raises(ValueError):
        IdleChipArchiveWorker(registry_roots=(), lookback_seconds=100.0)
    with pytest.raises(TypeError):
        IdleChipArchiveWorker(registry_roots=("not-a-path",))


def test_lookback_must_exceed_idle_threshold() -> None:
    with pytest.raises(ValueError, match="lookback"):
        IdleChipArchiveWorker(
            registry_roots=(), idle_seconds=10 * DAY, lookback_seconds=5 * DAY
        )


# --- config plumbing ---------------------------------------------------------


def test_config_parses_archive_idle_chips(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.config import BridgeConfig

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "session_bridge": {
                "claude_visibility": {
                    "enabled": True,
                    "archive_idle_chips": True,
                    "idle_chip_archive_seconds": 43_200,
                }
            }
        },
    )

    config = BridgeConfig.load(path=tmp_path / "session_bridge.toml")

    assert config.claude_visibility.archive_idle_chips is True
    assert config.claude_visibility.idle_chip_archive_seconds == 43_200


def test_config_archive_idle_chips_defaults_and_rejections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.config import BridgeConfig

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"session_bridge": {}}
    )
    defaults = BridgeConfig.load(path=tmp_path / "session_bridge.toml")
    assert defaults.claude_visibility.archive_idle_chips is False
    assert defaults.claude_visibility.idle_chip_archive_seconds == 86_400

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "session_bridge": {"claude_visibility": {"archive_idle_chips": "yes"}}
        },
    )
    with pytest.raises(ValueError, match="archive_idle_chips"):
        BridgeConfig.load(path=tmp_path / "session_bridge.toml")

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "session_bridge": {
                "claude_visibility": {"idle_chip_archive_seconds": 60}
            }
        },
    )
    with pytest.raises(ValueError, match="idle_chip_archive_seconds"):
        BridgeConfig.load(path=tmp_path / "session_bridge.toml")


# --- coordinator + serve wiring ---------------------------------------------


def test_coordinator_idle_chip_archiver_must_provide_run_once() -> None:
    from session_bridge.coordinator import SessionBridgeCoordinator
    from tests.session_bridge.test_coordinator import (
        _sidebar_config,
        _SidebarScanStore,
    )

    with pytest.raises(TypeError, match="idle_chip_archiver must provide run_once"):
        SessionBridgeCoordinator(
            config=_sidebar_config(),
            store=_SidebarScanStore(),
            adapters={},
            target_adapters={},
            clock=lambda: 0.0,
            idle_chip_archiver=object(),
        )


@pytest.mark.asyncio
async def test_successful_scan_runs_idle_chip_archiver_off_loop_thread() -> None:
    from threading import get_ident

    from session_bridge.coordinator import SessionBridgeCoordinator
    from session_bridge.models import Provider
    from tests.session_bridge.test_coordinator import (
        _LifecycleClaudeAdapter,
        _sidebar_config,
        _SidebarScanStore,
    )

    event_loop_thread = get_ident()
    calls: list[int] = []

    class RecordingArchiver:
        def run_once(self) -> dict[str, int]:
            calls.append(get_ident())
            return {"examined": 0, "archived": 0, "skipped": 0, "throttled": 0}

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=_SidebarScanStore(),
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={},
        clock=lambda: 3_000_000.0,
        idle_chip_archiver=RecordingArchiver(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert len(calls) == 1
    assert calls[0] != event_loop_thread


def _serve_runtime_coordinator(db, monkeypatch, *, archive_idle_chips: bool):
    from dataclasses import replace

    from session_bridge.catalog import UnifiedCatalog
    from session_bridge.cli import ProductionBackend
    from session_bridge.config import BridgeConfig, ClaudeVisibilityConfig
    from session_bridge.models import Provider
    from session_bridge.store import SessionBridgeStore

    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    backend = ProductionBackend(
        replace(
            BridgeConfig(),
            claude_visibility=replace(
                ClaudeVisibilityConfig(),
                enabled=True,
                archive_idle_chips=archive_idle_chips,
            ),
        )
    )
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable", lambda name: (name,)
    )
    return backend._provider_runtime(
        targets=False, catalog_only=False, providers=(Provider.CLAUDE,)
    )


@pytest.fixture
def db(tmp_path):
    from hermes_state import SessionDB

    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def test_serve_runtime_wires_idle_chip_archiver_when_configured(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = Path("C:/sentinel-registry")
    monkeypatch.setattr(
        "session_bridge.cli.discover_ccd_registry_roots", lambda: (sentinel,)
    )
    coordinator = _serve_runtime_coordinator(db, monkeypatch, archive_idle_chips=True)
    archiver = coordinator._idle_chip_archiver
    assert isinstance(archiver, IdleChipArchiveWorker)
    assert archiver._registry_roots == (sentinel,)


def test_serve_runtime_omits_idle_chip_archiver_when_disabled(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _serve_runtime_coordinator(db, monkeypatch, archive_idle_chips=False)
    assert coordinator._idle_chip_archiver is None
