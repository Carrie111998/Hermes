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
