from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tools import browser_tool


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
    patches = _browser_patches(_capture_runner(b"small-png", calls))
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
    source.write_bytes(b"small-png")
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
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch("tools.vision_tools._build_native_vision_tool_result", side_effect=native_result), \
            patch("tools.vision_tools._resize_image_for_vision", return_value="data:image/jpeg;base64,U01BTEw=") as resize:
        result = browser_tool.browser_vision("inspect", task_id="bounded-large")

    assert isinstance(result, dict)
    resize.assert_called_once()
    assert attached["image_data_url"] == "data:image/jpeg;base64,U01BTEw="
    assert len(str(attached["image_data_url"])) < len(oversized)
