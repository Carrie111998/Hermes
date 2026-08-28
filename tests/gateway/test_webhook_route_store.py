"""Behavioral coverage for the profile-aware webhook route store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

import gateway.platforms.webhook_store as webhook_store_module
from gateway.platforms.webhook_contract import WebhookContractError, WebhookRouteConfig
from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_models import (
    WebhookRouteDocument,
    from_persisted_route,
    to_persisted_route,
)
from gateway.platforms.webhook_store import (
    WebhookRouteStore,
    WebhookRouteStoreCorruptError,
    WebhookRouteStoreUnsafePathError,
    stores_for_served_profiles,
)


def _route(name: str, *, profile: str = "default") -> WebhookRouteDocument:
    return WebhookRouteDocument(
        name=name,
        profile=profile,
        secret_ref=f"WEBHOOK_ROUTE_{name.upper().replace('-', '_')}",
    )


def test_document_delegates_security_binding_to_canonical_contract():
    route = WebhookRouteDocument(
        name="github-push",
        profile="alpha",
        provider="github",
        signature_mode=None,
        events=["push"],
        secret_ref="WEBHOOK_ROUTE_GITHUB_PUSH",
    )

    assert isinstance(route.contract, WebhookRouteConfig)
    assert route.contract.provider == "github"
    assert route.contract.signature_mode == "github"
    assert route.contract.profile == "alpha"


def test_provider_specific_fields_reach_the_canonical_contract():
    trello = WebhookRouteDocument(
        name="trello-card",
        provider="trello",
        signature_mode=None,
        callback_url="https://hooks.example.test/trello",
        secret_ref="WEBHOOK_ROUTE_TRELLO_CARD",
    )
    assert trello.contract.signature_context == "https://hooks.example.test/trello"

    with pytest.raises(ValidationError, match="callback_url"):
        WebhookRouteDocument(
            name="trello-card",
            provider="trello",
            signature_mode=None,
            secret_ref="WEBHOOK_ROUTE_TRELLO_CARD",
        )


def test_historical_cli_route_uses_github_default_not_generic_default():
    route = from_persisted_route(
        "legacy",
        {
            "description": "Agent-created subscription: legacy",
            "secret": "plaintext-until-migration",
        },
        profile="default",
    )

    assert route.contract.provider == "github"
    assert route.contract.signature_mode == "github"
    assert route.secret_ref is None
    assert route.has_legacy_plaintext_secret is True
    assert "plaintext-until-migration" not in repr(route)
    assert "plaintext-until-migration" not in str(route.model_dump())
    assert to_persisted_route(route)["secret"] == "plaintext-until-migration"


def test_plaintext_is_never_reinterpreted_as_a_secret_reference():
    route = from_persisted_route(
        "legacy",
        {
            "provider": "generic",
            "secret": "not-an-env-name",
        },
        profile="default",
    )
    assert route.secret_ref is None
    assert route.legacy_secret == "not-an-env-name"

    with pytest.raises(ValidationError, match="both secret_ref and plaintext"):
        WebhookRouteDocument(
            name="ambiguous",
            secret_ref="WEBHOOK_ROUTE_AMBIGUOUS",
            secret="plaintext",
        )


@pytest.mark.parametrize("reserved", ["legacy_secret", "legacy_secret_value"])
def test_internal_secret_attribute_names_cannot_be_retained_as_extras(reserved):
    with pytest.raises(ValidationError, match="reserved secret field"):
        WebhookRouteDocument.model_validate({
            "name": "ambiguous",
            "provider": "generic",
            "secret": "hidden-value",
            reserved: "repr-visible-value",
        })


@pytest.mark.parametrize("name", ["../escape", "Bad", "-leading", "", "a/b"])
def test_document_rejects_noncanonical_route_names(name: str):
    with pytest.raises(ValidationError):
        WebhookRouteDocument(name=name, secret_ref="WEBHOOK_ROUTE_TEST")


def test_profile_paths_are_isolated_and_served_homes_are_exact(tmp_path):
    alpha_home = tmp_path / "profiles" / "alpha"
    beta_home = tmp_path / "profiles" / "beta"
    stores = stores_for_served_profiles([("alpha", alpha_home), ("beta", beta_home)])
    stores["alpha"].save({"alpha-route": _route("alpha-route", profile="alpha")})
    stores["beta"].save({"beta-route": _route("beta-route", profile="beta")})

    assert stores["alpha"].path == alpha_home / "webhook_subscriptions.json"
    assert stores["beta"].path == beta_home / "webhook_subscriptions.json"
    assert list(stores["alpha"].load()) == ["alpha-route"]
    assert list(stores["beta"].load()) == ["beta-route"]


def test_store_rejects_traversal_and_profile_home_mismatch(tmp_path):
    with pytest.raises(WebhookRouteStoreUnsafePathError, match="traversal"):
        WebhookRouteStore(tmp_path / "safe" / ".." / "escape")
    with pytest.raises(WebhookRouteStoreUnsafePathError, match="does not match"):
        WebhookRouteStore.for_profile_home("alpha", tmp_path / "profiles" / "beta")


def test_store_rejects_embedded_null_root():
    with pytest.raises(WebhookRouteStoreUnsafePathError, match="valid path"):
        WebhookRouteStore("invalid\x00root")


def test_windows_move_flags_are_write_through_platform_data():
    assert webhook_store_module._windows_move_flags(replace_existing=True) == (
        webhook_store_module._WINDOWS_MOVE_REPLACE_EXISTING
        | webhook_store_module._WINDOWS_MOVE_WRITE_THROUGH
    )
    assert (
        webhook_store_module._windows_move_flags(replace_existing=False)
        == webhook_store_module._WINDOWS_MOVE_WRITE_THROUGH
    )


@pytest.mark.windows_only
def test_windows_native_store_round_trip(tmp_path):
    store = WebhookRouteStore(tmp_path, profile="alpha")
    assert store.probe_identity() is None
    store.lock_path.write_bytes(b"")
    store.save({"route": _route("route", profile="alpha")})

    assert store.lock_path.read_bytes() == b"0"
    assert list(store.load()) == ["route"]
    assert store.probe_identity() is not None


def test_save_is_sorted_atomic_owner_only_and_temp_free(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.save({
        "zulu": _route("zulu"),
        "alpha": _route("alpha"),
        "middle": _route("middle"),
    })

    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(data) == ["alpha", "middle", "zulu"]
    assert list(store.load()) == ["alpha", "middle", "zulu"]
    assert not list(tmp_path.glob(".*.tmp"))
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_management_corruption_raises_without_changing_exact_bytes(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    corrupt = b'{"route":{"provider":"generic"},"route":{}}'
    store.path.write_bytes(corrupt)

    with pytest.raises(WebhookRouteStoreCorruptError):
        store.load()

    assert store.path.read_bytes() == corrupt
    assert not list(tmp_path.glob("webhook_subscriptions.json.corrupt-*"))


@pytest.mark.parametrize(
    "corrupt",
    [
        b'{"route":{"provider":"generic","secret":"value","future":1e999}}',
        b'{"route":{"provider":"generic","secret":"value","description":"\\ud800"}}',
    ],
)
def test_management_rejects_noncanonical_json_values_without_mutation(
    tmp_path,
    corrupt,
):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(corrupt)

    with pytest.raises(WebhookRouteStoreCorruptError):
        store.load()

    assert store.path.read_bytes() == corrupt


def test_corrupt_store_traceback_does_not_disclose_plaintext_secret(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    sentinel = "never-log-this-route-secret"
    store.path.write_text(
        json.dumps({
            "route": {
                "provider": "generic",
                "secret": sentinel,
                "response_mode": "callback",
                "callback": {"url": "file:///invalid"},
            }
        }),
        encoding="utf-8",
    )

    with pytest.raises(WebhookRouteStoreCorruptError) as raised:
        store.load()

    rendered = "".join(traceback.format_exception(raised.value))
    assert sentinel not in rendered


def test_runtime_corruption_quarantines_exact_bytes_and_returns_revocation(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    corrupt = b"{not-json"
    store.path.write_bytes(corrupt)

    snapshot = store.load_runtime()

    assert snapshot.routes == {}
    assert snapshot.quarantined_path is not None
    assert snapshot.quarantined_path.read_bytes() == corrupt
    assert not store.path.exists()
    assert snapshot.content_sha256 is not None


def test_runtime_oversize_store_is_quarantined_without_unbounded_read(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    oversized = 4 * 1024 * 1024 + 1
    with store.path.open("wb") as stream:
        stream.truncate(oversized)

    snapshot = store.load_runtime()

    assert snapshot.routes == {}
    assert snapshot.content_sha256 is None
    assert snapshot.quarantined_path is not None
    assert snapshot.quarantined_path.stat().st_size == oversized
    assert not store.path.exists()


def test_update_refuses_to_replace_a_corrupt_store(tmp_path):
    store = WebhookRouteStore(tmp_path)
    store.profile_root.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"broken")

    with pytest.raises(WebhookRouteStoreCorruptError):
        store.update(lambda routes: {**routes, "new": _route("new")})

    assert store.path.read_bytes() == b"broken"


def test_store_rejects_symlink_file_and_parent(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(tmp_path, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(WebhookRouteStoreUnsafePathError, match="real directories"):
        WebhookRouteStore(linked_root).load()

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    (real_root / "webhook_subscriptions.json").symlink_to(target)
    with pytest.raises(WebhookRouteStoreUnsafePathError, match="must not be a link"):
        WebhookRouteStore(real_root).load()
    assert target.read_text(encoding="utf-8") == "{}"


def test_store_rejects_profile_directory_writable_by_other_accounts(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX ownership and mode contract")
    store_root = tmp_path / "shared-store"
    store_root.mkdir(mode=0o700)
    store_root.chmod(0o777)

    with pytest.raises(
        WebhookRouteStoreUnsafePathError,
        match="group- or world-writable",
    ):
        WebhookRouteStore(store_root).save({"route": _route("route")})

    assert not (store_root / "webhook_subscriptions.json").exists()


def test_route_hardlink_is_rejected_before_mode_or_content_mutation(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    outside = tmp_path / "outside.json"
    content = b'{"route":{"provider":"generic","secret":"value"}}'
    outside.write_bytes(content)
    outside.chmod(0o644)
    try:
        os.link(outside, store_root / "webhook_subscriptions.json")
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(WebhookRouteStoreUnsafePathError, match="one filesystem link"):
        WebhookRouteStore(store_root).load()

    assert outside.read_bytes() == content
    if os.name == "posix":
        assert outside.stat().st_mode & 0o777 == 0o644


def test_lock_hardlink_is_rejected_before_mode_or_content_mutation(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"sentinel")
    outside.chmod(0o644)
    try:
        os.link(outside, store_root / ".webhook_subscriptions.lock")
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(WebhookRouteStoreUnsafePathError, match="one filesystem link"):
        WebhookRouteStore(store_root).save({"route": _route("route")})

    assert outside.read_bytes() == b"sentinel"
    if os.name == "posix":
        assert outside.stat().st_mode & 0o777 == 0o644


def test_lock_symlink_is_rejected_without_touching_target(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"sentinel")
    try:
        (store_root / ".webhook_subscriptions.lock").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(WebhookRouteStoreUnsafePathError, match="must not be a link"):
        WebhookRouteStore(store_root).save({"route": _route("route")})

    assert outside.read_bytes() == b"sentinel"


def test_mutated_nested_document_is_deeply_revalidated_before_save(tmp_path):
    store = WebhookRouteStore(tmp_path)
    route = WebhookRouteDocument(
        name="callback",
        secret_ref="WEBHOOK_ROUTE_CALLBACK",
        response_mode="callback",
        callback={"url": "https://example.test/result"},
    )
    route.callback["url"] = "file:///not-authorized"

    with pytest.raises(ValidationError, match="absolute HTTP"):
        store.save({"callback": route})

    assert not store.path.exists()


def test_serialization_sorts_nested_unknown_policy_keys(tmp_path):
    store = WebhookRouteStore(tmp_path)
    route = _route("route")
    route.future_policy = {"zulu": 1, "alpha": {"zulu": 2, "alpha": 3}}

    store.save({"route": route})

    raw = store.path.read_text(encoding="utf-8")
    assert raw.index('"alpha"') < raw.index('"zulu"')
    nested = json.loads(raw)["route"]["future_policy"]["alpha"]
    assert list(nested) == ["alpha", "zulu"]


def test_concurrent_updates_do_not_lose_independent_routes(tmp_path):
    store = WebhookRouteStore(tmp_path)

    def add(index: int) -> None:
        name = f"route-{index:02d}"
        store.update(lambda current: {**current, name: _route(name)})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(24)))

    routes = store.load()
    assert len(routes) == 24
    assert list(routes) == sorted(routes)


def test_store_rejects_cross_profile_documents(tmp_path):
    store = WebhookRouteStore(tmp_path, profile="alpha")
    with pytest.raises(ValueError, match="profile store"):
        store.save({"route": _route("route", profile="beta")})


def _write_runtime_routes(home, routes: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "webhook_subscriptions.json").write_text(
        json.dumps(routes),
        encoding="utf-8",
    )


def _multiplex_adapter(root, *, allowlist=None, static_routes=None) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "secret": "listener-owner-secret",
                "routes": static_routes or {},
            },
        )
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(
            multiplex_profiles=True,
            multiplex_profile_allowlist=allowlist,
        ),
        _resolve_profile_home_for_source=lambda source: (
            root if source.profile == "default" else root / "profiles" / source.profile
        ),
    )
    runner.adapters = {Platform.WEBHOOK: adapter}
    adapter.gateway_runner = runner
    # These integration tests exercise route-store publication. The production
    # resolver/context and their own suites remain responsible for tool grants.
    adapter._resolve_admitted_toolsets = lambda route, source, strict_config=False: []
    adapter._profile_runtime_context = lambda source: nullcontext()
    return adapter


def test_multiplex_runtime_loads_only_exact_admitted_profile_stores(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    beta = tmp_path / "profiles" / "beta"
    _write_runtime_routes(
        tmp_path,
        {"default-hook": {"provider": "generic", "secret": "default-secret"}},
    )
    _write_runtime_routes(
        alpha,
        {"alpha-hook": {"provider": "generic", "secret": "alpha-secret"}},
    )
    _write_runtime_routes(
        beta,
        {"beta-hook": {"provider": "generic", "secret": "beta-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])

    adapter._reload_dynamic_routes()

    assert set(adapter._dynamic_routes) == {"default-hook", "alpha-hook"}
    assert adapter._dynamic_routes["default-hook"]["profile"] == "default"
    assert adapter._dynamic_routes["alpha-hook"]["profile"] == "alpha"
    assert set(adapter._dynamic_profile_store_state) == {"default", "alpha"}


def test_multiplex_runtime_quarantines_and_revokes_only_corrupt_profile(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {"default-hook": {"provider": "generic", "secret": "default-secret"}},
    )
    _write_runtime_routes(
        alpha,
        {"alpha-hook": {"provider": "generic", "secret": "alpha-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter._reload_dynamic_routes()
    assert set(adapter._dynamic_routes) == {"default-hook", "alpha-hook"}

    corrupt = b"{broken-profile-store"
    alpha_store = alpha / "webhook_subscriptions.json"
    alpha_store.write_bytes(corrupt)
    adapter._dynamic_profile_reload_after = 0.0
    adapter._reload_dynamic_routes()

    assert set(adapter._dynamic_routes) == {"default-hook"}
    quarantines = list(alpha.glob("webhook_subscriptions.json.corrupt-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == corrupt
    assert not alpha_store.exists()


def test_multiplex_runtime_qualifies_same_route_name_by_public_profile(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {"shared": {"provider": "generic", "secret": "default-secret"}},
    )
    _write_runtime_routes(
        alpha,
        {"shared": {"provider": "generic", "secret": "alpha-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])

    adapter._reload_dynamic_routes()

    assert set(adapter._dynamic_routes_by_key) == {
        ("default", "shared"),
        ("alpha", "shared"),
    }
    assert set(adapter._authenticated_route_registry) == {
        ("default", "shared"),
        ("alpha", "shared"),
    }
    assert adapter._authenticated_route_registry[("default", "shared")].secret == (
        "default-secret"
    )
    assert adapter._authenticated_route_registry[("alpha", "shared")].secret == (
        "alpha-secret"
    )
    # The historical name-only facade cannot choose between siblings.
    assert "shared" not in adapter._dynamic_routes
    assert "shared" not in adapter._routes


def test_qualified_route_key_must_match_embedded_public_profile(tmp_path):
    adapter = _multiplex_adapter(tmp_path, allowlist=[])

    with pytest.raises(WebhookContractError, match="carries profile 'beta'"):
        adapter._qualified_route_candidates({
            ("alpha", "shared"): {
                "profile": "beta",
                "provider": "generic",
                "secret": "beta-secret",
            }
        })


def test_static_and_dynamic_same_name_coexist_across_profiles(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        alpha,
        {"shared": {"provider": "generic", "secret": "alpha-secret"}},
    )
    adapter = _multiplex_adapter(
        tmp_path,
        allowlist=["alpha"],
        static_routes={"shared": {"provider": "generic", "secret": "static-secret"}},
    )

    adapter._reload_dynamic_routes()

    assert set(adapter._authenticated_route_registry) == {
        ("default", "shared"),
        ("alpha", "shared"),
    }
    assert adapter._authenticated_route_registry[("default", "shared")].secret == (
        "static-secret"
    )
    assert adapter._authenticated_route_registry[("alpha", "shared")].secret == (
        "alpha-secret"
    )


def test_disabling_same_name_sibling_revokes_only_its_qualified_authority(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    default_route = {"provider": "generic", "secret": "default-secret"}
    alpha_route = {"provider": "generic", "secret": "alpha-secret"}
    _write_runtime_routes(tmp_path, {"shared": default_route})
    _write_runtime_routes(alpha, {"shared": alpha_route})
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter._reload_dynamic_routes()
    assert adapter._record_rate_limit_hit("shared", 1.0, profile="default")
    assert adapter._record_rate_limit_hit("shared", 1.0, profile="alpha")

    _write_runtime_routes(
        alpha,
        {"shared": {**alpha_route, "enabled": False}},
    )
    adapter._dynamic_profile_reload_after = 0.0
    adapter._reload_dynamic_routes()

    assert set(adapter._authenticated_route_registry) == {("default", "shared")}
    assert adapter._authenticated_route_registry[("default", "shared")].secret == (
        "default-secret"
    )
    assert ("default", "shared") in adapter._rate_counts
    assert ("alpha", "shared") not in adapter._rate_counts


@pytest.mark.asyncio
async def test_same_name_profiles_route_to_exact_immutable_bundle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "default-secret",
            }
        },
    )
    _write_runtime_routes(
        alpha,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "alpha-secret",
            }
        },
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter.handle_message = AsyncMock()
    adapter._reload_dynamic_routes()
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post(
        "/p/{profile}/webhooks/{route_name}",
        adapter._handle_webhook,
    )
    body = b'{"value":1}'

    def headers(secret: str) -> dict[str, str]:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        }

    async with TestClient(TestServer(app)) as client:
        crossed_default = await client.post(
            "/webhooks/shared",
            data=body,
            headers=headers("alpha-secret"),
        )
        crossed_alpha = await client.post(
            "/p/alpha/webhooks/shared",
            data=body,
            headers=headers("default-secret"),
        )
        accepted_default = await client.post(
            "/webhooks/shared",
            data=body,
            headers=headers("default-secret"),
        )
        accepted_alpha = await client.post(
            "/p/alpha/webhooks/shared",
            data=body,
            headers=headers("alpha-secret"),
        )

    assert crossed_default.status == crossed_alpha.status == 401
    assert accepted_default.status == accepted_alpha.status == 202
    await asyncio.sleep(0.05)
    assert [
        call.args[0].source.profile for call in adapter.handle_message.await_args_list
    ] == ["default", "alpha"]


@pytest.mark.asyncio
async def test_same_name_sibling_republish_preserves_only_unchanged_inflight_bundle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "default-secret",
            }
        },
    )
    _write_runtime_routes(
        alpha,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "alpha-secret",
            }
        },
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter.handle_message = AsyncMock()
    adapter._reload_dynamic_routes()
    prior_default = adapter._authenticated_route_registry[("default", "shared")]
    prior_alpha = adapter._authenticated_route_registry[("alpha", "shared")]
    body = b'{"value":"in-flight"}'

    class BlockingRequest:
        def __init__(self, profile: str, secret: str):
            self.match_info = {
                "route_name": "shared",
                **({"profile": profile} if profile else {}),
            }
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            self.headers = {
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            }
            self.content_length = len(body)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def read(self) -> bytes:
            self.started.set()
            await self.release.wait()
            return body

    default_request = BlockingRequest("", "default-secret")
    alpha_request = BlockingRequest("alpha", "alpha-secret")
    default_task = asyncio.create_task(adapter._handle_webhook(default_request))
    alpha_task = asyncio.create_task(adapter._handle_webhook(alpha_request))
    await asyncio.gather(
        default_request.started.wait(),
        alpha_request.started.wait(),
    )

    _write_runtime_routes(
        alpha,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "rotated-alpha-secret",
            }
        },
    )
    adapter._dynamic_profile_reload_after = 0.0
    adapter._reload_dynamic_routes()

    assert adapter._authenticated_route_registry[("default", "shared")] is prior_default
    assert adapter._authenticated_route_registry[("alpha", "shared")] is not prior_alpha
    default_request.release.set()
    alpha_request.release.set()
    default_response, alpha_response = await asyncio.gather(
        default_task,
        alpha_task,
    )

    assert default_response.status == 202
    assert alpha_response.status == 503
    await asyncio.sleep(0)
    assert len(adapter.handle_message.await_args_list) == 1
    assert adapter.handle_message.await_args.args[0].source.profile == "default"


@pytest.mark.asyncio
async def test_same_name_profiles_have_independent_request_rate_buckets(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "default-secret",
            }
        },
    )
    _write_runtime_routes(
        alpha,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "alpha-secret",
            }
        },
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter.handle_message = AsyncMock()
    adapter._rate_limit = 1
    adapter._reload_dynamic_routes()
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post(
        "/p/{profile}/webhooks/{route_name}",
        adapter._handle_webhook,
    )

    def headers(secret: str, body: bytes) -> dict[str, str]:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        }

    first = b'{"value":1}'
    second = b'{"value":2}'
    async with TestClient(TestServer(app)) as client:
        default_first = await client.post(
            "/webhooks/shared",
            data=first,
            headers=headers("default-secret", first),
        )
        default_second = await client.post(
            "/webhooks/shared",
            data=second,
            headers=headers("default-secret", second),
        )
        alpha_first = await client.post(
            "/p/alpha/webhooks/shared",
            data=first,
            headers=headers("alpha-secret", first),
        )
        alpha_second = await client.post(
            "/p/alpha/webhooks/shared",
            data=second,
            headers=headers("alpha-secret", second),
        )

    assert default_first.status == alpha_first.status == 202
    assert default_second.status == alpha_second.status == 429
    assert set(adapter._rate_counts) == {
        ("default", "shared"),
        ("alpha", "shared"),
    }


@pytest.mark.asyncio
async def test_bare_path_never_falls_back_to_only_named_profile_route(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        alpha,
        {
            "shared": {
                "signature_mode": "generic_v1",
                "secret": "alpha-secret",
            }
        },
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter.handle_message = AsyncMock()
    adapter._reload_dynamic_routes()
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post(
        "/p/{profile}/webhooks/{route_name}",
        adapter._handle_webhook,
    )
    body = b'{"value":2}'
    signature = hmac.new(b"alpha-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
    }

    async with TestClient(TestServer(app)) as client:
        bare = await client.post("/webhooks/shared", data=body, headers=headers)
        named = await client.post(
            "/p/alpha/webhooks/shared",
            data=body,
            headers=headers,
        )

    assert bare.status == 404
    assert named.status == 202


def test_multiplex_runtime_skips_disabled_and_never_borrows_owner_secret(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        alpha,
        {
            "disabled": {
                "provider": "generic",
                "secret": "alpha-secret",
                "enabled": False,
            },
            "missing-secret": {"provider": "generic"},
            "unresolved-ref": {
                "provider": "generic",
                "secret_ref": "WEBHOOK_ROUTE_UNRESOLVED",
            },
        },
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])

    adapter._reload_dynamic_routes()

    assert adapter._dynamic_routes == {}


def test_multiplex_runtime_revokes_snapshot_if_profile_incarnation_changes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        tmp_path,
        {"default-hook": {"provider": "generic", "secret": "default-secret"}},
    )
    _write_runtime_routes(
        alpha,
        {"alpha-hook": {"provider": "generic", "secret": "alpha-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter._reload_dynamic_routes()
    assert "alpha-hook" in adapter._dynamic_routes
    real_generation = adapter._current_profile_authority_generation
    alpha_calls = 0

    def changing_generation(profile, *, route_name):
        nonlocal alpha_calls
        if profile == "alpha" and route_name == "profile-store":
            alpha_calls += 1
            return "a" * 64 if alpha_calls == 1 else "b" * 64
        return real_generation(profile, route_name=route_name)

    monkeypatch.setattr(
        adapter,
        "_current_profile_authority_generation",
        changing_generation,
    )
    adapter._dynamic_profile_reload_after = 0.0
    adapter._reload_dynamic_routes()

    assert "default-hook" in adapter._dynamic_routes
    assert "alpha-hook" not in adapter._dynamic_routes


def test_multiplex_runtime_unchanged_fast_path_performs_no_store_io(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_runtime_routes(
        tmp_path,
        {"hook": {"provider": "generic", "secret": "default-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=[])
    clock = [100.0]
    monkeypatch.setattr(
        "gateway.platforms.webhook_intake.time.monotonic",
        lambda: clock[0],
    )
    real_load = WebhookRouteStore.load_runtime
    calls = []

    def counted_load(store):
        calls.append(store.profile)
        return real_load(store)

    monkeypatch.setattr(WebhookRouteStore, "load_runtime", counted_load)
    adapter._reload_dynamic_routes()
    first_calls = list(calls)
    bind_calls = []
    real_bind = adapter._bind_route_authentication_authorities

    def counted_bind(routes):
        bind_calls.append(tuple(sorted(routes)))
        return real_bind(routes)

    monkeypatch.setattr(adapter, "_bind_route_authentication_authorities", counted_bind)

    clock[0] = 100.5
    adapter._reload_dynamic_routes()
    assert calls == first_calls
    assert bind_calls == []

    clock[0] = 101.1
    adapter._reload_dynamic_routes()
    assert len(calls) > len(first_calls)
    assert bind_calls == []


def test_multiplex_integrity_rechecks_are_globally_staggered(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for profile in ("alpha", "beta", "gamma"):
        _write_runtime_routes(
            tmp_path / "profiles" / profile,
            {
                f"{profile}-hook": {
                    "provider": "generic",
                    "secret": f"{profile}-secret",
                }
            },
        )
    adapter = _multiplex_adapter(
        tmp_path,
        allowlist=["alpha", "beta", "gamma"],
    )
    clock = [200.0]
    monkeypatch.setattr(
        "gateway.platforms.webhook_intake.time.monotonic",
        lambda: clock[0],
    )
    adapter._reload_dynamic_routes()

    real_load = WebhookRouteStore.load_runtime
    calls = []

    def counted_load(store):
        calls.append(store.profile)
        return real_load(store)

    monkeypatch.setattr(WebhookRouteStore, "load_runtime", counted_load)
    clock[0] = 201.1
    adapter._reload_dynamic_routes()

    # Four admitted profiles (default + three named), but only the single
    # round-robin integrity slot gets a deep read when all identities match.
    assert len(calls) == 1


def test_multiplex_runtime_busy_writer_retains_prior_exact_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    alpha = tmp_path / "profiles" / "alpha"
    _write_runtime_routes(
        alpha,
        {"alpha-hook": {"provider": "generic", "secret": "alpha-secret"}},
    )
    adapter = _multiplex_adapter(tmp_path, allowlist=["alpha"])
    adapter._reload_dynamic_routes()
    prior = dict(adapter._dynamic_routes["alpha-hook"])
    real_load = WebhookRouteStore.load_runtime

    def busy_alpha(store):
        if store.profile == "alpha":
            raise TimeoutError("writer still owns atomic switch")
        return real_load(store)

    monkeypatch.setattr(WebhookRouteStore, "load_runtime", busy_alpha)
    adapter._dynamic_profile_reload_after = 0.0
    adapter._reload_dynamic_routes()

    assert adapter._dynamic_routes["alpha-hook"] == prior
