"""Unit tests for hermes_cli.managed_scope (resolver + loaders + key helpers)."""
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest


# ── Directory resolver ───────────────────────────────────────────────────────


def test_subprocess_inherits_managed_scope_test_isolation(tmp_path):
    expected = tmp_path / "managed_scope_absent"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli import managed_scope; "
            "print(managed_scope._DEFAULT_MANAGED_DIR)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(expected)


def test_subprocess_bootstrap_is_silent_without_importable_hermes(tmp_path):
    env = dict(os.environ)
    bootstrap = env["PYTHONPATH"].split(os.pathsep)[0]
    env["PYTHONPATH"] = bootstrap
    result = subprocess.run(
        [sys.executable, "-c", "print('sentinel')"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "sentinel\n"
    assert result.stderr == ""


def test_get_managed_dir_env_override(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert managed_scope.get_managed_dir() == managed



    monkeypatch.setenv("HERMES_MANAGED_DIR", "   ")  # whitespace = unset
    # The shared fixture injects a missing system default.
    assert managed_scope.get_managed_dir() is None


def test_injected_missing_default_is_inert(monkeypatch):
    """The shared test-harness injection keeps the real system scope isolated."""
    from hermes_cli import managed_scope

    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    assert managed_scope.get_managed_dir() is None


def test_pytest_env_var_does_not_disable_fixed_default(tmp_path, monkeypatch):
    """A forged test sentinel must not influence production resolution."""
    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    default_dir.mkdir()
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")
    assert managed_scope.get_managed_dir() == default_dir


def test_absent_override_falls_back_to_default_outside_test(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    default_dir.mkdir()
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "missing"))

    assert managed_scope.get_managed_dir() == default_dir


def test_circular_override_falls_back_to_default_for_config_and_env(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import env_loader, managed_scope

    default_dir = tmp_path / "default-managed"
    default_dir.mkdir()
    (default_dir / "config.yaml").write_text("source: default\n", encoding="utf-8")
    (default_dir / ".env").write_text(
        "MANAGED_SCOPE_LOOP_SENTINEL=managed\n",
        encoding="utf-8",
    )
    circular = tmp_path / "circular"
    circular.symlink_to(circular)

    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        managed_scope._is_trusted_managed_dir,
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(circular))
    monkeypatch.setenv("MANAGED_SCOPE_LOOP_SENTINEL", "user")
    managed_scope.invalidate_managed_cache()

    assert managed_scope.get_managed_dir() == default_dir
    assert managed_scope.load_managed_config() == {"source": "default"}
    env_loader._apply_managed_env()
    assert os.environ["MANAGED_SCOPE_LOOP_SENTINEL"] == "managed"


def test_untrusted_existing_override_falls_back_outside_test(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    override_dir = tmp_path / "user-managed"
    default_dir.mkdir()
    override_dir.mkdir()
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setattr(managed_scope, "_managed_dir_is_trusted", lambda _path: None)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(override_dir))

    assert managed_scope.get_managed_dir() == default_dir


def test_trusted_existing_override_is_selected_outside_test(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    override_dir = tmp_path / "root-managed"
    default_dir.mkdir()
    override_dir.mkdir()
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        lambda path: path.resolve(),
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(override_dir))

    assert managed_scope.get_managed_dir() == override_dir


def test_trusted_managed_dir_rejects_empty_or_world_writable(tmp_path):
    from hermes_cli import managed_scope

    checker = getattr(managed_scope, "_is_trusted_managed_dir")
    managed = tmp_path / "managed"
    managed.mkdir()
    assert not checker(managed)

    (managed / "config.yaml").write_text("{}\n", encoding="utf-8")
    managed.chmod(0o777)
    assert not checker(managed)


def test_trusted_managed_dir_accepts_protected_root_policy_and_rejects_symlink(
    tmp_path,
):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("root ownership contract requires a root test process")

    from hermes_cli import managed_scope

    checker = getattr(managed_scope, "_is_trusted_managed_dir")
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o755)
    marker = managed / ".hermes-managed"
    marker.write_text("hermes-managed-scope-v1\n", encoding="utf-8")
    marker.chmod(0o644)
    config = managed / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    config.chmod(0o644)
    assert checker(managed)

    config.unlink()
    target = tmp_path / "root-policy.yaml"
    target.write_text("{}\n", encoding="utf-8")
    config.symlink_to(target)
    assert not checker(managed)


def test_trusted_managed_dir_rejects_invalid_writable_or_symlinked_marker(
    tmp_path,
):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("root ownership contract requires a root test process")

    from hermes_cli import managed_scope

    checker = managed_scope._is_trusted_managed_dir
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o755)
    (managed / "config.yaml").write_text("{}\n", encoding="utf-8")
    marker = managed / ".hermes-managed"

    marker.write_text("not-authorized\n", encoding="utf-8")
    assert not checker(managed)

    marker.write_text("hermes-managed-scope-v1\n", encoding="utf-8")
    marker.chmod(0o666)
    assert not checker(managed)

    marker.unlink()
    target = tmp_path / "marker-target"
    target.write_text("hermes-managed-scope-v1\n", encoding="utf-8")
    marker.symlink_to(target)
    assert not checker(managed)


def test_load_managed_config_uses_validated_dir_after_symlink_swap(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import managed_scope

    trusted = tmp_path / "trusted"
    attacker = tmp_path / "attacker"
    trusted.mkdir()
    attacker.mkdir()
    (trusted / "config.yaml").write_text("source: trusted\n", encoding="utf-8")
    (attacker / "config.yaml").write_text("source: attacker\n", encoding="utf-8")

    alias = tmp_path / "managed"
    alias.symlink_to(trusted, target_is_directory=True)

    def validate_then_swap(path):
        resolved = path.resolve(strict=True)
        alias.unlink()
        alias.symlink_to(attacker, target_is_directory=True)
        return resolved

    monkeypatch.setattr(managed_scope, "_managed_dir_is_trusted", validate_then_swap)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(alias))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.load_managed_config() == {"source": "trusted"}


def test_load_managed_config_rejects_nonsticky_writable_namespace(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import managed_scope

    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    managed = unsafe_parent / "managed"
    managed.mkdir(mode=0o755)
    (managed / "config.yaml").write_text("source: swapped\n", encoding="utf-8")

    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        lambda path: path.resolve(strict=True),
    )
    monkeypatch.setattr(
        managed_scope,
        "_managed_ancestor_stat_is_trusted",
        lambda value: not value.st_mode & 0o022 or bool(value.st_mode & stat.S_ISVTX),
        raising=False,
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.load_managed_config() == {}


def test_unsafe_override_namespace_falls_back_to_default_policy(
    tmp_path,
    monkeypatch,
):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("root-owned namespace contract requires a root test process")

    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    default_dir.mkdir(mode=0o755)
    (default_dir / "config.yaml").write_text("source: default\n", encoding="utf-8")

    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    override = unsafe_parent / "override"
    override.mkdir(mode=0o755)
    (override / "config.yaml").write_text("source: override\n", encoding="utf-8")

    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        managed_scope._is_trusted_managed_dir,
    )
    monkeypatch.setattr(
        managed_scope,
        "_managed_stat_is_trusted",
        lambda value: value.st_uid == 0 and not value.st_mode & 0o022,
    )
    monkeypatch.setattr(
        managed_scope,
        "_managed_ancestor_stat_is_trusted",
        lambda value: value.st_uid == 0
        and (not value.st_mode & 0o022 or bool(value.st_mode & stat.S_ISVTX)),
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(override))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.get_managed_dir() == default_dir
    assert managed_scope.load_managed_config() == {"source": "default"}


def test_unmarked_root_owned_override_cannot_suppress_default_policy(
    tmp_path,
    monkeypatch,
):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("root-owned authorization contract requires a root test process")

    from hermes_cli import managed_scope

    default_dir = tmp_path / "default-managed"
    default_dir.mkdir(mode=0o755)
    (default_dir / "config.yaml").write_text("locked: true\n", encoding="utf-8")

    decoy = tmp_path / "root-owned-decoy"
    decoy.mkdir(mode=0o755)
    (decoy / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", default_dir)
    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        managed_scope._is_trusted_managed_dir,
    )
    monkeypatch.setattr(
        managed_scope,
        "_managed_stat_is_trusted",
        lambda value: value.st_uid == 0 and not value.st_mode & 0o022,
    )
    monkeypatch.setattr(
        managed_scope,
        "_managed_ancestor_stat_is_trusted",
        lambda value: value.st_uid == 0
        and (not value.st_mode & 0o022 or bool(value.st_mode & stat.S_ISVTX)),
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(decoy))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.get_managed_dir() == default_dir
    assert managed_scope.load_managed_config() == {"locked": True}


def test_load_managed_config_keeps_directory_inode_pinned_during_name_swap(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    alternate = tmp_path / "alternate"
    displaced = tmp_path / "displaced"
    managed.mkdir()
    alternate.mkdir()
    (managed / "config.yaml").write_text("source: trusted\n", encoding="utf-8")
    (alternate / "config.yaml").write_text("source: alternate\n", encoding="utf-8")

    real_open = managed_scope._open_trusted_managed_dir
    swapped = False

    def open_then_swap(path):
        nonlocal swapped
        opened = real_open(path)
        if opened is not None and not swapped:
            managed.rename(displaced)
            alternate.rename(managed)
            swapped = True
        return opened

    monkeypatch.setattr(
        managed_scope,
        "_managed_dir_is_trusted",
        lambda path: path.resolve(strict=True),
    )
    monkeypatch.setattr(managed_scope, "_open_trusted_managed_dir", open_then_swap)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.load_managed_config() == {"source": "trusted"}


def test_load_managed_config_rejects_policy_symlink_swapped_after_validation(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    config = managed / "config.yaml"
    config.write_text("source: trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.yaml"
    attacker.write_text("source: attacker\n", encoding="utf-8")

    def validate_then_swap(path):
        resolved = path.resolve(strict=True)
        config.unlink()
        config.symlink_to(attacker)
        return resolved

    monkeypatch.setattr(managed_scope, "_managed_dir_is_trusted", validate_then_swap)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.load_managed_config() == {}


def test_apply_managed_env_rejects_policy_symlink_swapped_after_validation(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import env_loader, managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    managed_env = managed / ".env"
    managed_env.write_text("MANAGED_SCOPE_RACE_SENTINEL=trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.env"
    attacker.write_text("MANAGED_SCOPE_RACE_SENTINEL=attacker\n", encoding="utf-8")

    def validate_then_swap(path):
        resolved = path.resolve(strict=True)
        managed_env.unlink()
        managed_env.symlink_to(attacker)
        return resolved

    monkeypatch.setattr(managed_scope, "_managed_dir_is_trusted", validate_then_swap)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.delenv("MANAGED_SCOPE_RACE_SENTINEL", raising=False)
    managed_scope.invalidate_managed_cache()

    env_loader._apply_managed_env()

    assert "MANAGED_SCOPE_RACE_SENTINEL" not in os.environ


# ── Loaders + key helpers ────────────────────────────────────────────────────


def _write_managed(tmp_path, monkeypatch, *, config=None, env=None):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    if config is not None:
        (managed / "config.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    if env is not None:
        (managed / ".env").write_text(textwrap.dedent(env), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed








def test_load_managed_env_and_is_env_managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(
        tmp_path, monkeypatch, env="OPENAI_API_BASE=https://org.example/v1\n"
    )
    assert managed_scope.load_managed_env() == {
        "OPENAI_API_BASE": "https://org.example/v1"
    }
    assert managed_scope.is_env_managed("OPENAI_API_BASE") is True
    assert managed_scope.is_env_managed("OTHER") is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_load_managed_config_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    os.mkfifo(managed / "config.yaml")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    assert managed_scope.load_managed_config() == {}


def test_editing_managed_config_invalidates_cache(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = _write_managed(tmp_path, monkeypatch, config="model:\n  default: v1\n")
    assert managed_scope.load_managed_config()["model"]["default"] == "v1"
    (managed / "config.yaml").write_text("model:\n  default: v2\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    assert managed_scope.load_managed_config()["model"]["default"] == "v2"


def test_cache_detects_same_size_edit_with_restored_mtime(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = _write_managed(tmp_path, monkeypatch, config="source: one\n")
    config = managed / "config.yaml"
    original = config.stat()
    assert managed_scope.load_managed_config() == {"source": "one"}

    time.sleep(1.01)
    config.write_text("source: two\n", encoding="utf-8")
    os.utime(config, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert config.stat().st_ctime_ns != original.st_ctime_ns

    assert managed_scope.load_managed_config() == {"source": "two"}


def test_managed_dir_env_scrubbed_by_default():
    """conftest must scrub HERMES_MANAGED_DIR so a dev-shell value can't leak in."""
    import os

    assert "HERMES_MANAGED_DIR" not in os.environ
