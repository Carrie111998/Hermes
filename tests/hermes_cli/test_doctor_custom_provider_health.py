"""Custom-provider connectivity checks for ``hermes doctor``."""

from __future__ import annotations

import contextlib
import io
import ssl
import sys
import threading
import types
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import config as config_mod
from hermes_cli import doctor


class _ModelsHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, str]]] = []
    status_code = 200

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests.append((self.path, dict(self.headers.items())))
        body = b'{"data": [{"id": "local-model"}]}'
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextlib.contextmanager
def _models_server():
    _ModelsHandler.requests = []
    _ModelsHandler.status_code = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_isolated_doctor(monkeypatch, tmp_path, config_yaml: str) -> str:
    home = tmp_path / ".hermes"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (home / ".env").write_text("PENG_API_KEY=resolved-provider-secret\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PENG_API_KEY", "resolved-provider-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "HERMES_HOME", home)
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor, "_DHH", str(home))
    monkeypatch.setattr(doctor, "_APIKEY_PROVIDERS_CACHE", [])

    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *args, **kwargs: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    from hermes_cli import auth

    monkeypatch.setattr(auth, "get_anthropic_key", lambda: "")
    monkeypatch.setattr(auth, "get_nous_auth_status", lambda: {})
    monkeypatch.setattr(auth, "get_codex_auth_status", lambda: {})
    monkeypatch.setattr(auth, "get_xai_oauth_auth_status", lambda: {})

    from agent import bedrock_adapter

    monkeypatch.setattr(bedrock_adapter, "has_aws_credentials", lambda: False)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        doctor.run_doctor(Namespace(fix=False, ack=None))
    return output.getvalue()


def test_doctor_probes_custom_provider_with_resolved_runtime_config(
    monkeypatch,
    tmp_path,
):
    with _models_server() as server:
        output = _run_isolated_doctor(
            monkeypatch,
            tmp_path,
            f"""\
model:
  provider: custom:pengcc8
  default: local-model
custom_providers:
  - name: pengcc8
    base_url: http://127.0.0.1:{server.server_port}/v1
    api_key: ${{PENG_API_KEY}}
    extra_headers:
      X-Tenant-Secret: tenant-header-secret
""",
        )

    assert "pengcc8" in output
    assert "reachable" in output
    assert _ModelsHandler.requests
    path, headers = _ModelsHandler.requests[0]
    assert path == "/v1/models"
    assert headers["Authorization"] == "Bearer resolved-provider-secret"
    assert headers["X-Tenant-Secret"] == "tenant-header-secret"
    assert "resolved-provider-secret" not in output
    assert "tenant-header-secret" not in output


def _probe_spec(base_url: str) -> doctor._CustomProviderProbeSpec:
    return doctor._CustomProviderProbeSpec(
        label="test-provider",
        base_url=base_url,
        api_key="provider-secret",
        api_mode="chat_completions",
        extra_headers={"X-Secret": "header-secret"},
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_custom_provider_probe_classifies_authentication_rejection(status_code):
    with _models_server() as server:
        _ModelsHandler.status_code = status_code
        result = doctor._probe_custom_provider_health(
            _probe_spec(f"http://127.0.0.1:{server.server_port}/v1")
        )

    assert result.kind == "authentication_rejected"
    assert result.status_code == status_code


@pytest.mark.parametrize("status_code", [404, 405])
def test_custom_provider_probe_treats_missing_models_endpoint_as_reachable(status_code):
    with _models_server() as server:
        _ModelsHandler.status_code = status_code
        result = doctor._probe_custom_provider_health(
            _probe_spec(f"http://127.0.0.1:{server.server_port}/v1")
        )

    assert result.kind == "models_unsupported"
    assert result.status_code == status_code


def test_custom_provider_probe_classifies_other_4xx_as_warning():
    with _models_server() as server:
        _ModelsHandler.status_code = 429
        result = doctor._probe_custom_provider_health(
            _probe_spec(f"http://127.0.0.1:{server.server_port}/v1")
        )

    assert result.kind == "http_warning"
    assert result.status_code == 429


def test_custom_provider_probe_classifies_5xx_as_provider_failure():
    with _models_server() as server:
        _ModelsHandler.status_code = 503
        result = doctor._probe_custom_provider_health(
            _probe_spec(f"http://127.0.0.1:{server.server_port}/v1")
        )

    assert result.kind == "provider_failure"
    assert result.status_code == 503


def test_custom_provider_probe_uses_anthropic_auth_headers():
    with _models_server() as server:
        spec = doctor._CustomProviderProbeSpec(
            label="anthropic-proxy",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="anthropic-provider-secret",
            api_mode="anthropic_messages",
            extra_headers={},
        )
        result = doctor._probe_custom_provider_health(spec)

    assert result.kind == "reachable"
    _, raw_headers = _ModelsHandler.requests[0]
    headers = {name.lower(): value for name, value in raw_headers.items()}
    assert headers["x-api-key"] == "anthropic-provider-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers


def test_custom_provider_probe_applies_provider_tls_settings(monkeypatch):
    from agent import ssl_verify

    captured: dict[str, object] = {}

    def fake_resolve_httpx_verify(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(ssl_verify, "resolve_httpx_verify", fake_resolve_httpx_verify)
    with _models_server() as server:
        spec = doctor._CustomProviderProbeSpec(
            label="tls-provider",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key=None,
            api_mode="chat_completions",
            extra_headers={},
            ssl_ca_cert="/tmp/provider-ca.pem",
            ssl_verify=False,
        )
        result = doctor._probe_custom_provider_health(spec)

    assert result.kind == "reachable"
    assert captured["ca_bundle"] == "/tmp/provider-ca.pem"
    assert captured["ssl_verify"] is False


def test_custom_provider_probe_classifies_timeout(monkeypatch):
    import httpx

    def raise_timeout(_client, url, **_kwargs):
        raise httpx.ReadTimeout(
            "provider-secret",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", raise_timeout)
    result = doctor._probe_custom_provider_health(
        _probe_spec("https://provider.example/v1")
    )

    assert result.kind == "timeout"


def test_custom_provider_probe_classifies_wrapped_ssl_error(monkeypatch):
    import httpx

    def raise_ssl_error(_client, url, **_kwargs):
        try:
            raise ssl.SSLCertVerificationError(1, "certificate verify failed")
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError(
                "provider-secret",
                request=httpx.Request("GET", url),
            ) from cause

    monkeypatch.setattr(httpx.Client, "get", raise_ssl_error)
    result = doctor._probe_custom_provider_health(
        _probe_spec("https://provider.example/v1")
    )

    assert result.kind == "ssl_error"


def test_custom_provider_specs_omit_disabled_entries_and_preserve_tls(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """\
providers:
  enabled-provider:
    name: Enabled Provider
    api: https://enabled.example/v1
    ssl_ca_cert: /tmp/provider-ca.pem
    ssl_verify: false
  disabled-provider:
    name: Disabled Provider
    api: https://disabled.example/v1
    enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    specs = doctor._build_custom_provider_probe_specs()

    assert [spec.label for spec in specs] == ["Enabled Provider"]
    assert specs[0].ssl_ca_cert == "/tmp/provider-ca.pem"
    assert specs[0].ssl_verify is False
