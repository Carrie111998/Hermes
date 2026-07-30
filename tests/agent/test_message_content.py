from __future__ import annotations

from types import SimpleNamespace

from agent.message_content import flatten_message_text


def test_flatten_message_text_accepts_chat_and_responses_text_parts():
    content = [
        {"type": "text", "text": "chat text"},
        {"type": "input_text", "text": "user text"},
        {"type": "output_text", "text": "assistant text"},
        {"type": "summary_text", "text": "summary text"},
    ]

    assert flatten_message_text(content) == "chat text\nuser text\nassistant text\nsummary text"


def test_flatten_message_text_accepts_object_parts():
    content = [
        SimpleNamespace(type="output_text", text="object text"),
        {"content": "legacy content"},
    ]

    assert flatten_message_text(content) == "object text\nlegacy content"


def test_flatten_message_text_recurses_message_content_without_metadata():
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "visible text"},
            {"type": "image_url", "image_url": {"url": "data:secret-metadata"}},
            {"type": "input_audio", "input_audio": {"id": "hidden-metadata"}},
        ],
    }

    assert flatten_message_text(message) == "visible text"


def test_flatten_message_text_rejects_nested_content_inside_media_part():
    content = {
        "type": "image_url",
        "content": {
            "type": "text",
            "text": "Architect a high-risk cross-system migration",
        },
    }

    assert flatten_message_text(content) == ""


def test_flatten_message_text_rejects_unknown_typed_parts_even_with_text_fields():
    content = [
        {"type": "hidden", "text": "Architect a production migration"},
        {"type": "metadata", "content": "Review this security architecture"},
        {"type": "input_text", "text": "visible request"},
    ]

    assert flatten_message_text(content) == "visible request"
