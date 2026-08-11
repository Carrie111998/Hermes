"""Behavior coverage for directory-plugin pre-import integrity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugin_integrity import (
    PluginIntegrityError,
    evidence_path,
    record_plugin_entrypoint,
    verified_entrypoint_bytes,
)
from hermes_cli.plugins import PluginManager, PluginManifest


def _write_plugin(home: Path, name: str = "guarded") -> Path:
    plugin = home / "plugins" / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "1.0.0"}),
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n"
        "    from pathlib import Path\n"
        "    Path(__file__).with_name('registered').write_text('yes')\n",
        encoding="utf-8",
    )
    return plugin


def _enable_integrity(home: Path, name: str = "guarded") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {"_config_version": 35, "plugins": {"enabled": [name]}}
        ),
        encoding="utf-8",
    )


def test_clean_verified_fixture_imports_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    _enable_integrity(home)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    record_plugin_entrypoint("guarded", plugin)

    manager = PluginManager()
    manager.discover_and_load()

    assert manager._plugins["guarded"].enabled is True
    assert (plugin / "registered").read_text(encoding="utf-8") == "yes"


@pytest.mark.parametrize("source", ["user", "project"])
def test_missing_evidence_rejects_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    _enable_integrity(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manifest = PluginManifest(
        name="guarded", key="guarded", source=source, path=str(plugin)
    )

    with pytest.raises(PluginIntegrityError, match="missing plugin integrity evidence"):
        PluginManager()._load_directory_module(manifest)

    assert not (plugin / "registered").exists()


def test_tampered_entrypoint_never_executes_in_subprocess(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    _enable_integrity(home)
    previous_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        record_plugin_entrypoint("guarded", plugin)
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home

    sentinel = plugin / "sentinel"
    (plugin / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('sentinel').write_text('executed')\n"
        "def register(ctx):\n"
        "    Path(__file__).with_name('registered').write_text('yes')\n",
        encoding="utf-8",
    )
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_BUNDLED_PLUGINS": str(bundled),
        "PYTHONPATH": os.pathsep.join(
            value
            for value in [str(Path.cwd()), os.environ.get("PYTHONPATH", "")]
            if value
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.plugins import PluginManager; "
            "m=PluginManager(); m.discover_and_load(); "
            "p=m._plugins['guarded']; "
            "assert not p.enabled; assert 'integrity mismatch' in (p.error or '')",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert not (plugin / "registered").exists()


def _write_evidence(home: Path, plugins: list[dict[str, str]]) -> None:
    store = home / "plugin-integrity" / "directory-plugins.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"version": 1, "plugins": plugins}),
        encoding="utf-8",
    )


def _valid_record(plugin: Path) -> dict[str, str]:
    import hashlib

    return {
        "key": "guarded",
        "path": str(plugin.resolve()),
        "entrypoint": "__init__.py",
        "sha256": hashlib.sha256((plugin / "__init__.py").read_bytes()).hexdigest(),
    }


def test_duplicate_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    record = _valid_record(plugin)
    _write_evidence(home, [record, dict(record)])

    with pytest.raises(PluginIntegrityError, match="duplicate plugin integrity evidence"):
        verified_entrypoint_bytes("guarded", plugin)


def test_unsafe_evidence_entrypoint_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    record = _valid_record(plugin)
    record["entrypoint"] = "../outside.py"
    _write_evidence(home, [record])

    with pytest.raises(PluginIntegrityError, match="unsafe entrypoint"):
        verified_entrypoint_bytes("guarded", plugin)


def test_non_regular_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = evidence_path()
    store.mkdir(parents=True)

    with pytest.raises(PluginIntegrityError, match="non-regular"):
        verified_entrypoint_bytes("guarded", plugin)


def test_symlinked_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = tmp_path / "outside-evidence.json"
    target.write_text(
        json.dumps({"version": 1, "plugins": [_valid_record(plugin)]}),
        encoding="utf-8",
    )
    store = evidence_path()
    store.parent.mkdir(parents=True)
    try:
        store.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable on this host: {exc}")

    with pytest.raises(PluginIntegrityError, match="symlinked"):
        verified_entrypoint_bytes("guarded", plugin)


def test_symlinked_entrypoint_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    target = tmp_path / "outside.py"
    target.write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    (plugin / "__init__.py").unlink()
    try:
        (plugin / "__init__.py").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable on this host: {exc}")

    with pytest.raises(PluginIntegrityError, match="symlinked plugin entrypoint"):
        record_plugin_entrypoint("guarded", plugin)


def test_non_regular_entrypoint_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plugin = _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (plugin / "__init__.py").unlink()
    (plugin / "__init__.py").mkdir()

    with pytest.raises(PluginIntegrityError, match="non-regular plugin entrypoint"):
        record_plugin_entrypoint("guarded", plugin)


def test_v35_migration_seeds_existing_user_plugin_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli.config_migrations import _migrate_to_35

    home = tmp_path / "home"
    plugin = _write_plugin(home)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    results: dict[str, list[str]] = {"config_added": [], "warnings": []}

    _migrate_to_35(results, quiet=True)

    assert verified_entrypoint_bytes("guarded", plugin) == (
        plugin / "__init__.py"
    ).read_bytes()
    assert results["warnings"] == []
