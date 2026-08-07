"""Typed exact execution authority for top-level agent work."""

from __future__ import annotations

import json

import pytest

from tools import approval
from tools import terminal_tool


OWNER_ID = "owner-top-level-exact"
SESSION_KEY = "session-top-level-exact"


@pytest.fixture(autouse=True)
def _exact_local_runtime(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "approvals:\n  plan_owner_user_ids:\n    - owner-top-level-exact\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(approval, "_writer_boundary_policy_required", lambda: False)
    monkeypatch.setattr(approval, "_canonical_brain_required", lambda: False)
    monkeypatch.setattr(approval, "_observed_session_user_id", lambda: "")
    monkeypatch.setattr(approval, "_observed_session_platform", lambda: "")
    monkeypatch.setattr(approval, "_observed_session_message_id", lambda: "")
    approval.clear_session_local(SESSION_KEY)
    session_token = approval.set_current_session_key(SESSION_KEY)
    yield
    approval.reset_current_session_key(session_token)
    approval.clear_session_local(SESSION_KEY)


def _grant(*, command: str, code: str = "") -> None:
    approval.grant_plan_capability(
        session_key=SESSION_KEY,
        plan_id="plan-top-level-exact",
        plan_revision=1,
        exact_commands=[command],
        exact_code_scripts=[code] if code else [],
        approved_by_user_id=OWNER_ID,
        max_uses_per_command=1,
    )


def _assert_semantic_authority_absent() -> None:
    for name in (
        "detect_hardline_command",
        "detect_dangerous_command",
        "_match_user_deny_rule",
        "_command_matches_permanent_allowlist",
    ):
        assert not hasattr(approval, name)


def test_top_level_exact_hit_and_miss_never_reach_semantic_authority(monkeypatch):
    command = "printf 'exact terminal bytes'"
    code = "print('exact code bytes')\n"
    _grant(command=command, code=code)
    _assert_semantic_authority_absent()

    mismatch = approval.check_all_command_guards(command + " ", "local")
    terminal_hit = approval.check_all_command_guards(command, "local")
    code_hit = approval.check_execute_code_guard(code, "local")

    assert mismatch["outcome"] == "exact_plan_capability_required"
    assert terminal_hit["plan_capability"] == "plan-top-level-exact"
    assert code_hit["plan_capability"] == "plan-top-level-exact"


def test_exact_subject_binds_backend_resource_and_raw_input(monkeypatch):
    command = "printf exact"
    _grant(command=command)
    _assert_semantic_authority_absent()

    backend_miss = approval.check_all_command_guards(
        command,
        "ssh",
        env_config={
            "env_type": "ssh",
            "ssh_host": "host.example",
            "ssh_user": "runner",
            "ssh_port": 22,
            "ssh_key": "/keys/runner",
        },
    )
    raw_miss = approval.check_all_command_guards(f"{command}\n", "local")

    assert backend_miss["outcome"] == "exact_plan_capability_required"
    assert raw_miss["outcome"] == "exact_plan_capability_required"

    with pytest.raises(ValueError, match="resource binding mismatch"):
        approval.consume_plan_capability(
            SESSION_KEY,
            command,
            resource_sha256="0" * 64,
        )


def test_exact_subject_binds_lexically_normalized_local_cwd(tmp_path):
    base = tmp_path / "workspace"
    config = {"env_type": "local", "cwd": str(base)}

    relative = approval._exact_execution_subject(
        "terminal",
        "printf exact",
        env_type="local",
        env_config=config,
        effective_cwd="build/../release",
    )
    absolute = approval._exact_execution_subject(
        "terminal",
        "printf exact",
        env_type="local",
        env_config=config,
        effective_cwd=str(base / "release"),
    )
    mismatch = approval._exact_execution_subject(
        "terminal",
        "printf exact",
        env_type="local",
        env_config=config,
        effective_cwd=str(base / "other"),
    )

    assert relative == absolute
    assert relative["cwd_sha256"] != mismatch["cwd_sha256"]
    assert relative["subject_sha256"] != mismatch["subject_sha256"]


def test_exact_subject_uses_posix_relative_cwd_contract_for_ssh():
    config = {
        "env_type": "ssh",
        "cwd": "/srv/project",
        "ssh_host": "host.example",
        "ssh_user": "runner",
        "ssh_port": 22,
        "ssh_key": "/keys/runner",
    }

    relative = approval._exact_execution_subject(
        "terminal",
        "printf exact",
        env_type="ssh",
        env_config=config,
        effective_cwd="build/../release",
    )
    absolute = approval._exact_execution_subject(
        "terminal",
        "printf exact",
        env_type="ssh",
        env_config=config,
        effective_cwd="/srv/project/release",
    )

    assert relative == absolute
    assert approval._normalize_execution_cwd(
        "logs/../out",
        env_type="ssh",
        env_config={**config, "cwd": "~/project"},
    ) == "~/project/out"


def test_plan_capability_rejects_same_command_under_different_cwd(
    monkeypatch,
    tmp_path,
):
    base = tmp_path / "workspace"
    other = tmp_path / "other"
    base.mkdir()
    other.mkdir()
    config = {
        "env_type": "local",
        "cwd": str(base),
        "timeout": 30,
        "lifetime_seconds": 60,
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    terminal_tool.record_session_cwd(SESSION_KEY, str(base))
    command = "printf cwd-bound"
    _grant(command=command)
    _assert_semantic_authority_absent()

    mismatch = approval.check_all_command_guards(
        command,
        "local",
        env_config=config,
        effective_cwd=str(other),
    )
    hit = approval.check_all_command_guards(
        command,
        "local",
        env_config=config,
        effective_cwd=str(base / "."),
    )

    assert mismatch["outcome"] == "exact_plan_capability_required"
    assert hit["plan_capability"] == "plan-top-level-exact"


@pytest.mark.parametrize(
    ("env_type", "base_cwd", "relative_cwd", "expected_cwd"),
    [
        ("local", "/tmp/hermes-project", "build/../release", "/tmp/hermes-project/release"),
        ("ssh", "/srv/project", "build/../release", "/srv/project/release"),
    ],
)
def test_terminal_uses_same_normalized_cwd_for_authority_and_execution(
    monkeypatch,
    env_type,
    base_cwd,
    relative_cwd,
    expected_cwd,
):
    class FakeEnv:
        env = {}
        cwd = base_cwd

        def execute(self, _command, **kwargs):
            self.executed_cwd = kwargs["cwd"]
            return {"output": "", "returncode": 0}

    config = {
        "env_type": env_type,
        "cwd": base_cwd,
        "timeout": 30,
        "lifetime_seconds": 60,
        "ssh_host": "host.example",
        "ssh_user": "runner",
        "ssh_port": 22,
        "ssh_key": "/keys/runner",
    }
    fake = FakeEnv()
    seen = {}

    def exact_authority(_command, _env_type, **kwargs):
        seen["authority_cwd"] = kwargs["effective_cwd"]
        return {"approved": True, "plan_capability": "plan-cwd"}

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(terminal_tool, "_active_environments", {SESSION_KEY: fake})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_check_exact_authority", exact_authority)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda *_args, **kwargs: kwargs["exact_authority"],
    )

    result = json.loads(terminal_tool.terminal_tool(
        command="printf exact",
        task_id=SESSION_KEY,
        workdir=relative_cwd,
    ))

    assert result["exit_code"] == 0
    assert seen["authority_cwd"] == expected_cwd
    assert fake.executed_cwd == expected_cwd


def test_consume_is_idempotent_for_same_exact_tool_call_id():
    command = "printf idempotent"
    _grant(command=command)
    first_tokens = approval.set_current_observability_context(
        turn_id="turn-1",
        tool_call_id="call-1",
    )
    try:
        assert approval.consume_plan_capability(SESSION_KEY, command) == (
            "plan-top-level-exact"
        )
        assert approval.consume_plan_capability(SESSION_KEY, command) == (
            "plan-top-level-exact"
        )
    finally:
        approval.reset_current_observability_context(first_tokens)

    second_tokens = approval.set_current_observability_context(
        turn_id="turn-1",
        tool_call_id="call-2",
    )
    try:
        assert approval.consume_plan_capability(SESSION_KEY, command) is None
    finally:
        approval.reset_current_observability_context(second_tokens)


def test_explicit_tool_worker_session_binding_does_not_fall_back_to_default():
    command = "printf session-bound"
    _grant(command=command)
    empty_context = approval.set_current_session_key("")
    try:
        decision = approval.check_all_command_guards(
            command,
            "local",
            session_key=SESSION_KEY,
        )
    finally:
        approval.reset_current_session_key(empty_context)

    assert decision["plan_capability"] == "plan-top-level-exact"


def test_exact_miss_uses_one_operation_transport_without_semantics(monkeypatch):
    _grant(command="printf planned")
    _assert_semantic_authority_absent()
    seen = []

    def approve_once(command, description, **kwargs):
        seen.append((command, description, kwargs))
        return "once"

    decision = approval.check_all_command_guards(
        "printf additional-exact-operation",
        "local",
        approval_callback=approve_once,
    )

    assert decision["approved"] is True
    assert decision["exact_one_operation"] is True
    assert seen[0][2]["allow_permanent"] is False


def test_terminal_exact_hit_needs_no_gateway_lifecycle_text_parser(
    monkeypatch,
    tmp_path,
):
    command = "pwd"
    config = {
        "env_type": "local",
        "cwd": str(tmp_path),
        "timeout": 30,
        "lifetime_seconds": 60,
        "local_persistent": False,
        "host_cwd": str(tmp_path),
    }
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    terminal_tool.record_session_cwd(SESSION_KEY, str(tmp_path))
    _grant(command=command)
    assert not hasattr(terminal_tool, "_contains_gateway_lifecycle_command")

    result = json.loads(
        terminal_tool.terminal_tool(
            command=command,
            task_id=SESSION_KEY,
            workdir=str(tmp_path),
        )
    )

    assert result["exit_code"] == 0
    approval = result["approval"]
    assert "requester-authorized" in approval
    assert "plan-top-level-exact" in approval
    assert "owner-approved" not in approval
