from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import ipaddress
from pathlib import Path
from typing import Any, TypeVar

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_ENV_PREFIX = "HERMES_SESSION_BRIDGE_"
_ENV_NAMES = frozenset({
    f"{_ENV_PREFIX}{suffix}"
    for suffix in (
        "ALLOW_NON_LOOPBACK",
        "AUTOMATIC_CREATION",
        "BACKFILL_DAYS",
        "CATALOG_ENABLED",
        "CATALOG_SCAN_SECONDS",
        "CREATES_PER_MINUTE",
        "HOST",
        "INCLUDE_ARCHIVED_CODEX",
        "MAX_ATTEMPTS",
        "PORT",
        "RECONCILE_SECONDS",
        "STOP_AFTER_ATTEMPTS",
        "STOP_ERROR_RATE",
        "TOKEN",
    )
})
_LIVE_CHARACTERIZATION_ENV_NAMES = frozenset({
    f"{_ENV_PREFIX}LIVE_TESTS",
})
# A 1,091-character canonical Windows structural envelope: 260-character cwd,
# 120-character title, required headings, and filesystem-safety instructions.
# 1,280 is the next 256-character allocation boundary, preserving a fixed
# 189-character margin without allowing a partial preview fragment.
MIN_READABLE_PREVIEW_BUDGET_CHARS = 1280
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 7484
    catalog_scan_seconds: float = 3
    reconcile_seconds: float = 30
    allow_non_loopback: bool = False


@dataclass(frozen=True)
class CatalogConfig:
    enabled: bool = True
    include_archived_codex: bool = True


@dataclass(frozen=True)
class MirrorsConfig:
    automatic_creation: bool = False
    backfill_days: int = 30
    creates_per_minute: int = 6
    max_attempts: int = 5
    stop_after_attempts: int = 20
    stop_error_rate: float = 0.25


@dataclass(frozen=True)
class SidebarConfig:
    inbox_cwd: str | None = None
    placement_generation: int = 1
    enabled: bool = False
    continuous: bool = False
    delivery_mode: str = "desktop_broker"
    broker_thread_id: str | None = None
    broker_project_id: str | None = None
    broker_cwd: str | None = None
    backfill_days: int = 30
    continuous_batch_limit: int = 5
    manual_batch_limit: int = 10
    lease_seconds: int = 300
    max_attempts: int = 5
    heartbeat_interval_seconds: int = 60
    heartbeat_grace_seconds: int = 120
    oldest_job_alert_seconds: int = 300
    readable_preview_enabled: bool = True
    legacy_hydration_enabled: bool = False
    preview_budget_chars: int = 24_000

    @property
    def heartbeat_stale_seconds(self) -> int:
        return self.heartbeat_interval_seconds + self.heartbeat_grace_seconds


@dataclass(frozen=True)
class ClaudeVisibilityConfig:
    enabled: bool = False
    continuous: bool = False
    backfill_days: int = 30
    continuous_batch_limit: int = 1
    manual_batch_limit: int = 10
    lease_seconds: int = 300
    max_attempts: int = 5
    daily_registration_limit: int = 25
    reserved_cost_per_attempt_usd: Decimal = Decimal("0.02")
    emergency_daily_cost_usd: Decimal = Decimal("0.50")
    process_timeout_seconds: int = 120
    discovery_timeout_seconds: int = 30
    float_activity: bool = False
    archive_idle_chips: bool = False
    reconcile_desktop_registries: bool = False
    idle_chip_archive_seconds: int = 86_400


@dataclass(frozen=True)
class BridgeConfig:
    service: ServiceConfig = ServiceConfig()
    catalog: CatalogConfig = CatalogConfig()
    mirrors: MirrorsConfig = MirrorsConfig()
    sidebar: SidebarConfig = SidebarConfig()
    claude_visibility: ClaudeVisibilityConfig = ClaudeVisibilityConfig()

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        config_home: Path | None = None,
    ) -> BridgeConfig:
        scope_token = (
            set_hermes_home_override(config_home) if config_home is not None else None
        )
        try:
            return cls._load(path=path, environ=environ)
        finally:
            if scope_token is not None:
                reset_hermes_home_override(scope_token)

    @classmethod
    def _load(
        cls,
        *,
        path: Path | None,
        environ: Mapping[str, str] | None,
    ) -> BridgeConfig:
        config_path = (
            path if path is not None else get_hermes_home() / "session_bridge.toml"
        )
        environment = os.environ if environ is None else environ
        unknown_environment = sorted(
            name
            for name in environment
            if name.startswith(_ENV_PREFIX)
            and name not in _ENV_NAMES
            and name not in _LIVE_CHARACTERIZATION_ENV_NAMES
        )
        if unknown_environment:
            raise ValueError(
                f"unknown session bridge environment variable: {unknown_environment[0]}"
            )
        document = _load_document(config_path)
        from hermes_cli.config import load_config

        yaml_document = load_config()
        if not isinstance(yaml_document, Mapping):
            raise ValueError("config.yaml root must be a mapping")
        session_bridge = _mapping_section(yaml_document, "session_bridge")
        sidebar = _mapping_section(session_bridge, "sidebar")
        claude_visibility = _mapping_section(session_bridge, "claude_visibility")
        _reject_unknown_keys(
            sidebar,
            allowed=frozenset({
                "enabled",
                "continuous",
                "backfill_days",
                "continuous_batch_limit",
                "manual_batch_limit",
                "lease_seconds",
                "max_attempts",
                "heartbeat_grace_seconds",
                "readable_preview_enabled",
                "legacy_hydration_enabled",
                "preview_budget_chars",
                "inbox_cwd",
                "placement_generation",
                "delivery_mode",
                "broker_thread_id",
                "broker_project_id",
                "broker_cwd",
                "heartbeat_interval_seconds",
                "oldest_job_alert_seconds",
            }),
            scope="session_bridge.sidebar",
        )
        _reject_unknown_keys(
            claude_visibility,
            allowed=frozenset({
                "enabled",
                "continuous",
                "backfill_days",
                "continuous_batch_limit",
                "manual_batch_limit",
                "lease_seconds",
                "max_attempts",
                "daily_registration_limit",
                "reserved_cost_per_attempt_usd",
                "emergency_daily_cost_usd",
                "process_timeout_seconds",
                "discovery_timeout_seconds",
                "float_activity",
                "archive_idle_chips",
                "reconcile_desktop_registries",
                "idle_chip_archive_seconds",
            }),
            scope="session_bridge.claude_visibility",
        )

        _reject_unknown_keys(
            document,
            allowed=frozenset({"service", "catalog", "mirrors"}),
            scope="root",
        )
        service = _section(document, "service")
        catalog = _section(document, "catalog")
        mirrors = _section(document, "mirrors")
        _reject_unknown_keys(
            service,
            allowed=frozenset({
                "host",
                "port",
                "catalog_scan_seconds",
                "reconcile_seconds",
                "allow_non_loopback",
            }),
            scope="service",
        )
        _reject_unknown_keys(
            catalog,
            allowed=frozenset({"enabled", "include_archived_codex"}),
            scope="catalog",
        )
        _reject_unknown_keys(
            mirrors,
            allowed=frozenset({
                "automatic_creation",
                "backfill_days",
                "creates_per_minute",
                "max_attempts",
                "stop_after_attempts",
                "stop_error_rate",
            }),
            scope="mirrors",
        )

        permission_env = f"{_ENV_PREFIX}ALLOW_NON_LOOPBACK"
        if permission_env in environment:
            raise ValueError(
                "non-loopback permission may only be set explicitly in TOML"
            )

        defaults = cls()
        allow_non_loopback = _toml_bool(
            service.get("allow_non_loopback", defaults.service.allow_non_loopback),
            "service.allow_non_loopback",
        )
        host = _env_or_toml(
            environment,
            "HOST",
            service.get("host", defaults.service.host),
            lambda value: _host(value, "service.host"),
            lambda value: _host(value, "service.host"),
        )
        if not _is_loopback_host(host) and not allow_non_loopback:
            raise ValueError(
                "service.host is non-loopback; set service.allow_non_loopback = true "
                "explicitly in TOML to permit it"
            )

        service_config = ServiceConfig(
            host=host,
            port=_env_or_toml(
                environment,
                "PORT",
                service.get("port", defaults.service.port),
                lambda value: _toml_int(
                    value, "service.port", minimum=1, maximum=65535
                ),
                lambda value: _env_int(value, "service.port", minimum=1, maximum=65535),
            ),
            catalog_scan_seconds=_env_or_toml(
                environment,
                "CATALOG_SCAN_SECONDS",
                service.get(
                    "catalog_scan_seconds", defaults.service.catalog_scan_seconds
                ),
                lambda value: _toml_float(
                    value,
                    "service.catalog_scan_seconds",
                    minimum=0.0,
                    exclusive_minimum=True,
                ),
                lambda value: _env_float(
                    value,
                    "service.catalog_scan_seconds",
                    minimum=0.0,
                    exclusive_minimum=True,
                ),
            ),
            reconcile_seconds=_env_or_toml(
                environment,
                "RECONCILE_SECONDS",
                service.get("reconcile_seconds", defaults.service.reconcile_seconds),
                lambda value: _toml_float(
                    value,
                    "service.reconcile_seconds",
                    minimum=0.0,
                    exclusive_minimum=True,
                ),
                lambda value: _env_float(
                    value,
                    "service.reconcile_seconds",
                    minimum=0.0,
                    exclusive_minimum=True,
                ),
            ),
            allow_non_loopback=allow_non_loopback,
        )
        catalog_config = CatalogConfig(
            enabled=_env_or_toml(
                environment,
                "CATALOG_ENABLED",
                catalog.get("enabled", defaults.catalog.enabled),
                lambda value: _toml_bool(value, "catalog.enabled"),
                lambda value: _env_bool(value, "catalog.enabled"),
            ),
            include_archived_codex=_env_or_toml(
                environment,
                "INCLUDE_ARCHIVED_CODEX",
                catalog.get(
                    "include_archived_codex",
                    defaults.catalog.include_archived_codex,
                ),
                lambda value: _toml_bool(value, "catalog.include_archived_codex"),
                lambda value: _env_bool(value, "catalog.include_archived_codex"),
            ),
        )
        mirrors_config = MirrorsConfig(
            automatic_creation=_env_or_toml(
                environment,
                "AUTOMATIC_CREATION",
                mirrors.get("automatic_creation", defaults.mirrors.automatic_creation),
                lambda value: _toml_bool(value, "mirrors.automatic_creation"),
                lambda value: _env_bool(value, "mirrors.automatic_creation"),
            ),
            backfill_days=_env_or_toml(
                environment,
                "BACKFILL_DAYS",
                mirrors.get("backfill_days", defaults.mirrors.backfill_days),
                lambda value: _toml_int(value, "mirrors.backfill_days", minimum=0),
                lambda value: _env_int(value, "mirrors.backfill_days", minimum=0),
            ),
            creates_per_minute=_env_or_toml(
                environment,
                "CREATES_PER_MINUTE",
                mirrors.get("creates_per_minute", defaults.mirrors.creates_per_minute),
                lambda value: _toml_int(value, "mirrors.creates_per_minute", minimum=1),
                lambda value: _env_int(value, "mirrors.creates_per_minute", minimum=1),
            ),
            max_attempts=_env_or_toml(
                environment,
                "MAX_ATTEMPTS",
                mirrors.get("max_attempts", defaults.mirrors.max_attempts),
                lambda value: _toml_int(value, "mirrors.max_attempts", minimum=1),
                lambda value: _env_int(value, "mirrors.max_attempts", minimum=1),
            ),
            stop_after_attempts=_env_or_toml(
                environment,
                "STOP_AFTER_ATTEMPTS",
                mirrors.get(
                    "stop_after_attempts", defaults.mirrors.stop_after_attempts
                ),
                lambda value: _toml_int(
                    value, "mirrors.stop_after_attempts", minimum=1
                ),
                lambda value: _env_int(value, "mirrors.stop_after_attempts", minimum=1),
            ),
            stop_error_rate=_env_or_toml(
                environment,
                "STOP_ERROR_RATE",
                mirrors.get("stop_error_rate", defaults.mirrors.stop_error_rate),
                lambda value: _toml_float(
                    value, "mirrors.stop_error_rate", minimum=0.0, maximum=1.0
                ),
                lambda value: _env_float(
                    value, "mirrors.stop_error_rate", minimum=0.0, maximum=1.0
                ),
            ),
        )
        sidebar_defaults = cls().sidebar
        inbox_cwd = _canonical_sidebar_string(
            sidebar.get("inbox_cwd", sidebar_defaults.inbox_cwd),
            "session_bridge.sidebar.inbox_cwd",
            allow_none=True,
        )
        placement_generation = _toml_int(
            sidebar.get(
                "placement_generation", sidebar_defaults.placement_generation
            ),
            "session_bridge.sidebar.placement_generation",
        )
        if placement_generation != 1:
            raise ValueError(
                "session_bridge.sidebar.placement_generation must be exactly 1"
            )
        lease_seconds = _toml_int(
            sidebar.get("lease_seconds", sidebar_defaults.lease_seconds),
            "session_bridge.sidebar.lease_seconds",
        )
        if lease_seconds != 300:
            raise ValueError("session_bridge.sidebar.lease_seconds must be exactly 300")
        max_attempts = _toml_int(
            sidebar.get("max_attempts", sidebar_defaults.max_attempts),
            "session_bridge.sidebar.max_attempts",
        )
        if max_attempts != 5:
            raise ValueError("session_bridge.sidebar.max_attempts must be exactly 5")
        delivery_mode = _canonical_sidebar_string(
            sidebar.get("delivery_mode", sidebar_defaults.delivery_mode),
            "session_bridge.sidebar.delivery_mode",
        )
        if delivery_mode != "desktop_broker":
            raise ValueError(
                "session_bridge.sidebar.delivery_mode must be exactly desktop_broker"
            )
        broker_thread_id = _canonical_sidebar_string(
            sidebar.get("broker_thread_id", sidebar_defaults.broker_thread_id),
            "session_bridge.sidebar.broker_thread_id",
            allow_none=True,
        )
        broker_project_id = _canonical_sidebar_string(
            sidebar.get("broker_project_id", sidebar_defaults.broker_project_id),
            "session_bridge.sidebar.broker_project_id",
            allow_none=True,
        )
        broker_cwd = _canonical_sidebar_string(
            sidebar.get("broker_cwd", sidebar_defaults.broker_cwd),
            "session_bridge.sidebar.broker_cwd",
            allow_none=True,
        )
        heartbeat_interval_seconds = _toml_int(
            sidebar.get(
                "heartbeat_interval_seconds",
                sidebar_defaults.heartbeat_interval_seconds,
            ),
            "session_bridge.sidebar.heartbeat_interval_seconds",
        )
        if heartbeat_interval_seconds != 60:
            raise ValueError(
                "session_bridge.sidebar.heartbeat_interval_seconds must be exactly 60"
            )
        oldest_job_alert_seconds = _toml_int(
            sidebar.get(
                "oldest_job_alert_seconds",
                sidebar_defaults.oldest_job_alert_seconds,
            ),
            "session_bridge.sidebar.oldest_job_alert_seconds",
        )
        if oldest_job_alert_seconds != 300:
            raise ValueError(
                "session_bridge.sidebar.oldest_job_alert_seconds must be exactly 300"
            )
        sidebar_config = SidebarConfig(
            inbox_cwd=inbox_cwd,
            placement_generation=placement_generation,
            enabled=_toml_bool(
                sidebar.get("enabled", sidebar_defaults.enabled),
                "session_bridge.sidebar.enabled",
            ),
            continuous=_toml_bool(
                sidebar.get("continuous", sidebar_defaults.continuous),
                "session_bridge.sidebar.continuous",
            ),
            delivery_mode=delivery_mode,
            broker_thread_id=broker_thread_id,
            broker_project_id=broker_project_id,
            broker_cwd=broker_cwd,
            backfill_days=_toml_int(
                sidebar.get("backfill_days", sidebar_defaults.backfill_days),
                "session_bridge.sidebar.backfill_days",
                minimum=0,
            ),
            continuous_batch_limit=_toml_int(
                sidebar.get(
                    "continuous_batch_limit",
                    sidebar_defaults.continuous_batch_limit,
                ),
                "session_bridge.sidebar.continuous_batch_limit",
                minimum=1,
                maximum=10,
            ),
            manual_batch_limit=_toml_int(
                sidebar.get(
                    "manual_batch_limit",
                    sidebar_defaults.manual_batch_limit,
                ),
                "session_bridge.sidebar.manual_batch_limit",
                minimum=1,
                maximum=10,
            ),
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_grace_seconds=_toml_int(
                sidebar.get(
                    "heartbeat_grace_seconds",
                    sidebar_defaults.heartbeat_grace_seconds,
                ),
                "session_bridge.sidebar.heartbeat_grace_seconds",
                minimum=0,
            ),
            oldest_job_alert_seconds=oldest_job_alert_seconds,
            readable_preview_enabled=_toml_bool(
                sidebar.get(
                    "readable_preview_enabled",
                    sidebar_defaults.readable_preview_enabled,
                ),
                "session_bridge.sidebar.readable_preview_enabled",
            ),
            legacy_hydration_enabled=_toml_bool(
                sidebar.get(
                    "legacy_hydration_enabled",
                    sidebar_defaults.legacy_hydration_enabled,
                ),
                "session_bridge.sidebar.legacy_hydration_enabled",
            ),
            preview_budget_chars=_toml_int(
                sidebar.get(
                    "preview_budget_chars",
                    sidebar_defaults.preview_budget_chars,
                ),
                "session_bridge.sidebar.preview_budget_chars",
                minimum=MIN_READABLE_PREVIEW_BUDGET_CHARS,
                maximum=100_000,
            ),
        )
        if sidebar_config.enabled and sidebar_config.continuous:
            if not all(
                (
                    sidebar_config.inbox_cwd,
                    sidebar_config.broker_thread_id,
                    sidebar_config.broker_project_id,
                    sidebar_config.broker_cwd,
                )
            ):
                raise ValueError("desktop broker identity is required for continuous delivery")
            if not sidebar_config.readable_preview_enabled:
                raise ValueError(
                    "desktop broker readable preview is required for continuous delivery"
                )
            if service_config.catalog_scan_seconds > 60:
                raise ValueError(
                    "desktop broker continuous delivery requires catalog_scan_seconds at most 60"
                )
        claude_visibility_defaults = cls().claude_visibility
        claude_visibility_config = ClaudeVisibilityConfig(
            enabled=_toml_bool(
                claude_visibility.get("enabled", claude_visibility_defaults.enabled),
                "session_bridge.claude_visibility.enabled",
            ),
            continuous=_toml_bool(
                claude_visibility.get(
                    "continuous", claude_visibility_defaults.continuous
                ),
                "session_bridge.claude_visibility.continuous",
            ),
            backfill_days=_toml_int(
                claude_visibility.get(
                    "backfill_days", claude_visibility_defaults.backfill_days
                ),
                "session_bridge.claude_visibility.backfill_days",
                minimum=1,
            ),
            continuous_batch_limit=_toml_int(
                claude_visibility.get(
                    "continuous_batch_limit",
                    claude_visibility_defaults.continuous_batch_limit,
                ),
                "session_bridge.claude_visibility.continuous_batch_limit",
                minimum=1,
            ),
            manual_batch_limit=_toml_int(
                claude_visibility.get(
                    "manual_batch_limit",
                    claude_visibility_defaults.manual_batch_limit,
                ),
                "session_bridge.claude_visibility.manual_batch_limit",
                minimum=1,
            ),
            lease_seconds=_toml_int(
                claude_visibility.get(
                    "lease_seconds", claude_visibility_defaults.lease_seconds
                ),
                "session_bridge.claude_visibility.lease_seconds",
                minimum=1,
            ),
            max_attempts=_toml_int(
                claude_visibility.get(
                    "max_attempts", claude_visibility_defaults.max_attempts
                ),
                "session_bridge.claude_visibility.max_attempts",
                minimum=1,
            ),
            daily_registration_limit=_toml_int(
                claude_visibility.get(
                    "daily_registration_limit",
                    claude_visibility_defaults.daily_registration_limit,
                ),
                "session_bridge.claude_visibility.daily_registration_limit",
                minimum=1,
            ),
            reserved_cost_per_attempt_usd=_positive_decimal(
                claude_visibility.get(
                    "reserved_cost_per_attempt_usd",
                    claude_visibility_defaults.reserved_cost_per_attempt_usd,
                ),
                "session_bridge.claude_visibility.reserved_cost_per_attempt_usd",
            ),
            emergency_daily_cost_usd=_positive_decimal(
                claude_visibility.get(
                    "emergency_daily_cost_usd",
                    claude_visibility_defaults.emergency_daily_cost_usd,
                ),
                "session_bridge.claude_visibility.emergency_daily_cost_usd",
            ),
            process_timeout_seconds=_toml_int(
                claude_visibility.get(
                    "process_timeout_seconds",
                    claude_visibility_defaults.process_timeout_seconds,
                ),
                "session_bridge.claude_visibility.process_timeout_seconds",
                minimum=1,
            ),
            discovery_timeout_seconds=_toml_int(
                claude_visibility.get(
                    "discovery_timeout_seconds",
                    claude_visibility_defaults.discovery_timeout_seconds,
                ),
                "session_bridge.claude_visibility.discovery_timeout_seconds",
                minimum=1,
            ),
            float_activity=_toml_bool(
                claude_visibility.get(
                    "float_activity",
                    claude_visibility_defaults.float_activity,
                ),
                "session_bridge.claude_visibility.float_activity",
            ),
            archive_idle_chips=_toml_bool(
                claude_visibility.get(
                    "archive_idle_chips",
                    claude_visibility_defaults.archive_idle_chips,
                ),
                "session_bridge.claude_visibility.archive_idle_chips",
            ),
            reconcile_desktop_registries=_toml_bool(
                claude_visibility.get(
                    "reconcile_desktop_registries",
                    claude_visibility_defaults.reconcile_desktop_registries,
                ),
                "session_bridge.claude_visibility.reconcile_desktop_registries",
            ),
            idle_chip_archive_seconds=_toml_int(
                claude_visibility.get(
                    "idle_chip_archive_seconds",
                    claude_visibility_defaults.idle_chip_archive_seconds,
                ),
                "session_bridge.claude_visibility.idle_chip_archive_seconds",
                minimum=3600,
            ),
        )
        if claude_visibility_config.continuous_batch_limit != 1:
            raise ValueError(
                "session_bridge.claude_visibility.continuous_batch_limit "
                "must be exactly 1"
            )
        return cls(
            service=service_config,
            catalog=catalog_config,
            mirrors=mirrors_config,
            sidebar=sidebar_config,
            claude_visibility=claude_visibility_config,
        )


def _load_document(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as config_file:
        return tomllib.load(config_file)


def _section(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def _mapping_section(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _reject_unknown_keys(
    values: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    scope: str,
) -> None:
    unknown = sorted(set(values).difference(allowed))
    if unknown:
        raise ValueError(f"unknown {scope} configuration key: {unknown[0]}")


def _env_or_toml(
    environ: Mapping[str, str],
    suffix: str,
    toml_value: object,
    parse_toml: Callable[[object], _Result],
    parse_env: Callable[[str], _Result],
) -> _Result:
    env_name = f"{_ENV_PREFIX}{suffix}"
    if env_name in environ:
        return parse_env(environ[env_name])
    return parse_toml(toml_value)


def _host(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip().lower()


def _canonical_sidebar_string(
    value: object,
    name: str,
    *,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if not is_canonical_sidebar_string(value):
        if name == "session_bridge.sidebar.inbox_cwd":
            raise ValueError(f"{name} must be a non-empty string")
        raise ValueError(f"{name} must be a canonical non-empty single-line string")
    return value


def is_canonical_sidebar_string(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    return not any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or character in {"\u2028", "\u2029"}
        for character in value
    )


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _toml_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _env_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _toml_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return _validate_int(value, name, minimum=minimum, maximum=maximum)


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"{name} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than 0")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{name} must be finite")
    if exponent < -6:
        raise ValueError(f"{name} supports at most 6 decimal places")
    if parsed > Decimal("1000000"):
        raise ValueError(f"{name} cannot exceed 1000000 USD")
    return parsed


def _env_int(
    value: str,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if _INTEGER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an integer")
    return _validate_int(int(value), name, minimum=minimum, maximum=maximum)


def _validate_int(
    value: int,
    name: str,
    *,
    minimum: int | None,
    maximum: int | None,
) -> int:
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _toml_float(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    return _validate_float(
        float(value),
        name,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
    )


def _env_float(
    value: str,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    return _validate_float(
        parsed,
        name,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
    )


def _validate_float(
    value: float,
    name: str,
    *,
    minimum: float | None,
    maximum: float | None,
    exclusive_minimum: bool,
) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and (
        value <= minimum if exclusive_minimum else value < minimum
    ):
        comparison = "greater than" if exclusive_minimum else "at least"
        raise ValueError(f"{name} must be {comparison} {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value
