"""Unit tests for hermes_storage (no live Mongo required)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml


def test_peer_cert_cn():
    from hermes_storage.mtls import peer_cert_cn

    cert = {
        "subject": (((("countryName", "US"),), (("commonName", "home-pc"),))),
    }
    # getpeercert subject is nested tuples of ((key, value),)
    cert = {"subject": ((("commonName", "home-pc"),),)}
    assert peer_cert_cn(cert) == "home-pc"
    assert peer_cert_cn(None) is None


def test_enroll_code_roundtrip(tmp_path):
    from hermes_storage.enroll_flow import (
        create_enroll_code,
        load_pending,
        mark_used,
        normalize_code,
        redeem_code,
    )

    pending = create_enroll_code(profile="default", name="pc1", ttl_seconds=120, cp=tmp_path)
    assert "-" in pending.code
    loaded = load_pending(pending.code, tmp_path)
    assert loaded is not None
    assert loaded.is_valid()
    redeem_code(pending.code, machine_name="pc1", cp=tmp_path)
    mark_used(pending.code, used_by="pc1", cp=tmp_path)
    used = load_pending(normalize_code(pending.code), tmp_path)
    assert used is not None and used.used is True
    try:
        redeem_code(pending.code, machine_name="x", cp=tmp_path)
        assert False, "should reject used code"
    except ValueError:
        pass


def test_bootstrap_x509_fields(tmp_path, monkeypatch):
    from hermes_storage.bootstrap import load_bootstrap, reset_bootstrap_cache

    reset_bootstrap_cache()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://db:27017/?replicaSet=rs0&tls=true&authMechanism=MONGODB-X509&authSource=%24external",
            "profile": "default",
            "machine_id": "home-pc",
            "auth_mode": "x509",
            "tls": {
                "ca_file": "certs/ca.crt",
                "cert_key_file": "certs/agent.pem",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    cfg = load_bootstrap(force=True)
    assert cfg is not None
    assert cfg.auth_mode == "x509"
    assert cfg.resolved_tls_ca() == (tmp_path / "certs" / "ca.crt").resolve()
    assert cfg.resolved_tls_cert_key() == (tmp_path / "certs" / "agent.pem").resolve()
    reset_bootstrap_cache()


def test_bootstrap_loads_from_file(tmp_path, monkeypatch):
    from hermes_storage.bootstrap import load_bootstrap, reset_bootstrap_cache

    reset_bootstrap_cache()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://localhost:27017",
            "profile": "coder",
            "machine_id": "pc-home",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    cfg = load_bootstrap(force=True)
    assert cfg is not None
    assert cfg.profile == "coder"
    assert cfg.profile_db == "hermes_profile_coder"
    assert cfg.machine_id == "pc-home"
    reset_bootstrap_cache()


def test_is_mongo_mode_false_without_uri(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import is_mongo_mode, reset_bootstrap_cache

    reset_bootstrap_cache()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert is_mongo_mode() is False
    reset_bootstrap_cache()


def test_machine_id_stable():
    from hermes_storage.machine_id import compute_machine_id

    a = compute_machine_id()
    b = compute_machine_id()
    assert a == b
    assert compute_machine_id(override="My PC!") == "My_PC"


def test_overlay_merge_and_extract():
    from hermes_storage.overlay import (
        deep_merge,
        extract_machine_overlay,
        strip_machine_local,
    )

    base = {"model": {"default": "x"}, "terminal": {"timeout": 30, "cwd": "/old"}}
    overlay = {"terminal": {"cwd": "/new"}, "browser": {"cdp_url": "http://127.0.0.1:9222"}}
    merged = deep_merge(base, overlay)
    assert merged["model"]["default"] == "x"
    assert merged["terminal"]["cwd"] == "/new"
    assert merged["terminal"]["timeout"] == 30
    assert merged["browser"]["cdp_url"].startswith("http")

    full = {
        "model": {"default": "y"},
        "terminal": {"cwd": "/home/me", "backend": "local", "timeout": 60},
        "browser": {"headed": True},
        "mcp_servers": {"fs": {"command": "npx"}},
    }
    extracted = extract_machine_overlay(full)
    assert "cwd" in extracted["terminal"]
    assert "timeout" not in extracted["terminal"]
    shared = strip_machine_local(full)
    assert "cwd" not in shared.get("terminal", {})
    assert shared["model"]["default"] == "y"


def test_cluster_prompt_empty_without_mongo(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.cluster import format_cluster_prompt_block
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "nope.yaml"))
    assert format_cluster_prompt_block() == ""


def test_export_local_home(tmp_path):
    from hermes_storage.local.migrate import export_local_home

    (tmp_path / "memories").mkdir()
    (tmp_path / "SOUL.md").write_text("I am Hermes.", encoding="utf-8")
    (tmp_path / "memories" / "MEMORY.md").write_text("fact one", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    payload = export_local_home(tmp_path)
    assert "I am Hermes" in payload["soul"]
    assert payload["memories"]["memory"] == "fact one"
    assert payload["config"]["model"]["default"] == "test"
    assert any(s["name"] == "demo" for s in payload["skills"])


def test_cluster_tools_require_mongo(monkeypatch, tmp_path):
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage
    from tools.cluster_tools import cluster_activate_tool, cluster_status_tool

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import json

    assert json.loads(cluster_status_tool())["success"] is False
    assert json.loads(cluster_activate_tool(target="x"))["success"] is False


def test_require_storage_fails_without_mongo(monkeypatch, tmp_path):
    from hermes_storage import MongoStorageError, require_storage
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(tmp_path / "missing.yaml"))
    with pytest.raises(MongoStorageError):
        require_storage()


def test_sessiondb_mongo_mode_no_sqlite_fallback(monkeypatch, tmp_path):
    """Mongo mode must not open local state.db when the bridge fails."""
    from hermes_storage import MongoStorageError
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=1",
            "profile": "default",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)

    from hermes_storage.bootstrap import load_bootstrap
    assert load_bootstrap(force=True) is not None

    # Force bridge construction to fail after mode is detected.
    import hermes_storage.session_bridge as bridge

    def _boom(*_a, **_k):
        raise RuntimeError("simulated mongo bridge failure")

    monkeypatch.setattr(bridge, "MongoSessionAdapter", _boom)

    from hermes_state import SessionDB

    with pytest.raises(MongoStorageError) as ei:
        SessionDB()
    assert "split" in str(ei.value).lower() or "unavailable" in str(ei.value).lower()
    assert not (tmp_path / "state.db").exists()


def test_mongo_adapter_unknown_method_fail_loud():
    from hermes_storage.session_bridge import MongoSessionAdapter

    adapter = object.__new__(MongoSessionAdapter)
    adapter._mongo_mode = True
    with pytest.raises(AttributeError, match="not ported"):
        adapter.totally_fake_method_xyz()


def test_fail_hard_unreachable_uri(monkeypatch, tmp_path):
    from hermes_storage import MongoStorageError
    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage
    from hermes_state import SessionDB

    reset_bootstrap_cache()
    reset_storage()
    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200",
            "profile": "down",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    reset_bootstrap_cache()

    with pytest.raises(MongoStorageError):
        SessionDB()
    assert not (tmp_path / "state.db").exists()
