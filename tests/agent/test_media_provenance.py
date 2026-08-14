from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.media_provenance import (
    clear_media_provenance,
    is_trusted_media,
    register_trusted_media,
    register_trusted_tool_result,
    register_user_media_references,
    rehydrate_media_references,
)
from tools import browser_tool as _browser_tool  # noqa: F401
from tools import computer_use_tool as _computer_use_tool  # noqa: F401
from tools import image_generation_tool as _image_generation_tool  # noqa: F401
from tools import vision_tools


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    clear_media_provenance()
    yield
    clear_media_provenance()


def call_vision(session_id: str, reference: str) -> dict:
    raw = asyncio.run(vision_tools._handle_vision_analyze(
        {"image_url": reference, "question": "inspect"},
        session_id=session_id, task_id="test-task",
    ))
    return json.loads(raw)


def test_unregistered_reference_is_blocked_before_native_or_auxiliary_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auxiliary = AsyncMock(return_value="must not run")
    native = AsyncMock(return_value="must not run")
    monkeypatch.setattr(vision_tools, "vision_analyze_tool", auxiliary)
    monkeypatch.setattr(vision_tools, "_vision_analyze_native", native)

    result = call_vision("session-a", "/tmp/invented.png")

    assert result["error_type"] == "untrusted_media_reference"
    auxiliary.assert_not_awaited()
    native.assert_not_awaited()


def test_user_reference_is_allowed_only_in_its_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = "https://example.test/user-photo.png"
    assert register_user_media_references("session-a", f"inspect {reference}") == 1

    auxiliary = AsyncMock(return_value="trusted analysis")
    monkeypatch.setattr(vision_tools, "vision_analyze_tool", auxiliary)
    monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", lambda: False)

    raw = asyncio.run(vision_tools._handle_vision_analyze(
        {"image_url": reference, "question": "inspect"},
        session_id="session-a", task_id="test-task",
    ))
    assert raw == "trusted analysis"
    auxiliary.assert_awaited_once()

    assert call_vision("session-b", reference)["error_type"] == "untrusted_media_reference"


def test_registered_reference_reaches_native_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = "/tmp/user-provided.png"
    register_user_media_references("session-a", f"inspect {reference}")
    native = AsyncMock(return_value={"_multimodal": True, "content": []})
    auxiliary = AsyncMock(return_value="must not run")
    monkeypatch.setattr(vision_tools, "_vision_analyze_native", native)
    monkeypatch.setattr(vision_tools, "vision_analyze_tool", auxiliary)
    monkeypatch.setattr(
        vision_tools, "_should_use_native_vision_fast_path", lambda: True
    )

    result = asyncio.run(vision_tools._handle_vision_analyze(
        {"image_url": reference, "question": "inspect"},
        session_id="session-a",
        task_id="test-task",
    ))

    assert result == {"_multimodal": True, "content": []}
    native.assert_awaited_once()
    auxiliary.assert_not_awaited()


def test_user_extensionless_and_file_url_paths_are_registered() -> None:
    extensionless = "/tmp/upload-without-extension"
    file_url = "file:///tmp/photo.png"

    register_user_media_references(
        "session-a", f"inspect `{extensionless}` and {file_url}"
    )

    assert is_trusted_media("session-a", extensionless)
    assert is_trusted_media("session-a", file_url)


def test_existing_path_from_another_session_is_not_trusted() -> None:
    reference = "/tmp/shared-cache-image.png"
    register_trusted_media("session-a", [reference], origin="gateway_attachment")

    assert call_vision("session-b", reference)["error_type"] == "untrusted_media_reference"


def test_restart_rehydrates_only_grounded_user_and_producer_media() -> None:
    from agent.tool_dispatch_helpers import make_tool_result_message

    user_reference = "https://example.test/user-image.png"
    model_reference = "/tmp/model-invented.png"
    stale_reference = "/tmp/stale-tool-result.png"
    generated_reference = "/tmp/historical-generated.png"
    browser_reference = "https://example.test/historical-browser.jpg"
    screenshot_reference = "/tmp/historical browser screenshot.png"
    browser_result = make_tool_result_message(
        "browser_get_images",
        json.dumps({
            "success": True,
            "images": [{"src": browser_reference}],
        }),
        "call-browser",
    )
    browser_vision_summary = make_tool_result_message(
        "browser_vision",
        (
            "A page analysis that mentions /tmp/untrusted.png but ends with "
            f"the Hermes artifact. Screenshot path: {screenshot_reference}"
        ),
        "call-browser-vision",
    )
    browser_vision_summary["content"] += "\n[screenshot]"

    assert rehydrate_media_references("session-a", [
        {"role": "assistant", "content": f"inspect {model_reference}"},
        {
            "role": "tool",
            "tool_name": "image_generate",
            "tool_call_id": "unmatched",
            "content": json.dumps({"success": True, "image": model_reference}),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-stale",
                "function": {"name": "image_generate", "arguments": "{}"},
            }],
        },
        {"role": "user", "content": f"inspect {user_reference}"},
        {
            "role": "tool",
            "tool_name": "image_generate",
            "tool_call_id": "call-stale",
            "content": json.dumps({"success": True, "image": stale_reference}),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-generate",
                "function": {"name": "image_generate", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_name": "image_generate",
            "tool_call_id": "call-generate",
            "content": json.dumps({
                "success": True,
                "image": generated_reference,
            }),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-browser",
                "function": {"name": "browser_get_images", "arguments": "{}"},
            }],
        },
        browser_result,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-browser-vision",
                "function": {"name": "browser_vision", "arguments": "{}"},
            }],
        },
        browser_vision_summary,
    ]) == 4
    assert is_trusted_media("session-a", user_reference)
    assert is_trusted_media("session-a", generated_reference)
    assert is_trusted_media("session-a", browser_reference)
    assert is_trusted_media("session-a", screenshot_reference)
    assert not is_trusted_media("session-a", "/tmp/untrusted.png")
    assert not is_trusted_media("session-a", model_reference)
    assert not is_trusted_media("session-a", stale_reference)


def test_session_db_round_trip_rehydrates_grounded_producer_media(tmp_path) -> None:
    from agent.tool_dispatch_helpers import make_tool_result_message
    from hermes_state import SessionDB

    session_id = "session-db-round-trip"
    reference = "https://example.test/persisted-browser-image.jpg"
    result = make_tool_result_message(
        "browser_get_images",
        json.dumps({
            "success": True,
            "images": [{"src": reference}],
        }),
        "call-browser",
    )
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        database.create_session(session_id, source="cli")
        database.append_messages_batch(session_id, [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-browser",
                    "type": "function",
                    "function": {
                        "name": "browser_get_images",
                        "arguments": "{}",
                    },
                }],
            },
            result,
        ])
        persisted = database.get_messages_as_conversation(session_id)
    finally:
        database.close()

    clear_media_provenance()
    assert rehydrate_media_references(session_id, persisted) == 1
    assert is_trusted_media(session_id, reference)


def test_only_designated_producer_results_register_exact_media() -> None:
    generated = "/tmp/generated.png"
    browser_image = "https://example.test/page-image.jpg"
    browser_screenshot = "/tmp/browser-screenshot.png"
    computer_capture = "data:image/png;base64,Y2FwdHVyZQ=="
    ordinary_path = "/tmp/read-file-output.png"

    assert register_trusted_tool_result(
        "session-a", "image_generate",
        json.dumps({"success": True, "image": generated}),
    ) == 1
    assert register_trusted_tool_result(
        "session-a", "browser_get_images",
        json.dumps({"success": True, "images": [{"src": browser_image}]}),
    ) == 1
    assert register_trusted_tool_result(
        "session-a", "read_file",
        json.dumps({"success": True, "path": ordinary_path}),
    ) == 0
    assert register_trusted_tool_result(
        "session-a", "browser_vision",
        json.dumps({
            "success": False,
            "error": "auxiliary vision unavailable",
            "screenshot_path": browser_screenshot,
        }),
    ) == 1
    assert register_trusted_tool_result(
        "session-a", "computer_use",
        {
            "_multimodal": True,
            "content": [{
                "type": "image_url",
                "image_url": {"url": computer_capture},
            }],
        },
    ) == 1
    assert is_trusted_media("session-a", generated)
    assert is_trusted_media("session-a", browser_image)
    assert is_trusted_media("session-a", browser_screenshot)
    assert is_trusted_media("session-a", computer_capture)
    assert not is_trusted_media("session-a", ordinary_path)


def test_executor_wiring_does_not_register_blocked_producer_result() -> None:
    from agent.tool_executor import _register_trusted_media_result

    agent = SimpleNamespace(session_id="session-a")
    _register_trusted_media_result(
        agent, "image_generate",
        json.dumps({"success": True, "image": "/tmp/allowed.png"}),
        blocked=False,
    )
    _register_trusted_media_result(
        agent, "image_generate",
        json.dumps({"success": True, "image": "/tmp/blocked.png"}),
        blocked=True,
    )
    assert is_trusted_media("session-a", "/tmp/allowed.png")
    assert not is_trusted_media("session-a", "/tmp/blocked.png")


def test_full_dispatch_rejects_before_vision_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import handle_function_call

    auxiliary = AsyncMock(return_value="must not run")
    monkeypatch.setattr(vision_tools, "vision_analyze_tool", auxiliary)

    raw = handle_function_call(
        "vision_analyze",
        {"image_url": "/tmp/unregistered.png", "question": "inspect"},
        task_id="test-task",
        session_id="session-a",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )

    assert json.loads(raw)["error_type"] == "untrusted_media_reference"
    auxiliary.assert_not_awaited()


@pytest.mark.parametrize(
    ("backend", "agent_visible_path"),
    [
        ("docker", "/root/.hermes/cache/images/upload.png"),
        ("ssh", "~/.hermes/cache/images/upload.png"),
        ("local", None),
    ],
)
def test_gateway_attachment_registers_real_backend_alias(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    agent_visible_path: str | None,
) -> None:
    from gateway.run import _register_gateway_media_paths

    hermes_home = tmp_path / ".hermes"
    image_path = hermes_home / "cache" / "images" / "upload.png"
    image_path.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", backend)

    _register_gateway_media_paths("session-a", [str(image_path)])

    assert is_trusted_media("session-a", str(image_path))
    if agent_visible_path is not None:
        assert is_trusted_media("session-a", agent_visible_path)
