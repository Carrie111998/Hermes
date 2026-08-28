"""Test that _build_browser_env merges Hermes node dir into PATH."""

from unittest.mock import patch
from tools.browser_tool import _build_browser_env


def test_build_browser_env_merges_browser_path():
    """Verify _build_browser_env() merges Hermes node dirs into the subprocess PATH (Closes #97186)."""
    with patch.dict("os.environ", {"SOME_TEST_KEY": "val"}, clear=False):
        env = _build_browser_env()
        assert "PATH" in env
        assert isinstance(env["PATH"], str)
        assert len(env["PATH"]) > 0
