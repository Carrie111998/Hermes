from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
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

    @contextmanager
    def stage(_name, _root):
        yield None, "a" * 40

    monkeypatch.setattr(managed, "stage_managed_update_candidate", stage)
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
    pull.assert_called_once_with(root, "a" * 40)


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


def _write_bootstrap_worker(root: Path, *, fail_after_cutover: bool = False) -> None:
    (root / "update_process.py").write_text(
        """
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


actual = Path(sys.argv[1]).resolve()
if sys.argv[2] != "migrate":
    raise SystemExit("legacy bootstrap did not use migrate")
home = Path(os.environ["HERMES_HOME"])
state_path = home / "t3code" / "service-state.json"
prior = git(actual, "rev-parse", "HEAD")
candidate = git(actual, "rev-parse", "@{upstream}^{commit}")
# Model a real pre-managed install: the native runtime is present, but the
# old integration never wrote service-state.json.  A migration must discover
# and snapshot that runtime identity without mutating anything before
# preflight, then establish canonical state as part of cutover/rollback.
old_version = "1.0"
if os.environ.get("TEST_HERMES_SOURCE_ROOT"):
    sys.path.insert(0, os.environ["TEST_HERMES_SOURCE_ROOT"])
from hermes_cli.managed_plugin_update import get_managed_update_contract

contract = get_managed_update_contract("t3code")
if contract is None:
    raise SystemExit("missing bootstrap contract")
contract.preflight(plugin_name="t3code", plugin_root=actual)
try:
    git(actual, "checkout", "--detach", candidate)
    if FAIL_AFTER_CUTOVER:
        raise RuntimeError("simulated activation failure")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "desired_state": "installed",
                "product_source_commit": candidate,
                "product_version": "2.0",
            }
        )
    )
    attestation = contract.complete(
        plugin_name="t3code",
        plugin_root=actual,
        source_commit=candidate,
        product_version="2.0",
    )
except Exception as error:
    git(actual, "checkout", "--detach", prior)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "desired_state": "installed",
                "product_source_commit": prior,
                "product_version": old_version,
            }
        )
    )
    contract.rollback(
        plugin_name="t3code",
        plugin_root=actual,
        source_commit=prior,
        product_version=old_version,
    )
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
print(
    json.dumps(
        {
            "ok": True,
            "version": "2.0",
            "source_commit": candidate,
            "attestation": attestation,
        }
    )
)
""".replace("FAIL_AFTER_CUTOVER", repr(fail_after_cutover)),
        encoding="utf-8",
    )


def _legacy_remote(
    tmp_path: Path,
    *,
    fail_after_cutover: bool = False,
    update_dependency: bool = False,
) -> tuple[Path, Path, str, str]:
    seed = tmp_path / "seed"
    seed.mkdir()
    _write_backend(
        seed, "old", delay=0.2, update_dependency=update_dependency
    )
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (seed / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "t3code", "version": "1.0"}),
        encoding="utf-8",
    )
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "legacy")
    prior = _git(seed, "rev-parse", "HEAD")

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    home = tmp_path / "home"
    root = home / "plugins" / "t3code"
    root.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", str(remote), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )

    _write_backend(seed, "new", update_dependency=update_dependency)
    _write_managed_manifest(seed)
    _write_bootstrap_worker(seed, fail_after_cutover=fail_after_cutover)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "managed")
    candidate = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", str(remote), "main")
    return home, root, prior, candidate


def _write_backend(
    root: Path,
    implementation: str,
    delay: float = 0,
    *,
    name: str = "t3code",
    update_dependency: bool = False,
) -> None:
    dashboard = root / "dashboard"
    dashboard.mkdir(parents=True, exist_ok=True)
    (dashboard / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "entry": "dist/index.js",
                "api": "plugin_api.py",
            }
        ),
        encoding="utf-8",
    )
    update_decorator = (
        "@router.post('/update', dependencies=[Depends(require_update_auth)])\n"
        if update_dependency
        else "@router.post('/update')\n"
    )
    (dashboard / "plugin_api.py").write_text(
        "import time\n"
        "from fastapi import APIRouter, Depends, Header, HTTPException\n"
        "router = APIRouter()\n"
        f"IMPLEMENTATION = {implementation!r}\n"
        f"DELAY = {delay!r}\n"
        "@router.get('/identity')\n"
        "def identity():\n"
        "    value = IMPLEMENTATION\n"
        "    time.sleep(DELAY)\n"
        "    return {'implementation': value}\n"
        "def require_update_auth(x_plugin_auth: str = Header(...)):\n"
        "    if x_plugin_auth != 'allowed':\n"
        "        raise HTTPException(status_code=403)\n"
        + update_decorator
        +
        "def runtime_only_update():\n"
        "    return {'runtime_only': IMPLEMENTATION}\n",
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


def test_fetched_unmanaged_target_keeps_exact_ff_only_update(
    tmp_path, monkeypatch
):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from hermes_cli import web_server

    seed = tmp_path / "seed"
    seed.mkdir()
    _write_backend(seed, "old", name="legacy")
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (seed / "plugin.yaml").write_text("name: legacy\nversion: '1'\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "one")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    home = tmp_path / "home"
    root = home / "plugins" / "legacy"
    root.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", str(remote), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_backend(seed, "new", name="legacy")
    (seed / "plugin.yaml").write_text("name: legacy\nversion: '2'\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "two")
    candidate = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", str(remote), "main")
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["legacy"], "disabled": []}})
    )
    web_server._dashboard_plugins_cache = None
    web_server._plugin_api_routes.clear()
    web_server._plugin_api_modules.clear()
    test_app = fastapi.FastAPI()
    test_app.middleware("http")(web_server._managed_plugin_bootstrap_gate)
    monkeypatch.setattr(web_server, "app", test_app)
    web_server._mount_plugin_api_routes()
    client = testclient.TestClient(test_app)

    plugin_update = client.post("/api/plugins/legacy/update")
    assert plugin_update.status_code == 200
    assert plugin_update.json() == {"runtime_only": "old"}
    assert _git(root, "rev-parse", "HEAD") != candidate

    result = plugins_cmd._update_user_plugin("legacy", root)

    assert result["update_mode"] == "git"
    assert _git(root, "rev-parse", "HEAD") == candidate
    assert managed.get_managed_update_spec(root, strict=True) is None


def test_legacy_update_route_with_plugin_auth_is_not_intercepted(
    tmp_path, monkeypatch
):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from hermes_cli import web_server

    home, root, prior_commit, _candidate_commit = _legacy_remote(
        tmp_path, update_dependency=True
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["t3code"], "disabled": []}})
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    web_server._dashboard_plugins_cache = None
    web_server._plugin_api_routes.clear()
    web_server._plugin_api_modules.clear()
    test_app = fastapi.FastAPI()
    test_app.middleware("http")(web_server._managed_plugin_bootstrap_gate)
    monkeypatch.setattr(web_server, "app", test_app)
    web_server._mount_plugin_api_routes()
    client = testclient.TestClient(test_app)

    assert client.post("/api/plugins/t3code/update").status_code == 422
    response = client.post(
        "/api/plugins/t3code/update",
        headers={"x-plugin-auth": "allowed"},
    )
    assert response.json() == {"runtime_only": "old"}
    assert _git(root, "rev-parse", "HEAD") == prior_commit


def test_legacy_managed_candidate_needs_host_before_source_cutover(
    tmp_path, monkeypatch
):
    home, root, prior_commit, candidate_commit = _legacy_remote(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(
        plugins_cmd.PluginOperationError,
        match="coordinator is unavailable",
    ):
        plugins_cmd._update_user_plugin("t3code", root)

    assert _git(root, "rev-parse", "HEAD") == prior_commit
    assert _git(root, "rev-parse", "origin/main") == candidate_commit
    assert managed.get_managed_update_spec(root, strict=True) is None


def test_partial_multi_host_bootstrap_is_ungated_on_preflight_failure(
    tmp_path, monkeypatch
):
    home, root, prior_commit, _candidate_commit = _legacy_remote(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    begun = Mock()
    released = Mock()

    with (
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=Mock(),
            begin_bootstrap=begun,
            release_bootstrap=released,
        ),
        managed.ManagedUpdateCoordinator(
            preflight=Mock(side_effect=RuntimeError("host cannot remount")),
            reload_backend=Mock(),
        ),
    ):
        with pytest.raises(
            plugins_cmd.PluginOperationError,
            match="host cannot remount",
        ):
            plugins_cmd._update_user_plugin("t3code", root)

    assert begun.call_count == released.call_count
    assert _git(root, "rev-parse", "HEAD") == prior_commit


@pytest.mark.live_system_guard_bypass
def test_partial_multi_host_complete_rolls_every_host_back(
    tmp_path, monkeypatch
):
    home, root, prior_commit, candidate_commit = _legacy_remote(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(
        "TEST_HERMES_SOURCE_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    instances = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(
        managed.secrets,
        "token_hex",
        lambda size: next(instances) if size == 16 else "c" * (size * 2),
    )
    calls_a: list[str] = []
    calls_b: list[str] = []
    released_a = Mock()
    released_b = Mock()

    def reload_a(_name, _root, source_commit, product_version):
        calls_a.append(source_commit)
        return {
            "reloaded": True,
            "loaded_source_commit": source_commit,
            "loaded_product_version": product_version,
        }

    def reload_b(_name, _root, source_commit, product_version):
        calls_b.append(source_commit)
        if source_commit == candidate_commit:
            raise RuntimeError("second host could not activate")
        return {
            "reloaded": True,
            "loaded_source_commit": source_commit,
            "loaded_product_version": product_version,
        }

    with (
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=reload_a,
            release_bootstrap=released_a,
        ),
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=reload_b,
            release_bootstrap=released_b,
        ),
    ):
        with pytest.raises(
            plugins_cmd.PluginOperationError,
            match="second host could not activate",
        ):
            plugins_cmd._update_user_plugin("t3code", root)

    assert calls_a == [candidate_commit, prior_commit]
    assert calls_b == [candidate_commit, prior_commit]
    assert released_a.call_count == 1
    assert released_b.call_count == 1
    assert _git(root, "rev-parse", "HEAD") == prior_commit


@pytest.mark.live_system_guard_bypass
def test_partial_multi_host_finalize_is_retriable_without_product_rollback(
    tmp_path, monkeypatch
):
    home, root, prior_commit, candidate_commit = _legacy_remote(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(
        "TEST_HERMES_SOURCE_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    instances = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(
        managed.secrets,
        "token_hex",
        lambda size: next(instances) if size == 16 else "c" * (size * 2),
    )
    released_a = Mock()
    release_attempts = 0

    def release_b(_name, _root):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise RuntimeError("second host release failed")

    def reload(_name, _root, source_commit, product_version):
        return {
            "reloaded": True,
            "loaded_source_commit": source_commit,
            "loaded_product_version": product_version,
        }

    with (
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=reload,
            release_bootstrap=released_a,
        ),
        managed.ManagedUpdateCoordinator(
            preflight=lambda _name, _root: None,
            reload_backend=reload,
            release_bootstrap=release_b,
        ),
    ):
        with pytest.raises(
            plugins_cmd.PluginOperationError,
            match="second host release failed",
        ):
            plugins_cmd._update_user_plugin("t3code", root)

        assert _git(root, "rev-parse", "HEAD") == candidate_commit
        spec = managed.get_managed_update_spec(root, strict=True)
        assert spec is not None
        managed.finalize_managed_update_bootstrap(
            "t3code",
            root,
            managed.ManagedUpdateCandidate(
                source_commit=candidate_commit,
                prior_commit=prior_commit,
                staged_root=root,
                spec=spec,
            ),
        )

    assert released_a.call_count == 1
    assert release_attempts == 2
    assert _git(root, "rev-parse", "HEAD") == candidate_commit


def test_bootstrap_allows_managed_release_behind_staged_tip(
    tmp_path, monkeypatch
):
    home, root, prior_commit, release_commit = _legacy_remote(tmp_path)
    seed = tmp_path / "seed"
    (seed / "tip.txt").write_text("after release\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "post-release tip")
    tip_commit = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", str(tmp_path / "remote.git"), "main")
    _git(root, "fetch", "-q")
    monkeypatch.setenv("HERMES_HOME", str(home))

    def reload(_name, _root, source_commit, product_version):
        return {
            "reloaded": True,
            "loaded_source_commit": source_commit,
            "loaded_product_version": product_version,
        }

    payload = {
        "plugin_name": "t3code",
        "requested_plugin_name": "t3code",
        "plugin_root": str(root),
        "bootstrap_prior_commit": prior_commit,
        "bootstrap_candidate_commit": tip_commit,
        "bootstrap_contract": "t3code-hermes-v1",
    }
    with managed.ManagedUpdateCoordinator(
        preflight=lambda _name, _root: None,
        reload_backend=reload,
    ) as coordinator:
        coordinator._handle({**payload, "operation": "bootstrap_begin"})
        _git(root, "checkout", "--detach", release_commit)
        result = coordinator._handle(
            {
                **payload,
                "operation": "complete",
                "source_commit": release_commit,
                "product_version": "2.0",
            }
        )

    assert result["result"]["loaded_source_commit"] == release_commit


@pytest.mark.parametrize("fail_after_cutover", [False, True])
@pytest.mark.live_system_guard_bypass
def test_legacy_checkout_bootstraps_or_rolls_back_as_one_product(
    tmp_path, monkeypatch, request, fail_after_cutover
):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from hermes_cli import web_server

    home, root, prior_commit, candidate_commit = _legacy_remote(
        tmp_path, fail_after_cutover=fail_after_cutover
    )
    state_path = home / "t3code" / "service-state.json"
    assert not state_path.exists()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["t3code"], "disabled": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(
        "TEST_HERMES_SOURCE_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    web_server._dashboard_plugins_cache = None
    web_server._plugin_api_routes.clear()
    web_server._plugin_api_modules.clear()
    web_server._plugin_api_gated.clear()
    web_server._plugin_api_active.clear()

    test_app = fastapi.FastAPI()
    test_app.middleware("http")(web_server._managed_plugin_bootstrap_gate)
    monkeypatch.setattr(web_server, "app", test_app)
    web_server._mount_plugin_api_routes()
    assert web_server._has_mounted_plugin_backend() is True
    client = testclient.TestClient(test_app)
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": "old"
    }

    coordinator = managed.ManagedUpdateCoordinator(
        preflight=web_server._preflight_managed_plugin_reload,
        reload_backend=web_server._reload_managed_plugin_backend,
        begin_bootstrap=web_server._begin_managed_plugin_bootstrap,
        release_bootstrap=web_server._release_managed_plugin_bootstrap,
    )
    request.addfinalizer(coordinator.close)

    stale_response: dict[str, object] = {}
    stale = threading.Thread(
        target=lambda: stale_response.update(
            client.get("/api/plugins/t3code/identity").json()
        )
    )
    stale.start()
    time.sleep(0.05)

    if fail_after_cutover:
        with pytest.raises(
            plugins_cmd.PluginOperationError,
            match="simulated activation failure",
        ):
            plugins_cmd._update_user_plugin("t3code", root)
    else:
        response = client.post("/api/plugins/t3code/update")
        assert response.status_code == 200
        result = response.json()
        assert result["update_mode"] == "managed"
        assert result["source_commit"] == candidate_commit

    stale.join(timeout=2)
    assert stale_response == {"implementation": "old"}
    expected_commit = prior_commit if fail_after_cutover else candidate_commit
    expected_version = "1.0" if fail_after_cutover else "2.0"
    expected_implementation = "old" if fail_after_cutover else "new"
    assert _git(root, "rev-parse", "HEAD") == expected_commit
    assert web_server._managed_product_identity("t3code", root) == (
        expected_commit,
        expected_version,
    )
    assert client.get("/api/plugins/t3code/identity").json() == {
        "implementation": expected_implementation
    }
    assert "t3code" not in web_server._plugin_api_gated


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
