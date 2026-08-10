"""Fail-soft regression tests for desktop image pre-analysis (issue #83291).

Dragging images into the desktop chat ran a serial vision pre-analysis
(``_enrich_with_attached_images``) that could hang for minutes per image,
swallow failures silently, and — on interrupt — kill the whole turn with
api_calls=0 and an empty response. The contract now: per-image timeout
(``_IMAGE_PREANALYSIS_TIMEOUT_S``), the real error printed to stderr, degrade
to a ``vision_analyze`` retry hint, user text always preserved, and the
function NEVER raises.
"""

import asyncio
import json

import pytest

from tui_gateway.server import _enrich_with_attached_images

_SUCCESS = json.dumps({"success": True, "analysis": "A red ball on a table."})


@pytest.fixture
def fake_image(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")
    return img


def test_failure_degrades_to_retry_hint_and_preserves_text(fake_image, monkeypatch):
    async def boom(image_url, user_prompt):
        raise RuntimeError("vision backend exploded")

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", boom)

    result = _enrich_with_attached_images("what is this?", [str(fake_image)])

    assert "analysis failed" in result
    assert "vision_analyze using image_url" in result
    assert str(fake_image) in result
    assert "what is this?" in result  # user text preserved


def test_timeout_degrades_and_does_not_hang(fake_image, monkeypatch):
    async def slow(image_url, user_prompt):
        await asyncio.sleep(60)  # would stall the turn without the timeout
        return _SUCCESS

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", slow)
    monkeypatch.setattr("tui_gateway.server._IMAGE_PREANALYSIS_TIMEOUT_S", 0.05)

    result = _enrich_with_attached_images("what is this?", [str(fake_image)])

    assert "analysis failed" in result
    assert "vision_analyze using image_url" in result
    assert "what is this?" in result


@pytest.mark.parametrize(
    "interrupt",
    [asyncio.CancelledError("user hit stop"), KeyboardInterrupt("ctrl-c")],
)
def test_interrupt_does_not_kill_turn(fake_image, monkeypatch, interrupt):
    async def interrupted(image_url, user_prompt):
        raise interrupt

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", interrupted)

    result = _enrich_with_attached_images("what is this?", [str(fake_image)])

    assert "analysis failed" in result
    assert "vision_analyze using image_url" in result
    assert "what is this?" in result


def test_success_keeps_description_and_hint(fake_image, monkeypatch):
    async def ok(image_url, user_prompt):
        return _SUCCESS

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", ok)

    result = _enrich_with_attached_images("what is this?", [str(fake_image)])

    assert "A red ball on a table." in result
    assert "vision_analyze using image_url" in result
    assert "what is this?" in result


def test_failure_logs_real_error_to_stderr(fake_image, monkeypatch, capsys):
    async def boom(image_url, user_prompt):
        raise TimeoutError("vision call timed out")

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", boom)

    _enrich_with_attached_images("what is this?", [str(fake_image)])

    captured = capsys.readouterr()
    assert "[tui_gateway] vision pre-analysis failed" in captured.err
    assert str(fake_image) in captured.err
    assert "timed out" in captured.err  # the real error, never swallowed


def test_mixed_good_and_failing_images_both_handled(tmp_path, monkeypatch):
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n good")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n bad")

    async def flaky(image_url, user_prompt):
        if "bad" in image_url:
            raise RuntimeError("oops")
        return _SUCCESS

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", flaky)

    result = _enrich_with_attached_images("keep me", [str(bad), str(good)])

    assert "keep me" in result
    assert "A red ball on a table." in result
    assert "analysis failed" in result
