from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import ipaddress
from pathlib import Path
from typing import Any, TypeVar

from hermes_constants import get_hermes_home


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
    )
})
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
class BridgeConfig:
    service: ServiceConfig = ServiceConfig()
    catalog: CatalogConfig = CatalogConfig()
    mirrors: MirrorsConfig = MirrorsConfig()

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> BridgeConfig:
        config_path = (
            path if path is not None else get_hermes_home() / "session_bridge.toml"
        )
        environment = os.environ if environ is None else environ
        unknown_environment = sorted(
            name
            for name in environment
            if name.startswith(_ENV_PREFIX) and name not in _ENV_NAMES
        )
        if unknown_environment:
            raise ValueError(
                f"unknown session bridge environment variable: "
                f"{unknown_environment[0]}"
            )
        document = _load_document(config_path)

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
        return cls(
            service=service_config,
            catalog=catalog_config,
            mirrors=mirrors_config,
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
