"""Security contracts for the local WhatsApp bridge dependency tree."""

import json
from pathlib import Path
from types import SimpleNamespace


BRIDGE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"


def _semver(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0]
    major, minor, patch = core.split(".", 2)
    return int(major), int(minor), int(patch)


def test_whatsapp_bridge_pins_patched_parser_dependencies():
    """Keep known DoS/prototype-pollution fixes in the shipped bridge lock."""
    package = json.loads((BRIDGE_DIR / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((BRIDGE_DIR / "package-lock.json").read_text(encoding="utf-8"))

    assert package["overrides"]["body-parser"] == "^2.3.0"
    assert package["overrides"]["protobufjs"] == "^8.7.1"
    assert package["overrides"]["qs"] == "^6.15.2"
    assert package["overrides"]["ws"] == "^8.21.0"
    assert package["dependencies"]["link-preview-js"].startswith("^4.")
    assert "legacy-peer-deps=true" in (BRIDGE_DIR / ".npmrc").read_text(encoding="utf-8").splitlines()
    assert _semver(lock["packages"]["node_modules/body-parser"]["version"]) >= (2, 3, 0)
    assert _semver(lock["packages"]["node_modules/protobufjs"]["version"]) >= (8, 7, 1)
    assert _semver(lock["packages"]["node_modules/qs"]["version"]) >= (6, 15, 2)
    assert _semver(lock["packages"]["node_modules/ws"]["version"]) >= (8, 21, 0)


def test_bridge_logs_allowlist_size_without_exposing_identifiers():
    bridge_source = (BRIDGE_DIR / "bridge.js").read_text(encoding="utf-8")

    assert "Allowed users configured: ${ALLOWED_USERS.size}" in bridge_source
    assert "Array.from(ALLOWED_USERS).join" not in bridge_source


def test_logged_out_message_requires_repair_without_destructive_reset():
    bridge_source = (BRIDGE_DIR / "bridge.js").read_text(encoding="utf-8")

    assert "Run `hermes whatsapp` to pair this device again." in bridge_source
    assert "Delete session" not in bridge_source


def test_whatsapp_enabled_environment_override_is_authoritative(monkeypatch):
    import hermes_cli.gateway as gateway_mod
    from plugins.platforms.whatsapp.adapter import _is_connected

    monkeypatch.setattr(gateway_mod, "get_env_value", lambda key: "true")
    disabled_yaml_config = SimpleNamespace(enabled=False, extra={})

    assert _is_connected(disabled_yaml_config) is True
