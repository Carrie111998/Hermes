"""Regression tests for installed Codex executable identity resolution."""

from __future__ import annotations

import subprocess

import pytest

import agent.codex_version as cv


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(cv, "_memo", {})
    monkeypatch.delenv("HERMES_CODEX_BIN", raising=False)


def _fake_codex(tmp_path, version: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "configured-codex"
    path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'codex-cli {version}'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_configured_binary_version_is_queried(tmp_path, monkeypatch):
    binary = _fake_codex(tmp_path, "0.222.3")
    monkeypatch.setenv("HERMES_CODEX_BIN", str(binary))

    assert cv.resolve_codex_executable() == str(binary)
    assert cv.get_codex_cli_version() == "0.222.3"


def test_backend_headers_advertise_configured_binary(tmp_path, monkeypatch):
    from agent.auxiliary_client import _codex_backend_headers

    binary = _fake_codex(tmp_path, "0.228.1")
    monkeypatch.setenv("HERMES_CODEX_BIN", str(binary))

    headers = _codex_backend_headers("")
    assert headers["originator"] == "hermes-agent"
    assert headers["User-Agent"] == "codex_cli_rs/0.228.1 (Hermes Agent)"


def test_backend_headers_do_not_claim_an_uninstalled_version(monkeypatch):
    from agent.auxiliary_client import _codex_backend_headers

    monkeypatch.setenv("HERMES_CODEX_BIN", "/does/not/exist/codex")
    headers = _codex_backend_headers("")
    assert headers["User-Agent"] == "codex_cli_rs/0.0.0 (Hermes Agent)"


def test_model_metadata_catalog_advertises_configured_binary(tmp_path, monkeypatch):
    from agent import model_metadata

    binary = _fake_codex(tmp_path, "0.228.2")
    monkeypatch.setenv("HERMES_CODEX_BIN", str(binary))
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"slug": "gpt-test", "context_window": 1234}]}

    class Requests:
        @staticmethod
        def get(url, **kwargs):
            captured["url"] = url
            return Response()

    monkeypatch.setattr(model_metadata, "requests", Requests())
    monkeypatch.setattr(model_metadata, "_ensure_requests", lambda: None)
    monkeypatch.setattr(model_metadata, "_codex_oauth_context_cache", {})

    models, fresh = model_metadata._fetch_codex_oauth_context_lengths_with_source(
        "test-token"
    )
    assert fresh is True
    assert models == {"gpt-test": 1234}
    assert captured["url"].endswith("?client_version=0.228.2")


def test_explicit_binary_wins_over_environment(tmp_path, monkeypatch):
    configured = _fake_codex(tmp_path, "0.222.3")
    explicit = _fake_codex(tmp_path / "explicit", "0.223.4")
    monkeypatch.setenv("HERMES_CODEX_BIN", str(configured))

    assert cv.resolve_codex_executable(str(explicit)) == str(explicit)
    assert cv.get_codex_cli_version(str(explicit)) == "0.223.4"


def test_default_executable_is_codex():
    assert cv.resolve_codex_executable() == "codex"


def test_missing_binary_returns_truthful_unknown(monkeypatch):
    monkeypatch.setenv("HERMES_CODEX_BIN", "/does/not/exist/codex")
    assert cv.get_codex_cli_version() == "0.0.0"


def test_nonzero_binary_returns_truthful_unknown(monkeypatch):
    monkeypatch.setattr(
        cv.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "bad"),
    )
    assert cv.get_codex_cli_version("broken-codex") == "0.0.0"


def test_unparseable_binary_returns_truthful_unknown(monkeypatch):
    monkeypatch.setattr(
        cv.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "codex unknown", ""),
    )
    assert cv.get_codex_cli_version("odd-codex") == "0.0.0"


def test_version_on_stderr_is_accepted(monkeypatch):
    monkeypatch.setattr(
        cv.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "", "codex-cli 0.224.5"
        ),
    )
    assert cv.get_codex_cli_version("stderr-codex") == "0.224.5"


def test_prerelease_version_is_preserved(monkeypatch):
    monkeypatch.setattr(
        cv.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "codex-cli 0.225.0-beta.2+build.7", ""
        ),
    )
    assert cv.get_codex_cli_version("preview-codex") == "0.225.0-beta.2+build.7"


def test_result_is_memoized_per_executable(monkeypatch):
    calls = []

    def query(executable):
        calls.append(executable)
        return "0.226.0"

    monkeypatch.setattr(cv, "_query_installed_version", query)
    assert cv.get_codex_cli_version("codex-a") == "0.226.0"
    assert cv.get_codex_cli_version("codex-a") == "0.226.0"
    assert cv.get_codex_cli_version("codex-b") == "0.226.0"
    assert calls == ["codex-a", "codex-b"]


def test_subprocess_contract_disables_stdin(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "codex-cli 0.227.0", "")

    monkeypatch.setattr(cv.subprocess, "run", run)
    assert cv.get_codex_cli_version("/opt/codex") == "0.227.0"
    assert captured["command"] == ["/opt/codex", "--version"]
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["timeout"] == cv._VERSION_QUERY_TIMEOUT_SECONDS
