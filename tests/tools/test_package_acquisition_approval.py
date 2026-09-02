"""Security boundary tests for package-acquisition approval."""

from unittest.mock import patch as mock_patch

import pytest

import tools.approval as approval_module
from tools.approval import check_all_command_guards, detect_package_acquisition


@pytest.fixture(autouse=True)
def _isolate_approval_state():
    permanent = set(approval_module._permanent_approved)
    approval_module._permanent_approved.clear()
    yield
    approval_module._permanent_approved.clear()
    approval_module._permanent_approved.update(permanent)


@pytest.mark.parametrize(
    "command",
    [
        "pip install plausible-vendor-sdk",
        "python -m pip install plausible-vendor-sdk",
        "python3.11 -m pip install -r requirements.txt",
        "pipx run plausible-vendor-cli",
        "uv pip install plausible-vendor-sdk",
        "uv add plausible-vendor-sdk",
        "uv run --with plausible-vendor-sdk python -c 'import plausible_vendor_sdk'",
        "uvx plausible-vendor-cli",
        "npm install plausible-vendor-sdk",
        "npm add plausible-vendor-sdk",
        "npm --prefix run install plausible-vendor-sdk",
        "uv --directory run add plausible-vendor-sdk",
        "npm ci",
        "npm exec plausible-vendor-cli",
        "npx plausible-vendor-cli",
        "pnpm add plausible-vendor-sdk",
        "pnpm dlx plausible-vendor-cli",
        "yarn add plausible-vendor-sdk",
        "yarn dlx plausible-vendor-cli",
        "cargo install plausible-vendor-cli",
        "gem install plausible-vendor-cli",
        "go install example.invalid/vendor/cli@latest",
        "dotnet tool install plausible-vendor-cli",
        "poetry add plausible-vendor-sdk",
        "composer require plausible-vendor/sdk",
        "bundle install",
        "apk add openssh",
        "sudo apt-get install plausible-vendor-cli",
        "command pip install plausible-vendor-sdk",
        "builtin pip install plausible-vendor-sdk",
        "env -i /opt/venv/bin/pip install plausible-vendor-sdk",
        "sudo -u nobody /opt/venv/bin/pip install plausible-vendor-sdk",
        "env FOO=1 npx plausible-vendor-cli",
        "PIP_INDEX_URL=https://registry.example /opt/venv/bin/python -m pip install plausible-vendor-sdk",
        "./venv/bin/pip install plausible-vendor-sdk",
        "./node_modules/.bin/npx plausible-vendor-cli",
        "echo ready && uv add plausible-vendor-sdk",
        "sh -c 'uv add plausible-vendor-sdk'",
        "bash <<'EOF'\npip install plausible-vendor-sdk\nEOF",
        "nice pip install plausible-vendor-sdk",
        "timeout 5 pip install plausible-vendor-sdk",
        "stdbuf -oL pip install plausible-vendor-sdk",
        "xargs pip install <<< plausible-vendor-sdk",
        "find . -exec pip install plausible-vendor-sdk \\;",
        "eval 'pip install plausible-vendor-sdk'",
        "source <(pip install plausible-vendor-sdk)",
        ". <(pip install plausible-vendor-sdk)",
        "docker run --rm python:3 pip install plausible-vendor-sdk",
        "docker exec build-container pip install plausible-vendor-sdk",
        "podman run --rm node npm install plausible-vendor-sdk",
        "pip --disable-pip-version-check install plausible-vendor-sdk",
        "pip --cache-dir list install plausible-vendor-sdk",
        "python -m pip --cache-dir list install plausible-vendor-sdk",
        "pip --trusted-host show install plausible-vendor-sdk",
        "apt-get -o remove install plausible-vendor-sdk",
        "uv --quiet pip install plausible-vendor-sdk",
        "uv pip --system install plausible-vendor-sdk",
        "/usr/bin/env pip install plausible-vendor-sdk",
        r"C:\Python311\Scripts\pip.exe install plausible-vendor-sdk",
        r"cmd /c C:\Python311\Scripts\pip.exe install plausible-vendor-sdk",
        "wsl python3 -m pip install plausible-vendor-sdk",
        'pip in"stall" plausible-vendor-sdk',
        'pip i"$EMPTY"nstall plausible-vendor-sdk',
    ],
)
def test_detects_package_acquisition_commands(command):
    detected, key, description = detect_package_acquisition(command)

    assert detected is True
    assert key == "package acquisition"
    assert "package" in description.lower()


@pytest.mark.parametrize(
    "command",
    [
        "apk add openssh",
        "npm add plausible-vendor-sdk",
        "uv run --with plausible-vendor-sdk python -c 'import plausible_vendor_sdk'",
    ],
)
def test_acquisition_aliases_reach_owner_gate_before_isolated_and_off_bypasses(
    monkeypatch, command
):
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "package-alias-owner-gate-test")
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "off"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)

    result = check_all_command_guards(command, "docker")

    assert result["approved"] is False
    assert result["approval_pending"] is True
    assert result["pattern_key"] == "package acquisition"
    assert result["single_operation"] is True


@pytest.mark.parametrize(
    "command",
    [
        "pip list",
        "pip show plausible-vendor-sdk",
        "npm test",
        "npm run build",
        "npm --prefix install run build",
        "uv --directory add run python script.py",
        "pnpm exec eslint .",
        "yarn run test",
        "cargo test",
        "go test ./...",
        "grep 'pip install plausible-vendor-sdk' README.md",
        "echo 'npx plausible-vendor-cli'",
        "python - <<'PY'\nprint('pip install plausible-vendor-sdk')\nPY",
        "printf '%s\\n' 'npm install plausible-vendor-sdk'",
        "cat > README.md <<'EOF'\npip install plausible-vendor-sdk\nEOF",
        "pip install --help",
        "npm install --help",
        "npx --help plausible-cli",
        "uvx --help plausible-cli",
        "deno --help install pkg",
        "pacman --help -S pkg",
    ],
)
def test_does_not_flag_non_acquisition_package_manager_usage(command):
    assert detect_package_acquisition(command) == (False, None, None)


def _safe_tirith(_command):
    return {"action": "allow", "findings": [], "summary": ""}


def test_smart_mode_never_sends_package_acquisition_to_aux_llm(monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "package-smart-test")
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "smart"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)
    monkeypatch.setattr(
        approval_module,
        "_smart_approve",
        lambda *_args, **_kwargs: pytest.fail(
            "package acquisition reached smart approval"
        ),
    )

    result = check_all_command_guards("npx plausible-vendor-cli", "local")

    assert result["approved"] is False
    assert result["approval_pending"] is True
    assert result["pattern_key"] == "package acquisition"
    assert result["allow_permanent"] is False
    assert result["single_operation"] is True


def test_package_acquisition_ignores_session_and_permanent_allowlists(monkeypatch):
    session_key = "package-allowlist-test"
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "manual"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)
    approval_module.approve_session(session_key, "package acquisition")
    approval_module._permanent_approved.add("package acquisition")

    result = check_all_command_guards("pip install plausible-vendor-sdk", "local")

    assert result["approved"] is False
    assert result["approval_pending"] is True
    assert result["single_operation"] is True


@pytest.mark.parametrize("approval_mode", ["smart", "off"])
def test_unattended_package_acquisition_fails_closed_even_when_cron_or_yolo_allows(
    monkeypatch, approval_mode
):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(
        approval_module,
        "_get_approval_config",
        lambda: {"mode": approval_mode, "cron_mode": "approve"},
    )
    monkeypatch.setattr(approval_module, "_get_cron_approval_mode", lambda: "approve")

    result = check_all_command_guards("uv add plausible-vendor-sdk", "local")

    assert result["approved"] is False
    assert result["pattern_key"] == "package acquisition"
    assert "unattended" in result["message"].lower()


def test_owner_approval_is_one_operation_even_if_client_returns_session(monkeypatch):
    session_key = "package-once-test"
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "manual"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)
    approval_module.clear_session(session_key)

    with mock_patch.object(
        approval_module, "prompt_dangerous_approval", return_value="session"
    ) as prompt:
        first = check_all_command_guards("pip install plausible-vendor-sdk", "local")

    assert first["approved"] is True
    assert first["user_approved"] is True
    assert approval_module.is_approved(session_key, "package acquisition") is False
    assert prompt.call_args.kwargs["allow_session"] is False
    assert prompt.call_args.kwargs["allow_permanent"] is False

    with mock_patch.object(
        approval_module, "prompt_dangerous_approval", return_value="deny"
    ) as second_prompt:
        second = check_all_command_guards("pip install another-sdk", "local")

    assert second["approved"] is False
    second_prompt.assert_called_once()


def test_interactive_yolo_still_requires_owner_approval(monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "package-yolo-test")
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "off"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)

    result = check_all_command_guards("npm install plausible-vendor-sdk", "local")

    assert result["approved"] is False
    assert result["approval_pending"] is True
    assert result["pattern_key"] == "package acquisition"
    assert result["allow_session"] is False
    assert result["allow_permanent"] is False


def test_selected_transport_cannot_persist_package_approval(monkeypatch):
    session_key = "package-transport-test"
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "manual"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)
    approval_module.clear_session(session_key)

    seen = {}

    def _transport(**kwargs):
        seen.update(kwargs)
        return {"selected": True, "choice": "always"}

    monkeypatch.setattr(approval_module, "_present_with_selected_transport", _transport)

    result = check_all_command_guards("uv add plausible-vendor-sdk", "local")

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert seen["allow_session"] is False
    assert seen["allow_permanent"] is False
    assert approval_module.is_approved(session_key, "package acquisition") is False


def test_isolated_container_still_owner_gates_package_acquisition(monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "package-container-test")
    monkeypatch.setattr(
        approval_module, "_get_approval_config", lambda: {"mode": "manual"}
    )
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.tirith_security.check_command_security", _safe_tirith)

    result = check_all_command_guards(
        "pip install plausible-vendor-sdk",
        "docker",
        has_host_access=False,
    )

    assert result["approved"] is False
    assert result["approval_pending"] is True
    assert result["pattern_key"] == "package acquisition"
    assert result["allow_session"] is False
    assert result["allow_permanent"] is False


def test_stale_gateway_choice_is_narrowed_before_wakeup():
    session_key = "package-stale-choice-test"
    entry = approval_module._ApprovalEntry({
        "request_id": "pkg-1",
        "single_operation": True,
    })
    approval_module._gateway_queues[session_key] = [entry]
    try:
        resolved = approval_module.resolve_gateway_approval(
            session_key,
            "always",
            request_id="pkg-1",
        )
        assert resolved == 1
        assert entry.result == "once"
    finally:
        approval_module._gateway_queues.pop(session_key, None)


def test_approve_all_does_not_resolve_single_operation_requests():
    session_key = "package-approve-all-test"
    first = approval_module._ApprovalEntry({
        "request_id": "pkg-1",
        "single_operation": True,
    })
    second = approval_module._ApprovalEntry({
        "request_id": "pkg-2",
        "single_operation": True,
    })
    approval_module._gateway_queues[session_key] = [first, second]
    try:
        resolved = approval_module.resolve_gateway_approval(
            session_key,
            "once",
            resolve_all=True,
        )
        assert resolved == 0
        assert first.result is None
        assert second.result is None
        assert approval_module._gateway_queues[session_key] == [first, second]
    finally:
        approval_module._gateway_queues.pop(session_key, None)


def test_identical_single_operation_requests_do_not_coalesce(monkeypatch):
    session_key = "package-no-coalesce-test"
    existing = approval_module._ApprovalEntry({
        "request_id": "pkg-existing",
        "command": "pip install plausible-vendor-sdk",
        "pattern_keys": ["package acquisition"],
        "single_operation": True,
    })
    approval_module._gateway_queues[session_key] = [existing]
    monkeypatch.setattr(
        approval_module,
        "_await_coalesced_leader",
        lambda *_args, **_kwargs: pytest.fail(
            "single-operation request coalesced with another execution"
        ),
    )

    def resolve_exact(approval_data):
        approval_module.resolve_gateway_approval(
            session_key,
            "once",
            request_id=approval_data["request_id"],
        )

    try:
        result = approval_module._await_gateway_decision(
            session_key,
            resolve_exact,
            {
                "command": "pip install plausible-vendor-sdk",
                "pattern_key": "package acquisition",
                "pattern_keys": ["package acquisition"],
                "description": approval_module.PACKAGE_ACQUISITION_DESCRIPTION,
                "single_operation": True,
                "allow_session": False,
                "allow_permanent": False,
            },
        )
        assert result["choice"] == "once"
        assert existing.result is None
    finally:
        approval_module._gateway_queues.pop(session_key, None)
