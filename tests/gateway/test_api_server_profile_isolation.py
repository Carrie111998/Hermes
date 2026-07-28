"""Frozen single-profile identity contracts for the API-server listener."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.api_request_scope import (
    APIProfileGenerationError,
    APIRequestScopeError,
    capture_api_profile_identity,
    freeze_api_profile_inventory,
    verify_api_profile_identity,
)
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def test_frozen_generation_rejects_same_path_delete_and_recreate(tmp_path):
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    original = freeze_api_profile_inventory((("alpha", alpha_home),))[0]

    retired_home = tmp_path / "retired-alpha"
    alpha_home.rename(retired_home)
    alpha_home.mkdir()

    with pytest.raises(APIProfileGenerationError, match="restart required"):
        verify_api_profile_identity(original)

    replacement = freeze_api_profile_inventory((("alpha", alpha_home),))[0]
    assert original.profile_generation != replacement.profile_generation


def test_inventory_rejects_symlink_aliases_to_one_profile_home(tmp_path):
    real_home = tmp_path / "profiles" / "real"
    real_home.mkdir(parents=True)
    alias_home = tmp_path / "profiles" / "alias"
    try:
        os.symlink(real_home, alias_home, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(APIRequestScopeError, match="one canonical home"):
        freeze_api_profile_inventory(
            (("alpha", real_home), ("beta", alias_home))
        )


def test_frozen_generation_rejects_symlink_retarget_with_old_target_alive(
    tmp_path,
):
    old_target = tmp_path / "old-target"
    new_target = tmp_path / "new-target"
    old_target.mkdir()
    new_target.mkdir()
    source = tmp_path / "profiles" / "alpha"
    source.parent.mkdir()
    try:
        os.symlink(old_target, source, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    identity = freeze_api_profile_inventory((("alpha", source),))[0]

    source.unlink()
    os.symlink(new_target, source, target_is_directory=True)
    assert old_target.is_dir()

    with pytest.raises(APIProfileGenerationError, match="restart required"):
        verify_api_profile_identity(identity)


def test_profile_generation_ignores_normal_child_file_mutations(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    identity = capture_api_profile_identity("alpha", home)

    child = home / "gateway.log"
    child.write_text("one", encoding="utf-8")
    child.write_text("two", encoding="utf-8")
    child.unlink()
    connection = sqlite3.connect(home / "state.db")
    try:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.commit()
    finally:
        connection.close()

    verify_api_profile_identity(identity)
    assert (
        capture_api_profile_identity("alpha", home).profile_generation
        == identity.profile_generation
    )


def test_concurrent_profile_marker_initialization_converges(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    with ThreadPoolExecutor(max_workers=16) as executor:
        identities = list(
            executor.map(
                lambda _: capture_api_profile_identity("alpha", home),
                range(64),
            )
        )

    assert len({identity.profile_generation for identity in identities}) == 1
    marker = home / ".api-server-profile-id"
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600
    assert len(marker.read_text(encoding="ascii").strip()) == 64


def test_verification_never_recreates_missing_profile_marker(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    identity = capture_api_profile_identity("alpha", home)
    marker = Path(identity.canonical_home) / ".api-server-profile-id"
    marker.unlink()

    with pytest.raises(APIProfileGenerationError, match="restart required"):
        verify_api_profile_identity(identity)

    assert not marker.exists()


def test_recreated_marker_with_copied_content_is_still_a_new_generation(
    tmp_path,
):
    home = tmp_path / "profile"
    home.mkdir()
    identity = capture_api_profile_identity("alpha", home)
    marker = Path(identity.canonical_home) / ".api-server-profile-id"
    copied_content = marker.read_bytes()
    retired_marker = home / ".retired-api-profile-id"
    marker.rename(retired_marker)
    marker.write_bytes(copied_content)
    marker.chmod(0o600)

    with pytest.raises(APIProfileGenerationError, match="restart required"):
        verify_api_profile_identity(identity)


def test_profile_marker_must_remain_owner_only(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    identity = capture_api_profile_identity("alpha", home)
    marker = Path(identity.canonical_home) / ".api-server-profile-id"
    marker.chmod(0o640)

    with pytest.raises(APIProfileGenerationError, match="restart required"):
        verify_api_profile_identity(identity)


def test_adapter_keeps_runner_owned_inventory_tuple_verbatim(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    inventory = freeze_api_profile_inventory((("default", home),))
    runner_freeze = MagicMock(return_value=inventory)
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False),
        _freeze_served_profile_inventory=runner_freeze,
    )

    try:
        first = adapter._freeze_api_profile_inventory()
        second = adapter._freeze_api_profile_inventory()
    finally:
        adapter._response_store.close()

    assert first is inventory
    assert second is inventory
    assert first[0] is inventory[0]
    runner_freeze.assert_called_once_with()


@pytest.mark.asyncio
async def test_registry_mutation_after_freeze_cannot_redirect_or_relabel(
    tmp_path,
    monkeypatch,
):
    profile_home = tmp_path / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    replacement_home = tmp_path / "profiles" / "evil"
    replacement_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: profile_home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "alpha",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("alpha", profile_home)],
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )
    frozen = adapter._freeze_api_profile_inventory()

    # Runtime registries are mutable process configuration, not listener
    # authority.  Once the listener owns a tuple, later values must be ignored.
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: replacement_home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "evil",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: (_ for _ in ()).throw(
            AssertionError("served profiles were recaptured")
        ),
    )

    async def inspect_authority(_request):
        from gateway.platforms.api_server import _api_request_authority

        authority = _api_request_authority.get()
        return web.json_response(
            {
                "profile": authority.profile,
                "canonical_home": authority.canonical_home,
                "generation": authority.profile_generation,
            }
        )

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/inspect", inspect_authority)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/inspect")
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 200
    assert body == {
        "profile": "alpha",
        "canonical_home": str(profile_home.resolve()),
        "generation": frozen[0].profile_generation,
    }


@pytest.mark.asyncio
async def test_single_profile_request_uses_frozen_home_not_ambient_home(
    tmp_path,
    monkeypatch,
):
    frozen_home = tmp_path / "profile-a"
    ambient_home = tmp_path / "profile-b"
    frozen_home.mkdir()
    ambient_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(frozen_home))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", frozen_home)],
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )
    adapter._freeze_api_profile_inventory()

    # Simulate a stale ambient registry/env after listener startup.
    monkeypatch.setenv("HERMES_HOME", str(ambient_home))

    async def inspect_runtime_home(_request):
        from hermes_constants import get_hermes_home

        return web.json_response(
            {"home": str(get_hermes_home().resolve())}
        )

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/inspect", inspect_runtime_home)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/inspect")
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 200
    assert body["home"] == str(frozen_home.resolve())


@pytest.mark.asyncio
async def test_single_profile_symlink_retarget_never_enters_replacement_home(
    tmp_path,
    monkeypatch,
):
    original_home = tmp_path / "profile-a"
    replacement_home = tmp_path / "profile-b"
    original_home.mkdir()
    replacement_home.mkdir()
    source_home = tmp_path / "active-profile"
    try:
        os.symlink(
            original_home,
            source_home,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", source_home)],
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )
    adapter._freeze_api_profile_inventory()

    source_home.unlink()
    os.symlink(
        replacement_home,
        source_home,
        target_is_directory=True,
    )
    entered = {"handler": False}

    async def must_not_enter(_request):
        entered["handler"] = True
        return web.json_response({"unexpected": True})

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/inspect", must_not_enter)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/inspect")
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 503
    assert body["error"]["code"] == "profile_restart_required"
    assert entered["handler"] is False


@pytest.mark.asyncio
async def test_middleware_rejects_same_path_recreation_for_listener_lifetime(
    tmp_path,
    monkeypatch,
):
    profile = "alpha"
    profile_home = tmp_path / "profiles" / profile
    profile_home.mkdir(parents=True)

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [(profile, profile_home)],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: profile,
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: profile_home,
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )
    adapter._freeze_api_profile_inventory()

    retired_home = tmp_path / "retired"
    profile_home.rename(retired_home)
    profile_home.mkdir()

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/health", adapter._handle_health)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/health")
            assert response.status == 503
            assert (await response.json())["error"]["code"] == (
                "profile_restart_required"
            )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_response_store_reopens_if_profile_changes_before_listener_start(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    old_store = adapter._response_store
    old_store.put("resp_old", {"response": {"id": "resp_old"}})
    old_generation = adapter._response_store_default_identity.profile_generation

    retired_home = tmp_path / "retired-home"
    home.rename(retired_home)
    home.mkdir()
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", home)],
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )

    try:
        inventory = adapter._freeze_api_profile_inventory()
        assert inventory[0].profile_generation != old_generation
        assert adapter._response_store is not old_store
        assert adapter._response_store.get("resp_old") is None
        assert adapter._response_store._db_path == str(
            home / "response_store.db"
        )
        with pytest.raises(sqlite3.ProgrammingError):
            old_store.get("resp_old")
    finally:
        await adapter.disconnect()


def test_response_store_rebind_does_not_publish_closed_replacement_on_failure(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    original_store = adapter._response_store
    original_close = original_store.close
    original_identity = adapter._response_store_default_identity

    replacement_home = tmp_path / "replacement"
    replacement_home.mkdir()
    replacement_identity = capture_api_profile_identity(
        "default",
        replacement_home,
    )
    replacement_store = MagicMock()
    original_store.close = MagicMock(side_effect=RuntimeError("close failed"))

    with patch(
        "gateway.platforms.api_server.ResponseStore",
        return_value=replacement_store,
    ):
        with pytest.raises(RuntimeError, match="close failed"):
            adapter._rebind_startup_response_store(
                (replacement_identity,)
            )

    assert adapter._response_store is original_store
    assert adapter._response_store_default_identity == original_identity
    replacement_store.close.assert_called_once_with()
    original_close()
