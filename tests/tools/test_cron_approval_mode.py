"""Tests for approvals.cron_mode — configurable approval behavior for cron jobs."""

import pytest

import tools.approval as approval_module
from tools.approval import (
    _get_cron_approval_mode,
    check_all_command_guards,
    check_dangerous_command,
    detect_dangerous_command,
)


@pytest.fixture(autouse=True)
def _clear_approval_state():
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")


def _enter_cron_session(monkeypatch, value: str = "1"):
    """Mark this test as a cron session for both ContextVar and env paths.

    ``HERMES_CRON_SESSION`` is now a ``contextvars.ContextVar`` set by
    ``cron/scheduler.py:run_job``. Tests that previously used
    ``monkeypatch.setenv("HERMES_CRON_SESSION", "1")`` must additionally
    set the ContextVar — otherwise ``env_var_enabled`` (which consults
    the ContextVar first, with no ``os.environ`` fallback) returns False
    and the test no longer exercises the cron branch.

    Companion teardown: the autouse ``_reset_cron_session_after`` fixture
    clears the marker between tests so a cron test cannot leak into a
    sibling that did not opt in.
    """
    from gateway.session_context import set_cron_session
    set_cron_session(value)
    # Preserve the historical env-var write so code paths that read it
    # directly via ``os.getenv`` (rather than ``env_var_enabled``) keep
    # working. Safe to remove once all readers are migrated.
    monkeypatch.setenv("HERMES_CRON_SESSION", value)


@pytest.fixture(autouse=True)
def _isolate_cron_session_per_test():
    """Force the cron-session marker back to its default between tests.

    Production code scopes the cron-session marker via a
    ``contextvars.ContextVar``, so the only public API to clear it is
    ``reset_cron_session(token)`` — which restores to whatever value
    was active when the original ``set()`` ran. That means a cron
    test's ``set_cron_session("1")`` followed by any teardown cannot
    re-establish the default: the prior value was the default, but
    another test's ``set`` may have already overwritten it.

    The module-level ``force_reset_cron_session()`` shortcut captures
    a token at gateway-module import time (when the var is still
    ``_UNSET``) and uses it to deterministically revert to the default.
    Tests get a clean slate every iteration.
    """
    yield
    from gateway.session_context import force_reset_cron_session
    try:
        force_reset_cron_session()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# _get_cron_approval_mode() config parsing
# ---------------------------------------------------------------------------

class TestCronApprovalModeParsing:
    def test_default_is_deny(self):
        """When no config is set, cron_mode defaults to 'deny'."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {}}):
            assert _get_cron_approval_mode() == "deny"

    def test_explicit_deny(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "deny"}}):
            assert _get_cron_approval_mode() == "deny"

    def test_explicit_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "approve"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_off_maps_to_approve(self):
        """'off' is an alias for 'approve' (matches --yolo semantics)."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "off"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_allow_maps_to_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "allow"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_yes_maps_to_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "yes"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_case_insensitive(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "APPROVE"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_unknown_value_defaults_to_deny(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": "maybe"}}):
            assert _get_cron_approval_mode() == "deny"

    def test_config_load_failure_defaults_to_deny(self):
        """If config loading fails entirely, default to deny (safe)."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", side_effect=RuntimeError("config broken")):
            assert _get_cron_approval_mode() == "deny"

    def test_yaml_boolean_false_maps_to_deny(self):
        """YAML 1.1 parses bare 'off' as False. Ensure it maps to deny."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config", return_value={"approvals": {"cron_mode": False}}):
            # str(False) = "False", which is not in the approve set, so deny
            assert _get_cron_approval_mode() == "deny"


# ---------------------------------------------------------------------------
# check_dangerous_command() with cron session
# ---------------------------------------------------------------------------

class TestCronDenyMode:
    """When HERMES_CRON_SESSION is set and cron_mode=deny, dangerous commands are blocked."""

    def test_dangerous_command_blocked_in_cron_deny_mode(self, monkeypatch):
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("rm -rf /tmp/stuff", "local")
            assert not result["approved"]
            assert "BLOCKED" in result["message"]
            assert "cron_mode" in result["message"]

    def test_safe_command_allowed_in_cron_deny_mode(self, monkeypatch):
        """Non-dangerous commands still work even with cron_mode=deny."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("ls -la", "local")
            assert result["approved"]

    def test_multiple_dangerous_patterns_blocked(self, monkeypatch):
        """All dangerous patterns are blocked, not just rm."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        dangerous_commands = [
            "rm -rf /",
            "chmod 777 /etc/passwd",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
        ]

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            for cmd in dangerous_commands:
                is_dangerous, _, _ = detect_dangerous_command(cmd)
                if is_dangerous:
                    result = check_dangerous_command(cmd, "local")
                    assert not result["approved"], f"Should be blocked: {cmd}"
                    assert "BLOCKED" in result["message"]

    def test_block_message_includes_description(self, monkeypatch):
        """The block message should mention what pattern was matched."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("rm -rf /tmp/stuff", "local")
            assert not result["approved"]
            # Should contain the description of what was flagged
            assert "dangerous" in result["message"].lower() or "delete" in result["message"].lower()


class TestCronApproveMode:
    """When HERMES_CRON_SESSION is set and cron_mode=approve, dangerous commands pass through."""

    def test_dangerous_command_allowed_in_cron_approve_mode(self, monkeypatch):
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
            result = check_dangerous_command("rm -rf /tmp/stuff", "local")
            assert result["approved"]


# ---------------------------------------------------------------------------
# check_all_command_guards() with cron session
# ---------------------------------------------------------------------------

class TestCronDenyModeAllGuards:
    """The combined guard function also respects cron_mode."""

    def test_dangerous_command_blocked_in_combined_guard(self, monkeypatch):
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_all_command_guards("rm -rf /tmp/stuff", "local")
            assert not result["approved"]
            assert "BLOCKED" in result["message"]

    def test_safe_command_allowed_in_combined_guard(self, monkeypatch):
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_all_command_guards("echo hello", "local")
            assert result["approved"]

    def test_combined_guard_approve_mode(self, monkeypatch):
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
            result = check_all_command_guards("rm -rf /tmp/stuff", "local")
            assert result["approved"]


# ---------------------------------------------------------------------------
# Edge cases: cron mode interaction with other approval mechanisms
# ---------------------------------------------------------------------------

class TestCronModeInteractions:
    """Cron mode should NOT interfere with other approval bypass mechanisms."""

    def test_container_env_still_auto_approves(self, monkeypatch):
        """Docker/sandbox environments bypass approvals regardless of cron_mode."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("rm -rf /", "docker")
            assert result["approved"]

    def test_yolo_overrides_cron_deny(self, monkeypatch):
        """--yolo still bypasses cron_mode=deny for dangerous (non-hardline) commands."""
        _enter_cron_session(monkeypatch)
        monkeypatch.setenv("HERMES_YOLO_MODE", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)

        # _YOLO_MODE_FROZEN is frozen at module import time (security: prevents
        # prompt injection from runtime-setting HERMES_YOLO_MODE). When the
        # test process imports tools.approval BEFORE this test sets the env,
        # the frozen value is False and yolo-bypass paths don't activate.
        # Patch the module attribute directly to simulate process-startup
        # with HERMES_YOLO_MODE=1.
        from unittest.mock import patch as mock_patch
        import tools.approval
        with (
            mock_patch.object(tools.approval, "_YOLO_MODE_FROZEN", True),
            mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"),
        ):
            # Use a dangerous-but-not-hardline command — `rm -rf /` is now
            # hardline-blocked regardless of yolo (see test_hardline_blocklist.py).
            result = check_dangerous_command("rm -rf /tmp/stuff", "local")
            assert result["approved"]

    def test_non_cron_non_interactive_still_auto_approves(self, monkeypatch):
        """Non-cron, non-interactive sessions (e.g. scripted usage) still auto-approve."""
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        result = check_dangerous_command("rm -rf /tmp/stuff", "local")
        assert result["approved"]


class TestCronWithGatewayOrigin:
    """Cron jobs originating from a gateway platform must NOT be treated as gateway.

    cron/scheduler.py binds HERMES_SESSION_PLATFORM via contextvars for
    delivery routing (so cron output lands back in the origin chat). The
    API-server approvals work (PR #20311) made check_dangerous_command treat
    any contextvar-bound platform as a gateway session. That would route
    cron-from-telegram/discord/etc. through submit_pending with no listener,
    hanging the job instead of respecting approvals.cron_mode.
    """

    def test_cron_with_telegram_origin_uses_cron_mode_not_gateway(self, monkeypatch):
        """Cron + contextvar platform=telegram + cron_mode=deny → BLOCKED, not pending."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        from gateway.session_context import set_session_vars, clear_session_vars
        tokens = set_session_vars(platform="telegram", chat_id="123")
        try:
            from unittest.mock import patch as mock_patch
            with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
                result = check_dangerous_command("rm -rf /tmp/stuff", "local")
                # Cron-mode path: BLOCKED message, NOT pending/approval_required.
                assert not result["approved"]
                assert "BLOCKED" in result["message"]
                assert "cron_mode" in result["message"]
                assert result.get("status") != "approval_required"
        finally:
            clear_session_vars(tokens)

    def test_cron_with_telegram_origin_approve_mode_allows(self, monkeypatch):
        """Cron + contextvar platform=telegram + cron_mode=approve → allowed via cron path."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        from gateway.session_context import set_session_vars, clear_session_vars
        tokens = set_session_vars(platform="discord", chat_id="456")
        try:
            from unittest.mock import patch as mock_patch
            with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
                result = check_dangerous_command("rm -rf /tmp/stuff", "local")
                assert result["approved"]
                # Should NOT be a gateway-approval response.
                assert result.get("status") != "approval_required"
        finally:
            clear_session_vars(tokens)

    def test_cron_with_telegram_origin_combined_guard_uses_cron_mode(self, monkeypatch):
        """check_all_command_guards must also honor cron_mode over gateway classification."""
        _enter_cron_session(monkeypatch)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        from gateway.session_context import set_session_vars, clear_session_vars
        tokens = set_session_vars(platform="telegram", chat_id="789")
        try:
            from unittest.mock import patch as mock_patch
            with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
                result = check_all_command_guards("rm -rf /tmp/stuff", "local")
                assert not result["approved"]
                assert "BLOCKED" in result["message"]
                assert result.get("status") != "approval_required"
        finally:
            clear_session_vars(tokens)
