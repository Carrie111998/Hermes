"""Regression coverage for browser automation exception redaction (FAI-8390)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest


_SENTINELS = {
    "authorization": "browser-auth-sentinel-8390",
    "cookie": "browser-cookie-sentinel-8390",
    "access": "browser-access-sentinel-8390",
    "refresh": "browser-refresh-sentinel-8390",
    "callback_repr": "callback-repr-auth-sentinel-8390",
}


def _credential_bearing_browser_error() -> RuntimeError:
    return RuntimeError(
        "browser request failed:\n"
        f"Authorization: Bearer {_SENTINELS['authorization']}\n"
        f"Cookie: admin_session={_SENTINELS['cookie']}\n"
        f'{{"access_token":"{_SENTINELS["access"]}",'
        f'"refresh_token":"{_SENTINELS["refresh"]}"}}'
    )


class _CredentialBearingFailingCallback:
    """Callable whose exception and diagnostic representation both carry secrets."""

    def __init__(self, observed=None):
        self.observed = observed

    def __repr__(self):
        return f"<callback Authorization: Bearer {_SENTINELS['callback_repr']}>"

    def __call__(self, **payload):
        if self.observed is not None:
            self.observed.append(payload)
        raise _credential_bearing_browser_error()


def test_failing_browser_request_redacts_returned_error_transcript_and_logs(caplog):
    """A browser handler exception must be safe at every persistence boundary."""
    from model_tools import handle_function_call

    caplog.set_level(logging.ERROR)
    with patch("model_tools.registry.dispatch", side_effect=_credential_bearing_browser_error()):
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    transcript = json.dumps(
        [{"role": "tool", "name": "browser_console", "content": returned}],
        ensure_ascii=False,
    )
    persisted_logs = caplog.text

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
        assert sentinel not in transcript
        assert sentinel not in persisted_logs
    assert "***" in returned


def test_structured_browser_failure_is_redacted_before_observer_and_transcript():
    """Normally returned error JSON must be safe before post-tool observers run."""
    from model_tools import handle_function_call
    from tools.registry import registry

    observed = []
    entry = registry.get_entry("browser_console")
    assert entry is not None
    original = entry.handler

    def failed_browser_request(_args, **_kwargs):
        return json.dumps(
            {
                "success": False,
                "error": str(_credential_bearing_browser_error()),
            }
        )

    entry.handler = failed_browser_request
    try:
        with (
            patch(
                "hermes_cli.plugins.has_hook",
                side_effect=lambda name: name == "post_tool_call",
            ),
            patch(
                "hermes_cli.plugins.invoke_hook",
                side_effect=lambda _name, **payload: observed.append(payload) or [],
            ),
        ):
            returned = handle_function_call(
                "browser_console",
                {"expression": "fetch('/api/admin/acquisition')"},
                task_id="fai-8390",
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
            )
    finally:
        entry.handler = original

    transcript = json.dumps(
        [{"role": "tool", "name": "browser_console", "content": returned}],
        ensure_ascii=False,
    )
    observed_payload = json.dumps(observed, ensure_ascii=False)
    assert observed, "post_tool_call observer did not receive the tool result"
    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
        assert sentinel not in transcript
        assert sentinel not in observed_payload
    assert "***" in returned


def test_post_tool_call_exception_redacts_browser_authentication_material(caplog):
    """A failing observer cannot reflect browser credentials into process logs."""
    from model_tools import handle_function_call
    from hermes_cli.plugins import PluginManager

    observed = []
    raw = json.dumps(
        {
            "success": False,
            "error": str(_credential_bearing_browser_error()),
        }
    )

    manager = PluginManager()
    manager._hooks["post_tool_call"] = [_CredentialBearingFailingCallback(observed)]
    caplog.set_level(logging.DEBUG)
    with (
        patch("model_tools.registry.dispatch", return_value=raw),
        patch("hermes_cli.plugins._plugin_manager", manager),
    ):
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    transcript = json.dumps(
        [{"role": "tool", "name": "browser_console", "content": returned}],
        ensure_ascii=False,
    )
    observed_payload = json.dumps(observed, ensure_ascii=False)

    assert observed, "post_tool_call observer did not receive the tool result"
    for sentinel in _SENTINELS.values():
        assert sentinel not in caplog.text
        assert sentinel not in returned
        assert sentinel not in observed_payload
        assert sentinel not in transcript
    assert "***" in caplog.text
    assert "***" in returned


def test_plugin_middleware_exception_redacts_authentication_material(caplog):
    """Middleware callback isolation cannot persist raw credentials either."""
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._middleware["tool_request"] = [_CredentialBearingFailingCallback()]
    caplog.set_level(logging.WARNING)

    assert manager.invoke_middleware("tool_request", payload={}) == []

    for sentinel in _SENTINELS.values():
        assert sentinel not in caplog.text
    assert "***" in caplog.text


def test_tool_execution_middleware_exception_redacts_browser_authentication_material(
    caplog,
):
    """Execution middleware cannot bypass the manager's safe logging boundary."""
    from hermes_cli.plugins import PluginManager
    from model_tools import handle_function_call

    observed = []
    raw = json.dumps(
        {
            "success": False,
            "error": str(_credential_bearing_browser_error()),
        }
    )

    manager = PluginManager()
    manager._middleware["tool_execution"] = [_CredentialBearingFailingCallback()]
    manager._hooks["post_tool_call"] = [lambda **payload: observed.append(payload)]
    caplog.set_level(logging.DEBUG)

    with (
        patch("hermes_cli.plugins._plugin_manager", manager),
        patch("model_tools.registry.dispatch", return_value=raw),
    ):
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    transcript = json.dumps(
        [{"role": "tool", "name": "browser_console", "content": returned}],
        ensure_ascii=False,
    )
    observed_payload = json.dumps(observed, ensure_ascii=False)

    assert observed, "post_tool_call observer did not receive the tool result"
    for sentinel in _SENTINELS.values():
        assert sentinel not in caplog.text
        assert sentinel not in returned
        assert sentinel not in observed_payload
        assert sentinel not in transcript
    assert "***" in caplog.text
    assert "***" in returned
    assert "Traceback" not in caplog.text


def test_registry_exception_is_redacted_before_dispatch_log_and_result(caplog):
    """The central registry cannot log a raw exception before outer redaction."""
    from model_tools import handle_function_call
    from tools.registry import registry

    caplog.set_level(logging.ERROR)
    entry = registry.get_entry("browser_console")
    assert entry is not None
    original = entry.handler

    def failed_browser_request(_args, **_kwargs):
        raise _credential_bearing_browser_error()

    entry.handler = failed_browser_request
    try:
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )
    finally:
        entry.handler = original

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
        assert sentinel not in caplog.text
    assert "***" in returned


def test_tool_error_helper_force_redacts_authentication_material():
    """Structured failures are sanitized at construction, even before dispatch."""
    from tools.registry import tool_error

    returned = tool_error(_credential_bearing_browser_error(), success=False)

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
    assert "***" in returned


def test_tool_error_helper_redacts_sensitive_structured_fields():
    """Authentication fields adjacent to an error cannot bypass message redaction."""
    from tools.registry import tool_error

    returned = tool_error(
        "browser request failed",
        success=False,
        headers={
            "Authorization": _SENTINELS["authorization"],
            "Cookie": _SENTINELS["cookie"],
        },
        accessToken=_SENTINELS["access"],
        refresh_token=_SENTINELS["refresh"],
    )

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
    assert returned.count("***") >= 4


def test_structured_tool_error_fails_closed_for_hostile_nested_value():
    """An unserializable nested diagnostic cannot bypass or break redaction."""
    from tools.registry import tool_error

    class HostileDiagnostic:
        def __str__(self):
            raise RuntimeError("cannot stringify diagnostic")

    returned = tool_error(
        "browser request failed",
        success=False,
        diagnostic=HostileDiagnostic(),
    )

    payload = json.loads(returned)
    assert payload["success"] is False
    assert "redaction failed" in payload["diagnostic"].lower()


@pytest.mark.parametrize(
    ("value", "sentinel"),
    [
        (
            {"headers": {"X-API-Key": "opaque-x-api-sentinel-8390"}},
            "opaque-x-api-sentinel-8390",
        ),
        (
            {"headers": {"X-Auth-Token": "opaque-x-auth-sentinel-8390"}},
            "opaque-x-auth-sentinel-8390",
        ),
        (
            "request failed: {'X-API-Key': 'repr-x-api-sentinel-8390'}",
            "repr-x-api-sentinel-8390",
        ),
        (
            "request failed: {'Authorization': ['Bearer list-auth-sentinel-8390']}",
            "list-auth-sentinel-8390",
        ),
        (
            "request failed: {'access_token': ['list-access-sentinel-8390']}",
            "list-access-sentinel-8390",
        ),
        (
            {"headers": {"X-Goog-Api-Key": "x-goog-sentinel-8390"}},
            "x-goog-sentinel-8390",
        ),
        (
            {"headers": {"X-API-Token": "x-api-token-sentinel-8390"}},
            "x-api-token-sentinel-8390",
        ),
        (
            {"headers": {"X-Access-Token": "x-access-token-sentinel-8390"}},
            "x-access-token-sentinel-8390",
        ),
        (
            "request failed: {'client_secret': 'client-secret-sentinel-8390'}",
            "client-secret-sentinel-8390",
        ),
        (
            "request failed: {'password': 'password-sentinel-8390'}",
            "password-sentinel-8390",
        ),
        (
            "request failed: {'Authorization': ('Bearer', 'tuple-auth-sentinel-8390')}",
            "tuple-auth-sentinel-8390",
        ),
        (
            {"Authorization: Bearer mapping-key-sentinel-8390": True},
            "mapping-key-sentinel-8390",
        ),
        ("X-Registry-Auth: registry-auth-sentinel-8390", "registry-auth-sentinel-8390"),
        ("X-Sentry-Auth: sentry-auth-sentinel-8390", "sentry-auth-sentinel-8390"),
        ("X-Auth-Key: auth-key-sentinel-8390", "auth-key-sentinel-8390"),
        (
            "Ocp-Apim-Subscription-Key: subscription-sentinel-8390",
            "subscription-sentinel-8390",
        ),
        (
            {"headers": {"X-Registry-Auth": "structured-registry-sentinel-8390"}},
            "structured-registry-sentinel-8390",
        ),
        (
            {"credentials": "credentials-sentinel-8390"},
            "credentials-sentinel-8390",
        ),
        (
            {"client_credentials": "client-credentials-sentinel-8390"},
            "client-credentials-sentinel-8390",
        ),
        (
            {"aws_credentials": ["aws-access-sentinel-8390", "aws-secret-sentinel-8390"]},
            "aws-access-sentinel-8390",
        ),
        (
            "X-Registry-Config: registry-config-sentinel-8390",
            "registry-config-sentinel-8390",
        ),
        (
            {"headers": {"X-Registry-Config": "structured-config-sentinel-8390"}},
            "structured-config-sentinel-8390",
        ),
    ],
)
def test_reviewer_authentication_variants_are_force_redacted(value, sentinel):
    """Header aliases and non-scalar repr values remain non-replayable."""
    from agent.redact import redact_tool_error_value

    returned = redact_tool_error_value(value)

    assert sentinel not in str(returned)
    assert "***" in str(returned)


def test_structured_tool_error_suppresses_unknown_objects_and_cycles():
    """Unknown and cyclic diagnostics fail closed without huge partial payloads."""
    from agent.redact import redact_tool_error_value

    class OpaqueSecret:
        def __str__(self):
            return "opaque-object-sentinel-8390"

    cycle = {}
    cycle["self"] = cycle
    returned = redact_tool_error_value(
        {"unknown": OpaqueSecret(), "cycle": cycle}
    )
    serialized = json.dumps(returned)

    assert "opaque-object-sentinel-8390" not in serialized
    assert "redaction failed" in serialized.lower()
    assert len(serialized) < 1_000


def test_browser_cleanup_redacts_provider_exception_before_logging(caplog):
    """Cleanup paths use the same safe browser-error logging boundary."""
    import tools.browser_tool as browser_tool

    sentinel = "provider-cleanup-auth-sentinel-8390"
    provider = SimpleNamespace(
        close_session=MagicMock(
            side_effect=RuntimeError(f"Authorization: Bearer {sentinel}")
        )
    )
    caplog.set_level(logging.DEBUG)
    with (
        patch.dict(
            browser_tool._active_sessions,
            {
                "fai-8390-cleanup": {
                    "bb_session_id": "session-id",
                    "session_name": "",
                }
            },
            clear=True,
        ),
        patch("tools.browser_tool._run_browser_command"),
        patch("tools.browser_tool._get_cloud_provider", return_value=provider),
        patch("tools.browser_tool._maybe_stop_recording"),
    ):
        browser_tool.cleanup_browser("fai-8390-cleanup")

    assert sentinel not in caplog.text
    assert "Authorization: ***" in caplog.text


def test_transform_hook_cannot_reintroduce_browser_authentication_material():
    """Plugin transforms run before a final browser-result redaction boundary."""
    from model_tools import handle_function_call
    from tools.registry import registry

    sentinel = "transform-hook-auth-sentinel-8390"
    entry = registry.get_entry("browser_console")
    assert entry is not None
    original = entry.handler
    entry.handler = lambda _args, **_kwargs: json.dumps(
        {"success": False, "error": "initial safe failure"}
    )
    try:
        with (
            patch(
                "hermes_cli.plugins.has_hook",
                side_effect=lambda name: name == "transform_tool_result",
            ),
            patch(
                "hermes_cli.plugins.invoke_hook",
                return_value=[
                    json.dumps(
                        {
                            "success": False,
                            "error": "transformed failure",
                            "headers": {"Authorization": f"Bearer {sentinel}"},
                        }
                    )
                ],
            ),
        ):
            returned = handle_function_call(
                "browser_console",
                {"expression": "fetch('/api/admin/acquisition')"},
                task_id="fai-8390",
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
            )
    finally:
        entry.handler = original

    assert sentinel not in returned
    assert "***" in returned


def test_successful_browser_result_is_safe_before_post_tool_observer():
    """Middleware/handlers cannot expose auth in successful callback payloads."""
    from model_tools import handle_function_call

    sentinel = "success-observer-auth-sentinel-8390"
    observed = []
    raw = json.dumps(
        {
            "success": True,
            "warning": f"Authorization: Bearer {sentinel}",
        }
    )
    with (
        patch("model_tools.registry.dispatch", return_value=raw),
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "post_tool_call",
        ),
        patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=lambda _name, **payload: observed.append(payload) or [],
        ),
    ):
        returned = handle_function_call(
            "browser_console",
            {"expression": "'ok'"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    assert sentinel not in returned
    assert sentinel not in json.dumps(observed)
    assert "***" in returned


def test_browser_error_redaction_fails_closed_when_redactor_raises(caplog):
    """A redactor failure must discard the exception instead of leaking it."""
    from model_tools import handle_function_call

    caplog.set_level(logging.ERROR)
    with (
        patch("model_tools.registry.dispatch", side_effect=_credential_bearing_browser_error()),
        patch("agent.redact.redact_sensitive_text", side_effect=RuntimeError("redactor unavailable")),
    ):
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
        assert sentinel not in caplog.text
    assert "redaction failed" in returned.lower()


def test_browser_error_redaction_fails_closed_when_exception_cannot_stringify(caplog):
    """A hostile exception ``__str__`` cannot escape the safe error boundary."""
    from model_tools import handle_function_call

    class UnprintableBrowserError(RuntimeError):
        def __str__(self):
            raise RuntimeError("cannot serialize browser exception")

    caplog.set_level(logging.ERROR)
    with patch("model_tools.registry.dispatch", side_effect=UnprintableBrowserError()):
        returned = handle_function_call(
            "browser_console",
            {"expression": "fetch('/api/admin/acquisition')"},
            task_id="fai-8390",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        )

    assert "redaction failed" in returned.lower()
    assert "redaction failed" in caplog.text.lower()


def test_browser_eval_redacts_failed_browser_command_payload():
    """A structured browser-command failure is sanitized before serialization."""
    from tools.browser_tool import _browser_eval

    failed_command = {
        "success": False,
        "error": str(_credential_bearing_browser_error()),
    }
    with (
        patch("tools.browser_tool._last_session_key", return_value="fai-8390"),
        patch("tools.browser_tool._is_camofox_mode", return_value=False),
        patch("tools.browser_tool._eval_ssrf_guard_active", return_value=False),
        patch("tools.browser_tool._run_browser_command", return_value=failed_command),
    ):
        returned = _browser_eval("fetch('/api/admin/acquisition')", task_id="fai-8390")

    for sentinel in _SENTINELS.values():
        assert sentinel not in returned
    assert "***" in returned


def test_browser_output_redacts_structured_authentication_fields():
    """Opaque values are removed when their mapping key identifies a secret."""
    from tools.browser_tool import _redact_browser_output

    payload = {
        "headers": {
            "Authorization": _SENTINELS["authorization"],
            "Proxy-Authorization": "proxy-auth-sentinel-8390",
            "Cookie": _SENTINELS["cookie"],
        },
        "access_token": _SENTINELS["access"],
        "accessToken": "camel-access-sentinel-8390",
        "refresh_token": _SENTINELS["refresh"],
        "sessionCookie": "session-cookie-sentinel-8390",
        "IDToken": "id-token-sentinel-8390",
        "CSRFToken": "csrf-token-sentinel-8390",
        "status": 401,
        "key": "ArrowDown",
    }

    redacted = _redact_browser_output(payload)

    assert redacted["headers"]["Authorization"] == "***"
    assert redacted["headers"]["Proxy-Authorization"] == "***"
    assert redacted["headers"]["Cookie"] == "***"
    assert redacted["access_token"] == "***"
    assert redacted["accessToken"] == "***"
    assert redacted["refresh_token"] == "***"
    assert redacted["sessionCookie"] == "***"
    assert redacted["IDToken"] == "***"
    assert redacted["CSRFToken"] == "***"
    assert redacted["status"] == 401
    assert redacted["key"] == "ArrowDown"


def test_browser_session_failure_redacts_result_and_browser_log(caplog):
    """The browser command boundary sanitizes before returning or logging."""
    from tools.browser_tool import _run_browser_command

    caplog.set_level(logging.WARNING)
    with (
        patch("tools.browser_tool._find_agent_browser", return_value="agent-browser"),
        patch("tools.browser_tool._requires_real_termux_browser_install", return_value=False),
        patch("tools.browser_tool._is_local_mode", return_value=False),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch(
            "tools.browser_tool._get_session_info",
            side_effect=_credential_bearing_browser_error(),
        ),
    ):
        result = _run_browser_command("fai-8390", "eval", ["document.title"])

    serialized = json.dumps(result, ensure_ascii=False)
    for sentinel in _SENTINELS.values():
        assert sentinel not in serialized
        assert sentinel not in caplog.text
    assert "***" in serialized


@pytest.mark.parametrize(
    ("message", "sentinel"),
    [
        (
            "Authoriz" + "ation: Digest username=u, nonce=digest-nonce-sentinel-8390, "
            "response=digest-response-sentinel-8390",
            "digest-nonce-sentinel-8390",
        ),
        (
            "Authoriz" + 'ation: Bearer "quoted-auth-sentinel-8390"',
            "quoted-auth-sentinel-8390",
        ),
        ('Cookie: "quoted-cookie-sentinel-8390"', "quoted-cookie-sentinel-8390"),
        (
            repr(
                {
                    "Authorization": "repr-auth-sentinel-8390",
                    "Cookie": "repr-cookie-sentinel-8390",
                }
            ),
            "repr-cookie-sentinel-8390",
        ),
    ],
)
def test_tool_error_redacts_parameterized_quoted_and_repr_credentials(message, sentinel):
    """Non-Bearer and quoted/repr authentication shapes are fully removed."""
    from agent.redact import redact_tool_error

    redacted = redact_tool_error(message)

    assert sentinel not in redacted
    assert "***" in redacted


def test_tool_error_redacts_browser_endpoint_query_credentials():
    """CDP/browser endpoint query credentials are authentication material."""
    from agent.redact import redact_tool_error

    endpoint_token = "endpoint-query-sentinel-8390"
    error = RuntimeError(
        "browser session failed at "
        f"wss://browser.invalid/devtools?apiKey={endpoint_token}&mode=debug"
    )

    redacted = redact_tool_error(error)

    assert endpoint_token not in redacted
    assert "mode=debug" in redacted
    assert "***" in redacted


def test_browser_subprocess_redacts_stderr_and_non_json_stdout_before_logging(
    caplog, tmp_path
):
    """Raw agent-browser streams are sanitized before diagnostic log calls."""
    from tools.browser_tool import _run_browser_command

    subprocess_auth = "subprocess-auth-sentinel-8390"
    subprocess_cookie = "subprocess-cookie-sentinel-8390"
    raw_output = (
        "Authoriz" + f"ation: Bearer {subprocess_auth}\nCookie: {subprocess_cookie}"
    )
    process = MagicMock(returncode=1)
    process.wait.return_value = 1
    session = {
        "session_name": "test-session",
        "session_id": "test-id",
        "cdp_url": None,
    }

    caplog.set_level(logging.DEBUG)
    with (
        patch("tools.browser_tool._find_agent_browser", return_value="agent-browser"),
        patch("tools.browser_tool._requires_real_termux_browser_install", return_value=False),
        patch("tools.browser_tool._is_local_mode", return_value=False),
        patch("tools.browser_tool._get_session_info", return_value=session),
        patch("tools.browser_tool._socket_safe_tmpdir", return_value=str(tmp_path)),
        patch("tools.browser_tool._build_browser_env", return_value={"PATH": ""}),
        patch("tools.browser_tool._merge_browser_path", return_value=""),
        patch("tools.browser_tool._get_browser_engine", return_value="auto"),
        patch("tools.browser_tool._discover_homebrew_node_dirs", return_value=[]),
        patch("subprocess.Popen", return_value=process),
        patch("os.open", return_value=99),
        patch("os.close"),
        patch("os.unlink"),
        patch("builtins.open", mock_open(read_data=raw_output)),
        patch("tools.interrupt.is_interrupted", return_value=False),
    ):
        result = _run_browser_command("fai-8390", "eval", ["document.title"])

    persisted = caplog.text + json.dumps(result, ensure_ascii=False)
    assert subprocess_auth not in persisted
    assert subprocess_cookie not in persisted
    assert "***" in persisted


def test_tool_executor_exception_formatter_redacts_before_sibling_catch_paths():
    """Sequential/concurrent executor catches share the fail-closed formatter."""
    from agent.tool_executor import _safe_tool_exception

    formatted = _safe_tool_exception(
        "Error executing browser_console", _credential_bearing_browser_error()
    )

    for sentinel in _SENTINELS.values():
        assert sentinel not in formatted
    assert "***" in formatted


def test_chromium_auto_install_redacts_subprocess_failure_log(caplog, monkeypatch):
    """Lazy-install stdout/stderr cannot bypass the browser log boundary."""
    import tools.browser_tool as browser_tool

    install_auth = "install-auth-sentinel-8390"
    install_cookie = "install-cookie-sentinel-8390"
    raw_stream = (
        "Authoriz" + f"ation: Bearer {install_auth}\nCookie: {install_cookie}"
    )
    monkeypatch.setattr(browser_tool, "_chromium_autoinstall_attempted", False)
    caplog.set_level(logging.WARNING)
    with (
        patch("tools.browser_tool._running_in_docker", return_value=False),
        patch("tools.lazy_deps._allow_lazy_installs", return_value=True),
        patch("tools.browser_tool._find_agent_browser", return_value="agent-browser"),
        patch(
            "subprocess.run",
            return_value=SimpleNamespace(
                returncode=1,
                stderr=raw_stream,
                stdout="",
            ),
        ),
    ):
        assert browser_tool._maybe_autoinstall_chromium() is False

    assert install_auth not in caplog.text
    assert install_cookie not in caplog.text
    assert "***" in caplog.text
