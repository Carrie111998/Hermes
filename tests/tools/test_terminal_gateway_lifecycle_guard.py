"""Terminal wiring for the all-turn shared-gateway lifecycle guard."""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from cron import lifecycle_guard
from tools import terminal_tool


def test_force_cannot_bypass_absolute_gateway_pid_kill(monkeypatch):
    monkeypatch.setattr(
        lifecycle_guard,
        "_resolve_gateway_main_pid",
        lambda: 133375,
    )
    mock_env = MagicMock()
    mock_env.cwd = "/tmp"
    mock_env.execute.return_value = {"output": "unexpected", "returncode": 0}
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }

    with ExitStack() as stack:
        stack.enter_context(
            patch("tools.terminal_tool._get_env_config", return_value=config)
        )
        stack.enter_context(patch("tools.terminal_tool._start_cleanup_thread"))
        stack.enter_context(
            patch("tools.terminal_tool._active_environments", {"default": mock_env})
        )
        stack.enter_context(
            patch("tools.terminal_tool._last_activity", {"default": 0})
        )
        stack.enter_context(patch("tools.terminal_tool._session_cwd", {}))
        stack.enter_context(
            patch(
                "tools.terminal_tool._check_all_guards",
                return_value={"approved": True},
            )
        )
        result = json.loads(
            terminal_tool.terminal_tool(
                command="/bin/kill -TERM 133375",
                force=True,
            )
        )

    assert result["status"] == "error"
    assert "shared Hermes gateway" in result["error"]
    mock_env.execute.assert_not_called()
