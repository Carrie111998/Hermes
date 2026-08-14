"""Tests for PR-2/PR-3: per-child memory + permission boundary.

Run with:  python -m unittest tests.tools.test_pr_nous_child_memory -v
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    _build_child_agent,
    _strip_blocked_tools,
    delegate_task,
    _blocked_toolsets_for_role,
)


def _make_mock_parent(depth=0, enabled_toolsets=None):
    """Create a mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = enabled_toolsets
    parent.disabled_toolsets = None
    parent.valid_tool_names = [
        "web_search", "terminal", "read_file", "write_file",
        "memory_add", "memory_replace", "memory_remove",
        "delegate_task", "clarify",
    ]
    return parent


class TestChildMemory(unittest.TestCase):
    """child_memory=True enables per-child persistent memory."""

    @patch("tools.delegate_tool._load_config")
    def test_child_memory_true_passes_skip_memory_false(self, mock_cfg):
        """When child_memory=True, the child AIAgent gets skip_memory=False."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test memory child",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                child_memory=True,
            )

        _, kwargs = mock_agent_cls.call_args
        self.assertFalse(kwargs["skip_memory"])

    @patch("tools.delegate_tool._load_config")
    def test_child_memory_false_preserves_current_behaviour(self, mock_cfg):
        """When child_memory=False (default), skip_memory remains True."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test default child",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
            )

        _, kwargs = mock_agent_cls.call_args
        self.assertTrue(kwargs["skip_memory"])

    @patch("tools.delegate_tool._load_config")
    def test_child_memory_adds_memory_toolset(self, mock_cfg):
        """child_memory=True re-adds 'memory' toolset stripped by _strip_blocked_tools."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "memory"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test memory toolset",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                child_memory=True,
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertIn("memory", child_toolsets)

    @patch("tools.delegate_tool._load_config")
    def test_child_memory_false_does_not_add_memory_toolset(self, mock_cfg):
        """child_memory=False strips 'memory' as before."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "memory"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test default child",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                child_memory=False,
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertNotIn("memory", child_toolsets)

    @patch("tools.delegate_tool._load_config")
    def test_child_memory_removes_memory_from_disabled(self, mock_cfg):
        """child_memory=True removes 'memory' from child_disabled_toolsets."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "memory"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test memory disabled",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                child_memory=True,
            )

        _, kwargs = mock_agent_cls.call_args
        disabled = kwargs.get("disabled_toolsets", [])
        self.assertNotIn("memory", disabled)


class TestPermissionBoundary(unittest.TestCase):
    """allowed_toolsets creates a permission boundary for the child."""

    @patch("tools.delegate_tool._load_config")
    def test_allowed_toolsets_intersected_with_parent(self, mock_cfg):
        """allowed_toolsets is intersected with the parent's available toolsets."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "web"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test intersection",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                allowed_toolsets=["terminal", "web", "browser", "rl"],
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertIn("terminal", child_toolsets)
        self.assertIn("web", child_toolsets)
        self.assertNotIn("browser", child_toolsets)
        self.assertNotIn("rl", child_toolsets)

    @patch("tools.delegate_tool._load_config")
    def test_allowed_toolsets_none_preserves_inheritance(self, mock_cfg):
        """allowed_toolsets=None preserves full parent toolset inheritance."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "web"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test inheritance",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertIn("terminal", child_toolsets)
        self.assertIn("file", child_toolsets)
        self.assertIn("web", child_toolsets)

    @patch("tools.delegate_tool._load_config")
    def test_allowed_toolsets_empty_yields_empty(self, mock_cfg):
        """When allowed_toolsets has no overlap with parent, child gets empty toolsets."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test empty",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                allowed_toolsets=["web", "browser"],
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertEqual(child_toolsets, [])

    @patch("tools.delegate_tool._load_config")
    def test_allowed_toolsets_blocked_tools_still_stripped(self, mock_cfg):
        """Blocked toolsets are stripped even when listed in allowed_toolsets."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "clarify", "memory", "cronjob"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test blocked strip",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                allowed_toolsets=["terminal", "clarify", "memory", "cronjob"],
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertIn("terminal", child_toolsets)
        self.assertNotIn("clarify", child_toolsets)
        self.assertNotIn("memory", child_toolsets)
        self.assertNotIn("cronjob", child_toolsets)

    @patch("tools.delegate_tool._load_config")
    def test_allowed_toolsets_with_child_memory_keeps_memory(self, mock_cfg):
        """child_memory=True preserves 'memory' within allowed_toolsets boundary."""
        mock_cfg.return_value = {}
        parent = _make_mock_parent(enabled_toolsets=["terminal", "file", "memory"])

        with patch("run_agent.AIAgent") as mock_agent_cls:
            _build_child_agent(
                task_index=0,
                goal="Test memory + boundary",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
                child_memory=True,
                allowed_toolsets=["terminal", "memory"],
            )

        _, kwargs = mock_agent_cls.call_args
        child_toolsets = kwargs.get("enabled_toolsets", [])
        self.assertIn("terminal", child_toolsets)
        self.assertIn("memory", child_toolsets)
        self.assertFalse(kwargs.get("skip_memory"))


class TestDelegateTaskSignature(unittest.TestCase):
    """Verify delegate_task public API accepts new parameters."""

    def test_delegate_task_accepts_child_memory_param(self):
        """delegate_task() signature includes child_memory."""
        import inspect
        sig = inspect.signature(delegate_task)
        params = sig.parameters
        self.assertIn("child_memory", params)
        self.assertFalse(params["child_memory"].default)

    def test_build_child_agent_accepts_new_params(self):
        """_build_child_agent() signature includes child_memory and allowed_toolsets."""
        import inspect
        sig = inspect.signature(_build_child_agent)
        params = sig.parameters
        self.assertIn("child_memory", params)
        self.assertIn("allowed_toolsets", params)
        self.assertFalse(params["child_memory"].default)
        self.assertIsNone(params["allowed_toolsets"].default)


if __name__ == "__main__":
    unittest.main()
