from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tools import browser_camofox, browser_tool


def _png_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color="white").save(output, format="PNG")
    return output.getvalue()


def _capture_runner(payload: bytes, calls: list[list[str]]):
    def run(_task_id, command, args, **_kwargs):
        calls.append([command, *args])
        if command == "screenshot":
            output = Path(args[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            return {"success": True, "data": {"path": str(output)}}
        raise AssertionError(f"unexpected browser command: {command}")

    return run


def _native_result(**kwargs):
    return {
        "content": [],
        "meta": {"attached_data_url": kwargs["image_data_url"]},
        "text_summary": "captured",
    }


def _browser_patches(runner):
    return (
        patch.object(browser_tool, "_is_camofox_mode", return_value=False),
        patch.object(browser_tool, "_is_local_backend", return_value=True),
        patch.object(browser_tool, "_get_browser_engine", return_value="chrome"),
        patch.object(browser_tool, "_run_browser_command", side_effect=runner),
        patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=True),
        patch("tools.vision_tools._build_native_vision_tool_result", side_effect=_native_result),
    )


def test_browser_vision_captures_viewport_by_default_and_full_page_only_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    calls: list[list[str]] = []
    patches = _browser_patches(_capture_runner(_png_bytes(), calls))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        browser_tool.browser_vision("inspect", task_id="bounded-default")
        browser_tool.browser_vision("inspect", full_page=True, task_id="bounded-full")

    screenshot_calls = [call for call in calls if call[0] == "screenshot"]
    assert len(screenshot_calls) == 2
    assert "--full" not in screenshot_calls[0]
    assert "--full" in screenshot_calls[1]


def test_browser_vision_forwards_full_page_to_lightpanda_chrome_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    source = tmp_path / "fallback.png"
    source.write_bytes(_png_bytes())
    fallback_args: list[str] = []

    def fallback(_task_id, args, _timeout):
        fallback_args.extend(args)
        return {"success": True, "data": {"path": str(source)}}

    with patch.object(browser_tool, "_is_camofox_mode", return_value=False), \
            patch.object(browser_tool, "_is_local_backend", return_value=True), \
            patch.object(browser_tool, "_get_browser_engine", return_value="lightpanda"), \
            patch.object(browser_tool, "_should_inject_engine", return_value=True), \
            patch.object(browser_tool, "_chrome_fallback_screenshot", side_effect=fallback), \
            patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=True), \
            patch("tools.vision_tools._build_native_vision_tool_result", side_effect=_native_result):
        result = browser_tool.browser_vision("inspect", full_page=True, task_id="bounded-lightpanda")

    assert isinstance(result, dict), result
    assert "--full" in fallback_args, result


def test_browser_vision_downscales_large_native_attachment_before_returning(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    calls: list[list[str]] = []
    attached: dict[str, object] = {}

    def native_result(**kwargs):
        attached.update(kwargs)
        return {"content": [], "meta": {}, "text_summary": "captured"}

    oversized = b"x" * (4 * 1024 * 1024 + 64)
    patches = _browser_patches(_capture_runner(oversized, calls))
    small_data_url = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode("ascii")
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch("tools.vision_tools._build_native_vision_tool_result", side_effect=native_result), \
            patch("tools.vision_tools._resize_image_for_vision", return_value=small_data_url) as resize:
        result = browser_tool.browser_vision("inspect", task_id="bounded-large")

    assert isinstance(result, dict)
    resize.assert_called_once()
    assert attached["image_data_url"] == small_data_url
    assert len(str(attached["image_data_url"])) < len(oversized)


def test_browser_vision_fails_closed_when_resize_cannot_bound_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    calls: list[list[str]] = []
    oversized = b"x" * (4 * 1024 * 1024 + 64)
    oversized_url = "data:image/png;base64," + base64.b64encode(oversized).decode("ascii")
    patches = _browser_patches(_capture_runner(oversized, calls))

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch("tools.vision_tools._build_native_vision_tool_result") as native_result, \
            patch("tools.vision_tools._resize_image_for_vision", return_value=oversized_url) as resize:
        result = browser_tool.browser_vision("inspect", task_id="bounded-failure")

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["code"] == "browser_vision_attachment_unbounded"
    assert payload["screenshot_path"]
    native_result.assert_not_called()
    assert resize.call_args.kwargs["max_base64_bytes"] == 2 * 1024 * 1024
    assert resize.call_args.kwargs["max_dimension"] == 4096


def test_camofox_vision_forwards_full_page_to_screenshot_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    seen: dict[str, object] = {}

    def get_raw(path, params):
        seen["path"] = path
        seen["params"] = params
        return SimpleNamespace(content=_png_bytes())

    llm_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="looks good"))]
    )
    with patch.object(browser_camofox, "_get_session", return_value={
            "tab_id": "tab-1", "user_id": "user-1"}), \
            patch.object(browser_camofox, "_camofox_private_page_block", return_value=None), \
            patch.object(browser_camofox, "_get_raw", side_effect=get_raw), \
            patch.object(browser_camofox, "load_config", return_value={}), \
            patch("agent.auxiliary_client.call_llm", return_value=llm_response):
        result = browser_camofox.camofox_vision(
            "inspect", full_page=True, task_id="camofox-full-page")

    assert json.loads(result)["success"] is True
    assert seen["params"] == {"userId": "user-1", "fullPage": "true"}


def test_camofox_vision_fails_closed_before_aux_call_when_resize_is_still_oversized(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    oversized = b"x" * (4 * 1024 * 1024 + 64)
    oversized_url = "data:image/png;base64," + base64.b64encode(oversized).decode("ascii")

    with patch.object(browser_camofox, "_get_session", return_value={
            "tab_id": "tab-1", "user_id": "user-1"}), \
            patch.object(browser_camofox, "_camofox_private_page_block", return_value=None), \
            patch.object(browser_camofox, "_get_raw", return_value=SimpleNamespace(content=oversized)), \
            patch("tools.vision_tools._resize_image_for_vision", return_value=oversized_url), \
            patch("agent.auxiliary_client.call_llm") as call_llm:
        result = browser_camofox.camofox_vision(
            "inspect", full_page=True, task_id="camofox-bounded-failure")

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["code"] == "browser_vision_attachment_unbounded"
    assert payload["screenshot_path"]
    call_llm.assert_not_called()
