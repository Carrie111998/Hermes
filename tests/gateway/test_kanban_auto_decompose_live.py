"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.kanban_watchers import _resolve_auto_decompose_settings
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _reset_config_caches(config_module, managed_scope=None):
    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()
    config_module._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_module._LKG_CONFIG_CACHE_PATHS.clear()
    config_module._STRICT_CURRENT_CONFIG_DIGESTS.clear()
    config_module._STRICT_CURRENT_MANAGED_DIGESTS.clear()
    config_module._STRICT_CURRENT_CACHE_ENTRIES.clear()
    if managed_scope is not None:
        managed_scope.invalidate_managed_cache()


def test_generated_default_config_is_manual():
    assert DEFAULT_CONFIG["kanban"]["auto_decompose"] is False


def test_disabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is False
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_explicit_true_opts_in():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {
            "kanban": {
                "auto_decompose": True,
                "auto_decompose_per_tick": 5,
            }
        }
    )
    assert enabled is True
    assert per_tick == 5


def test_config_read_failure_fails_closed():
    def fail_to_load():
        raise OSError("synthetic config read failure")

    assert _resolve_auto_decompose_settings(fail_to_load) == (False, 3)


@pytest.mark.parametrize(
    "config",
    [
        {"kanban": "bad"},
        {"kanban": []},
        {"kanban": {"auto_decompose": "false"}},
        {"kanban": {"auto_decompose": "true"}},
        {"kanban": {"auto_decompose": 1}},
        {"kanban": {"auto_decompose": 0}},
    ],
)
def test_only_exact_boolean_true_enables_auto_decompose(config):
    assert _resolve_auto_decompose_settings(lambda: config) == (False, 3)


@pytest.mark.parametrize("value", ["9", True, 1.5, {}, [], None])
def test_malformed_per_tick_uses_safe_default(value):
    assert _resolve_auto_decompose_settings(
        lambda: {
            "kanban": {
                "auto_decompose": True,
                "auto_decompose_per_tick": value,
            }
        }
    ) == (True, 3)


@pytest.mark.parametrize("value", [0, -4])
def test_integer_per_tick_is_clamped_to_one(value):
    assert _resolve_auto_decompose_settings(
        lambda: {
            "kanban": {
                "auto_decompose": True,
                "auto_decompose_per_tick": value,
            }
        }
    ) == (True, 1)


def test_real_loader_rejects_lkg_when_current_config_is_invalid(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_mod._LKG_CONFIG_CACHE_PATHS.clear()
    config_mod._STRICT_CURRENT_CONFIG_DIGESTS.clear()

    config_path.write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    loader = config_mod.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 9)

    # Invalid YAML must not inherit the prior enabled last-known-good config.
    config_path.write_text("kanban:\n  auto_decompose: [\n", encoding="utf-8")
    assert config_mod.load_config()["kanban"]["auto_decompose"] is True
    assert _resolve_auto_decompose_settings(loader) == (False, 3)

    # Structurally invalid but parseable YAML can also make load_config() take
    # its LKG branch; the strict-current loader must reject that path as well.
    config_path.write_text(
        "max_turns: 3\nagent: malformed\nkanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    assert config_mod.load_config()["kanban"]["auto_decompose"] is True
    assert _resolve_auto_decompose_settings(loader) == (False, 3)

    config_path.write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 7\n",
        encoding="utf-8",
    )
    assert _resolve_auto_decompose_settings(loader) == (False, 7)


def test_strict_current_loader_bypasses_lkg_provenance_cache(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    from hermes_cli import config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_mod._LKG_CONFIG_CACHE_PATHS.clear()
    config_mod._STRICT_CURRENT_CONFIG_DIGESTS.clear()
    config_mod._STRICT_CURRENT_CACHE_ENTRIES.clear()

    loader = config_mod.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 9)

    path_key = str(config_path)
    with config_mod._CONFIG_LOCK:
        cached = config_mod._LOAD_CONFIG_CACHE[path_key]
        stale_value = deepcopy(cached[4])
        stale_value["kanban"]["auto_decompose"] = False
        stale_value["kanban"]["auto_decompose_per_tick"] = 3
        config_mod._LOAD_CONFIG_CACHE[path_key] = (
            cached[0],
            cached[1],
            cached[2],
            cached[3],
            stale_value,
            cached[5],
        )
        config_mod._LKG_CONFIG_CACHE_PATHS.add(path_key)
        # Neutralize the newer cache-identity defense: this probe must prove
        # that the LKG marker independently bypasses a cache tuple even when it
        # appears to be the last strictly authorized entry.
        config_mod._STRICT_CURRENT_CACHE_ENTRIES[path_key] = (
            None,
            config_mod._LOAD_CONFIG_CACHE[path_key],
        )

    # Current bytes are unchanged and valid. Cache provenance—not file metadata
    # or content digest—is what requires the strict loader to bypass this entry.
    assert _resolve_auto_decompose_settings(loader) == (True, 9)
    assert path_key not in config_mod._LKG_CONFIG_CACHE_PATHS


def test_strict_current_loader_rechecks_readability_on_cache_hit(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_mod._LKG_CONFIG_CACHE_PATHS.clear()
    config_mod._STRICT_CURRENT_CONFIG_DIGESTS.clear()

    loader = config_mod.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 4)

    real_open = open

    def deny_config_read(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file) == config_path and "r" in mode:
            raise PermissionError("synthetic unreadable current config")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny_config_read)
    assert _resolve_auto_decompose_settings(loader) == (False, 3)


def test_strict_current_loader_fails_closed_for_managed_overlay(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_mod
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    managed_path.write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 6\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_mod._LKG_CONFIG_CACHE_PATHS.clear()
    config_mod._STRICT_CURRENT_CONFIG_DIGESTS.clear()
    config_mod._STRICT_CURRENT_MANAGED_DIGESTS.clear()
    managed_scope.invalidate_managed_cache()

    loader = config_mod.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 6)

    managed_path.write_text("kanban:\n  auto_decompose: [\n", encoding="utf-8")
    assert _resolve_auto_decompose_settings(loader) == (False, 3)

    managed_path.write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 6\n",
        encoding="utf-8",
    )
    assert _resolve_auto_decompose_settings(loader) == (True, 6)

    real_open = open

    def deny_managed_read(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file) == managed_path and "r" in mode:
            raise PermissionError("synthetic unreadable managed config")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny_managed_read)
    assert _resolve_auto_decompose_settings(loader) == (False, 3)


def test_strict_user_content_change_with_same_metadata_is_observed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module

    home = tmp_path / ".hermes-same-user"
    home.mkdir()
    config_path = home / "config.yaml"
    old = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 4\n"
    new = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 4\n"
    assert len(old.encode("utf-8")) == len(new.encode("utf-8"))
    config_path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)

    loader = config_module.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 4)
    original_stat = config_path.stat()
    config_path.write_text(new, encoding="utf-8")
    os.utime(
        config_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    current_stat = config_path.stat()
    assert (current_stat.st_mtime_ns, current_stat.st_size) == (
        original_stat.st_mtime_ns,
        original_stat.st_size,
    )
    assert _resolve_auto_decompose_settings(loader) == (False, 4)


def test_first_strict_user_read_bypasses_same_metadata_ordinary_cache(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module

    home = tmp_path / ".hermes-first-strict-user"
    home.mkdir()
    config_path = home / "config.yaml"
    old = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 8\n"
    new = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 8\n"
    assert len(old.encode("utf-8")) == len(new.encode("utf-8"))
    config_path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)

    assert config_module.load_config()["kanban"]["auto_decompose"] is True
    original_stat = config_path.stat()
    config_path.write_text(new, encoding="utf-8")
    os.utime(
        config_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 8)


def test_strict_user_digest_aba_rejects_newer_ordinary_cache(tmp_path, monkeypatch):
    from hermes_cli import config as config_module

    home = tmp_path / ".hermes-user-digest-aba"
    home.mkdir()
    config_path = home / "config.yaml"
    disabled = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 8\n"
    enabled = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 8\n"
    assert len(disabled.encode("utf-8")) == len(enabled.encode("utf-8"))
    config_path.write_text(disabled, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)

    strict_loader = config_module.load_config_strict_current
    assert _resolve_auto_decompose_settings(strict_loader) == (False, 8)

    first_stat = config_path.stat()
    config_path.write_text(enabled, encoding="utf-8")
    enabled_mtime = first_stat.st_mtime_ns + 2_000_000_000
    os.utime(config_path, ns=(first_stat.st_atime_ns, enabled_mtime))
    assert config_module.load_config()["kanban"]["auto_decompose"] is True

    enabled_stat = config_path.stat()
    config_path.write_text(disabled, encoding="utf-8")
    os.utime(
        config_path,
        ns=(enabled_stat.st_atime_ns, enabled_stat.st_mtime_ns),
    )
    assert _resolve_auto_decompose_settings(strict_loader) == (False, 8)


def test_strict_managed_content_change_with_same_metadata_is_observed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-same-managed"
    managed = tmp_path / "managed-same"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    old = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 6\n"
    new = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 6\n"
    assert len(old.encode("utf-8")) == len(new.encode("utf-8"))
    managed_path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    loader = config_module.load_config_strict_current
    assert _resolve_auto_decompose_settings(loader) == (True, 6)
    original_stat = managed_path.stat()
    managed_path.write_text(new, encoding="utf-8")
    os.utime(
        managed_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    current_stat = managed_path.stat()
    assert (current_stat.st_mtime_ns, current_stat.st_size) == (
        original_stat.st_mtime_ns,
        original_stat.st_size,
    )
    assert _resolve_auto_decompose_settings(loader) == (False, 6)


def test_first_strict_managed_read_bypasses_same_metadata_ordinary_cache(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-first-strict-managed"
    managed = tmp_path / "managed-first-strict"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    old = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 9\n"
    new = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n"
    assert len(old.encode("utf-8")) == len(new.encode("utf-8"))
    managed_path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    assert config_module.load_config()["kanban"]["auto_decompose"] is True
    original_stat = managed_path.stat()
    managed_path.write_text(new, encoding="utf-8")
    os.utime(
        managed_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 9)


def test_first_strict_read_invalidates_standalone_managed_cache(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-standalone-managed"
    managed = tmp_path / "managed-standalone"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    old = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 9\n"
    new = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n"
    assert len(old.encode("utf-8")) == len(new.encode("utf-8"))
    managed_path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    standalone = managed_scope.load_managed_config()
    assert standalone["kanban"]["auto_decompose"] is True
    assert not config_module._LOAD_CONFIG_CACHE
    original_stat = managed_path.stat()
    managed_path.write_text(new, encoding="utf-8")
    os.utime(
        managed_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 9)


def test_strict_managed_digest_aba_rejects_newer_ordinary_cache(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-managed-digest-aba"
    managed = tmp_path / "managed-digest-aba"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    disabled = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n"
    enabled = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 9\n"
    assert len(disabled.encode("utf-8")) == len(enabled.encode("utf-8"))
    managed_path.write_text(disabled, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    strict_loader = config_module.load_config_strict_current
    assert _resolve_auto_decompose_settings(strict_loader) == (False, 9)

    first_stat = managed_path.stat()
    managed_path.write_text(enabled, encoding="utf-8")
    enabled_mtime = first_stat.st_mtime_ns + 2_000_000_000
    os.utime(managed_path, ns=(first_stat.st_atime_ns, enabled_mtime))
    assert config_module.load_config()["kanban"]["auto_decompose"] is True

    enabled_stat = managed_path.stat()
    managed_path.write_text(disabled, encoding="utf-8")
    os.utime(
        managed_path,
        ns=(enabled_stat.st_atime_ns, enabled_stat.st_mtime_ns),
    )
    assert _resolve_auto_decompose_settings(strict_loader) == (False, 9)


def test_strict_user_replacement_during_descriptor_read_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module

    home = tmp_path / ".hermes-read-race-user"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n  auto_decompose: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)
    original_stat = config_path.stat()
    real_stat = Path.stat
    real_open = open
    phase = {"replaced": False, "stat_calls": 0}

    class ReplacingReader:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def read(self, *args, **kwargs):
            data = self.inner.read(*args, **kwargs)
            replacement = home / "read-replacement.yaml"
            with real_open(replacement, "w", encoding="utf-8") as output:
                output.write("kanban:\n  auto_decompose: false\n")
            os.replace(replacement, config_path)
            phase["replaced"] = True
            return data

    def replacing_open(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        if Path(file) == config_path and "r" in mode:
            return ReplacingReader(opened)
        return opened

    def phase_specific_stat(path, *args, **kwargs):
        if path == config_path and phase["replaced"]:
            phase["stat_calls"] += 1
            if phase["stat_calls"] > 1:
                # Neutralize the later post-load comparison so this test proves
                # the immediate descriptor/path guard independently.
                return original_stat
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", replacing_open)
    monkeypatch.setattr(Path, "stat", phase_specific_stat)
    with pytest.raises(
        RuntimeError,
        match="config.yaml changed while strict-current read was in progress",
    ):
        config_module.load_config_strict_current()
    assert phase == {"replaced": True, "stat_calls": 1}


def test_strict_managed_replacement_during_descriptor_read_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-read-race-managed"
    managed = tmp_path / "managed-read-race"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    managed_path.write_text(
        "kanban:\n  auto_decompose: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)
    original_stat = managed_path.stat()
    real_stat = Path.stat
    real_open = open
    phase = {"replaced": False, "stat_calls": 0}

    class ReplacingReader:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def read(self, *args, **kwargs):
            data = self.inner.read(*args, **kwargs)
            replacement = managed / "read-replacement.yaml"
            with real_open(replacement, "w", encoding="utf-8") as output:
                output.write("kanban:\n  auto_decompose: false\n")
            os.replace(replacement, managed_path)
            phase["replaced"] = True
            return data

    def replacing_open(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        if Path(file) == managed_path and "r" in mode:
            return ReplacingReader(opened)
        return opened

    def phase_specific_stat(path, *args, **kwargs):
        if path == managed_path and phase["replaced"]:
            phase["stat_calls"] += 1
            if phase["stat_calls"] > 1:
                # As above, keep the post-load guard from masking this phase.
                return original_stat
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", replacing_open)
    monkeypatch.setattr(Path, "stat", phase_specific_stat)
    with pytest.raises(
        RuntimeError,
        match="managed config.yaml changed during strict-current read",
    ):
        config_module.load_config_strict_current()
    assert phase == {"replaced": True, "stat_calls": 1}


def test_strict_user_replacement_during_load_fails_closed(tmp_path, monkeypatch):
    from hermes_cli import config as config_module

    home = tmp_path / ".hermes-race-user"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n  auto_decompose: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)
    original_loader = config_module._load_config_impl

    def replacing_loader(*, want_deepcopy, allow_last_known_good):
        result = original_loader(
            want_deepcopy=want_deepcopy,
            allow_last_known_good=allow_last_known_good,
        )
        replacement = home / "replacement.yaml"
        replacement.write_text(
            "kanban:\n  auto_decompose: false\n",
            encoding="utf-8",
        )
        os.replace(replacement, config_path)
        return result

    monkeypatch.setattr(config_module, "_load_config_impl", replacing_loader)
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 3)


def test_strict_managed_replacement_during_load_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / ".hermes-race-managed"
    managed = tmp_path / "managed-race"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n",
        encoding="utf-8",
    )
    managed_path = managed / "config.yaml"
    managed_path.write_text(
        "kanban:\n  auto_decompose: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)
    original_loader = config_module._load_config_impl

    def replacing_loader(*, want_deepcopy, allow_last_known_good):
        result = original_loader(
            want_deepcopy=want_deepcopy,
            allow_last_known_good=allow_last_known_good,
        )
        replacement = managed / "replacement.yaml"
        replacement.write_text(
            "kanban:\n  auto_decompose: false\n",
            encoding="utf-8",
        )
        os.replace(replacement, managed_path)
        return result

    monkeypatch.setattr(config_module, "_load_config_impl", replacing_loader)
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 3)


@pytest.mark.parametrize("invalid_root", ["false\n", "0\n", "[]\n", '\"\"\n', "null\n"])
def test_strict_current_rejects_falsy_non_mapping_user_root(
    tmp_path, monkeypatch, invalid_root
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / "home-invalid-user-root"
    managed = tmp_path / "managed-enables"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(invalid_root, encoding="utf-8")
    (managed / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    with pytest.raises(TypeError, match="config.yaml root must be a mapping"):
        config_module.load_config_strict_current()
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 3)


@pytest.mark.parametrize("invalid_root", ["false\n", "0\n", "[]\n", '\"\"\n', "null\n"])
def test_strict_current_rejects_falsy_non_mapping_managed_root(
    tmp_path, monkeypatch, invalid_root
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / "home-enables"
    managed = tmp_path / "managed-invalid-root"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 7\n",
        encoding="utf-8",
    )
    (managed / "config.yaml").write_text(invalid_root, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches(config_module, managed_scope)

    with pytest.raises(TypeError, match="managed config.yaml root must be a mapping"):
        config_module.load_config_strict_current()
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 3)


def test_strict_current_binds_merged_cache_to_managed_source_path(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import kanban_diagnostics
    from hermes_cli import managed_scope
    from plugins.kanban.dashboard import plugin_api

    home = tmp_path / "home-managed-path-aba"
    managed_enabled = tmp_path / "managed-enabled"
    managed_disabled = tmp_path / "managed-disabled"
    for directory in (home, managed_enabled, managed_disabled):
        directory.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    enabled = "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 9\n"
    disabled = "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n"
    assert len(enabled.encode("utf-8")) == len(disabled.encode("utf-8"))
    enabled_path = managed_enabled / "config.yaml"
    disabled_path = managed_disabled / "config.yaml"
    enabled_path.write_text(enabled, encoding="utf-8")
    disabled_path.write_text(disabled, encoding="utf-8")
    shared_mtime_ns = 1_700_000_000_000_000_000
    for path in (enabled_path, disabled_path):
        stat_result = path.stat()
        os.utime(path, ns=(stat_result.st_atime_ns, shared_mtime_ns))

    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_config_caches(config_module, managed_scope)
    strict_loader = config_module.load_config_strict_current

    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_disabled))
    assert _resolve_auto_decompose_settings(strict_loader) == (False, 9)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_enabled))
    assert _resolve_auto_decompose_settings(strict_loader) == (True, 9)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_disabled))

    assert _resolve_auto_decompose_settings(strict_loader) == (False, 9)
    assert plugin_api.get_orchestration_settings()["auto_decompose"] is False
    diagnostic_config = kanban_diagnostics.load_runtime_diagnostics_config()
    diagnostic_status = kanban_diagnostics.triage_aux_status(diagnostic_config)
    assert diagnostic_status is not None
    assert diagnostic_status["auto_decompose"] is False


def test_strict_user_source_path_change_during_load_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module

    home_disabled = tmp_path / "home-source-disabled"
    home_enabled = tmp_path / "home-source-enabled"
    home_disabled.mkdir()
    home_enabled.mkdir()
    (home_disabled / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (home_enabled / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home_disabled))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)
    original_loader = config_module._load_config_impl

    def changing_source_loader(*, want_deepcopy, allow_last_known_good):
        monkeypatch.setenv("HERMES_HOME", str(home_enabled))
        return original_loader(
            want_deepcopy=want_deepcopy,
            allow_last_known_good=allow_last_known_good,
        )

    monkeypatch.setattr(config_module, "_load_config_impl", changing_source_loader)
    with pytest.raises(RuntimeError, match="config source path changed"):
        config_module.load_config_strict_current()


def test_strict_managed_source_path_change_during_load_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import managed_scope

    home = tmp_path / "home-managed-source-race"
    managed_disabled = tmp_path / "managed-source-disabled"
    managed_enabled = tmp_path / "managed-source-enabled"
    for directory in (home, managed_disabled, managed_enabled):
        directory.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (managed_disabled / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (managed_enabled / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_disabled))
    _reset_config_caches(config_module, managed_scope)
    original_loader = config_module._load_config_impl

    def changing_source_loader(*, want_deepcopy, allow_last_known_good):
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_enabled))
        return original_loader(
            want_deepcopy=want_deepcopy,
            allow_last_known_good=allow_last_known_good,
        )

    monkeypatch.setattr(config_module, "_load_config_impl", changing_source_loader)
    with pytest.raises(RuntimeError, match="managed config source path changed"):
        config_module.load_config_strict_current()


@pytest.mark.skipif(os.name == "nt", reason="symlink replacement requires POSIX semantics")
@pytest.mark.parametrize("alias_kind", ["config", "parent"])
def test_strict_user_referent_aba_uses_captured_source(
    tmp_path, monkeypatch, alias_kind
):
    from hermes_cli import config as config_module

    disabled_home = tmp_path / "referent-disabled"
    enabled_home = tmp_path / "referent-enabled"
    disabled_home.mkdir()
    enabled_home.mkdir()
    disabled_text = (
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n"
    )
    enabled_text = (
        "kanban:\n  auto_decompose: true \n  auto_decompose_per_tick: 9\n"
    )
    assert len(enabled_text) == len(disabled_text)
    (disabled_home / "config.yaml").write_text(disabled_text, encoding="utf-8")
    (enabled_home / "config.yaml").write_text(enabled_text, encoding="utf-8")

    selected_home = tmp_path / "referent-selected"
    if alias_kind == "parent":
        selected_home.symlink_to(disabled_home, target_is_directory=True)
    else:
        selected_home.mkdir()
        (selected_home / "config.yaml").symlink_to(
            disabled_home / "config.yaml"
        )

    monkeypatch.setenv("HERMES_HOME", str(selected_home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)
    config_path = selected_home / "config.yaml"
    real_open = open
    config_open_count = 0
    race_fired = False
    race_armed = True

    def referent_aba_open(path, *args, **kwargs):
        nonlocal config_open_count, race_fired
        if Path(path) == config_path:
            config_open_count += 1
            if race_armed and config_open_count == 2:
                race_fired = True
                alias = selected_home if alias_kind == "parent" else config_path
                transient = enabled_home if alias_kind == "parent" else (
                    enabled_home / "config.yaml"
                )
                alias.unlink()
                alias.symlink_to(transient, target_is_directory=alias_kind == "parent")
                opened = real_open(path, *args, **kwargs)
                alias.unlink()
                alias.symlink_to(
                    disabled_home
                    if alias_kind == "parent"
                    else disabled_home / "config.yaml",
                    target_is_directory=alias_kind == "parent",
                )
                return opened
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "open", referent_aba_open, raising=False)

    raced_result = _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    )
    assert raced_result == (False, 9)
    assert config_open_count == 1
    assert race_fired is False

    # The strict reader itself still opens and validates the selected source on
    # every call. Only the ordinary merge pipeline must consume the captured
    # mapping instead of reopening the mutable pathname.
    race_armed = False
    subsequent_result = _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    )
    assert subsequent_result == (False, 9)
    assert config_open_count == 2


def test_strict_user_selector_aba_loads_the_selected_source(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import kanban_diagnostics
    from plugins.kanban.dashboard import plugin_api

    selected = tmp_path / "user-selected-disabled"
    transient = tmp_path / "user-transient-enabled"
    selected.mkdir()
    transient.mkdir()
    (selected / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (transient / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(selected))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches(config_module)
    original_loader = config_module._load_config_impl

    def selector_aba_loader(*, want_deepcopy, allow_last_known_good):
        monkeypatch.setenv("HERMES_HOME", str(transient))
        try:
            return original_loader(
                want_deepcopy=want_deepcopy,
                allow_last_known_good=allow_last_known_good,
            )
        finally:
            monkeypatch.setenv("HERMES_HOME", str(selected))

    monkeypatch.setattr(config_module, "_load_config_impl", selector_aba_loader)
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 9)
    assert plugin_api.get_orchestration_settings()["auto_decompose"] is False
    diagnostic_status = kanban_diagnostics.triage_aux_status(
        kanban_diagnostics.load_runtime_diagnostics_config()
    )
    assert diagnostic_status is not None
    assert diagnostic_status["auto_decompose"] is False


def test_strict_managed_selector_aba_loads_the_selected_source(
    tmp_path, monkeypatch
):
    from hermes_cli import config as config_module
    from hermes_cli import kanban_diagnostics
    from hermes_cli import managed_scope
    from plugins.kanban.dashboard import plugin_api

    home = tmp_path / "managed-selector-home"
    selected = tmp_path / "managed-selected-disabled"
    transient = tmp_path / "managed-transient-enabled"
    for directory in (home, selected, transient):
        directory.mkdir()
    (home / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (selected / "config.yaml").write_text(
        "kanban:\n  auto_decompose: false\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    (transient / "config.yaml").write_text(
        "kanban:\n  auto_decompose: true\n  auto_decompose_per_tick: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(selected))
    _reset_config_caches(config_module, managed_scope)
    original_loader = config_module._load_config_impl

    def selector_aba_loader(*, want_deepcopy, allow_last_known_good):
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(transient))
        try:
            return original_loader(
                want_deepcopy=want_deepcopy,
                allow_last_known_good=allow_last_known_good,
            )
        finally:
            monkeypatch.setenv("HERMES_MANAGED_DIR", str(selected))

    monkeypatch.setattr(config_module, "_load_config_impl", selector_aba_loader)
    assert _resolve_auto_decompose_settings(
        config_module.load_config_strict_current
    ) == (False, 9)
    assert plugin_api.get_orchestration_settings()["auto_decompose"] is False
    diagnostic_status = kanban_diagnostics.triage_aux_status(
        kanban_diagnostics.load_runtime_diagnostics_config()
    )
    assert diagnostic_status is not None
    assert diagnostic_status["auto_decompose"] is False


@pytest.mark.parametrize(
    ("relative_path", "manual_marker", "stale_default_on_marker"),
    [
        (
            "website/docs/user-guide/features/kanban-tutorial.md",
            "Decomposition is manual by default.",
            "By default the dispatcher auto-runs the **decomposer**",
        ),
        (
            "website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/"
            "user-guide/features/kanban-tutorial.md",
            "默认使用 Manual 模式。",
            "默认情况下，dispatcher 会对此处的任务自动运行**分解器**",
        ),
    ],
)
def test_kanban_tutorials_document_manual_default(
    relative_path, manual_marker, stale_default_on_marker
):
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / relative_path).read_text(encoding="utf-8")

    assert manual_marker in text
    assert stale_default_on_marker not in text
