import unittest

from plugins.platforms.whatsapp.adapter import (
    _apply_ownership_router_env,
    _ownership_router_config_hash,
)


class OwnershipRouterConfigHashTests(unittest.TestCase):
    def test_adapter_preserves_eight_custom_prefixes(self):
        from plugins.platforms.whatsapp.adapter import _normalize_ownership_router_prefixes

        custom = [f"custom-{index}" for index in range(1, 9)]
        self.assertEqual(_normalize_ownership_router_prefixes(custom), custom)

    def test_hash_is_stable_and_covers_behavior_and_secret(self):
        base = _ownership_router_config_hash(
            "https://router.example/classify",
            "secret-a",
            1500,
            ["jeffersom"],
        )
        self.assertEqual(len(base), 16)
        self.assertEqual(
            base,
            _ownership_router_config_hash(
                "https://router.example/classify",
                "secret-a",
                1500,
                ["jeffersom"],
            ),
        )
        self.assertNotEqual(
            base,
            _ownership_router_config_hash(
                "https://router.example/classify",
                "secret-b",
                1500,
                ["jeffersom"],
            ),
        )
        self.assertNotEqual(
            base,
            _ownership_router_config_hash(
                "https://router.example/classify",
                "secret-a",
                2500,
                ["jeffersom"],
            ),
        )

    def test_disabled_router_has_empty_fingerprint(self):
        self.assertEqual(
            _ownership_router_config_hash("", "unused", 1500, ["jeffersom"]),
            "",
        )

    def test_disabled_router_clears_inherited_environment(self):
        env = {
            "WHATSAPP_OWNERSHIP_ROUTER_URL": "https://stale.invalid",
            "WHATSAPP_OWNERSHIP_ROUTER_TOKEN": "stale-token",
            "WHATSAPP_OWNERSHIP_ROUTER_TIMEOUT_MS": "9999",
            "WHATSAPP_OWNERSHIP_ROUTER_PREFIXES": '["stale"]',
            "WHATSAPP_OWNERSHIP_ROUTER_CONFIG_HASH": "stale-hash",
            "KEEP_ME": "yes",
        }
        _apply_ownership_router_env(env, "", "", 1500, ["jeffersom"], "")
        self.assertEqual(env, {"KEEP_ME": "yes"})


if __name__ == "__main__":
    unittest.main()
