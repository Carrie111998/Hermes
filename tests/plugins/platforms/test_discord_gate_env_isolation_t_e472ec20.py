"""Regression guard for t_e472ec20 — mention-gate vars must be per-profile isolated.

Bug: ``DISCORD_THREAD_REQUIRE_MENTION`` / ``DISCORD_REQUIRE_MENTION`` /
``DISCORD_BOTS_REQUIRE_INLINE_MENTION`` were absent from ``_GATE_ENV_KEYS`` and
read via bare ``os.getenv()``. Under ``gateway.multiplex_profiles`` only the
default profile's ``.env`` lands in process-global ``os.environ`` — a
secondary profile's own ``.env`` value for these three vars was therefore
inert, and the default profile's value silently governed every specialist
adapter. This is exactly the failure mode the anti-ack-loop mention-gate
guarantee exists to prevent (see hermes-multi-bot-discord skill).

Fix: add the three keys to ``_GATE_ENV_KEYS`` (so ``_snapshot_gate_env``
captures them from the owning profile's secret scope at connect() time) and
route the three reader methods through ``self._gate_env(...)`` instead of
``os.getenv(...)``.

Covers:
- multiplex path: two adapters, opposite gate-env-snapshot values, each
  resolves its own (the actual bug scenario).
- single-profile / pre-connect path: no snapshot installed, falls back to
  ``_scoped_gate_env`` -> plain ``os.getenv`` (unchanged legacy behaviour).
- ``config.extra`` still wins over env/snapshot when explicitly configured
  (per-profile YAML override, unaffected by this fix).
"""
from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord import adapter as ad
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(extra: dict | None = None, snapshot: dict | None = None) -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="x", extra=dict(extra or {}))
    adapter._gate_env_snapshot = snapshot
    return adapter


class TestGateEnvKeysIncludeMentionGates:
    def test_all_three_vars_are_gate_keys(self):
        assert "DISCORD_THREAD_REQUIRE_MENTION" in ad._GATE_ENV_KEYS
        assert "DISCORD_REQUIRE_MENTION" in ad._GATE_ENV_KEYS
        assert "DISCORD_BOTS_REQUIRE_INLINE_MENTION" in ad._GATE_ENV_KEYS


class TestMultiplexIsolation:
    """The actual bug: two profiles, opposite values, each must see its own."""

    def test_thread_require_mention_isolated_per_adapter(self, monkeypatch):
        # Process-global env says the opposite of what each profile wants —
        # proves the snapshot wins, not os.environ.
        monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "false")

        profile_a = _adapter(snapshot={"DISCORD_THREAD_REQUIRE_MENTION": "true"})
        profile_b = _adapter(snapshot={"DISCORD_THREAD_REQUIRE_MENTION": "false"})

        assert profile_a._discord_thread_require_mention() is True
        assert profile_b._discord_thread_require_mention() is False

    def test_require_mention_isolated_per_adapter(self, monkeypatch):
        monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")

        profile_a = _adapter(snapshot={"DISCORD_REQUIRE_MENTION": "true"})
        profile_b = _adapter(snapshot={"DISCORD_REQUIRE_MENTION": "false"})

        assert profile_a._discord_require_mention() is True
        assert profile_b._discord_require_mention() is False

    def test_bots_require_inline_mention_isolated_per_adapter(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOTS_REQUIRE_INLINE_MENTION", "true")

        profile_a = _adapter(snapshot={"DISCORD_BOTS_REQUIRE_INLINE_MENTION": "false"})
        profile_b = _adapter(snapshot={"DISCORD_BOTS_REQUIRE_INLINE_MENTION": "true"})

        assert profile_a._discord_bots_require_inline_mention() is False
        assert profile_b._discord_bots_require_inline_mention() is True

    def test_empty_snapshot_value_falls_back_to_default_not_other_profile(self, monkeypatch):
        """A profile with no opinion (empty snapshot slot) must get the
        documented default, never whatever another profile/env has set."""
        monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "true")  # default profile's value

        specialist = _adapter(snapshot={"DISCORD_THREAD_REQUIRE_MENTION": ""})

        # default for thread_require_mention is False -- must NOT inherit "true"
        assert specialist._discord_thread_require_mention() is False


class TestSinglePreConnectFallback:
    """No snapshot installed (pre-connect, or single-profile deployment):
    legacy plain os.getenv behaviour must be unchanged."""

    def test_thread_require_mention_default_false(self, monkeypatch):
        monkeypatch.delenv("DISCORD_THREAD_REQUIRE_MENTION", raising=False)
        adapter = _adapter(snapshot=None)
        assert adapter._discord_thread_require_mention() is False

    def test_thread_require_mention_reads_os_environ_when_unscoped(self, monkeypatch):
        monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "true")
        adapter = _adapter(snapshot=None)
        assert adapter._discord_thread_require_mention() is True

    def test_require_mention_default_true(self, monkeypatch):
        monkeypatch.delenv("DISCORD_REQUIRE_MENTION", raising=False)
        adapter = _adapter(snapshot=None)
        assert adapter._discord_require_mention() is True

    def test_bots_require_inline_mention_default_false(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOTS_REQUIRE_INLINE_MENTION", raising=False)
        adapter = _adapter(snapshot=None)
        assert adapter._discord_bots_require_inline_mention() is False


class TestConfigExtraStillWins:
    """Per-profile YAML (config.extra) must still override env/snapshot —
    this is the documented interim mitigation and must keep working."""

    def test_extra_overrides_snapshot_and_env(self, monkeypatch):
        monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "false")
        adapter = _adapter(
            extra={"thread_require_mention": True},
            snapshot={"DISCORD_THREAD_REQUIRE_MENTION": "false"},
        )
        assert adapter._discord_thread_require_mention() is True


class TestApplyYamlConfigSeedsExtra:
    """`_apply_yaml_config` must seed the three vars into seeded_extra so
    ``platforms.discord.extra`` is a supported per-profile surface."""

    def test_seeds_all_three_from_top_level_discord_cfg(self):
        seeded = ad._apply_yaml_config(
            {},
            {
                "require_mention": False,
                "thread_require_mention": True,
                "bots_require_inline_mention": True,
            },
        )
        assert seeded is not None
        assert seeded["require_mention"] == "false"
        assert seeded["thread_require_mention"] == "true"
        assert seeded["bots_require_inline_mention"] == "true"

    def test_seeds_from_nested_platforms_discord_extra(self):
        yaml_cfg = {
            "platforms": {
                "discord": {
                    "extra": {
                        "thread_require_mention": True,
                        "require_mention": False,
                        "bots_require_inline_mention": True,
                    }
                }
            }
        }
        seeded = ad._apply_yaml_config(yaml_cfg, {})
        assert seeded is not None
        assert seeded["thread_require_mention"] == "true"
        assert seeded["require_mention"] == "false"
        assert seeded["bots_require_inline_mention"] == "true"
