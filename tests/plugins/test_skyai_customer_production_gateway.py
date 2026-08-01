from __future__ import annotations

import json
from importlib import metadata

import pytest

from plugins.skyai_customer import dev_gateway, discord_delivery
from plugins.skyai_customer import production_gateway


def production_env() -> dict[str, str]:
    return {
        "SKYAI_RUNTIME_MODE": "production",
        "SKYAI_V2_PROFILE_HOME": (
            "/var/lib/skyai/codex/profiles/skyai-v2-prod"
        ),
        "SKYAI_V2_BUILD_COMMIT": (
            "92fd0078b20f42f3d0227f8f47f04a5cf7bb8fca"
        ),
        "SKYAI_PRODUCTION_BIND_HOST": "10.80.0.4",
        "SKYAI_TRUSTED_PROXY_CIDR": "10.80.2.0/28",
        "SKYAI_V2_CANARY_TOKEN": "exact-ingress-token",
        "SKYAI_DISCORD_MIRROR_ENABLED": "1",
        "SKYAI_DISCORD_MIRROR_CREATE_THREADS": "1",
        "SKYAI_DISCORD_MIRROR_CHANNEL_ID": (
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        "SKYAI_DISCORD_BOT_TOKEN": "exact-discord-token",
        "SKYAI_DISCORD_MIRROR_DATABASE_URL": (
            "postgresql://mirror-runtime@database.invalid/skyai"
        ),
        "PORT": "8080",
    }


def test_load_production_settings_is_live_durable_and_exact() -> None:
    environ = production_env()
    environ.update(
        {
            "SKYAI_DISCORD_MIRROR_WORKER_POLL_SECONDS": "0.5",
            "SKYAI_DISCORD_MIRROR_LEASE_SECONDS": "45",
            "SKYAI_DISCORD_MIRROR_BATCH_SIZE": "7",
            "SKYAI_DISCORD_MIRROR_BASE_BACKOFF_SECONDS": "3",
            "SKYAI_DISCORD_MIRROR_MAX_BACKOFF_SECONDS": "90",
            "SKYAI_DISCORD_MIRROR_PAYLOAD_RETENTION_SECONDS": "86400",
        }
    )

    settings = production_gateway.load_production_settings(environ)

    assert settings.runtime_mode == dev_gateway.RUNTIME_MODE_PRODUCTION
    assert settings.version == production_gateway.PRODUCTION_VERSION
    assert settings.profile_home == production_gateway.PRODUCTION_PROFILE_HOME
    assert settings.host == "10.80.0.4"
    assert settings.trusted_proxy_cidr == "10.80.2.0/28"
    assert settings.port == 8080
    assert settings.live_model is True
    assert settings.allow_public_bind is True
    assert settings.discord_mirror_enabled is True
    assert settings.discord_mirror_create_threads is True
    assert settings.discord_mirror_durable_required is True
    assert settings.discord_mirror_thread_store is None
    assert settings.discord_mirror_worker_poll_seconds == 0.5
    assert settings.discord_mirror_lease_seconds == 45
    assert settings.discord_mirror_batch_size == 7
    assert settings.discord_mirror_base_backoff_seconds == 3
    assert settings.discord_mirror_max_backoff_seconds == 90
    assert settings.discord_mirror_payload_retention_seconds == 86400
    assert settings.build_commit == (
        "92fd0078b20f42f3d0227f8f47f04a5cf7bb8fca"
    )


@pytest.mark.parametrize(
    "missing",
    [
        "SKYAI_RUNTIME_MODE",
        "SKYAI_V2_PROFILE_HOME",
        "SKYAI_V2_BUILD_COMMIT",
        "SKYAI_PRODUCTION_BIND_HOST",
        "SKYAI_TRUSTED_PROXY_CIDR",
        "SKYAI_DISCORD_MIRROR_ENABLED",
        "SKYAI_DISCORD_MIRROR_CREATE_THREADS",
        "SKYAI_DISCORD_MIRROR_CHANNEL_ID",
        "SKYAI_DISCORD_BOT_TOKEN",
        "SKYAI_DISCORD_MIRROR_DATABASE_URL",
        "PORT",
    ],
)
def test_load_production_settings_fails_closed_on_missing_binding(
    missing: str,
) -> None:
    environ = production_env()
    del environ[missing]

    with pytest.raises(ValueError, match=missing):
        production_gateway.load_production_settings(environ)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SKYAI_RUNTIME_MODE", "Production"),
        ("SKYAI_DISCORD_MIRROR_ENABLED", "true"),
        ("SKYAI_DISCORD_MIRROR_CREATE_THREADS", "0"),
        ("SKYAI_DISCORD_MIRROR_CHANNEL_ID", "1510888721614901358 "),
        ("SKYAI_V2_PROFILE_HOME", "relative/profile"),
        ("SKYAI_V2_PROFILE_HOME", "/var/lib/skyai/profiles/production"),
        (
            "SKYAI_V2_BUILD_COMMIT",
            "92fd0078b",
        ),
        (
            "SKYAI_V2_BUILD_COMMIT",
            "92FD0078B20F42F3D0227F8F47F04A5CF7BB8FCA",
        ),
        (
            "SKYAI_V2_BUILD_COMMIT",
            "92fd0078b20f42f3d0227f8f47f04a5cf7bb8fca ",
        ),
        ("SKYAI_PRODUCTION_BIND_HOST", "0.0.0.0"),
        ("SKYAI_PRODUCTION_BIND_HOST", "10.80.0.4 "),
        ("SKYAI_PRODUCTION_BIND_HOST", "not-an-ip"),
        ("SKYAI_TRUSTED_PROXY_CIDR", "0.0.0.0/0"),
        ("SKYAI_TRUSTED_PROXY_CIDR", "10.80.2.1/28"),
        ("SKYAI_TRUSTED_PROXY_CIDR", "10.80.2.0/28 "),
        ("PORT", " 8080"),
        ("PORT", "0"),
        ("PORT", "65536"),
        ("SKYAI_DISCORD_MIRROR_WORKER_POLL_SECONDS", "nan"),
        ("SKYAI_DISCORD_MIRROR_BATCH_SIZE", "10.0"),
    ],
)
def test_load_production_settings_rejects_repaired_or_wrong_config(
    name: str,
    value: str,
) -> None:
    environ = production_env()
    environ[name] = value

    with pytest.raises(ValueError):
        production_gateway.load_production_settings(environ)


def test_production_does_not_use_generic_secret_aliases() -> None:
    database_environ = production_env()
    del database_environ["SKYAI_DISCORD_MIRROR_DATABASE_URL"]
    database_environ["DATABASE_URL"] = (
        "postgresql://generic.invalid/skyai"
    )

    with pytest.raises(
        ValueError,
        match="SKYAI_DISCORD_MIRROR_DATABASE_URL",
    ):
        production_gateway.load_production_settings(database_environ)

    discord_environ = production_env()
    del discord_environ["SKYAI_DISCORD_BOT_TOKEN"]
    discord_environ["DISCORD_BOT_TOKEN"] = "generic-discord-token"
    with pytest.raises(ValueError, match="SKYAI_DISCORD_BOT_TOKEN"):
        production_gateway.load_production_settings(discord_environ)


def test_production_token_is_optional_with_exact_trusted_proxy_boundary() -> None:
    environ = production_env()
    del environ["SKYAI_V2_CANARY_TOKEN"]

    settings = production_gateway.load_production_settings(environ)

    assert settings.auth_token == ""
    assert settings.host == "10.80.0.4"
    assert settings.trusted_proxy_cidr == "10.80.2.0/28"


def test_verify_production_dependencies_requires_both_exact_distributions() -> None:
    versions = {
        "psycopg": "3.2.9",
        "psycopg-binary": "3.2.9",
    }

    production_gateway.verify_production_dependencies(
        distribution_version=versions.__getitem__,
        find_spec=lambda name: object() if name == "psycopg" else None,
    )

    versions["psycopg-binary"] = "3.2.8"
    with pytest.raises(RuntimeError, match="psycopg-binary==3.2.9"):
        production_gateway.verify_production_dependencies(
            distribution_version=versions.__getitem__,
            find_spec=lambda _name: object(),
        )


def test_verify_production_dependencies_rejects_missing_distribution() -> None:
    def missing_version(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    with pytest.raises(RuntimeError, match="psycopg==3.2.9"):
        production_gateway.verify_production_dependencies(
            distribution_version=missing_version,
            find_spec=lambda _name: None,
        )


def test_production_main_rejects_every_cli_argument_before_startup() -> None:
    with pytest.raises(SystemExit, match="accepts no CLI arguments"):
        production_gateway.main(["--dev"])


def test_production_main_uses_only_production_settings(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    for name, value in production_env().items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(
        production_gateway,
        "verify_production_dependencies",
        lambda: captured.setdefault("dependencies_verified", True),
    )
    monkeypatch.setattr(
        dev_gateway,
        "create_app",
        lambda settings: captured.setdefault("settings", settings),
    )
    monkeypatch.setattr(
        dev_gateway.web,
        "run_app",
        lambda app, **kwargs: captured.update(
            {"app": app, "run_kwargs": kwargs}
        ),
    )

    assert production_gateway.main([]) == 0

    settings = captured["settings"]
    assert isinstance(settings, dev_gateway.CanarySettings)
    assert settings.runtime_mode == dev_gateway.RUNTIME_MODE_PRODUCTION
    assert settings.live_model is True
    assert settings.discord_mirror_durable_required is True
    assert captured["dependencies_verified"] is True
    assert captured["run_kwargs"] == {"host": "10.80.0.4", "port": 8080}


@pytest.mark.asyncio
async def test_production_app_has_no_dev_or_compare_route() -> None:
    settings = production_gateway.load_production_settings(production_env())
    app = dev_gateway.create_app(
        settings,
        delivery_repository=(
            discord_delivery.InMemoryDiscordDeliveryRepository()
        ),
        discord_transport=object(),  # type: ignore[arg-type]
    )
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
    }

    assert ("POST", "/chatkit/message") in routes
    assert ("POST", "/chatkit/dev-message") not in routes
    assert ("POST", "/qa/compare") not in routes

    health_route = next(
        route
        for route in app.router.routes()
        if route.method == "GET" and route.resource.canonical == "/health"
    )
    response = await health_route.handler(None)
    payload = json.loads(response.text)
    assert payload["service"] == "skyai-hermes-v2-production"
    assert payload["runtime_mode"] == "production"


@pytest.mark.asyncio
async def test_production_readiness_is_false_before_first_database_poll() -> None:
    settings = production_gateway.load_production_settings(production_env())
    app = dev_gateway.create_app(
        settings,
        delivery_repository=(
            discord_delivery.InMemoryDiscordDeliveryRepository()
        ),
        discord_transport=object(),  # type: ignore[arg-type]
    )
    ready_route = next(
        route
        for route in app.router.routes()
        if route.method == "GET" and route.resource.canonical == "/ready"
    )

    response = await ready_route.handler(None)
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["status"] == "not_ready"
    assert payload["discord_mirror"]["durable_required"] is True
    assert (
        payload["discord_mirror"]["first_database_poll_succeeded"]
        is False
    )
