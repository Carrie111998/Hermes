import asyncio
from types import SimpleNamespace

from plugins.platforms.whatsapp import adapter


def test_physical_lock_blocks(tmp_path, monkeypatch):
    lock = tmp_path / "locks/whatsapp-outbound.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("locked\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert str(lock) in adapter.whatsapp_outbound_block_reason()


def test_explicit_false_config_blocks_without_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert "configuration" in adapter.whatsapp_outbound_block_reason(
        {"outbound_enabled": False}
    )


def test_missing_policy_preserves_upstream_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert adapter.whatsapp_outbound_block_reason() is None


def test_standalone_sender_fails_before_transport_import(tmp_path, monkeypatch):
    lock = tmp_path / "locks/whatsapp-outbound.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("locked\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = asyncio.run(
        adapter._standalone_send(
            SimpleNamespace(extra={}),
            "0000000000000@s.whatsapp.net",
            "must not send",
        )
    )
    assert "hard-blocked" in result["error"]
