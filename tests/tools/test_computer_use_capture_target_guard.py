"""Explicit capture app requests must fail closed before response materialization."""

import json
from unittest.mock import Mock, patch

import pytest

from tools.computer_use.backend import CaptureResult, UIElement
from tools.computer_use.tool import _dispatch


def _capture(app: str) -> CaptureResult:
    return CaptureResult(
        mode="som",
        width=32,
        height=32,
        png_b64="iVBORw0KGgoAAAANSUhEUg==",
        elements=[UIElement(index=0, role="button", label="Private", bounds=(0, 0, 1, 1))],
        app=app,
        window_title="Synthetic window",
        png_bytes_len=16,
    )


def _backend(returned_app: str):
    backend = Mock()
    backend.capture.return_value = _capture(returned_app)
    return backend


def test_capture_mismatch_fails_before_response_spill_or_vision():
    from tools.computer_use import tool as cu_tool

    backend = _backend("Hermes Agent - Dashboard — Mozilla Firefox")
    with patch.object(cu_tool, "_capture_response", return_value={"unexpected": True}) as response, \
         patch.object(cu_tool, "_spill_elements_to_file") as spill, \
         patch.object(cu_tool, "_route_capture_through_aux_vision") as vision:
        result = _dispatch(backend, "capture", {"mode": "som", "app": "Hermes"})

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["code"] == "capture_target_mismatch"
    assert "elements" not in payload
    assert "screenshot" not in payload
    response.assert_not_called()
    spill.assert_not_called()
    vision.assert_not_called()


def test_capture_empty_returned_app_fails_closed():
    from tools.computer_use import tool as cu_tool

    with patch.object(cu_tool, "_capture_response", return_value={"unexpected": True}) as response:
        result = _dispatch(
            _backend(""), "capture", {"mode": "som", "app": "Hermes"}
        )

    payload = json.loads(result)
    assert payload["code"] == "capture_target_mismatch"
    response.assert_not_called()


def test_capture_canonical_chrome_alias_passes():
    from tools.computer_use import tool as cu_tool

    expected = {"matched": True}
    with patch.object(cu_tool, "_capture_response", return_value=expected) as response:
        result = _dispatch(
            _backend("google-chrome"),
            "capture",
            {"mode": "som", "app": "chrome"},
        )

    assert result is expected
    response.assert_called_once()


@pytest.mark.parametrize(
    ("requested_app", "returned_app"),
    [
        ("Terminal", "GNOME Terminal"),
        ("Code", "Visual Studio Code"),
        ("calc", "Calculator"),
    ],
)
def test_capture_legitimate_substring_app_resolution_passes(
    requested_app, returned_app
):
    from tools.computer_use import tool as cu_tool

    expected = {"matched": returned_app}
    with patch.object(cu_tool, "_capture_response", return_value=expected) as response:
        result = _dispatch(
            _backend(returned_app),
            "capture",
            {"mode": "som", "app": requested_app},
        )

    assert result is expected
    response.assert_called_once()


@pytest.mark.parametrize("requested_app", ["screen", "desktop", "all"])
@pytest.mark.parametrize(
    "returned_app", ["Progman", "WorkerW", "Finder", "plasmashell"]
)
def test_capture_desktop_sentinel_passes_resolved_shell_app(
    requested_app, returned_app
):
    from tools.computer_use import tool as cu_tool

    expected = {"desktop": returned_app}
    with patch.object(cu_tool, "_capture_response", return_value=expected) as response:
        result = _dispatch(
            _backend(returned_app),
            "capture",
            {"mode": "som", "app": requested_app},
        )

    assert result is expected
    response.assert_called_once()


def test_exact_pid_window_binding_is_exempt_from_app_response_guard():
    from tools.computer_use import tool as cu_tool

    expected = {"bound": True}
    backend = _backend("different-app")
    with patch.object(cu_tool, "_capture_response", return_value=expected) as response:
        result = _dispatch(
            backend,
            "capture",
            {
                "mode": "som",
                "app": "Hermes",
                "pid": 123,
                "window_id": 456,
            },
        )

    assert result is expected
    response.assert_called_once()
    backend.capture.assert_called_once_with(
        mode="som", app="Hermes", pid=123, window_id=456
    )
