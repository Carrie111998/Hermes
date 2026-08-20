"""Behavior contract for the TUI pre-prompt plugin dispatch gate."""

from typing import Any


def test_pre_prompt_dispatch_is_a_public_plugin_hook():
    """Removing the public registration point must break its real consumer."""
    from hermes_cli.plugins import VALID_HOOKS

    assert "pre_prompt_dispatch" in VALID_HOOKS


def test_required_image_turn_blocks_without_matching_directive():
    """A removed or unloaded required plugin must not expose the image to the agent."""
    from tui_gateway.prompt_dispatch_hooks import (
        REQUIRED_HANDLER_UNAVAILABLE_TEXT,
        resolve_prompt_dispatch_results,
    )

    decision = resolve_prompt_dispatch_results(
        [],
        required_prompt_handler="hoppe_ocr_approval",
        has_images=True,
    )

    assert decision.action == "block"
    assert decision.handler == "hoppe_ocr_approval"
    assert decision.text == REQUIRED_HANDLER_UNAVAILABLE_TEXT
    assert decision.reason == "required_prompt_handler_unavailable"


def test_required_image_turn_ignores_foreign_and_handlerless_directives():
    """Only the client-required handler may decide its protected image turn."""
    from tui_gateway.prompt_dispatch_hooks import (
        PromptDispatchDecision,
        resolve_prompt_dispatch_results,
    )

    decision = resolve_prompt_dispatch_results(
        [
            {"action": "allow"},
            {"action": "respond", "handler": "other", "text": "wrong"},
            {
                "action": "respond",
                "handler": "hoppe_ocr_approval",
                "text": "Freigabe angelegt",
                "reason": "ocr_approval_created",
            },
        ],
        required_prompt_handler="hoppe_ocr_approval",
        has_images=True,
    )

    assert decision == PromptDispatchDecision(
        action="respond",
        handler="hoppe_ocr_approval",
        text="Freigabe angelegt",
        reason="ocr_approval_created",
    )


def test_required_image_turn_accepts_matching_explicit_allow():
    """A matching handler can deliberately release a protected image turn."""
    from tui_gateway.prompt_dispatch_hooks import resolve_prompt_dispatch_results

    decision = resolve_prompt_dispatch_results(
        [
            {"action": "allow", "handler": "hoppe_ocr_approval"},
            {
                "action": "respond",
                "handler": "hoppe_ocr_approval",
                "text": "too late",
            },
        ],
        required_prompt_handler="hoppe_ocr_approval",
        has_images=True,
    )

    assert decision.action == "allow"
    assert decision.handler == "hoppe_ocr_approval"


def test_optional_desktop_image_uses_first_valid_response():
    """Desktop images retain OCR interception without claiming manual acceptance."""
    from tui_gateway.prompt_dispatch_hooks import resolve_prompt_dispatch_results

    decision = resolve_prompt_dispatch_results(
        [
            None,
            {"action": "allow"},
            {"action": "respond", "handler": "", "text": ""},
            {
                "action": "respond",
                "handler": "hoppe_ocr_approval",
                "text": "Desktop-Inbox",
            },
            {
                "action": "respond",
                "handler": "later",
                "text": "must not win",
            },
        ],
        required_prompt_handler=None,
        has_images=True,
    )

    assert decision.action == "respond"
    assert decision.handler == "hoppe_ocr_approval"
    assert decision.text == "Desktop-Inbox"


def test_plain_text_without_directive_allows_agent():
    """The image-only client policy must not block ordinary iOS text chat."""
    from tui_gateway.prompt_dispatch_hooks import resolve_prompt_dispatch_results

    decision = resolve_prompt_dispatch_results(
        [],
        required_prompt_handler="hoppe_ocr_approval",
        has_images=False,
    )

    assert decision.action == "allow"


def test_hook_invocation_exposes_only_immutable_public_payload(monkeypatch):
    """Plugins receive the stable public values, never mutable session internals."""
    from hermes_cli import lifecycle
    from tui_gateway.prompt_dispatch_hooks import invoke_pre_prompt_dispatch

    captured: dict[str, Any] = {}

    def capture(name: str, **kwargs: Any) -> list[dict[str, str]]:
        captured["name"] = name
        captured.update(kwargs)
        return [
            {
                "action": "respond",
                "handler": "hoppe_ocr_approval",
                "text": "Inbox",
            }
        ]

    monkeypatch.setattr(lifecycle, "invoke_hook", capture)

    decision = invoke_pre_prompt_dispatch(
        session_id="ios-ui",
        session_key="stored-ios",
        source="ios",
        text="Kontakt prüfen",
        attached_images=["/tmp/card.png"],
        required_prompt_handler="hoppe_ocr_approval",
    )

    assert decision.action == "respond"
    assert captured == {
        "name": "pre_prompt_dispatch",
        "session_id": "ios-ui",
        "session_key": "stored-ios",
        "surface": "tui",
        "source": "ios",
        "text": "Kontakt prüfen",
        "attached_images": ("/tmp/card.png",),
        "required_prompt_handler": "hoppe_ocr_approval",
    }


def test_hook_dispatch_failure_blocks_only_required_image_turn(monkeypatch):
    """A host/plugin dispatch failure closes the protected path without breaking text."""
    from hermes_cli import lifecycle
    from tui_gateway.prompt_dispatch_hooks import invoke_pre_prompt_dispatch

    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )

    blocked = invoke_pre_prompt_dispatch(
        session_id="ios-ui",
        session_key="stored-ios",
        source="ios",
        text="Bild",
        attached_images=["/tmp/card.png"],
        required_prompt_handler="hoppe_ocr_approval",
    )
    allowed = invoke_pre_prompt_dispatch(
        session_id="ios-ui",
        session_key="stored-ios",
        source="ios",
        text="Hallo",
        attached_images=[],
        required_prompt_handler="hoppe_ocr_approval",
    )

    assert blocked.action == "block"
    assert allowed.action == "allow"
