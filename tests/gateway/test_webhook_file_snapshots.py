"""Adversarial regular-file snapshot tests for webhook authority inputs."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import gateway.platforms.webhook_filters as webhook_filters
from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _DYNAMIC_ROUTES_FILENAME,
)
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookRouteConfig,
)
from gateway.platforms.webhook_filters import (
    MAX_SCRIPT_OUTPUT_COMBINED_BYTES,
    MAX_SCRIPT_OUTPUT_STREAM_BYTES,
    WebhookRouteProcessor,
    WebhookScriptDisposition,
)


requires_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="FIFO authority regressions require os.mkfifo",
)


def _scripts_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    return scripts


def _prepare_script(
    scripts: Path,
    *,
    name: str,
    source: str,
    timeout: int = 10,
):
    (scripts / name).write_text(source, encoding="utf-8")
    processor = WebhookRouteProcessor(script_timeout_seconds=timeout)
    prepared, error = processor.prepare_route_script(name)
    assert error is None
    assert prepared is not None
    return processor, prepared


def _write_fifo_once(path: Path) -> None:
    """Unblock a regressed blocking reader so the test fails instead of hangs."""

    try:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return
    try:
        os.write(fd, b"\n")
    finally:
        os.close(fd)


def _call_promptly_with_fifo_escape(
    fifo_path: Path,
    call: Callable[[], Any],
) -> Any:
    escape = threading.Timer(2.0, _write_fifo_once, args=(fifo_path,))
    escape.daemon = True
    escape.start()
    started = time.monotonic()
    try:
        result = call()
    finally:
        escape.cancel()
    assert time.monotonic() - started < 1.0
    return result


def _adapter(routes: dict[str, dict] | None = None) -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": routes or {},
            },
        )
    )


@requires_fifo
def test_filter_authority_fifo_fails_promptly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fifo = tmp_path / "filter-values.fifo"
    os.mkfifo(fifo)
    adapter = _adapter({
        "events": {
            "provider": "generic",
            "secret": "filter-fifo-secret",
            "filters": {"field": "actor", "in_file": str(fifo)},
        }
    })

    with pytest.raises(WebhookContractError, match="filter in_file is unavailable"):
        _call_promptly_with_fifo_escape(
            fifo,
            lambda: adapter._bind_route_authentication_authorities(adapter._routes),
        )


@requires_fifo
def test_gateway_config_authority_fifo_fails_promptly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fifo = tmp_path / "config.yaml"
    os.mkfifo(fifo)

    with pytest.raises(WebhookContractError, match="config is unavailable"):
        _call_promptly_with_fifo_escape(
            fifo,
            WebhookAdapter._load_gateway_config_for_authority,
        )


@requires_fifo
def test_dynamic_route_fifo_withdraws_promptly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fifo = tmp_path / _DYNAMIC_ROUTES_FILENAME
    os.mkfifo(fifo)
    adapter = _adapter()

    _call_promptly_with_fifo_escape(fifo, adapter._reload_dynamic_routes)

    assert adapter._dynamic_routes == {}
    assert adapter._routes == {}


def test_deep_filter_file_and_literal_authority_fail_as_contract_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    values = tmp_path / "deep.json"
    values.write_text("[" * 600 + "0" + "]" * 600, encoding="utf-8")
    adapter = _adapter()
    route = {
        "provider": "generic",
        "secret": "deep-filter-secret",
        "filters": {"field": "actor", "in_file": str(values)},
    }
    bound = WebhookRouteConfig.bind(
        "events",
        route,
        headers={},
        request_profile="default",
    )

    with pytest.raises(WebhookContractError):
        adapter._prepare_route_filter_authority(
            route,
            bound,
            authority_profile="default",
        )

    nested: object = {"field": "actor", "equals": "value"}
    for _ in range(600):
        nested = {"not": nested}
    literal_route = {**route, "filters": nested}
    with pytest.raises(WebhookContractError):
        adapter._prepare_route_filter_authority(
            literal_route,
            bound,
            authority_profile="default",
        )


def test_filter_file_huge_integer_is_a_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    values = tmp_path / "huge-integer.json"
    values.write_text("[" + "9" * 5_000 + "]", encoding="utf-8")
    adapter = _adapter()
    route = {
        "provider": "generic",
        "secret": "huge-integer-secret",
        "filters": {"field": "actor", "in_file": str(values)},
    }
    bound = WebhookRouteConfig.bind(
        "events",
        route,
        headers={},
        request_profile="default",
    )

    with pytest.raises(WebhookContractError, match="invalid JSON value"):
        adapter._prepare_route_filter_authority(
            route,
            bound,
            authority_profile="default",
        )


def test_filter_file_embedded_nul_is_a_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    route = {
        "provider": "generic",
        "secret": "nul-path-secret",
        "filters": {"field": "actor", "in_file": "bad\0path.json"},
    }
    bound = WebhookRouteConfig.bind(
        "events",
        route,
        headers={},
        request_profile="default",
    )

    with pytest.raises(WebhookContractError, match="filter in_file is unavailable"):
        adapter._prepare_route_filter_authority(
            route,
            bound,
            authority_profile="default",
        )


@requires_fifo
def test_script_fifo_fails_promptly_without_opening_a_blocking_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    fifo = scripts / "blocked.py"
    os.mkfifo(fifo)

    prepared, error = _call_promptly_with_fifo_escape(
        fifo,
        lambda: WebhookRouteProcessor().prepare_route_script("blocked.py"),
    )

    assert prepared is None
    assert "not a file" in str(error)


@requires_fifo
def test_script_symlink_to_fifo_fails_promptly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    fifo = scripts / "blocked-target.py"
    os.mkfifo(fifo)
    link = scripts / "blocked-link.py"
    try:
        link.symlink_to(fifo)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    prepared, error = _call_promptly_with_fifo_escape(
        fifo,
        lambda: WebhookRouteProcessor().prepare_route_script("blocked-link.py"),
    )

    assert prepared is None
    assert "not a file" in str(error)


@requires_fifo
def test_script_regular_to_fifo_race_fails_promptly_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    script = scripts / "raced.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    original_resolver = webhook_filters._resolve_script_path

    def replace_after_resolution(value: Any):
        resolved, error = original_resolver(value)
        assert resolved == script
        assert error is None
        script.unlink()
        os.mkfifo(script)
        return resolved, None

    monkeypatch.setattr(
        webhook_filters,
        "_resolve_script_path",
        replace_after_resolution,
    )

    prepared, error = _call_promptly_with_fifo_escape(
        script,
        lambda: WebhookRouteProcessor().prepare_route_script("raced.py"),
    )

    assert prepared is None
    assert "not a regular file" in str(error)


def test_script_symlink_to_regular_file_remains_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    target = scripts / "target.py"
    target.write_text("print('{}')\n", encoding="utf-8")
    link = scripts / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    prepared, error = WebhookRouteProcessor().prepare_route_script("linked.py")

    assert error is None
    assert prepared is not None
    assert prepared.source == "print('{}')\n"


@pytest.mark.live_system_guard_bypass
def test_script_oversized_stdout_is_terminated_and_typed_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    processor, prepared = _prepare_script(
        scripts,
        name="stdout.py",
        source=(
            "import sys, time\n"
            f"sys.stdout.write('x' * {MAX_SCRIPT_OUTPUT_STREAM_BYTES + 1})\n"
            "sys.stdout.flush()\n"
            "time.sleep(10)\n"
        ),
    )

    started = time.monotonic()
    result = processor.run_prepared_script(prepared, {})

    assert time.monotonic() - started < 3.0
    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "output exceeded" in str(result.error)


@pytest.mark.live_system_guard_bypass
def test_script_oversized_stderr_is_typed_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    processor, prepared = _prepare_script(
        scripts,
        name="stderr.py",
        source=(
            "import sys\n"
            f"sys.stderr.write('x' * {MAX_SCRIPT_OUTPUT_STREAM_BYTES + 1})\n"
        ),
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "output exceeded" in str(result.error)


@pytest.mark.live_system_guard_bypass
def test_script_combined_output_limit_is_enforced_across_both_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    half_plus_one = MAX_SCRIPT_OUTPUT_COMBINED_BYTES // 2 + 1
    processor, prepared = _prepare_script(
        scripts,
        name="combined.py",
        source=(
            "import sys\n"
            f"sys.stdout.write('o' * {half_plus_one})\n"
            "sys.stdout.flush()\n"
            f"sys.stderr.write('e' * {half_plus_one})\n"
        ),
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.INDETERMINATE
    assert "output exceeded" in str(result.error)


@pytest.mark.parametrize(
    "source",
    [
        "print('[' * 2000 + '0' + ']' * 2000)\n",
        "import sys\nsys.stdout.buffer.write(b'\\xff')\n",
    ],
)
def test_script_pathological_json_or_unicode_output_returns_a_typed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    processor, prepared = _prepare_script(
        scripts,
        name="pathological.py",
        source=source,
    )

    result = processor.run_prepared_script(prepared, {})

    assert result.disposition is WebhookScriptDisposition.CONTINUE
    assert isinstance(result.payload, dict)
    assert isinstance(result.payload.get("script_output"), str)


def test_recursive_script_payload_fails_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = _scripts_home(tmp_path, monkeypatch)
    processor, prepared = _prepare_script(
        scripts,
        name="unused.py",
        source="print('{}')\n",
    )
    payload: dict[str, Any] = {}
    payload["self"] = payload

    result = processor.run_prepared_script(prepared, payload)

    assert result.disposition is WebhookScriptDisposition.FAILED
    assert "cannot be serialized" in str(result.error)
