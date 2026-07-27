from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from hermes_cli import managed_plugin_update as managed
from hermes_cli import plugins_cmd


def _write_managed_manifest(root: Path, *, name: str = "t3code") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (root / "update_process.py").write_text("# worker\n", encoding="utf-8")
    (root / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0",
                "update": {
                    "mode": "managed",
                    "contract": "t3code-hermes-v1",
                    "entrypoint": "update_process.py",
                },
            }
        ),
        encoding="utf-8",
    )


def test_plugin_manifest_parses_managed_update_metadata(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))

    manager = PluginManager()
    manifests = manager._scan_directory(root.parent, "user")
    manifest = next(item for item in manifests if item.name == "t3code")

    assert manifest.update_mode == "managed"
    assert manifest.update_contract == "t3code-hermes-v1"
    assert manifest.update_entrypoint == "update_process.py"


def test_managed_dispatch_never_falls_back_to_git_pull(tmp_path, monkeypatch):
    root = tmp_path / "t3code"
    _write_managed_manifest(root)
    expected = {"ok": True, "version": "2.0", "source_commit": "a" * 40}
    managed_run = Mock(return_value=expected)
    monkeypatch.setattr(managed, "run_managed_update", managed_run)
    monkeypatch.setattr(
        plugins_cmd,
        "_git_pull_plugin_dir",
        Mock(side_effect=AssertionError("source-only fallback reached")),
    )

    result = plugins_cmd._update_user_plugin("t3code", root)

    assert result["update_mode"] == "managed"
    assert result["version"] == "2.0"
    managed_run.assert_called_once()


def test_invalid_managed_declaration_fails_without_git_fallback(
    tmp_path, monkeypatch
):
    root = tmp_path / "t3code"
    root.mkdir()
    (root / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "t3code",
                "update": {
                    "mode": "managed",
                    "contract": "t3code-hermes-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    pull = Mock(side_effect=AssertionError("source-only fallback reached"))
    monkeypatch.setattr(plugins_cmd, "_git_pull_plugin_dir", pull)

    with pytest.raises(
        plugins_cmd.PluginOperationError,
        match="update.entrypoint",
    ):
        plugins_cmd._update_user_plugin("t3code", root)
    pull.assert_not_called()


def test_unreadable_manifest_fails_closed_without_git_fallback(
    tmp_path, monkeypatch
):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.yaml").write_text("update: [\n", encoding="utf-8")
    pull = Mock(side_effect=AssertionError("source-only fallback reached"))
    monkeypatch.setattr(plugins_cmd, "_git_pull_plugin_dir", pull)

    with pytest.raises(
        plugins_cmd.PluginOperationError,
        match="Could not read managed plugin manifest",
    ):
        plugins_cmd._update_user_plugin("plugin", root)
    pull.assert_not_called()


def test_unmanaged_dispatch_keeps_ff_only_git_behavior(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "plugin.yaml").write_text("name: legacy\n", encoding="utf-8")
    pull = Mock(return_value=(True, "Already up to date."))
    monkeypatch.setattr(plugins_cmd, "_git_pull_plugin_dir", pull)
    monkeypatch.setattr(plugins_cmd, "_copy_example_files", Mock())

    result = plugins_cmd._update_user_plugin("legacy", root)

    assert result == {
        "ok": True,
        "name": "legacy",
        "output": "Already up to date.",
        "unchanged": True,
        "update_mode": "git",
    }
    pull.assert_called_once_with(root)


def test_dashboard_and_cli_share_managed_dispatch(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    (root / ".git").mkdir()
    monkeypatch.setattr(plugins_cmd, "_plugins_dir", lambda: home / "plugins")
    shared = Mock(
        return_value={
            "ok": True,
            "name": "t3code",
            "update_mode": "managed",
            "version": "2.0",
        }
    )
    monkeypatch.setattr(plugins_cmd, "_update_user_plugin", shared)

    plugins_cmd.cmd_update("t3code")
    dashboard = plugins_cmd.dashboard_update_user_plugin("t3code")

    assert dashboard["update_mode"] == "managed"
    assert shared.call_args_list[0].args == ("t3code", root.resolve())
    assert shared.call_args_list[1].args == ("t3code", root.resolve())
    assert "native runtime updated together" in capsys.readouterr().out


def test_host_unavailable_preflight_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    contract = managed.get_managed_update_contract("t3code")

    assert contract is not None
    with pytest.raises(
        managed.ManagedPluginUpdateError,
        match="coordinator is unavailable",
    ):
        contract.preflight(plugin_name="t3code", plugin_root=root)
    assert not (home / "runtime").exists()


def test_authenticated_worker_handoff_and_host_attestation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    preflight = Mock()
    def attest(
        _name: str, _root: Path, source_commit: str, product_version: str | None
    ) -> dict[str, object]:
        return {
            "reloaded": True,
            "loaded_source_commit": source_commit,
            "loaded_product_version": product_version,
        }

    reload_backend = Mock(side_effect=attest)

    with managed.ManagedUpdateCoordinator(
        preflight=preflight,
        reload_backend=reload_backend,
    ):
        contract = managed.get_managed_update_contract("t3code")
        assert contract is not None
        contract.preflight(plugin_name="t3code", plugin_root=root)
        attestation = contract.complete(
            plugin_name="t3code",
            plugin_root=root,
            source_commit="a" * 40,
            product_version="2.0",
        )
        rollback = contract.rollback(
            plugin_name="t3code",
            plugin_root=root,
            source_commit="b" * 40,
            product_version="1.0",
        )

    assert attestation == {
        "reloaded": True,
        "loaded_source_commit": "a" * 40,
        "loaded_product_version": "2.0",
    }
    assert rollback == {
        "reloaded": True,
        "loaded_source_commit": "b" * 40,
        "loaded_product_version": "1.0",
    }
    preflight.assert_called_once_with("t3code", root.resolve())
    assert reload_backend.call_args_list[0].args == (
        "t3code",
        root.resolve(),
        "a" * 40,
        "2.0",
    )
    assert reload_backend.call_args_list[1].args == (
        "t3code",
        root.resolve(),
        "b" * 40,
        "1.0",
    )


def test_handoff_remounts_every_live_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    reloads = [Mock(), Mock()]

    def attester(index: int):
        def attest(
            _name: str,
            _root: Path,
            source_commit: str,
            product_version: str | None,
        ) -> dict[str, object]:
            reloads[index](source_commit, product_version)
            return {
                "reloaded": True,
                "loaded_source_commit": source_commit,
                "loaded_product_version": product_version,
            }

        return attest

    with (
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=attester(0),
        ),
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=attester(1),
        ),
    ):
        contract = managed.get_managed_update_contract("t3code")
        assert contract is not None
        result = contract.complete(
            plugin_name="t3code",
            plugin_root=root,
            source_commit="a" * 40,
            product_version="2.0",
        )

    assert result["loaded_source_commit"] == "a" * 40
    for reload_backend in reloads:
        reload_backend.assert_called_once_with("a" * 40, "2.0")


def test_coordinator_rejects_unauthenticated_and_caller_spoofed_attestation(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))

    with managed.ManagedUpdateCoordinator(
        preflight=lambda _name, _root: None,
        reload_backend=lambda *_args: {
            "reloaded": True,
            "loaded_source_commit": "b" * 40,
            "loaded_product_version": "old",
        },
    ):
        descriptor_path = next(
            (home / "runtime" / "managed-plugin-update-hosts").glob("*.json")
        )
        descriptor = managed._read_descriptor(descriptor_path)
        with pytest.raises(
            managed.ManagedPluginUpdateError,
            match="did not respond",
        ):
            managed._ipc_call_one(
                {**descriptor, "authkey": b"x" * 32},
                {
                    "operation": "preflight",
                    "plugin_name": "t3code",
                    "requested_plugin_name": "t3code",
                    "plugin_root": str(root),
                },
            )

        contract = managed.get_managed_update_contract("t3code")
        assert contract is not None
        with pytest.raises(
            managed.ManagedPluginUpdateError,
            match="could not attest",
        ):
            contract.complete(
                plugin_name="t3code",
                plugin_root=root,
                source_commit="a" * 40,
                product_version="2.0",
            )


def test_stalled_local_client_cannot_block_authenticated_preflight(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(managed, "_HANDSHAKE_TIMEOUT_SECONDS", 0.2)

    with managed.ManagedUpdateCoordinator(
        preflight=lambda _name, _root: None,
        reload_backend=Mock(),
    ):
        descriptor_path = next(
            (home / "runtime" / "managed-plugin-update-hosts").glob("*.json")
        )
        descriptor = managed._read_descriptor(descriptor_path)
        stalled = socket.create_connection(descriptor["address"], timeout=1)
        try:
            contract = managed.get_managed_update_contract("t3code")
            assert contract is not None
            started = time.monotonic()
            contract.preflight(plugin_name="t3code", plugin_root=root)
            assert time.monotonic() - started < 1
        finally:
            stalled.close()


def test_plugin_update_lock_rejects_contention(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    root = tmp_path / "plugin"
    root.mkdir()

    with managed.plugin_update_lock(root):
        with pytest.raises(
            managed.ManagedPluginUpdateError,
            match="already in progress",
        ):
            with managed.plugin_update_lock(root):
                pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "Hermes Test",
            "GIT_AUTHOR_EMAIL": "hermes-test@example.invalid",
            "GIT_COMMITTER_NAME": "Hermes Test",
            "GIT_COMMITTER_EMAIL": "hermes-test@example.invalid",
        },
    )
    return result.stdout.strip()


def _write_backend(root: Path, implementation: str, delay: float = 0) -> None:
    dashboard = root / "dashboard"
    dashboard.mkdir(parents=True, exist_ok=True)
    (dashboard / "manifest.json").write_text(
        json.dumps(
            {
                "name": "t3code",
                "entry": "dist/index.js",
                "api": "plugin_api.py",
            }
        ),
        encoding="utf-8",
    )
    (dashboard / "plugin_api.py").write_text(
        "import time\n"
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        f"IMPLEMENTATION = {implementation!r}\n"
        f"DELAY = {delay!r}\n"
        "@router.get('/identity')\n"
        "def identity():\n"
        "    value = IMPLEMENTATION\n"
        "    time.sleep(DELAY)\n"
        "    return {'implementation': value}\n",
        encoding="utf-8",
    )


def _write_state(home: Path, source_commit: str, version: str) -> None:
    state = home / "t3code" / "service-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "desired_state": "installed",
                "product_source_commit": source_commit,
                "product_version": version,
            }
        ),
        encoding="utf-8",
    )


def test_complete_and_rollback_swap_live_routes_with_exact_attestation(
    tmp_path, monkeypatch, request
):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from hermes_cli import web_server

    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    _write_managed_manifest(root)
    _write_backend(root, "old", delay=0.2)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "old")
    old_commit = _git(root, "rev-parse", "HEAD")
    _write_state(home, old_commit, "1.0")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["t3code"], "disabled": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    web_server._dashboard_plugins_cache = None
    managed_row = next(
        row
        for row in web_server._merged_plugins_hub()["plugins"]
        if row["name"] == "t3code"
    )
    assert managed_row["can_update"] is True
    assert managed_row["can_update_git"] is False
    assert managed_row["update_mode"] == "managed"

    test_app = fastapi.FastAPI()
    monkeypatch.setattr(web_server, "app", test_app)
    web_server._dashboard_plugins_cache = None
    web_server._plugin_api_routes.clear()
    web_server._plugin_api_modules.clear()
    web_server._mount_plugin_api_routes()

    @test_app.get("/{path:path}")
    def fallback(path: str):
        return {"fallback": path}

    client = testclient.TestClient(test_app)
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": "old"
    }
    coordinator = managed.ManagedUpdateCoordinator(
        preflight=web_server._preflight_managed_plugin_reload,
        reload_backend=web_server._reload_managed_plugin_backend,
    )
    request.addfinalizer(coordinator.close)
    contract = managed.get_managed_update_contract("t3code")
    assert contract is not None
    contract.preflight(plugin_name="t3code", plugin_root=root)

    state_path = home / "t3code" / "service-state.json"
    state_path.write_text(
        json.dumps({"version": 1, "desired_state": "uninstalled"}),
        encoding="utf-8",
    )
    uninstalled = contract.rollback(
        plugin_name="t3code",
        plugin_root=root,
        source_commit=old_commit,
        product_version=None,
    )
    assert uninstalled == {
        "reloaded": True,
        "loaded_source_commit": old_commit,
        "loaded_product_version": None,
    }
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": "old"
    }

    stale_response: dict[str, object] = {}

    def call_old_route() -> None:
        stale_response.update(client.get("/api/plugins/t3code/identity").json())

    stale_request = threading.Thread(target=call_old_route)
    stale_request.start()
    time.sleep(0.05)
    _write_backend(root, "new")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "new")
    new_commit = _git(root, "rev-parse", "HEAD")
    _write_state(home, new_commit, "2.0")

    complete = contract.complete(
        plugin_name="t3code",
        plugin_root=root,
        source_commit=new_commit,
        product_version="2.0",
    )
    stale_request.join(timeout=2)

    assert stale_response == {"implementation": "old"}
    assert complete == {
        "reloaded": True,
        "loaded_source_commit": new_commit,
        "loaded_product_version": "2.0",
    }
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": "new"
    }

    _git(root, "checkout", "-q", old_commit)
    _write_state(home, old_commit, "1.0")
    rollback = contract.rollback(
        plugin_name="t3code",
        plugin_root=root,
        source_commit=old_commit,
        product_version="1.0",
    )

    assert rollback == {
        "reloaded": True,
        "loaded_source_commit": old_commit,
        "loaded_product_version": "1.0",
    }
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": "old"
    }
