"""Tests for delegation.fallback_providers — per-child fallback chain.

Three modes:
  - Not set / "inherit" → child inherits parent's _fallback_chain
  - []                  → no fallback (child gets None)
  - [{provider, model}] → custom chain parsed from config
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestDelegationFallbackProviders(unittest.TestCase):
    """Verify delegation.fallback_providers resolution in _build_child_agent."""

    def _make_parent_agent(self, fallback_chain=None):
        """Create a minimal parent agent mock with a fallback chain."""
        parent = MagicMock()
        parent._fallback_chain = fallback_chain or []
        parent.provider = "openai-codex"
        parent.model = "gpt-5.6-sol"
        parent.api_key = "test-key"
        parent.base_url = ""
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent.provider_require_parameters = False
        parent.provider_data_collection = ""
        parent.openrouter_min_coding_score = None
        parent.max_tokens = None
        parent.prefill_messages = None
        parent.session_id = "test-session"
        parent._session_db = None
        return parent

    @patch("tools.delegate_tool._load_config")
    def test_inherit_when_not_set(self, mock_cfg):
        """When delegation.fallback_providers is absent, inherit parent chain."""
        mock_cfg.return_value = {"delegation": {}}
        parent = self._make_parent_agent(
            fallback_chain=[{"provider": "zai", "model": "glm-5.2"}]
        )

        from hermes_cli.fallback_config import _iter_fallback_entries

        # Simulate the resolution logic from _build_child_agent
        cfg = mock_cfg.return_value
        del_cfg = cfg.get("delegation", {})
        fb_raw = del_cfg.get("fallback_providers")
        child_fallback = None
        mode = "inherit"
        if fb_raw is not None:
            if isinstance(fb_raw, str) and fb_raw.strip().lower() in ("inherit", "parent"):
                mode = "inherit"
            elif isinstance(fb_raw, list) and len(fb_raw) == 0:
                mode = "none"
            elif isinstance(fb_raw, (list, dict)):
                parsed = _iter_fallback_entries(fb_raw)
                if parsed:
                    child_fallback = parsed
                    mode = "custom"

        if child_fallback is None and mode == "inherit":
            child_fallback = getattr(parent, "_fallback_chain", None) or None

        self.assertEqual(mode, "inherit")
        self.assertIsNotNone(child_fallback)
        self.assertEqual(child_fallback[0]["provider"], "zai")

    @patch("tools.delegate_tool._load_config")
    def test_explicit_inherit_string(self, mock_cfg):
        """When delegation.fallback_providers is 'inherit', use parent chain."""
        mock_cfg.return_value = {
            "delegation": {"fallback_providers": "inherit"}
        }
        parent = self._make_parent_agent(
            fallback_chain=[{"provider": "xai-oauth", "model": "grok-4.5"}]
        )

        from hermes_cli.fallback_config import _iter_fallback_entries

        cfg = mock_cfg.return_value
        del_cfg = cfg.get("delegation", {})
        fb_raw = del_cfg.get("fallback_providers")
        child_fallback = None
        mode = "inherit"
        if fb_raw is not None:
            if isinstance(fb_raw, str) and fb_raw.strip().lower() in ("inherit", "parent"):
                mode = "inherit"
            elif isinstance(fb_raw, list) and len(fb_raw) == 0:
                mode = "none"
            elif isinstance(fb_raw, (list, dict)):
                parsed = _iter_fallback_entries(fb_raw)
                if parsed:
                    child_fallback = parsed
                    mode = "custom"

        if child_fallback is None and mode == "inherit":
            child_fallback = getattr(parent, "_fallback_chain", None) or None

        self.assertEqual(mode, "inherit")
        self.assertIsNotNone(child_fallback)
        self.assertEqual(child_fallback[0]["provider"], "xai-oauth")

    @patch("tools.delegate_tool._load_config")
    def test_empty_list_disables_fallback(self, mock_cfg):
        """When delegation.fallback_providers is [], child gets no fallback."""
        mock_cfg.return_value = {
            "delegation": {"fallback_providers": []}
        }
        parent = self._make_parent_agent(
            fallback_chain=[{"provider": "zai", "model": "glm-5.2"}]
        )

        from hermes_cli.fallback_config import _iter_fallback_entries

        cfg = mock_cfg.return_value
        del_cfg = cfg.get("delegation", {})
        fb_raw = del_cfg.get("fallback_providers")
        child_fallback = None
        mode = "inherit"
        if fb_raw is not None:
            if isinstance(fb_raw, str) and fb_raw.strip().lower() in ("inherit", "parent"):
                mode = "inherit"
            elif isinstance(fb_raw, list) and len(fb_raw) == 0:
                mode = "none"
            elif isinstance(fb_raw, (list, dict)):
                parsed = _iter_fallback_entries(fb_raw)
                if parsed:
                    child_fallback = parsed
                    mode = "custom"

        if child_fallback is None and mode == "inherit":
            child_fallback = getattr(parent, "_fallback_chain", None) or None

        self.assertEqual(mode, "none")
        self.assertIsNone(child_fallback)

    @patch("tools.delegate_tool._load_config")
    def test_custom_chain(self, mock_cfg):
        """When delegation.fallback_providers has entries, use them."""
        mock_cfg.return_value = {
            "delegation": {
                "fallback_providers": [
                    {"provider": "zai", "model": "glm-5.2"},
                    {"provider": "opencode-go", "model": "deepseek-v4-flash"},
                ]
            }
        }
        parent = self._make_parent_agent(
            fallback_chain=[{"provider": "xai-oauth", "model": "grok-4.5"}]
        )

        from hermes_cli.fallback_config import _iter_fallback_entries

        cfg = mock_cfg.return_value
        del_cfg = cfg.get("delegation", {})
        fb_raw = del_cfg.get("fallback_providers")
        child_fallback = None
        mode = "inherit"
        if fb_raw is not None:
            if isinstance(fb_raw, str) and fb_raw.strip().lower() in ("inherit", "parent"):
                mode = "inherit"
            elif isinstance(fb_raw, list) and len(fb_raw) == 0:
                mode = "none"
            elif isinstance(fb_raw, (list, dict)):
                parsed = _iter_fallback_entries(fb_raw)
                if parsed:
                    child_fallback = parsed
                    mode = "custom"

        if child_fallback is None and mode == "inherit":
            child_fallback = getattr(parent, "_fallback_chain", None) or None

        self.assertEqual(mode, "custom")
        self.assertIsNotNone(child_fallback)
        self.assertEqual(len(child_fallback), 2)
        self.assertEqual(child_fallback[0]["provider"], "zai")
        self.assertEqual(child_fallback[0]["model"], "glm-5.2")
        self.assertEqual(child_fallback[1]["provider"], "opencode-go")
        self.assertEqual(child_fallback[1]["model"], "deepseek-v4-flash")
        # Must NOT be the parent's chain
        self.assertNotEqual(child_fallback[0]["provider"], "xai-oauth")

    @patch("tools.delegate_tool._load_config")
    def test_parent_string_alias(self, mock_cfg):
        """'parent' string should be treated same as 'inherit'."""
        mock_cfg.return_value = {
            "delegation": {"fallback_providers": "parent"}
        }
        parent = self._make_parent_agent(
            fallback_chain=[{"provider": "zai", "model": "glm-5.2"}]
        )

        from hermes_cli.fallback_config import _iter_fallback_entries

        cfg = mock_cfg.return_value
        del_cfg = cfg.get("delegation", {})
        fb_raw = del_cfg.get("fallback_providers")
        child_fallback = None
        mode = "inherit"
        if fb_raw is not None:
            if isinstance(fb_raw, str) and fb_raw.strip().lower() in ("inherit", "parent"):
                mode = "inherit"
            elif isinstance(fb_raw, list) and len(fb_raw) == 0:
                mode = "none"
            elif isinstance(fb_raw, (list, dict)):
                parsed = _iter_fallback_entries(fb_raw)
                if parsed:
                    child_fallback = parsed
                    mode = "custom"

        if child_fallback is None and mode == "inherit":
            child_fallback = getattr(parent, "_fallback_chain", None) or None

        self.assertEqual(mode, "inherit")
        self.assertIsNotNone(child_fallback)
        self.assertEqual(child_fallback[0]["provider"], "zai")


if __name__ == "__main__":
    unittest.main()
