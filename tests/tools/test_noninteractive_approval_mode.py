"""Tests for approvals.noninteractive_mode — fail-closed for headless contexts."""

from unittest.mock import patch as mock_patch

import pytest

import tools.approval as approval_module
from tools.approval import (
    _get_noninteractive_approval_mode,
    check_dangerous_command,
)


@pytest.fixture(autouse=True)
def _clear_approval_state():
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")


@pytest.fixture
def headless(monkeypatch):
    """No interactive CLI, no gateway, no cron — the fail-open branch."""
    for var in (
        "HERMES_INTERACTIVE",
        "HERMES_GATEWAY_SESSION",
        "HERMES_CRON_SESSION",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(var, raising=False)


def _config(approvals):
    return mock_patch("hermes_cli.config.load_config", return_value={"approvals": approvals})


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------

class TestNoninteractiveApprovalModeParsing:
    def test_default_is_approve(self):
        """Absent config keeps today's behaviour — operators opt in to deny."""
        with _config({}):
            assert _get_noninteractive_approval_mode() == "approve"

    def test_explicit_deny(self):
        with _config({"noninteractive_mode": "deny"}):
            assert _get_noninteractive_approval_mode() == "deny"

    def test_explicit_approve(self):
        with _config({"noninteractive_mode": "approve"}):
            assert _get_noninteractive_approval_mode() == "approve"

    @pytest.mark.parametrize("value", ["DENY", " deny ", "block", "closed", "no"])
    def test_deny_synonyms_and_whitespace(self, value):
        with _config({"noninteractive_mode": value}):
            assert _get_noninteractive_approval_mode() == "deny"

    def test_unrecognised_value_falls_back_to_approve(self):
        """An unknown value must not silently start blocking a live deployment."""
        with _config({"noninteractive_mode": "maybe"}):
            assert _get_noninteractive_approval_mode() == "approve"

    def test_unreadable_config_falls_back_to_approve(self):
        with mock_patch("hermes_cli.config.load_config", side_effect=OSError("boom")):
            assert _get_noninteractive_approval_mode() == "approve"


# ---------------------------------------------------------------------------
# effect on check_dangerous_command
# ---------------------------------------------------------------------------

class TestNoninteractiveGate:
    def test_deny_blocks_a_dangerous_command(self, headless):
        with _config({"noninteractive_mode": "deny"}):
            result = check_dangerous_command("git push --force origin main", "local")
        assert result["approved"] is False
        assert "noninteractive_mode" in result["message"]

    def test_approve_preserves_existing_behaviour(self, headless):
        with _config({"noninteractive_mode": "approve"}):
            result = check_dangerous_command("git push --force origin main", "local")
        assert result["approved"] is True

    def test_default_config_preserves_existing_behaviour(self, headless):
        with _config({}):
            result = check_dangerous_command("git push --force origin main", "local")
        assert result["approved"] is True

    def test_harmless_command_is_unaffected(self, headless):
        with _config({"noninteractive_mode": "deny"}):
            result = check_dangerous_command("git status", "local")
        assert result["approved"] is True

    def test_cron_still_governed_by_cron_mode(self, headless, monkeypatch):
        """noninteractive_mode must not override the cron branch."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        with _config({"noninteractive_mode": "deny", "cron_mode": "approve"}):
            result = check_dangerous_command("git push --force origin main", "local")
        assert result["approved"] is True
