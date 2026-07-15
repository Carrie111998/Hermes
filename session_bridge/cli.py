"""Command-line control plane for the cross-harness session bridge."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Protocol

from agent.transports.codex_app_server import CodexAppServerClient
from hermes_state import SessionDB

from .catalog import UnifiedCatalog
from .characterize import (
    CharacterizationGateError,
    LiveCharacterizationError,
    resolve_characterization_gate,
    resolve_cli_executable,
    run_live_characterization,
)
from .claude_adapter import ClaudeSourceAdapter, ClaudeTargetAdapter
from .codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from .config import BridgeConfig
from .context_pack import ContextPackBuilder
from .coordinator import SessionBridgeCoordinator
from .mcp_server import create_app, resolve_bearer_token, resolve_marker_key
from .mirror import (
    BatchProgress,
    DiscoveryMode,
    EligibilityContext,
    MirrorPolicy,
    classify_mirror_eligibility,
    enqueue_mirror_job,
    should_halt_batch,
)
from .models import MirrorJobState, Provider, SidebarJobState
from .sidebar_skill import install_sidebar_skill
from .store import (
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_RETRYABLE_ERRORS,
    SessionBridgeStore,
)


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DEGRADED = 3
EXIT_ROLLOUT_GATE = 4
_MAX_BACKFILL_CREATE = 10
_BACKFILL_PAGE_SIZE = 1_000
_MAX_PLANNED_SESSIONS = 10_000
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "bearer",
    "context_pack",
    "credential",
    "marker_key",
    "native_path",
    "password",
    "payload",
    "secret",
    "source_cursor",
    "source_hash",
    "token",
)


def _production_codex_permission_preflight(cwd: str) -> bool:
    """Verify the production broker process can traverse the exact Codex cwd.

    Native task sandbox authorization is additionally proven by the rollout
    canary; this check is the fail-closed host-side gate available before
    handing a continuation back to Codex.
    """

    if type(cwd) is not str or not cwd or any(character in cwd for character in "\x00\r\n"):
        return False
    try:
        path = Path(cwd)
        if not path.is_absolute():
            return False
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.X_OK):
            return False
        with os.scandir(resolved) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


class _Backend(Protocol):
    def close(self) -> None: ...
    def serve(self) -> None: ...
    def scan(
        self, *, provider: str, all_history: bool, newest_first: bool
    ) -> Mapping[str, Any]: ...
    def status(self) -> Mapping[str, Any]: ...
    def sidebar_status(self) -> Mapping[str, Any]: ...
    def sidebar_backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> Mapping[str, Any]: ...
    def set_sidebar_continuous(self, *, enabled: bool) -> Mapping[str, Any]: ...
    def characterize(self, *, provider: str) -> Mapping[str, Any]: ...
    def characterization_status(self) -> str: ...
    def backfill_candidates(self, *, days: int) -> list[dict[str, Any]]: ...
    def apply_backfill(
        self, *, candidates: list[dict[str, Any]]
    ) -> Mapping[str, Any]: ...
    def mirror_preview(self, *, session_id: str, target: str) -> Mapping[str, Any]: ...
    def apply_mirror(self, *, session_id: str, target: str) -> Mapping[str, Any]: ...


class ConfigurationFailure(RuntimeError):
    """A fixed-code local configuration or authorization failure."""


class ProviderDegraded(RuntimeError):
    """A provider operation failed after local validation passed."""


class RolloutGateBlocked(RuntimeError):
    """A mutation was refused before its first durable write."""

    def __init__(self, gate: str) -> None:
        super().__init__(gate)
        self.gate = gate


class ProductionBackend:
    """Lazy production composition; tests inject a small fake backend."""

    def __init__(self, config: BridgeConfig) -> None:
        if not isinstance(config, BridgeConfig):
            raise TypeError("config must be a BridgeConfig")
        self.config = config
        self._db: SessionDB | None = None
        self._store: SessionBridgeStore | None = None
        self._catalog: UnifiedCatalog | None = None
        self._coordinator: SessionBridgeCoordinator | None = None
        self._codex_client: CodexAppServerClient | None = None

    def close(self) -> None:
        client, self._codex_client = self._codex_client, None
        if client is not None:
            client.close()
        db, self._db = self._db, None
        self._store = None
        self._catalog = None
        self._coordinator = None
        if db is not None:
            db.close()

    def serve(self) -> None:
        try:
            if self.config.mirrors.automatic_creation:
                try:
                    resolve_characterization_gate()
                except CharacterizationGateError as exc:
                    raise RolloutGateBlocked(f"characterization_{exc.code}") from exc
            coordinator = self._provider_runtime(
                targets=True,
                catalog_only=False,
                providers=(Provider.CLAUDE, Provider.CODEX),
            )
            catalog = self._require_catalog()
            store = self._require_store()
            token = resolve_bearer_token()
            app = create_app(
                catalog=catalog,
                coordinator=coordinator,
                store=store,
                config=self.config,
                token=token,
            )
            import uvicorn

            uvicorn.run(
                app,
                host=self.config.service.host,
                port=self.config.service.port,
                log_level="info",
            )
        except RolloutGateBlocked:
            raise
        except (OSError, PermissionError, ValueError) as exc:
            raise ConfigurationFailure("service_configuration_failed") from exc
        except RuntimeError as exc:
            if "token" in str(exc).casefold() or "marker" in str(exc).casefold():
                raise ConfigurationFailure("service_authorization_failed") from exc
            raise ProviderDegraded("service_start_failed") from exc

    def scan(
        self, *, provider: str, all_history: bool, newest_first: bool
    ) -> Mapping[str, Any]:
        if not all_history or not newest_first:
            raise ConfigurationFailure("unsupported_scan_mode")
        try:
            selected = None if provider == "all" else Provider(provider)
            if selected is None:
                summaries: list[dict[str, Any]] = []
                for candidate in (Provider.CLAUDE, Provider.CODEX):
                    try:
                        coordinator = self._provider_runtime(
                            targets=False,
                            catalog_only=True,
                            providers=(candidate,),
                        )
                        summaries.append(
                            asdict(asyncio.run(coordinator.scan_all_history(candidate)))
                        )
                    except ConfigurationFailure:
                        raise
                    except Exception:
                        summaries.append({
                            "provider": candidate.value,
                            "discovered": 0,
                            "indexed": 0,
                            "rebuilt": 0,
                            "failed": 1,
                            "duration_ms": 0,
                        })
                    finally:
                        self._release_provider_runtime()
                return {
                    "provider": None,
                    **{
                        field: sum(
                            float(summary.get(field, 0))
                            if field == "duration_ms"
                            else int(summary.get(field, 0))
                            for summary in summaries
                        )
                        for field in (
                            "discovered",
                            "indexed",
                            "rebuilt",
                            "failed",
                            "duration_ms",
                        )
                    },
                }
            coordinator = self._provider_runtime(
                targets=False,
                catalog_only=True,
                providers=(selected,),
            )
            summary = asyncio.run(coordinator.scan_all_history(selected))
            return asdict(summary)
        except ConfigurationFailure:
            raise
        except (OSError, PermissionError, ValueError) as exc:
            raise ConfigurationFailure("scan_configuration_failed") from exc
        except Exception as exc:
            raise ProviderDegraded("provider_scan_failed") from exc

    def status(self) -> Mapping[str, Any]:
        catalog = self._require_catalog()
        store = self._require_store()
        catalog_status = catalog.status()
        queue_counts = store.mirror_job_counts()
        breaker = store.get_mirror_breaker_progress()
        policy = self._policy()
        progress = BatchProgress(
            attempts=int(breaker.get("attempts", 0)),
            errors=int(breaker.get("errors", 0)),
        )
        degraded_catalog = any(
            int(value.get("degraded", 0)) > 0
            for value in catalog_status.get("providers", {}).values()
            if isinstance(value, Mapping)
        )
        manual_failures = int(queue_counts.get(MirrorJobState.MANUAL_FAILURE.value, 0))
        return {
            **catalog_status,
            "healthy": not degraded_catalog and manual_failures == 0,
            "mirror_mode": (
                "automatic" if self.config.mirrors.automatic_creation else "manual"
            ),
            "queue_counts": queue_counts,
            "rollout_breaker": {
                "attempts": progress.attempts,
                "errors": progress.errors,
                "halted": should_halt_batch(progress, policy),
            },
        }

    def sidebar_status(self) -> Mapping[str, Any]:
        status_time = time.time()
        raw = self._require_store().sidebar_delivery_status(now=status_time)
        return _public_sidebar_status(
            raw,
            now=status_time,
            grace_seconds=self.config.sidebar.heartbeat_grace_seconds,
        )

    def sidebar_backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> Mapping[str, Any]:
        coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self._require_store(),
            adapters={},
            target_adapters={},
            clock=time.time,
        )
        summary = asyncio.run(
            coordinator.backfill_sidebar_jobs_once(
                days=days,
                limit=limit,
                apply=apply,
            )
        )
        payload = asdict(summary)
        result = {
            "mode": "apply" if apply else "dry_run",
            "days": days,
            "limit": limit,
            **payload,
        }
        if not apply:
            result["would_queue"] = payload["queued"]
            result["queued"] = 0
        return result

    def set_sidebar_continuous(self, *, enabled: bool) -> Mapping[str, Any]:
        if type(enabled) is not bool:
            raise ConfigurationFailure("invalid_sidebar_continuous_mode")
        from hermes_cli.config import load_config, save_config

        document = load_config()
        if not isinstance(document, dict):
            raise ConfigurationFailure("invalid_hermes_config")
        session_bridge = document.get("session_bridge")
        if session_bridge is None:
            session_bridge = {}
            document["session_bridge"] = session_bridge
        if not isinstance(session_bridge, dict):
            raise ConfigurationFailure("invalid_session_bridge_config")
        sidebar = session_bridge.get("sidebar")
        if sidebar is None:
            sidebar = {}
            session_bridge["sidebar"] = sidebar
        if not isinstance(sidebar, dict):
            raise ConfigurationFailure("invalid_sidebar_config")
        sidebar["continuous"] = enabled
        save_config(
            document,
            preserve_keys={("session_bridge", "sidebar", "continuous")},
        )
        self.config = replace(
            self.config,
            sidebar=replace(self.config.sidebar, continuous=enabled),
        )
        return {
            "enabled": self.config.sidebar.enabled,
            "continuous": enabled,
        }

    def characterize(self, *, provider: str) -> Mapping[str, Any]:
        if provider != "all":
            raise ConfigurationFailure("characterization_requires_all_providers")
        try:
            with _temporary_environment("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1"):
                report_path = run_live_characterization(
                    claude_projects_root=_CLAUDE_PROJECTS_ROOT,
                )
            gate = resolve_characterization_gate()
        except LiveCharacterizationError as exc:
            raise ProviderDegraded("characterization_failed") from exc
        except CharacterizationGateError as exc:
            raise ProviderDegraded(f"characterization_{exc.code}") from exc
        except Exception as exc:
            raise ProviderDegraded("characterization_failed") from exc
        return {
            "passed": True,
            "report": report_path.name,
            "characterization_id": gate.characterization_id,
            "codex_registration_turn_required": (gate.codex_registration_turn_required),
        }

    def characterization_status(self) -> str:
        try:
            resolve_characterization_gate()
        except CharacterizationGateError as exc:
            return exc.code
        except Exception:
            return "invalid"
        return "passed"

    def backfill_candidates(self, *, days: int) -> list[dict[str, Any]]:
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ConfigurationFailure("invalid_backfill_days")
        store = self._require_store()
        now = time.time()
        after = now - days * 24 * 60 * 60
        policy = replace(self._policy(), backfill_days=days)
        planned: list[dict[str, Any]] = []
        catalog = self._require_catalog()
        cursor: tuple[float, str] | None = None
        examined = 0
        try:
            while True:
                remaining = _MAX_PLANNED_SESSIONS - examined
                if remaining <= 0:
                    raise ProviderDegraded("backfill_plan_truncated")
                projections = store.list_native_projections(
                    after=after,
                    limit=min(_BACKFILL_PAGE_SIZE, remaining),
                    cursor=cursor,
                )
                source_ids = [
                    f"{projection.provider.value}:{projection.native_id}"
                    for projection in projections
                ]
                mappings = store.list_existing_target_mappings(source_ids)
                context = EligibilityContext(
                    now=now,
                    discovery_mode=DiscoveryMode.INITIAL_BACKFILL,
                    continuous_watermark=None,
                    existing_target_mappings=frozenset(mappings),
                    policy=policy,
                )
                for projection in projections:
                    eligibility = classify_mirror_eligibility(projection, context)
                    if not eligibility.eligible:
                        continue
                    canonical_id = f"{projection.provider.value}:{projection.native_id}"
                    preview = catalog.mirror_preview(
                        canonical_id, eligibility.target_provider.value
                    )
                    if preview.get("would_enqueue") is not True:
                        continue
                    planned.append({
                        "canonical_id": canonical_id,
                        "provider": projection.provider.value,
                        "target_provider": eligibility.target_provider.value,
                        "last_active": float(projection.last_active),
                        "eligible": True,
                        "reason": eligibility.reason,
                    })
                examined += len(projections)
                if not projections.has_more:
                    break
                if projections.next_cursor is None:
                    raise ProviderDegraded("backfill_plan_cursor_missing")
                if examined >= _MAX_PLANNED_SESSIONS:
                    raise ProviderDegraded("backfill_plan_truncated")
                cursor = projections.next_cursor
        except ProviderDegraded:
            raise
        except Exception as exc:
            raise ProviderDegraded("backfill_plan_failed") from exc
        return _ordered_candidates(planned)

    def apply_backfill(self, *, candidates: list[dict[str, Any]]) -> Mapping[str, Any]:
        self._mutation_preflight()
        store = self._require_store()
        policy = self._policy()
        self._require_open_breaker(store, policy)
        totals = {
            "authorized": 0,
            "claimed": 0,
            "succeeded": 0,
            "retried": 0,
            "manual_failure": 0,
        }
        for candidate in _ordered_candidates(candidates):
            try:
                job = enqueue_mirror_job(
                    store,
                    candidate["canonical_id"],
                    Provider(candidate["target_provider"]),
                    policy=policy,
                    manual_authorized=True,
                    require_unmapped=True,
                    rollout_limited=True,
                )
            except PermissionError as exc:
                if totals["authorized"]:
                    return {
                        **totals,
                        "degraded": False,
                        "halted": True,
                        "partial": True,
                        "gate": "backfill_authority_revoked",
                    }
                raise RolloutGateBlocked("backfill_authority_revoked") from exc
            except (KeyError, TypeError, ValueError) as exc:
                if totals["authorized"]:
                    return {
                        **totals,
                        "degraded": False,
                        "halted": True,
                        "partial": True,
                        "gate": "backfill_candidate_invalid",
                    }
                raise RolloutGateBlocked("backfill_candidate_invalid") from exc
            totals["authorized"] += 1
            try:
                coordinator = self._provider_runtime(
                    targets=True,
                    catalog_only=False,
                    providers=(Provider.CLAUDE, Provider.CODEX),
                )
            except Exception:
                return {
                    **totals,
                    "degraded": True,
                    "halted": False,
                    "provider_available": False,
                }
            summary = asyncio.run(
                coordinator.process_jobs_once(job_ids=(job["id"],), limit=1)
            )
            for key in ("claimed", "succeeded", "retried", "manual_failure"):
                totals[key] += int(getattr(summary, key))
            if summary.claimed == 0 or summary.retried or summary.manual_failure:
                break
        progress = store.get_mirror_breaker_progress()
        halted = should_halt_batch(
            BatchProgress(
                attempts=int(progress.get("attempts", 0)),
                errors=int(progress.get("errors", 0)),
            ),
            policy,
        )
        return {
            **totals,
            "degraded": bool(totals["retried"] or totals["manual_failure"]),
            "halted": halted,
        }

    def mirror_preview(self, *, session_id: str, target: str) -> Mapping[str, Any]:
        try:
            return self._require_catalog().mirror_preview(session_id, target)
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateBlocked("mirror_invalid") from exc

    def apply_mirror(self, *, session_id: str, target: str) -> Mapping[str, Any]:
        self._mutation_preflight()
        store = self._require_store()
        policy = self._policy()
        self._require_open_breaker(store, policy)
        try:
            job = enqueue_mirror_job(
                store,
                session_id,
                Provider(target),
                policy=policy,
                manual_authorized=True,
                require_unmapped=True,
                rollout_limited=True,
            )
        except PermissionError as exc:
            raise RolloutGateBlocked("mirror_authority_revoked") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateBlocked("mirror_invalid") from exc
        try:
            coordinator = self._provider_runtime(
                targets=True,
                catalog_only=False,
                providers=(Provider.CLAUDE, Provider.CODEX),
            )
        except Exception:
            return {
                "session_id": session_id,
                "target_provider": target,
                "state": job.get("state"),
                "claimed": 0,
                "succeeded": 0,
                "retried": 0,
                "manual_failure": 0,
                "degraded": True,
                "provider_available": False,
            }
        summary = asyncio.run(
            coordinator.process_jobs_once(job_ids=(job["id"],), limit=1)
        )
        durable = next(
            (
                row
                for row in store.list_mirror_jobs(list(MirrorJobState), limit=1000)
                if row.get("id") == job["id"]
            ),
            job,
        )
        return {
            "session_id": session_id,
            "target_provider": target,
            "state": durable.get("state", job.get("state")),
            "claimed": summary.claimed,
            "succeeded": summary.succeeded,
            "retried": summary.retried,
            "manual_failure": summary.manual_failure,
            "degraded": bool(summary.retried or summary.manual_failure),
        }

    def _require_catalog(self) -> UnifiedCatalog:
        if self._catalog is None:
            self._db = SessionDB()
            self._store = SessionBridgeStore(self._db)
            self._catalog = UnifiedCatalog(self._db, self._store)
        return self._catalog

    def _require_store(self) -> SessionBridgeStore:
        self._require_catalog()
        assert self._store is not None
        return self._store

    def _provider_runtime(
        self,
        *,
        targets: bool,
        catalog_only: bool,
        providers: Sequence[Provider],
    ) -> SessionBridgeCoordinator:
        if self._coordinator is not None:
            return self._coordinator
        selected = tuple(dict.fromkeys(Provider(provider) for provider in providers))
        if not selected or any(
            provider not in (Provider.CLAUDE, Provider.CODEX) for provider in selected
        ):
            raise ConfigurationFailure("provider_selection_invalid")
        try:
            try:
                marker_key = resolve_marker_key()
            except (OSError, PermissionError, RuntimeError, ValueError) as exc:
                raise ConfigurationFailure("marker_key_unavailable") from exc
            source_adapters: dict[Provider, object] = {}
            claude_source: ClaudeSourceAdapter | None = None
            codex_source: CodexSourceAdapter | None = None
            if Provider.CLAUDE in selected:
                claude_source = ClaudeSourceAdapter(
                    _CLAUDE_PROJECTS_ROOT, marker_secret=marker_key
                )
                source_adapters[Provider.CLAUDE] = claude_source
            if Provider.CODEX in selected:
                codex_command = resolve_cli_executable("codex")
                if len(codex_command) != 1:
                    raise RuntimeError("codex_direct_runtime_required")
                self._codex_client = CodexAppServerClient(codex_bin=codex_command[0])
                codex_source = CodexSourceAdapter(
                    self._codex_client, marker_secret=marker_key
                )
                source_adapters[Provider.CODEX] = codex_source
            target_adapters: dict[Provider, object] = {}
            if targets:
                if claude_source is not None:
                    target_adapters[Provider.CLAUDE] = ClaudeTargetAdapter(
                        claude_source, marker_secret=marker_key
                    )
                if codex_source is not None and self._codex_client is not None:
                    target_adapters[Provider.CODEX] = CodexTargetAdapter(
                        self._codex_client,
                        source_adapter=codex_source,
                        marker_secret=marker_key,
                    )
            effective_config = self.config
            if catalog_only:
                effective_config = replace(
                    self.config,
                    mirrors=replace(
                        self.config.mirrors,
                        automatic_creation=False,
                    ),
                )
            catalog = self._require_catalog()
            self._coordinator = SessionBridgeCoordinator(
                config=effective_config,
                store=self._require_store(),
                adapters=source_adapters,
                target_adapters=target_adapters,
                context_builder=(
                    ContextPackBuilder(catalog.db, catalog.store) if targets else None
                ),
                claude_projects_root=(
                    _CLAUDE_PROJECTS_ROOT if Provider.CLAUDE in selected else None
                ),
                permission_preflight=_production_codex_permission_preflight,
            )
            return self._coordinator
        except Exception:
            self.close()
            raise

    def _release_provider_runtime(self) -> None:
        client, self._codex_client = self._codex_client, None
        self._coordinator = None
        if client is not None:
            client.close()

    def _policy(self) -> MirrorPolicy:
        mirrors = self.config.mirrors
        return MirrorPolicy(
            automatic_creation=mirrors.automatic_creation,
            backfill_days=mirrors.backfill_days,
            creates_per_minute=mirrors.creates_per_minute,
            max_attempts=mirrors.max_attempts,
            stop_after_attempts=mirrors.stop_after_attempts,
            stop_error_rate=mirrors.stop_error_rate,
        )

    def _mutation_preflight(self) -> None:
        try:
            resolve_marker_key()
            codex_command = resolve_cli_executable("codex")
            claude_command = resolve_cli_executable("claude")
        except (OSError, PermissionError, RuntimeError, ValueError) as exc:
            raise ConfigurationFailure("mutation_preflight_failed") from exc
        if len(codex_command) != 1 or not claude_command:
            raise ConfigurationFailure("mutation_preflight_failed")

    @staticmethod
    def _require_open_breaker(store: SessionBridgeStore, policy: MirrorPolicy) -> None:
        progress = store.get_mirror_breaker_progress()
        attempts = int(progress.get("attempts", 0))
        errors = int(progress.get("errors", 0))
        if errors > 0 and attempts > 0 and errors / attempts >= policy.stop_error_rate:
            raise RolloutGateBlocked("rollout_breaker_halted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-session-bridge",
        description="Unified Claude Code and Codex session catalog control plane.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "install-sidebar-skill",
        help="install the personal Codex sidebar delivery skill",
    )
    commands.add_parser("serve", help="serve the authenticated loopback MCP")

    scan = commands.add_parser("scan", help="import provider history into the catalog")
    scan.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    scan.add_argument(
        "--all-history",
        action="store_true",
        default=True,
        help="force a catalog-only full-history scan (default)",
    )

    status = commands.add_parser("status", help="show sanitized local bridge status")
    status.add_argument("--json", action="store_true")

    sidebar_status = commands.add_parser(
        "sidebar-status",
        help="show sanitized native sidebar delivery status",
    )
    sidebar_status.add_argument("--json", action="store_true")

    sidebar_backfill = commands.add_parser(
        "sidebar-backfill",
        help="preview or enqueue a bounded recent native sidebar batch",
    )
    sidebar_backfill.add_argument("--days", type=_bounded_sidebar_days, default=30)
    sidebar_backfill.add_argument("--limit", type=_bounded_sidebar_limit, default=10)
    sidebar_backfill_mode = sidebar_backfill.add_mutually_exclusive_group(required=True)
    sidebar_backfill_mode.add_argument("--dry-run", action="store_true")
    sidebar_backfill_mode.add_argument("--apply", action="store_true")

    sidebar_continuous = commands.add_parser(
        "sidebar-continuous",
        help="persist the native sidebar continuous registration mode",
    )
    sidebar_continuous_mode = sidebar_continuous.add_mutually_exclusive_group(
        required=True
    )
    sidebar_continuous_mode.add_argument("--enable", action="store_true")
    sidebar_continuous_mode.add_argument("--disable", action="store_true")

    characterize = commands.add_parser(
        "characterize", help="run the disposable live provider gate"
    )
    characterize.add_argument("--provider", choices=("all",), default="all")

    backfill = commands.add_parser(
        "backfill", help="plan or apply a bounded recent mirror batch"
    )
    backfill.add_argument("--days", type=_positive_int, default=30)
    backfill_mode = backfill.add_mutually_exclusive_group(required=True)
    backfill_mode.add_argument("--dry-run", action="store_true")
    backfill_mode.add_argument("--apply", action="store_true")
    backfill.add_argument(
        "--max-create", type=_bounded_create_count, default=_MAX_BACKFILL_CREATE
    )
    backfill.add_argument("--confirm-one-shot", action="store_true")

    mirror = commands.add_parser("mirror", help="plan or apply one native mirror")
    mirror.add_argument("session_id")
    mirror.add_argument("--target", choices=("claude", "codex"), required=True)
    mirror_mode = mirror.add_mutually_exclusive_group(required=True)
    mirror_mode.add_argument("--dry-run", action="store_true")
    mirror_mode.add_argument("--apply", action="store_true")
    mirror.add_argument("--confirm-one-shot", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], BridgeConfig] = BridgeConfig.load,
    backend_factory: Callable[[BridgeConfig], _Backend] = ProductionBackend,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install-sidebar-skill":
        try:
            installed = install_sidebar_skill()
        except Exception:
            _emit({"error": "configuration_error"})
            return EXIT_CONFIG
        _emit({"status": "installed", "path": str(installed)})
        return EXIT_OK
    try:
        config = config_loader()
        if not isinstance(config, BridgeConfig):
            raise TypeError("config loader did not return BridgeConfig")
        backend = backend_factory(config)
    except Exception:
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG

    try:
        if args.command == "serve":
            backend.serve()
            _emit({"status": "stopped"})
            return EXIT_OK
        if args.command == "scan":
            payload = dict(
                backend.scan(
                    provider=args.provider,
                    all_history=bool(args.all_history),
                    newest_first=True,
                )
            )
            _emit(payload)
            return EXIT_DEGRADED if int(payload.get("failed", 0)) else EXIT_OK
        if args.command == "status":
            payload = dict(backend.status())
            _emit(payload)
            return EXIT_OK if payload.get("healthy", True) is True else EXIT_DEGRADED
        if args.command == "sidebar-status":
            payload = dict(backend.sidebar_status())
            _emit(payload)
            return EXIT_OK if payload.get("healthy") is True else EXIT_DEGRADED
        if args.command == "sidebar-backfill":
            payload = dict(
                backend.sidebar_backfill(
                    days=args.days,
                    limit=args.limit,
                    apply=bool(args.apply),
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-continuous":
            payload = dict(
                backend.set_sidebar_continuous(enabled=bool(args.enable))
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "characterize":
            _emit(dict(backend.characterize(provider=args.provider)))
            return EXIT_OK
        if args.command == "backfill":
            return _backfill_command(args, config=config, backend=backend)
        if args.command == "mirror":
            return _mirror_command(args, config=config, backend=backend)
        raise ConfigurationFailure("unknown_command")
    except RolloutGateBlocked as exc:
        _emit({"error": "rollout_gate_blocked", "gate": exc.gate})
        return EXIT_ROLLOUT_GATE
    except ConfigurationFailure:
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG
    except ProviderDegraded:
        _emit({"error": "provider_degraded"})
        return EXIT_DEGRADED
    except (OSError, PermissionError, TypeError, ValueError):
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG
    except Exception:
        _emit({"error": "provider_degraded"})
        return EXIT_DEGRADED
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _backfill_command(
    args: argparse.Namespace,
    *,
    config: BridgeConfig,
    backend: _Backend,
) -> int:
    if args.confirm_one_shot and not args.apply:
        raise ConfigurationFailure("confirmation_requires_apply")
    if args.apply:
        _require_mutation_gate(
            config=config,
            backend=backend,
            confirmed=bool(args.confirm_one_shot),
        )
    candidates = _ordered_candidates(backend.backfill_candidates(days=args.days))
    if args.dry_run:
        _emit({
            "mode": "dry_run",
            "days": args.days,
            "count": len(candidates),
            "candidates": [_public_candidate(item) for item in candidates],
        })
        return EXIT_OK
    cap = min(
        int(args.max_create),
        config.mirrors.creates_per_minute,
        config.mirrors.stop_after_attempts,
    )
    payload = dict(backend.apply_backfill(candidates=candidates[:cap]))
    _emit(payload)
    if isinstance(payload.get("gate"), str):
        return EXIT_ROLLOUT_GATE
    if payload.get("degraded") is True:
        return EXIT_DEGRADED
    if payload.get("halted") is True and int(payload.get("claimed", 0)) == 0:
        return EXIT_ROLLOUT_GATE
    return EXIT_OK


def _mirror_command(
    args: argparse.Namespace,
    *,
    config: BridgeConfig,
    backend: _Backend,
) -> int:
    if args.confirm_one_shot and not args.apply:
        raise ConfigurationFailure("confirmation_requires_apply")
    preview = dict(
        backend.mirror_preview(session_id=args.session_id, target=args.target)
    )
    public_preview = _public_preview(preview)
    if args.dry_run:
        _emit({"mode": "dry_run", **public_preview})
        return EXIT_OK
    _require_mutation_gate(
        config=config,
        backend=backend,
        confirmed=bool(args.confirm_one_shot),
    )
    if preview.get("would_enqueue") is not True:
        reason = str(preview.get("reason") or "ineligible")
        raise RolloutGateBlocked(f"mirror_{reason}")
    payload = dict(backend.apply_mirror(session_id=args.session_id, target=args.target))
    _emit(payload)
    return EXIT_DEGRADED if payload.get("degraded") is True else EXIT_OK


def _require_mutation_gate(
    *, config: BridgeConfig, backend: _Backend, confirmed: bool
) -> None:
    if not config.catalog.enabled:
        raise RolloutGateBlocked("catalog_disabled")
    characterization = backend.characterization_status()
    if characterization != "passed":
        raise RolloutGateBlocked(f"characterization_{characterization}")
    if not config.mirrors.automatic_creation and not confirmed:
        raise RolloutGateBlocked("one_shot_confirmation_required")


def _ordered_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [dict(candidate) for candidate in candidates]
    return sorted(
        normalized,
        key=lambda item: (
            -float(item.get("last_active", 0.0)),
            str(item.get("canonical_id", "")),
            str(item.get("target_provider", "")),
        ),
    )


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "canonical_id",
            "provider",
            "target_provider",
            "last_active",
            "eligible",
            "reason",
        )
        if key in candidate
    }


def _public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: preview[key]
        for key in (
            "session_id",
            "target_provider",
            "would_enqueue",
            "reason",
            "job_state",
        )
        if key in preview
    }


def _public_sidebar_status(
    raw: Mapping[str, Any],
    *,
    now: float,
    grace_seconds: int,
) -> dict[str, Any]:
    status_time = _finite_status_number(now)
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise ConfigurationFailure("invalid_sidebar_heartbeat_grace")
    raw_counts = raw.get("counts")
    counts = raw_counts if isinstance(raw_counts, Mapping) else {}
    state_counts = {
        state.value: _status_count(
            counts.get(state.value, counts.get(state.name.casefold(), 0))
        )
        for state in SidebarJobState
    }
    raw_providers = raw.get("eligible_by_provider")
    providers = raw_providers if isinstance(raw_providers, Mapping) else {}
    eligible_by_provider = {
        Provider.CLAUDE.value: _status_count(providers.get(Provider.CLAUDE.value, 0)),
        Provider.HERMES.value: _status_count(providers.get(Provider.HERMES.value, 0)),
    }
    oldest_age = _optional_status_number(raw.get("oldest_pending_age_seconds"))
    heartbeat_at = _optional_status_number(raw.get("last_heartbeat_at"))
    heartbeat_age = (
        max(0.0, status_time - heartbeat_at) if heartbeat_at is not None else None
    )
    threshold = 60 + grace_seconds
    work_pending = sum(
        state_counts[state.value]
        for state in (
            SidebarJobState.PENDING,
            SidebarJobState.LEASED,
            SidebarJobState.RETRY,
        )
    ) > 0
    degraded_reasons: list[str] = []
    heartbeat_stale = (
        heartbeat_age > threshold
        if heartbeat_age is not None
        else oldest_age is not None and oldest_age > threshold
    )
    if work_pending and heartbeat_stale:
        degraded_reasons.append("broker_heartbeat_stale")
    if work_pending and oldest_age is not None and oldest_age > threshold:
        degraded_reasons.append("oldest_pending_stale")
    raw_codes = raw.get("recent_error_codes")
    allowed_codes = SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
    recent_codes = (
        [
            code
            for code in raw_codes
            if isinstance(code, str) and code in allowed_codes
        ][:10]
        if isinstance(raw_codes, list)
        else []
    )
    raw_latency = raw.get("delivery_latency_seconds")
    latency = raw_latency if isinstance(raw_latency, Mapping) else {}
    task_id = raw.get("last_visible_task_id")
    return {
        "healthy": not degraded_reasons,
        "degraded_reasons": degraded_reasons,
        "eligible_by_provider": eligible_by_provider,
        "counts": state_counts,
        "oldest_pending_age_seconds": oldest_age,
        "last_heartbeat_at": heartbeat_at,
        "last_successful_heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age,
        "last_visible_task_id": _redacted_task_id(task_id),
        "recent_error_codes": recent_codes,
        "delivery_latency_seconds": {
            percentile: _optional_status_number(latency.get(percentile))
            for percentile in ("p50", "p95", "p99")
        },
    }


def _status_count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ConfigurationFailure("invalid_sidebar_status")
    return value


def _finite_status_number(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    return float(value)


def _optional_status_number(value: object) -> float | None:
    if value is None:
        return None
    result = _finite_status_number(value)
    if result < 0:
        raise ConfigurationFailure("invalid_sidebar_status")
    return result


def _redacted_task_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 12:
        return "[REDACTED]"
    return f"{value[:8]}...{value[-4:]}"


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(
        fragment in key.casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS
    ):
        return None
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitized
            for item_key, item_value in value.items()
            if (sanitized := _sanitize(item_value, key=str(item_key))) is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            sanitized for item in value if (sanitized := _sanitize(item)) is not None
        ]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _emit(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            _sanitize(dict(payload)),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _bounded_create_count(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > _MAX_BACKFILL_CREATE:
        raise argparse.ArgumentTypeError(
            f"value must be at most {_MAX_BACKFILL_CREATE}"
        )
    return parsed


def _bounded_sidebar_days(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 30:
        raise argparse.ArgumentTypeError("value must be at most 30")
    return parsed


def _bounded_sidebar_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError("value must be at most 10")
    return parsed


@contextmanager
def _temporary_environment(name: str, value: str):
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
