"""sync_turn must accept structured (list-of-parts) message content.

The agent core can pass content in the OpenAI content-parts shape.  Before the
coercion fix, ``sanitize_context`` received the raw list and raised
``TypeError: expected string or bytes-like object, got 'list'`` — every turn
sync failed and Honcho silently accumulated nothing.
"""

from plugins.memory.honcho import HonchoMemoryProvider


def test_coerce_turn_text_flattens_content_parts():
    value = [
        {"type": "text", "text": "first part"},
        {"type": "text", "text": "second part"},
        {"type": "image_url", "image_url": {"url": "ignored"}},
    ]
    assert HonchoMemoryProvider._coerce_turn_text(value) == "first part\nsecond part"


def test_coerce_turn_text_passes_strings_through():
    assert HonchoMemoryProvider._coerce_turn_text("plain text") == "plain text"


def test_coerce_turn_text_handles_none_and_scalars():
    assert HonchoMemoryProvider._coerce_turn_text(None) == ""
    assert HonchoMemoryProvider._coerce_turn_text(42) == "42"


def test_coerce_turn_text_handles_plain_string_parts():
    assert HonchoMemoryProvider._coerce_turn_text(["a", "", "b"]) == "a\nb"
