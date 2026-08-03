from __future__ import annotations

from types import SimpleNamespace

from agent.message_content import flatten_message_text, has_non_text_content
from tools.todo_tool import TODO_INJECTION_HEADER


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


def test_has_non_text_content_accepts_media_and_structured_parts():
    assert has_non_text_content(
        [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    )
    assert has_non_text_content(
        [SimpleNamespace(type="input_audio", input_audio={"data": "AA=="})]
    )
    assert has_non_text_content(
        [{"type": "document", "document": {"name": "notes.pdf"}}]
    )


def test_has_non_text_content_rejects_text_and_blank_inputs():
    assert not has_non_text_content("plain text")
    assert not has_non_text_content([])
    assert not has_non_text_content([{"type": "text", "text": "hello"}])
    assert not has_non_text_content(SimpleNamespace(type="output_text", text="hello"))


def test_flatten_message_text_does_not_stringify_empty_structured_parts():
    assert flatten_message_text({}) == ""
    assert flatten_message_text({"type": "text", "text": ""}) == ""
    assert flatten_message_text(SimpleNamespace(type="output_text", text="")) == ""


def test_media_with_todo_text_keeps_structured_provenance():
    todo = f"{TODO_INJECTION_HEADER}\n- [>] Finish the task"
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        {"type": "text", "text": todo},
    ]

    assert has_non_text_content(content)
    assert flatten_message_text(content) == todo
