"""Tests for approvals.cron_mode — configurable approval behavior for cron jobs."""

import json
from unittest.mock import MagicMock, patch

import pytest

import tools.approval as approval_module
from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars
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
    reset_session_vars()
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")
    reset_session_vars()


def _configure_cron_smart(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "smart")


# ---------------------------------------------------------------------------
# _get_cron_approval_mode() config parsing
# ---------------------------------------------------------------------------

class TestCronApprovalModeParsing:
    def test_default_is_deny(self):
        """When no config is set, cron_mode defaults to 'deny'."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {}}):
            assert _get_cron_approval_mode() == "deny"

    def test_explicit_deny(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "deny"}}):
            assert _get_cron_approval_mode() == "deny"

    def test_explicit_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "approve"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_off_maps_to_approve(self):
        """'off' is an alias for 'approve' (matches --yolo semantics)."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "off"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_allow_maps_to_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "allow"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_yes_maps_to_approve(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "yes"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_case_insensitive(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "APPROVE"}}):
            assert _get_cron_approval_mode() == "approve"

    def test_smart_is_case_insensitive(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "SMART"}}):
            assert _get_cron_approval_mode() == "smart"

    def test_unknown_value_defaults_to_deny(self):
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": "maybe"}}):
            assert _get_cron_approval_mode() == "deny"

    def test_config_load_failure_defaults_to_deny(self):
        """If config loading fails entirely, default to deny (safe)."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("config broken")):
            assert _get_cron_approval_mode() == "deny"

    def test_yaml_boolean_false_maps_to_deny(self):
        """YAML 1.1 parses bare 'off' as False. Ensure it maps to deny."""
        from unittest.mock import patch as mock_patch
        with mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"cron_mode": False}}):
            # str(False) = "False", which is not in the approve set, so deny
            assert _get_cron_approval_mode() == "deny"


# ---------------------------------------------------------------------------
# ContextVar cron detection
# ---------------------------------------------------------------------------

class TestCronContextVarDetection:
    def test_legacy_env_fallback_still_marks_cron(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        assert approval_module._is_cron_approval_context() is True

    def test_explicit_blank_masks_leaked_cron_env_for_gateway_classification(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
        # A chat platform: unattended programmatic platforms (webhook,
        # msgraph_webhook, api_server) are intentionally NOT gateway
        # approval contexts anymore (#37284/#87509) — this test's subject
        # is the blank-cron-ContextVar masking, not platform policy.
        tokens = set_session_vars(platform="telegram", cron_session="")
        try:
            assert approval_module._is_cron_approval_context() is False
            assert approval_module._is_gateway_approval_context() is True
        finally:
            clear_session_vars(tokens)

    def test_scoped_cron_deny_for_dangerous_all_and_execute_code(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
        monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "deny")

        tokens = set_session_vars(cron_session="1")
        try:
            dangerous = check_dangerous_command("rm -rf /tmp/stuff", "local")
            combined = check_all_command_guards("rm -rf /tmp/stuff", "local")
            code = approval_module.check_execute_code_guard("import os", "local")
        finally:
            clear_session_vars(tokens)

        assert dangerous["approved"] is False
        assert combined["approved"] is False
        assert code["approved"] is False
        assert code["outcome"] == "blocked"

    def test_non_cron_blank_context_keeps_headless_execute_code_legacy_approved(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
        monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "deny")

        tokens = set_session_vars(cron_session="")
        try:
            result = approval_module.check_execute_code_guard("import os", "local")
        finally:
            clear_session_vars(tokens)

        assert result["approved"] is True


# ---------------------------------------------------------------------------
# check_dangerous_command() with cron session
# ---------------------------------------------------------------------------

class TestCronDenyMode:
    """When HERMES_CRON_SESSION is set and cron_mode=deny, dangerous commands are blocked."""

    def test_dangerous_command_blocked_in_cron_deny_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("ls -la", "local")
            assert result["approved"]

    def test_multiple_dangerous_patterns_blocked(self, monkeypatch):
        """All dangerous patterns are blocked, not just rm."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
            result = check_dangerous_command("rm -rf /tmp/stuff", "local")
            assert result["approved"]


# ---------------------------------------------------------------------------
# cron_mode=smart
# ---------------------------------------------------------------------------

class TestCronSmartMode:
    def test_embedded_hash_cannot_hide_dangerous_suffix_from_reviewer(
        self, monkeypatch
    ):
        command = "printf safe#; rm -rf .git"
        _configure_cron_smart(monkeypatch)
        response = MagicMock()
        response.choices[0].message.content = "DENY"
        call_llm = MagicMock(return_value=response)
        monkeypatch.setattr(
            "agent.auxiliary_client.call_llm",
            call_llm,
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )

        result = check_all_command_guards(command, "local")

        assert result["approved"] is False
        user_prompt = call_llm.call_args.kwargs["messages"][1]["content"]
        assert command in user_prompt

    def test_safe_command_skips_smart_reviewer(self, monkeypatch):
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda *_args: pytest.fail("safe commands must not call the reviewer"),
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )

        result = check_all_command_guards("echo cron-safe", "local")

        assert result == {"approved": True, "message": None}

    def test_direct_dangerous_command_reviews_actual_command_once(
        self, monkeypatch
    ):
        command = "rm -rf /tmp/cron-smart-direct"
        reviews = []
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda action, description, *, action_kind: reviews.append(
                (action_kind, action, description)
            ) or "approve",
        )
        monkeypatch.setattr(
            approval_module,
            "prompt_dangerous_approval",
            lambda *_args, **_kwargs: pytest.fail("cron smart must not prompt a human"),
        )

        result = check_dangerous_command(command, "local")

        assert result["approved"] is True
        assert result["smart_approved"] is True
        assert reviews == [("shell_command", command, result["description"])]

    @pytest.mark.parametrize(
        ("verdict", "approved"),
        [
            ("approve", True),
            ("deny", False),
            ("escalate", False),
            (RuntimeError("guardian crashed"), False),
        ],
    )
    def test_dangerous_pattern_is_reviewed_once_and_fails_closed(
        self, monkeypatch, verdict, approved
    ):
        command = "rm -rf /tmp/cron-smart-target"
        reviews = []

        def review(reviewed_command, findings, *, action_kind):
            reviews.append((action_kind, reviewed_command, findings))
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(approval_module, "_smart_approve", review)
        monkeypatch.setattr(
            approval_module,
            "prompt_dangerous_approval",
            lambda *_args, **_kwargs: pytest.fail("cron smart must not prompt a human"),
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )

        result = check_all_command_guards(command, "local")

        assert result["approved"] is approved
        assert reviews == [("shell_command", command, result["description"])]
        if approved:
            assert result["smart_approved"] is True
        else:
            assert result["outcome"] in {"denied", "blocked"}
            assert "smart reviewer could not safely approve" in result["message"].lower()
            assert result.get("approval_pending") is not True
            assert result["user_consent"] is False
            assert result.get("user_approved") is not True
            assert "default" not in approval_module._pending
            assert approval_module.get_pending_gateway_approval("default") is None

    @pytest.mark.parametrize(
        ("verdict", "approved"),
        [
            ("approve", True),
            ("deny", False),
            ("escalate", False),
            (RuntimeError("guardian crashed"), False),
        ],
    )
    def test_execute_code_reviews_complete_script_once_and_fails_closed(
        self, monkeypatch, verdict, approved
    ):
        code = "import os\nprint(os.getcwd())"
        synthetic_script = f"execute_code <<'PY'\n{code}\nPY"
        reviews = []

        def review(reviewed_script, findings, *, action_kind):
            reviews.append((action_kind, reviewed_script, findings))
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(approval_module, "_smart_approve", review)
        monkeypatch.setattr(
            approval_module,
            "prompt_dangerous_approval",
            lambda *_args, **_kwargs: pytest.fail("cron smart must not prompt a human"),
        )

        result = approval_module.check_execute_code_guard(code, "local")

        assert result["approved"] is approved
        assert reviews == [("execute_code", synthetic_script, result["description"])]
        if approved:
            assert result["smart_approved"] is True
            assert result["ephemeral_kernel"] is True
        else:
            assert "ephemeral_kernel" not in result
            assert result["outcome"] in {"denied", "blocked"}
            assert "smart reviewer could not safely approve" in result["message"].lower()
            assert result.get("approval_pending") is not True
            assert result["user_consent"] is False
            assert result.get("user_approved") is not True
            assert "default" not in approval_module._pending
            assert approval_module.get_pending_gateway_approval("default") is None

    def _approve_code_cells(self, monkeypatch):
        _configure_cron_smart(monkeypatch)
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda *_args, **_kwargs: "approve",
        )

    @pytest.mark.live_system_guard_bypass
    def test_two_approved_cells_never_share_local_interpreter_state(
        self, monkeypatch
    ):
        from tools import code_execution_tool
        from tools.code_kernel import _KERNELS

        self._approve_code_cells(monkeypatch)
        config = {"mode": "strict", "timeout": 30, "max_tool_calls": 5}
        with patch.object(code_execution_tool, "_load_config", return_value=config), \
             patch("tools.code_kernel.execute_in_session_kernel") as session_execute:
            first = json.loads(code_execution_tool.execute_code(
                "hidden_operation = lambda: 'unreviewed'",
                task_id="cron-smart-local",
                reset=False,
            ))
            assert first["status"] == "success", first
            assert len(_KERNELS) == 0

            second = json.loads(code_execution_tool.execute_code(
                "print(hidden_operation())",
                task_id="cron-smart-local",
                reset=False,
            ))

        assert second["status"] == "error", second
        assert "NameError" in second.get("error", "")
        assert len(_KERNELS) == 0
        session_execute.assert_not_called()

    @pytest.mark.parametrize(
        ("ephemeral", "expected_session_kernel", "expected_reset"),
        [(True, False, True), (False, True, False)],
        ids=["cron-smart-per-call", "normal-persistent"],
    )
    def test_remote_dispatch_selects_per_call_only_for_cron_smart(
        self, monkeypatch, ephemeral, expected_session_kernel, expected_reset
    ):
        from tools import code_execution_tool

        monkeypatch.setattr(
            "tools.terminal_tool._get_env_config",
            lambda: {"env_type": "ssh"},
        )
        guard_result = {"approved": True}
        if ephemeral:
            guard_result.update({
                "smart_approved": True,
                "ephemeral_kernel": True,
            })
        monkeypatch.setattr(
            approval_module,
            "check_execute_code_guard",
            lambda *_args, **_kwargs: guard_result,
        )
        result = json.dumps({"status": "success"})
        with patch.object(
            code_execution_tool, "_execute_remote", return_value=result,
        ) as execute:
            assert code_execution_tool.execute_code(
                "print('reviewed')", task_id="cron-task", reset=False,
            ) == result

        assert execute.call_args.kwargs["use_session_kernel"] is expected_session_kernel
        assert execute.call_args.kwargs["reset"] is expected_reset

    @pytest.mark.parametrize(
        ("verdict", "approved"),
        [("approve", True), ("deny", False), ("escalate", False)],
    )
    def test_tirith_only_finding_is_reviewed_once_and_fails_closed(
        self, monkeypatch, verdict, approved
    ):
        command = "curl https://homograph.example/path"
        reviews = []
        tirith_result = {
            "action": "warn",
            "findings": [
                {
                    "rule_id": "homograph-url",
                    "severity": "HIGH",
                    "title": "Homograph URL",
                    "description": "URL contains lookalike characters",
                }
            ],
            "summary": "homograph URL",
        }

        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda reviewed_command, findings, *, action_kind: reviews.append(
                (action_kind, reviewed_command, findings)
            ) or verdict,
        )
        monkeypatch.setattr(
            approval_module,
            "prompt_dangerous_approval",
            lambda *_args, **_kwargs: pytest.fail("cron smart must not prompt a human"),
        )
        monkeypatch.setattr(
            approval_module,
            "detect_dangerous_command",
            lambda _command: (False, None, None),
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: tirith_result,
        )

        result = check_all_command_guards(command, "local")

        assert result["approved"] is approved
        assert reviews == [("shell_command", command, result["description"])]
        assert "Homograph URL" in result["description"]
        if approved:
            assert result["smart_approved"] is True
        else:
            assert result["outcome"] in {"denied", "blocked"}
            assert "smart reviewer could not safely approve" in result["message"].lower()
            assert result.get("approval_pending") is not True

    def test_combined_tirith_and_dangerous_findings_use_one_review(self, monkeypatch):
        command = "rm -rf /tmp/combined-cron-smart"
        reviews = []
        tirith_result = {
            "action": "warn",
            "findings": [
                {
                    "rule_id": "combined-risk",
                    "severity": "HIGH",
                    "title": "Additional scanner risk",
                    "description": "Tirith also flagged this command",
                }
            ],
            "summary": "combined risk",
        }
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda reviewed_command, findings, *, action_kind: reviews.append(
                (action_kind, reviewed_command, findings)
            ) or "approve",
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: tirith_result,
        )

        result = check_all_command_guards(command, "local")

        assert result["approved"] is True
        assert result["smart_approved"] is True
        assert reviews == [("shell_command", command, result["description"])]
        assert "Additional scanner risk" in result["description"]
        assert "delete" in result["description"].lower()

    @pytest.mark.parametrize("approval_scope", ["session", "permanent", "command"])
    def test_terminal_allowlists_never_skip_cron_smart_review(
        self, monkeypatch, approval_scope
    ):
        command = "rm -rf /tmp/cron-smart-allowlisted"
        pattern_key = detect_dangerous_command(command)[1]
        reviews = []
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda action, description, *, action_kind: reviews.append(
                (action_kind, action, description)
            ) or "deny",
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )
        if approval_scope == "session":
            approval_module.approve_session("default", pattern_key)
        elif approval_scope == "permanent":
            approval_module.approve_permanent(pattern_key)
        else:
            approval_module.approve_permanent(command)

        result = check_all_command_guards(command, "local")

        assert result["approved"] is False
        assert len(reviews) == 1
        assert reviews[0][0] == "shell_command"

    def test_execute_code_allowlists_never_skip_single_cron_smart_review(
        self, monkeypatch
    ):
        reviews = []
        _configure_cron_smart(monkeypatch)
        approval_module.approve_session("default", "execute_code")
        approval_module.approve_permanent("execute_code")
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda action, description, *, action_kind: reviews.append(
                (action_kind, action, description)
            ) or "approve",
        )

        result = approval_module.check_execute_code_guard("print('once')", "local")

        assert result["approved"] is True
        assert result["smart_approved"] is True
        assert len(reviews) == 1
        assert reviews[0][0] == "execute_code"

    @pytest.mark.parametrize(
        ("guard", "payload", "expected_kind", "expected_structure"),
        [
            (
                check_all_command_guards,
                'rm -rf /tmp/cron-secret && printf %s "'
                'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" https://example.test',
                "shell_command",
                "rm -rf /tmp/cron-secret",
            ),
            (
                approval_module.check_execute_code_guard,
                'api_key = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"\nprint(api_key)',
                "execute_code",
                "execute_code <<'PY'",
            ),
        ],
    )
    def test_headless_helper_redacts_before_terminal_or_program_reviewer(
        self, monkeypatch, guard, payload, expected_kind, expected_structure
    ):
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        reviews = []
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda action, description, *, action_kind: reviews.append(
                (action_kind, action, description)
            ) or "escalate",
        )
        monkeypatch.setattr(
            "tools.tirith_security.check_command_security",
            lambda _command: {"action": "allow", "findings": [], "summary": ""},
        )

        result = guard(payload, "local")

        assert result["approved"] is False
        assert len(reviews) == 1
        action_kind, action, description = reviews[0]
        assert action_kind == expected_kind
        assert secret not in action
        assert secret not in description
        assert expected_structure in action


# ---------------------------------------------------------------------------
# check_all_command_guards() with cron session
# ---------------------------------------------------------------------------

class TestCronDenyModeAllGuards:
    """The combined guard function also respects cron_mode."""

    def test_dangerous_command_blocked_in_combined_guard(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_all_command_guards("echo hello", "local")
            assert result["approved"]

    def test_combined_guard_approve_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
            result = check_all_command_guards("rm -rf /tmp/stuff", "local")
            assert result["approved"]

    def test_tirith_content_threat_blocked_in_cron_deny(self, monkeypatch):
        """Content-level threats caught only by tirith (not the regex patterns)
        are blocked in cron-deny mode. Regression for #22070: previously the
        cron-deny early return ran only detect_dangerous_command and returned
        before reaching the tirith check, so these were silently approved."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        # A tirith "block" result while detect_dangerous_command reports safe:
        # proves the block comes from the tirith path, not the regex path.
        fake_tirith = {
            "action": "block",
            "findings": [{"severity": "HIGH", "title": "Homograph URL",
                          "description": "URL contains Cyrillic lookalike chars"}],
            "summary": "homograph url",
        }
        with (
            mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"),
            mock_patch("tools.approval.detect_dangerous_command",
                       return_value=(False, None, None)),
            mock_patch("tools.tirith_security.check_command_security",
                       return_value=fake_tirith),
        ):
            result = check_all_command_guards("curl http://xn--e1afmkfd.example/x", "local")
            assert not result["approved"]
            assert "BLOCKED" in result["message"]

    def test_tirith_import_error_fail_closed_blocks_in_cron_deny(self, monkeypatch):
        """When tirith is unavailable and security.tirith_fail_open is false,
        cron-deny mode blocks rather than silently allowing (a cron session has
        no user to approve). Mirrors the fail-closed handling in the main flow."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        import builtins
        _real_import = builtins.__import__

        def _blocked_import(name, *a, **k):
            if name.endswith("tirith_security"):
                raise ImportError("simulated missing tirith")
            return _real_import(name, *a, **k)

        with (
            mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"),
            mock_patch("tools.approval.detect_dangerous_command",
                       return_value=(False, None, None)),
            mock_patch("hermes_cli.config.load_config_readonly",
                       return_value={"security": {"tirith_enabled": True,
                                                   "tirith_fail_open": False}}),
            mock_patch.object(builtins, "__import__", _blocked_import),
        ):
            result = check_all_command_guards("echo hi", "local")
            assert not result["approved"]
            assert "tirith_fail_open" in result["message"]

    def test_tirith_import_error_fail_open_allows_in_cron_deny(self, monkeypatch):
        """When tirith is unavailable and tirith_fail_open is true (default),
        cron-deny mode allows safe commands — preserving pre-#22070 behavior."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        import builtins
        _real_import = builtins.__import__

        def _blocked_import(name, *a, **k):
            if name.endswith("tirith_security"):
                raise ImportError("simulated missing tirith")
            return _real_import(name, *a, **k)

        with (
            mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"),
            mock_patch("tools.approval.detect_dangerous_command",
                       return_value=(False, None, None)),
            mock_patch("hermes_cli.config.load_config_readonly",
                       return_value={"security": {"tirith_enabled": True,
                                                   "tirith_fail_open": True}}),
            mock_patch.object(builtins, "__import__", _blocked_import),
        ):
            result = check_all_command_guards("echo hi", "local")
            assert result["approved"]


# ---------------------------------------------------------------------------
# Edge cases: cron mode interaction with other approval mechanisms
# ---------------------------------------------------------------------------

class TestCronModeInteractions:
    """Cron mode should NOT interfere with other approval bypass mechanisms."""

    def test_container_env_still_auto_approves(self, monkeypatch):
        """Docker/sandbox environments bypass approvals regardless of cron_mode."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_dangerous_command("rm -rf /", "docker")
            assert result["approved"]

    def test_yolo_overrides_cron_deny(self, monkeypatch):
        """--yolo still bypasses cron_mode=deny for dangerous (non-hardline) commands."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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

    @pytest.mark.parametrize(
        "guard,payload",
        [
            (check_all_command_guards, "rm -rf /tmp/cron-smart-off"),
            (approval_module.check_execute_code_guard, "print('cron smart off')"),
        ],
    )
    def test_global_approval_off_still_bypasses_cron_smart_guardian(
        self, monkeypatch, guard, payload
    ):
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "off")
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda *_args, **_kwargs: pytest.fail(
                "global approvals.mode=off must bypass the guardian"
            ),
        )

        assert guard(payload, "local") == {"approved": True, "message": None}

    def test_container_still_bypasses_cron_smart_guardian(self, monkeypatch):
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda *_args, **_kwargs: pytest.fail(
                "isolated containers must bypass the guardian"
            ),
        )

        result = check_all_command_guards("rm -rf /", "docker")

        assert result == {"approved": True, "message": None}

    @pytest.mark.parametrize(
        "guard,payload",
        [
            (check_all_command_guards, "rm -rf /tmp/cron-smart-yolo"),
            (approval_module.check_execute_code_guard, "print('cron smart yolo')"),
        ],
    )
    def test_yolo_still_bypasses_cron_smart_guardian(
        self, monkeypatch, guard, payload
    ):
        _configure_cron_smart(monkeypatch)
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
        monkeypatch.setattr(
            approval_module,
            "_smart_approve",
            lambda *_args, **_kwargs: pytest.fail(
                "yolo must bypass the guardian"
            ),
        )

        assert guard(payload, "local") == {"approved": True, "message": None}


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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
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
