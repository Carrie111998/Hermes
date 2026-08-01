"""Fail-closed production entrypoint for the SkyAI Hermes v2 gateway.

The existing ``dev_gateway`` CLI remains an explicit DEV surface. This module
has no ``--dev`` compatibility path: it accepts no command-line arguments,
requires the exact production runtime contract from environment/secret
bindings, requires the pinned Postgres driver, and always enables the live
Hermes model plus the durable Discord outbox.
"""

from __future__ import annotations

from importlib import metadata, util
import ipaddress
import math
import os
from pathlib import Path
import re
import sys
from typing import Callable, Mapping

from plugins.skyai_customer import dev_gateway


PRODUCTION_VERSION = "skyai-hermes-v2.production"
PRODUCTION_PROFILE_HOME = Path(
    "/var/lib/skyai/codex/profiles/skyai-v2-prod"
)
REQUIRED_PSYCOPG_VERSION = "3.2.9"
EXACT_BUILD_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
RUNTIME_MODE_ENV = "SKYAI_RUNTIME_MODE"
PROFILE_HOME_ENV = "SKYAI_V2_PROFILE_HOME"
AUTH_TOKEN_ENV = "SKYAI_V2_CANARY_TOKEN"
PRODUCTION_BIND_HOST_ENV = "SKYAI_PRODUCTION_BIND_HOST"
TRUSTED_PROXY_CIDR_ENV = "SKYAI_TRUSTED_PROXY_CIDR"
BUILD_COMMIT_ENV = dev_gateway.BUILD_COMMIT_ENV
DISCORD_BOT_TOKEN_ENV = "SKYAI_DISCORD_BOT_TOKEN"
DISCORD_DATABASE_URL_ENV = "SKYAI_DISCORD_MIRROR_DATABASE_URL"
DISCORD_ENABLED_ENV = "SKYAI_DISCORD_MIRROR_ENABLED"
DISCORD_CREATE_THREADS_ENV = "SKYAI_DISCORD_MIRROR_CREATE_THREADS"
DISCORD_CHANNEL_ID_ENV = "SKYAI_DISCORD_MIRROR_CHANNEL_ID"


def _required_exact_env(
    environ: Mapping[str, str],
    name: str,
    *,
    secret: bool = False,
) -> str:
    value = environ.get(name)
    if type(value) is not str or value == "":
        kind = "secret binding" if secret else "environment value"
        raise ValueError(f"{name} requires a nonempty {kind}")
    if any(character.isspace() for character in value):
        kind = "secret binding" if secret else "environment value"
        raise ValueError(f"{name} {kind} must not contain whitespace")
    return value


def _require_exact_env_value(
    environ: Mapping[str, str],
    name: str,
    expected: str,
) -> None:
    value = _required_exact_env(environ, name)
    if value != expected:
        raise ValueError(f"{name} must exactly equal {expected!r}")


def _positive_int_env(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int | None = None,
) -> int:
    value = environ.get(name)
    if value is None:
        if default is None:
            raise ValueError(f"{name} requires a positive integer")
        return default
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ValueError(f"{name} must be an exact positive base-10 integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float_env(
    environ: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    value = environ.get(name)
    if value is None:
        return default
    if (
        type(value) is not str
        or not value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be an exact positive finite number")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an exact positive finite number"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be an exact positive finite number")
    return parsed


def _optional_exact_env(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = environ.get(name)
    if value is None:
        return default
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    return value


def _optional_exact_secret_env(
    environ: Mapping[str, str],
    name: str,
) -> str:
    value = environ.get(name, "")
    if type(value) is not str:
        raise ValueError(f"{name} must be a string secret binding")
    if value and any(character.isspace() for character in value):
        raise ValueError(
            f"{name} secret binding must not contain whitespace"
        )
    return value


def _required_private_bind_host(
    environ: Mapping[str, str],
) -> str:
    value = _required_exact_env(environ, PRODUCTION_BIND_HOST_ENV)
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(
            f"{PRODUCTION_BIND_HOST_ENV} must be an exact private IPv4 address"
        ) from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or str(address) != value
    ):
        raise ValueError(
            f"{PRODUCTION_BIND_HOST_ENV} must be an exact private IPv4 address"
        )
    return value


def load_production_settings(
    environ: Mapping[str, str],
) -> dev_gateway.CanarySettings:
    """Build the exact production settings contract without fallback aliases."""

    _require_exact_env_value(
        environ,
        RUNTIME_MODE_ENV,
        dev_gateway.RUNTIME_MODE_PRODUCTION,
    )
    _require_exact_env_value(environ, DISCORD_ENABLED_ENV, "1")
    _require_exact_env_value(environ, DISCORD_CREATE_THREADS_ENV, "1")
    _require_exact_env_value(
        environ,
        DISCORD_CHANNEL_ID_ENV,
        dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
    )

    profile_home_value = _required_exact_env(environ, PROFILE_HOME_ENV)
    if profile_home_value != str(PRODUCTION_PROFILE_HOME):
        raise ValueError(
            f"{PROFILE_HOME_ENV} must exactly equal "
            f"{str(PRODUCTION_PROFILE_HOME)!r}"
        )
    profile_home = PRODUCTION_PROFILE_HOME

    build_commit = _required_exact_env(environ, BUILD_COMMIT_ENV)
    if EXACT_BUILD_COMMIT_PATTERN.fullmatch(build_commit) is None:
        raise ValueError(
            f"{BUILD_COMMIT_ENV} must be exactly 40 lowercase hex characters"
        )

    port = _positive_int_env(environ, "PORT")
    if port > 65535:
        raise ValueError("PORT must not exceed 65535")

    host = _required_private_bind_host(environ)
    trusted_proxy_cidr = _required_exact_env(
        environ,
        TRUSTED_PROXY_CIDR_ENV,
    )

    settings = dev_gateway.CanarySettings(
        profile_home=profile_home,
        runtime_mode=dev_gateway.RUNTIME_MODE_PRODUCTION,
        host=host,
        port=port,
        live_model=True,
        allow_public_bind=True,
        auth_token=_optional_exact_secret_env(environ, AUTH_TOKEN_ENV),
        trusted_proxy_cidr=trusted_proxy_cidr,
        version=PRODUCTION_VERSION,
        discord_mirror_enabled=True,
        discord_mirror_bot_token=_required_exact_env(
            environ,
            DISCORD_BOT_TOKEN_ENV,
            secret=True,
        ),
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url=_required_exact_env(
            environ,
            DISCORD_DATABASE_URL_ENV,
            secret=True,
        ),
        discord_mirror_durable_required=True,
        discord_mirror_worker_poll_seconds=_positive_float_env(
            environ,
            "SKYAI_DISCORD_MIRROR_WORKER_POLL_SECONDS",
            default=1.0,
        ),
        discord_mirror_lease_seconds=_positive_int_env(
            environ,
            "SKYAI_DISCORD_MIRROR_LEASE_SECONDS",
            default=30,
        ),
        discord_mirror_batch_size=_positive_int_env(
            environ,
            "SKYAI_DISCORD_MIRROR_BATCH_SIZE",
            default=10,
        ),
        discord_mirror_base_backoff_seconds=_positive_int_env(
            environ,
            "SKYAI_DISCORD_MIRROR_BASE_BACKOFF_SECONDS",
            default=2,
        ),
        discord_mirror_max_backoff_seconds=_positive_int_env(
            environ,
            "SKYAI_DISCORD_MIRROR_MAX_BACKOFF_SECONDS",
            default=300,
        ),
        discord_mirror_payload_retention_seconds=_positive_int_env(
            environ,
            "SKYAI_DISCORD_MIRROR_PAYLOAD_RETENTION_SECONDS",
            default=604800,
        ),
        build_commit=build_commit,
        voice_backend_target=_optional_exact_env(
            environ,
            "SKYAI_VOICE_BACKEND_TARGET",
            dev_gateway.DEFAULT_VOICE_BACKEND_TARGET,
        ),
        voice_v1_base_url=_optional_exact_env(
            environ,
            "SKYAI_VOICE_V1_BASE_URL",
            "",
        ),
        voice_v1_path=_optional_exact_env(
            environ,
            "SKYAI_VOICE_V1_PATH",
            dev_gateway.DEFAULT_VOICE_V1_PATH,
        ),
    )
    dev_gateway.validate_settings(settings)
    return settings


def verify_production_dependencies(
    *,
    distribution_version: Callable[[str], str] = metadata.version,
    find_spec: Callable[[str], object | None] = util.find_spec,
) -> None:
    """Require both distributions installed by ``psycopg[binary]``."""

    for distribution in ("psycopg", "psycopg-binary"):
        try:
            installed = distribution_version(distribution)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"production requires {distribution}=={REQUIRED_PSYCOPG_VERSION}"
            ) from exc
        if installed != REQUIRED_PSYCOPG_VERSION:
            raise RuntimeError(
                f"production requires {distribution}=={REQUIRED_PSYCOPG_VERSION}"
            )
    if find_spec("psycopg") is None:
        raise RuntimeError("production requires the importable psycopg driver")


def main(argv: list[str] | None = None) -> int:
    runtime_argv = sys.argv[1:] if argv is None else argv
    if type(runtime_argv) is not list or runtime_argv:
        raise SystemExit(
            "SkyAI production gateway accepts no CLI arguments; "
            "bind the exact production environment contract"
        )

    settings = load_production_settings(os.environ)
    verify_production_dependencies()
    app = dev_gateway.create_app(settings)
    dev_gateway.web.run_app(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
