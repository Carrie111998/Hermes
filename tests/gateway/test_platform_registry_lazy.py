"""Lazy plugin-platform enumeration on the gateway config path.

Background: ``platform_registry.plugin_entries()`` / ``all_entries()`` call
``_resolve_all()``, which imports every bundled platform adapter — and each of
those imports its platform SDK.  ``gateway/config.py`` called those accessors
three times, so merely loading gateway config imported ~20 platform SDKs the
caller would never use.  Measured 2026-08-11 with HERMES_DISABLE_LAZY_INSTALLS=1:
``_apply_env_overrides`` took 106.75s and produced 11,711 sys.modules entries,
10,055 of them lark_oapi (Feishu).  That blew the 30s per-test cap, and because
``GET /api/status`` awaits the same path it was a production latency defect too.

These tests pin the gate that fixes it.  The load-bearing one is
``test_load_gateway_config_does_not_import_feishu_sdk`` — it is the assertion
that would have caught the original bug.
"""

import os
import sys

import pytest

from gateway.config import (
    _EAGER_PLATFORM_PLUGINS_ENV,
    _candidate_plugin_entries,
    _deferred_platform_may_be_configured,
    _platform_registry_names,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.platform_registry import PlatformEntry, PlatformRegistry


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry(name: str, source: str = "plugin") -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=name.title(),
        adapter_factory=lambda cfg: object(),
        check_fn=lambda: True,
        source=source,
    )


class _Tripwire:
    """Deferred loader that records whether it was ever run."""

    def __init__(self, registry: PlatformRegistry, name: str) -> None:
        self.registry = registry
        self.name = name
        self.ran = False

    def __call__(self) -> None:
        self.ran = True
        self.registry.register(_entry(self.name))


@pytest.fixture
def registry() -> PlatformRegistry:
    return PlatformRegistry()


@pytest.fixture(autouse=True)
def _empty_dotenv(monkeypatch):
    """Keep the gate's .env read hermetic.

    ``_visible_env_keys`` unions ``os.environ`` with the profile's .env file.
    Without this stub, whether a "must stay deferred" assertion holds would
    depend on the developer's real ~/.hermes/.env.  Tests that care about the
    .env path override this with their own ``load_env``.
    """
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_env", lambda: {})


# ── registry: hints round-trip, introspection imports nothing ────────────────


def test_deferred_env_hints_round_trip(registry):
    registry.register_deferred(
        "feishu", lambda: None, env_hints=("FEISHU_APP_ID", "FEISHU_")
    )
    assert registry.deferred_env_hints("feishu") == ("FEISHU_APP_ID", "FEISHU_")


def test_deferred_env_hints_default_empty(registry):
    registry.register_deferred("raft", lambda: None)
    assert registry.deferred_env_hints("raft") == ()
    assert registry.deferred_env_hints("never-registered") == ()


def test_deferred_names_and_hints_resolve_nothing(registry):
    tripwire = _Tripwire(registry, "feishu")
    registry.register_deferred("feishu", tripwire, env_hints=("FEISHU_",))

    assert registry.deferred_names() == ["feishu"]
    assert registry.deferred_env_hints("feishu") == ("FEISHU_",)
    assert registry.loaded_entries() == []
    assert not tripwire.ran, "introspection must not run the deferred loader"


def test_loaded_entries_excludes_pending_but_includes_resolved(registry):
    registry.register(_entry("relay", source="builtin"))
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))

    assert [e.name for e in registry.loaded_entries()] == ["relay"]
    # all_entries() keeps its eager contract for the callers that still want it.
    assert {e.name for e in registry.all_entries()} == {"relay"}


def test_hints_dropped_when_platform_materializes(registry):
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))
    registry.register(_entry("feishu"))
    assert registry.deferred_env_hints("feishu") == ()
    assert registry.deferred_names() == []


def test_unregister_drops_hints(registry):
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))
    registry.unregister("feishu")
    assert registry.deferred_env_hints("feishu") == ()


# ── name enumeration ─────────────────────────────────────────────────────────


def test_platform_registry_names_spans_loaded_and_deferred(registry):
    registry.register(_entry("relay", source="builtin"))
    registry.register(_entry("telegram", source="plugin"))
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))

    plugin_names = _platform_registry_names(registry, plugin_only=True)
    assert set(plugin_names) == {"telegram", "feishu"}
    assert "relay" not in plugin_names

    all_names = _platform_registry_names(registry, plugin_only=False)
    assert set(all_names) == {"relay", "telegram", "feishu"}


def test_platform_registry_names_does_not_resolve(registry):
    tripwire = _Tripwire(registry, "feishu")
    registry.register_deferred("feishu", tripwire, env_hints=("FEISHU_",))
    assert _platform_registry_names(registry, plugin_only=True) == ["feishu"]
    assert not tripwire.ran


# ── the gate ─────────────────────────────────────────────────────────────────


def test_gate_skips_platform_with_no_matching_env(registry, monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    for key in list(os.environ):
        if key.startswith("FEISHU_"):
            monkeypatch.delenv(key, raising=False)
    registry.register_deferred(
        "feishu", lambda: None, env_hints=("FEISHU_APP_ID", "FEISHU_")
    )
    assert not _deferred_platform_may_be_configured(registry, "feishu", set())


def test_gate_resolves_on_exact_env_name(registry, monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_abc123")
    registry.register_deferred(
        "feishu", lambda: None, env_hints=("FEISHU_APP_ID", "FEISHU_")
    )
    assert _deferred_platform_may_be_configured(registry, "feishu", set())


def test_gate_resolves_on_prefix_match_only(registry, monkeypatch):
    """A var the manifest never declared still trips the prefix hint."""
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_xyz")
    registry.register_deferred(
        "feishu", lambda: None, env_hints=("FEISHU_APP_ID", "FEISHU_")
    )
    assert _deferred_platform_may_be_configured(registry, "feishu", set())


def test_gate_reads_the_profile_dotenv_not_just_os_environ(registry, monkeypatch):
    """Regression: credentials that live ONLY in the profile .env still count.

    Plugin ``is_connected`` hooks resolve tokens via
    ``hermes_cli.config.get_env_value``, which falls back to the .env FILE.
    Gating on ``os.environ`` alone stopped Telegram and WhatsApp auto-enabling
    on a normal install — both have their token in .env and nothing in the
    process environment.  Caught by diffing enabled platforms, 2026-08-11.
    """
    import hermes_cli.config as hermes_config

    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    for key in list(os.environ):
        if key.startswith("TELEGRAM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        hermes_config, "load_env", lambda: {"TELEGRAM_BOT_TOKEN": "123:abc"}
    )

    registry.register_deferred(
        "telegram", lambda: None, env_hints=("TELEGRAM_", "TELEGRAM_BOT_TOKEN")
    )
    assert _deferred_platform_may_be_configured(registry, "telegram", set())


def test_gate_survives_an_unreadable_dotenv(registry, monkeypatch):
    import hermes_cli.config as hermes_config

    def _boom():
        raise OSError("unreadable")

    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    monkeypatch.setattr(hermes_config, "load_env", _boom)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")

    registry.register_deferred(
        "telegram", lambda: None, env_hints=("TELEGRAM_", "TELEGRAM_BOT_TOKEN")
    )
    # os.environ still consulted even though the .env read blew up.
    assert _deferred_platform_may_be_configured(registry, "telegram", set())


def test_gate_resolves_when_already_in_config(registry, monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    for key in list(os.environ):
        if key.startswith("FEISHU_"):
            monkeypatch.delenv(key, raising=False)
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))
    assert _deferred_platform_may_be_configured(registry, "feishu", {"feishu"})


def test_gate_fails_open_without_hints(registry, monkeypatch):
    """A manifest that declares no env vars must never be gated out."""
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    registry.register_deferred("raft", lambda: None)
    assert _deferred_platform_may_be_configured(registry, "raft", set())


def test_gate_fails_open_on_error(monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)

    class _Exploding:
        def deferred_env_hints(self, name):
            raise RuntimeError("boom")

    assert _deferred_platform_may_be_configured(_Exploding(), "feishu", set())


def test_escape_hatch_resolves_everything(registry, monkeypatch):
    monkeypatch.setenv(_EAGER_PLATFORM_PLUGINS_ENV, "1")
    for key in list(os.environ):
        if key.startswith("FEISHU_"):
            monkeypatch.delenv(key, raising=False)
    registry.register_deferred("feishu", lambda: None, env_hints=("FEISHU_",))
    assert _deferred_platform_may_be_configured(registry, "feishu", set())


# ── candidate selection end to end ───────────────────────────────────────────


def test_candidate_entries_resolves_only_configured_platforms(registry, monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    for key in list(os.environ):
        if key.startswith(("FEISHU_", "DISCORD_", "TELEGRAM_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")

    feishu = _Tripwire(registry, "feishu")
    discord = _Tripwire(registry, "discord")
    telegram = _Tripwire(registry, "telegram")
    registry.register_deferred("feishu", feishu, env_hints=("FEISHU_APP_ID", "FEISHU_"))
    registry.register_deferred(
        "discord", discord, env_hints=("DISCORD_BOT_TOKEN", "DISCORD_")
    )
    registry.register_deferred(
        "telegram", telegram, env_hints=("TELEGRAM_BOT_TOKEN", "TELEGRAM_")
    )

    entries = _candidate_plugin_entries(registry, GatewayConfig())

    assert telegram.ran, "configured platform must be materialized"
    assert not feishu.ran, "unconfigured platform must stay deferred"
    assert not discord.ran
    assert [e.name for e in entries] == ["telegram"]


def test_candidate_entries_resolves_platform_present_in_config(registry, monkeypatch):
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    for key in list(os.environ):
        if key.startswith("DISCORD_"):
            monkeypatch.delenv(key, raising=False)

    discord = _Tripwire(registry, "discord")
    registry.register_deferred(
        "discord", discord, env_hints=("DISCORD_BOT_TOKEN", "DISCORD_")
    )

    config = GatewayConfig()
    config.platforms[Platform.DISCORD] = PlatformConfig(enabled=False)

    _candidate_plugin_entries(registry, config)
    assert discord.ran, "a platform the user configured in YAML must resolve"


# ── manifest hint derivation ─────────────────────────────────────────────────


def test_manifest_hints_cover_declared_vars_and_prefixes():
    from hermes_cli.plugins import PluginManager, PluginManifest

    manifest = PluginManifest(
        name="feishu-platform",
        kind="platform",
        requires_env=[
            {"name": "FEISHU_APP_ID"},
            {"name": "FEISHU_APP_SECRET"},
        ],
    )
    hints = PluginManager()._platform_env_hints(manifest, "feishu")
    assert "FEISHU_APP_ID" in hints
    assert "FEISHU_APP_SECRET" in hints
    assert "FEISHU_" in hints


def test_manifest_hints_accept_bare_string_entries():
    from hermes_cli.plugins import PluginManager, PluginManifest

    manifest = PluginManifest(
        name="irc-platform", kind="platform", requires_env=["IRC_SERVER"]
    )
    hints = PluginManager()._platform_env_hints(manifest, "irc")
    assert "IRC_SERVER" in hints
    assert "IRC_" in hints


def test_manifest_hints_add_platform_name_prefix_for_mismatched_vars():
    """sms declares TWILIO_* — the SMS_ prefix must still be offered."""
    from hermes_cli.plugins import PluginManager, PluginManifest

    manifest = PluginManifest(
        name="sms-platform", kind="platform", requires_env=["TWILIO_ACCOUNT_SID"]
    )
    hints = PluginManager()._platform_env_hints(manifest, "sms")
    assert "TWILIO_" in hints
    assert "SMS_" in hints


def test_manifest_hints_empty_when_manifest_declares_nothing():
    """No requires_env => no gate => fail open.  Must NOT synthesize a prefix."""
    from hermes_cli.plugins import PluginManager, PluginManifest

    manifest = PluginManifest(name="raft-platform", kind="platform", requires_env=[])
    assert PluginManager()._platform_env_hints(manifest, "raft") == ()


def test_manifest_derived_name_matches_registered_name_for_every_bundled_platform():
    """Pin the convention the name-only enumeration depends on.

    ``_platform_registry_names`` substitutes the manifest-derived deferred key
    for the entry's real ``name``.  If an adapter ever registered under a
    different name, that platform would silently vanish from the shared-key
    bridging loop.  Checked STATICALLY (regex over the adapter source) — doing
    it by import would load every platform SDK, which is the very cost under
    test here.
    """
    import re
    from pathlib import Path

    from hermes_cli.plugins import PluginManager, PluginManifest

    root = Path(__file__).resolve().parents[2] / "plugins" / "platforms"
    if not root.is_dir():
        pytest.skip(f"bundled platform plugins not present at {root}")

    manager = PluginManager()
    mismatches = []
    checked = 0

    for plugin_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest_path = plugin_dir / "plugin.yaml"
        adapter_path = plugin_dir / "adapter.py"
        if not manifest_path.is_file() or not adapter_path.is_file():
            continue

        declared = re.search(
            r"^name:\s*(\S+)", manifest_path.read_text(encoding="utf-8"), re.M
        )
        manifest = PluginManifest(
            name=declared.group(1) if declared else "",
            kind="platform",
            path=str(plugin_dir),
        )
        derived = manager._platform_name_from_manifest(manifest)

        registered = re.search(
            r"register_platform\(\s*name=[\"']([^\"']+)[\"']",
            adapter_path.read_text(encoding="utf-8"),
        )
        if registered is None:
            continue  # registers some other way; name-only enumeration N/A

        checked += 1
        if registered.group(1) != derived:
            mismatches.append(
                f"{plugin_dir.name}: manifest-derived {derived!r} != "
                f"registered {registered.group(1)!r}"
            )

    assert checked >= 15, f"only inspected {checked} bundled platforms — check the glob"
    assert not mismatches, "deferred key must match the registered platform name:\n" + (
        "\n".join(mismatches)
    )


# ── the regression guard ─────────────────────────────────────────────────────


def test_load_gateway_config_does_not_import_feishu_sdk(monkeypatch, tmp_path):
    """The assertion that would have caught the original defect.

    With no FEISHU_* env vars and no Feishu config, loading gateway config must
    not drag in lark_oapi (~10k modules, 106s of the measured 118s).
    """
    if "lark_oapi" in sys.modules:
        pytest.skip("lark_oapi already imported by an earlier test in this process")

    for key in list(os.environ):
        if key.startswith("FEISHU_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(_EAGER_PLATFORM_PLUGINS_ENV, raising=False)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.config import load_gateway_config

    load_gateway_config()

    leaked = [m for m in sys.modules if m == "lark_oapi" or m.startswith("lark_oapi.")]
    assert not leaked, (
        f"load_gateway_config() imported {len(leaked)} lark_oapi modules with "
        "Feishu unconfigured — plugin enumeration went eager again"
    )
