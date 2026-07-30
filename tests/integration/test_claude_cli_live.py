import os

import pytest


@pytest.mark.skipif(
    os.environ.get("HERMES_LIVE_CLAUDE_CLI") != "1",
    reason="requires explicit Claude subscription live-test opt-in",
)
def test_guarded_claude_cli_subscription():
    from scripts.verify_claude_cli_provider import run_verification

    result = run_verification(model="opus")

    assert result["exact_response"] == "HERMES_CLAUDE_CLI_OK"
    assert result["tool_result"] == "HERMES_TOOL_OK"
    assert result["resumed"] is True
    assert result["provider"] == "claude-cli"
