"""Config integration tests — managed scope wins over user config at the leaf."""
import os
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


def test_managed_beats_user(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default: managed/model\n")
    assert cfg_get(load_config(), "model", "default") == "managed/model"


def test_managed_list_wins_wholesale(homes):
    """D3: a managed list value replaces the user's wholesale."""
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "toolsets:\n  enabled: [a, b, c]\n")
    _write(managed / "config.yaml", "toolsets:\n  enabled: [x]\n")
    assert cfg_get(load_config(), "toolsets", "enabled") == ["x"]


def test_editing_managed_file_invalidates_cache(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default: managed/v1\n")
    assert cfg_get(load_config(), "model", "default") == "managed/v1"
    _write(managed / "config.yaml", "model:\n  default: managed/v2\n")
    assert cfg_get(load_config(), "model", "default") == "managed/v2"


def test_same_size_managed_edit_with_restored_mtime_invalidates_public_cache(homes):
    from hermes_cli.config import cfg_get, load_config

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    managed_path = managed / "config.yaml"
    _write(managed_path, "model:\n  default: managed/v1\n")
    assert cfg_get(load_config(), "model", "default") == "managed/v1"

    before = managed_path.stat()
    managed_path.write_text("model:\n  default: managed/v2\n", encoding="utf-8")
    os.utime(
        managed_path,
        ns=(before.st_atime_ns, before.st_mtime_ns),
    )
    after = managed_path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    assert cfg_get(load_config(), "model", "default") == "managed/v2"


def test_invalid_user_yaml_reapplies_current_managed_overlay(homes):
    from hermes_cli.config import load_config

    home, managed = homes
    user_path = home / "config.yaml"
    managed_path = managed / "config.yaml"
    _write(user_path, "source: user\n")
    _write(managed_path, "source: managed-old\n")
    assert load_config()["source"] == "managed-old"

    # Do not clear any process cache: this reproduces a long-running gateway
    # while the user is in the middle of an invalid YAML edit.
    user_path.write_text("\tbroken:\n", encoding="utf-8")
    managed_path.write_text("source: managed-new\n", encoding="utf-8")

    assert load_config()["source"] == "managed-new"
    assert load_config()["source"] == "managed-new"

    # Removing a managed pin must reveal the last valid user layer, not retain
    # the old effective overlay that happened to be in the LKG.
    managed_path.unlink()
    assert load_config()["source"] == "user"


def test_save_config_lkg_preserves_env_template_for_future_expansion(
    homes, monkeypatch
):
    """The normalized user LKG must retain ``${VAR}``, not one old expansion."""
    from hermes_cli import config as cfg
    from hermes_cli.config import load_config, save_config

    home, _managed = homes
    user_path = home / "config.yaml"
    env_name = "HERMES_TEST_LKG_TEMPLATE"
    monkeypatch.setenv(env_name, "value-v1")
    _write(user_path, f"lkg_probe: ${{{env_name}}}\n")

    loaded = load_config()
    assert loaded["lkg_probe"] == "value-v1"
    save_config(loaded)

    path_key = str(user_path)
    assert f"${{{env_name}}}" in user_path.read_text(encoding="utf-8")
    assert cfg._LAST_NORMALIZED_USER_CONFIG_BY_PATH[path_key]["lkg_probe"] == (
        f"${{{env_name}}}"
    )

    # While the user file is temporarily malformed, the user LKG is expanded
    # against the *current* environment rather than pinning value-v1 forever.
    user_path.write_text("\tbroken-config:\n", encoding="utf-8")
    monkeypatch.setenv(env_name, "value-v2")
    assert load_config()["lkg_probe"] == "value-v2"


def test_legacy_effective_lkg_is_never_reused_as_user_layer(homes):
    """An old effective cache may contain obsolete managed pins; reject it."""
    from hermes_cli import config as cfg

    home, _managed = homes
    user_path = home / "config.yaml"
    user_path.write_text("\tbroken-config:\n", encoding="utf-8")
    path_key = str(user_path)

    cfg._LAST_NORMALIZED_USER_CONFIG_BY_PATH.pop(path_key, None)
    cfg._LAST_EXPANDED_CONFIG_BY_PATH[path_key] = {
        "source": "obsolete-managed-pin",
    }
    cfg._LOAD_CONFIG_CACHE.clear()
    assert cfg.get_config_path() == user_path
    assert cfg._LAST_NORMALIZED_USER_CONFIG_BY_PATH.get(path_key) is None
    assert cfg._LAST_EXPANDED_CONFIG_BY_PATH[path_key]["source"] == (
        "obsolete-managed-pin"
    )

    loaded = cfg.load_config()
    assert "source" not in loaded


def test_user_cannot_shadow_managed_literal_via_envref(homes, monkeypatch):
    """A managed literal must NOT be expandable via a ${VAR} the user controls.

    The managed value is a plain literal 'managed/locked' with no ${...}, so a
    user-defined env var has nothing to substitute. This asserts the managed
    literal survives verbatim regardless of user env, and that managed wins.
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    monkeypatch.setenv("EVIL", "user/override")
    _write(home / "config.yaml", "model:\n  default: ${EVIL}\n")
    _write(managed / "config.yaml", "model:\n  default: managed/locked\n")
    assert cfg_get(load_config(), "model", "default") == "managed/locked"
