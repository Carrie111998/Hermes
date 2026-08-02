"""Regression for #61276: api_server agent entry under multiplex isolation.

When gateway.multiplex_profiles is on, get_secret fails closed without a
profile secret scope. Requests with a ``/p/<profile>/`` prefix are scoped by
``_profile_scope(profile)``, but plain requests on the default listener used
to get ``nullcontext()`` — so agent runs crashed with UnscopedSecretError on
their first credential read (e.g. OPENROUTER_BASE_URL). ``_profile_scope``
now enters the DEFAULT profile's runtime scope when multiplex is active and
no profile was requested.

Adapted from PR #61283 by @giggling-ginger (originally targeting a
pre-``_profile_scope`` helper). Tests use an in-process aiohttp server and no
external gateway or network.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as ss
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True))


class TestProfileScopeDefaultFallback:
    def test_noop_when_multiplex_off(self, adapter, monkeypatch):
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://from-environ.example/v1")
        with adapter._profile_scope(None):
            # Legacy single-profile path: unscoped get_secret reads os.environ.
            assert ss.get_secret("OPENROUTER_BASE_URL") == "https://from-environ.example/v1"
        assert ss.current_secret_scope() is None


# Regression coverage for #72041: profile-bound API authentication
class TestProfileScopedApiAuthentication:
    @staticmethod
    def _request(token: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"},
            remote="127.0.0.1",
            transport=None,
            method="GET",
            path_qs="/p/worker/v1/models",
        )

    def test_named_profile_rejects_default_listener_key(
        self, adapter, tmp_path, monkeypatch
    ):
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        profile_key = "worker-profile-api-key-123456"
        default_key = "default-listener-api-key-123456"
        (profile_home / ".env").write_text(
            f"API_SERVER_KEY={profile_key}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: profile_home,
        )
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                assert adapter._check_auth(self._request(profile_key)) is None

                rejected = adapter._check_auth(self._request(default_key))
                assert rejected is not None
                assert rejected.status == 401
        finally:
            _api_request_profile.reset(profile_token)


@pytest.mark.asyncio
async def test_profile_middleware_binds_auth_before_handler(
    adapter, tmp_path, monkeypatch
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import GatewayConfig
    from gateway.platforms.api_server import _api_request_profile

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    profile_key = "a" * 32
    default_key = "b" * 32
    (worker_home / ".env").write_text(
        f"API_SERVER_KEY={profile_key}\n", encoding="utf-8"
    )
    adapter._api_key = default_key
    adapter.gateway_runner = type(
        "_Runner", (), {"config": GatewayConfig(multiplex_profiles=True)}
    )()
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", tmp_path), ("worker", worker_home)],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path if name == "default" else worker_home,
    )
    ss.set_multiplex_active(True)

    async def authenticated(request):
        auth_error = adapter._check_auth(request)
        if auth_error is not None:
            return auth_error
        return web.json_response(
            {"profile": _api_request_profile.get() or "default"}
        )

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/v1/test", authenticated)
    app.router.add_get("/p/{profile}/v1/test", authenticated)

    async with TestClient(TestServer(app)) as client:
        default_response = await client.get(
            "/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_response.status == 200

        default_alias = await client.get(
            "/p/default/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_alias.status == 200

        rejected = await client.get(
            "/p/worker/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert rejected.status == 401

        accepted = await client.get(
            "/p/worker/v1/test",
            headers={"Authorization": f"Bearer {profile_key}"},
        )
        assert accepted.status == 200
        assert (await accepted.json())["profile"] == "worker"


def _profile_inventory_app(adapter: APIServerAdapter) -> web.Application:
    """Mount the real inventory/capability route entries like connect()."""
    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    selected = {
        "/v1/profiles",
        "/v1/capabilities",
    }
    mounted = set()
    for method, path, handler in adapter._http_route_table():
        if path not in selected:
            continue
        app.router.add_route(method, path, handler)
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)
        mounted.add(path)
    assert mounted == selected
    return app


@pytest.mark.asyncio
async def test_profile_inventory_requires_default_listener_authority(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker"
    worker_home.mkdir(parents=True)
    default_key = "d" * 32
    worker_key = "w" * 32
    (worker_home / ".env").write_text(
        f"API_SERVER_KEY={worker_key}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(
        profiles,
        "_get_default_hermes_home",
        lambda: default_home,
    )
    monkeypatch.setattr(
        profiles,
        "_get_profiles_root",
        lambda: profiles_root,
    )
    profiles_to_serve_calls = 0
    real_profiles_to_serve = profiles.profiles_to_serve

    def tracked_profiles_to_serve(*, multiplex):
        nonlocal profiles_to_serve_calls
        profiles_to_serve_calls += 1
        return real_profiles_to_serve(multiplex=multiplex)

    monkeypatch.setattr(
        profiles,
        "profiles_to_serve",
        tracked_profiles_to_serve,
    )

    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": default_key})
    )
    adapter.gateway_runner = type(
        "_Runner",
        (),
        {"config": GatewayConfig(multiplex_profiles=True)},
    )()
    ss.set_multiplex_active(True)

    async with TestClient(
        TestServer(_profile_inventory_app(adapter))
    ) as client:
        default_response = await client.get(
            "/v1/profiles",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_response.status == 200
        default_inventory = await default_response.json()
        assert [
            item["id"] for item in default_inventory["data"]
        ] == ["default", "worker"]
        assert [
            item["served"] for item in default_inventory["data"]
        ] == [True, True]

        default_alias = await client.get(
            "/p/default/v1/profiles",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_alias.status == 200

        named_with_default_key = await client.get(
            "/p/worker/v1/profiles",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert named_with_default_key.status == 401

        default_with_named_key = await client.get(
            "/v1/profiles",
            headers={"Authorization": f"Bearer {worker_key}"},
        )
        assert default_with_named_key.status == 401

        calls_before_named_request = profiles_to_serve_calls
        named_response = await client.get(
            "/p/worker/v1/profiles",
            headers={"Authorization": f"Bearer {worker_key}"},
        )
        calls_after_named_route_resolution = profiles_to_serve_calls
        assert named_response.status == 403
        named_body = await named_response.json()
        assert named_body["error"]["code"] == (
            "profile_inventory_default_authority_required"
        )
        assert "data" not in named_body
        assert "active_profile" not in named_body
        # A prefixed route performs the existing middleware membership scan.
        # The endpoint must not perform a second, handler-owned global roster
        # load after authenticating a named-profile key.
        assert (
            calls_after_named_route_resolution - calls_before_named_request
        ) == 1

        default_capabilities = await client.get(
            "/v1/capabilities",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_capabilities.status == 200
        default_features = (await default_capabilities.json())["features"]
        assert default_features["profile_inventory"] is True
        assert default_features["profile_inventory_version"] == 1
        assert default_features["profile_inventory_complete"] is True
        assert default_features["profile_inventory_scope"] == "default_listener"
        assert default_features["profile_inventory_requires_api_key"] is True

        named_capabilities = await client.get(
            "/p/worker/v1/capabilities",
            headers={"Authorization": f"Bearer {worker_key}"},
        )
        assert named_capabilities.status == 200
        named_features = (await named_capabilities.json())["features"]
        assert named_features["profile_inventory"] is False
        assert named_features["profile_inventory_complete"] is False
        assert named_features["profile_inventory_scope"] == "default_listener"


@pytest.mark.asyncio
async def test_standalone_named_listener_cannot_enumerate_global_profiles(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker"
    worker_home.mkdir(parents=True)
    worker_key = "w" * 32

    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setattr(
        profiles,
        "_get_default_hermes_home",
        lambda: default_home,
    )
    monkeypatch.setattr(
        profiles,
        "_get_profiles_root",
        lambda: profiles_root,
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": worker_key})
    )
    ss.set_multiplex_active(False)

    async with TestClient(
        TestServer(_profile_inventory_app(adapter))
    ) as client:
        response = await client.get(
            "/v1/profiles",
            headers={"Authorization": f"Bearer {worker_key}"},
        )
        assert response.status == 403
        assert (await response.json())["error"]["code"] == (
            "profile_inventory_default_authority_required"
        )

        capabilities = await client.get(
            "/v1/capabilities",
            headers={"Authorization": f"Bearer {worker_key}"},
        )
        assert capabilities.status == 200
        assert (await capabilities.json())["features"][
            "profile_inventory"
        ] is False
