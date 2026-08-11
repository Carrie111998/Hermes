"""Managed MCP overlays resolve inside the selected shared-serve profile."""

from contextlib import contextmanager
import os

import yaml


def _write_yaml(path, value):
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def _clear_config_caches():
    from hermes_cli import managed_scope
    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


@contextmanager
def _profile_scope(home):
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def _setup_scopes(tmp_path, monkeypatch):
    base = tmp_path / "base"
    profiles = tmp_path / "profiles"
    managed = tmp_path / "managed"
    jane = profiles / "jane"
    louis = profiles / "louis"
    jane_managed = managed / "jane"
    louis_managed = managed / "louis"
    for path in (base, jane, louis, jane_managed, louis_managed):
        path.mkdir(parents=True)

    _write_yaml(
        base / "config.yaml",
        {"mcp_servers": {"base-only": {"url": "https://base.example/mcp"}}},
    )
    _write_yaml(
        jane / "config.yaml",
        {"display": {"skin": "jane"}},
    )
    _write_yaml(
        louis / "config.yaml",
        {"mcp_servers": {"louis-local": {"url": "https://louis.local/mcp"}}},
    )
    _write_yaml(
        jane_managed / "config.yaml",
        {
            "mcp_servers": {
                "gbrain": {
                    "url": "${GBRAIN_URL}",
                    "headers": {"Authorization": "Bearer ${GBRAIN_TOKEN}"},
                }
            }
        },
    )
    (jane_managed / ".env").write_text(
        "GBRAIN_URL=https://jane.gbrain.example/mcp\n"
        "GBRAIN_TOKEN=jane-managed-token\n",
        encoding="utf-8",
    )
    _write_yaml(louis_managed / "config.yaml", {"display": {"skin": "managed"}})

    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setenv("EVAOS_HERMES_MANAGED_PROFILE_ROOT", str(managed))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setenv("GBRAIN_URL", "https://process.example/mcp")
    monkeypatch.setenv("GBRAIN_TOKEN", "process-token")
    _clear_config_caches()
    return base, jane, louis


def test_profile_managed_mcp_is_discoverable_with_isolated_env(
    tmp_path, monkeypatch
):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _, jane, _ = _setup_scopes(tmp_path, monkeypatch)
    with _profile_scope(jane):
        assert mcp_startup._has_configured_mcp_servers() is True
        servers = _load_mcp_config()

    assert set(servers) == {"gbrain"}
    assert servers["gbrain"]["url"] == "https://jane.gbrain.example/mcp"
    assert servers["gbrain"]["headers"]["Authorization"] == (
        "Bearer jane-managed-token"
    )
    assert os.environ["GBRAIN_URL"] == "https://process.example/mcp"
    assert os.environ["GBRAIN_TOKEN"] == "process-token"


def test_profile_without_managed_mcp_sees_only_profile_config(
    tmp_path, monkeypatch
):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _, _, louis = _setup_scopes(tmp_path, monkeypatch)
    with _profile_scope(louis):
        assert mcp_startup._has_configured_mcp_servers() is True
        servers = _load_mcp_config()

    assert set(servers) == {"louis-local"}


def test_base_scope_mcp_config_is_unchanged(tmp_path, monkeypatch):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _setup_scopes(tmp_path, monkeypatch)
    assert mcp_startup._has_configured_mcp_servers() is True
    assert set(_load_mcp_config()) == {"base-only"}
