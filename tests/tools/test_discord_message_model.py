"""Tests for the Discord structured inbound message model (M1)."""

import pytest

from plugins.platforms.discord.message_model import (
    MessageContent,
    MessageProjectionError,
    project_message,
)


def test_plain_content_message():
    result = project_message(
        {
            "id": "100",
            "content": "hello world",
            "channel_id": "1",
            "author": {"id": "42", "username": "alice"},
        }
    )
    assert isinstance(result, MessageContent)
    assert result.content == "hello world"
    assert result.embeds == []
    assert result.attachments == []
    assert result.replied_to is None
    assert result.thread_starter is False
    assert result.flags == 0
    assert result.type == 0


def test_embed_only_message():
    result = project_message(
        {
            "id": "101",
            "content": "",
            "embeds": [{"type": "rich", "title": "A title", "description": "desc"}],
        }
    )
    assert result.content == ""
    assert len(result.embeds) == 1
    assert result.embeds[0]["title"] == "A title"


def test_referenced_message_sets_replied_to():
    result = project_message(
        {
            "id": "102",
            "content": "replying",
            "referenced_message": {"id": "50", "content": "original", "channel_id": "1"},
        }
    )
    assert result.replied_to == "50"
    assert result.content == "replying"


def test_missing_referenced_message_gives_none():
    result = project_message({"id": "103", "content": "no ref"})
    assert result.replied_to is None


def test_type_21_thread_starter():
    result = project_message({"id": "104", "content": "", "type": 21})
    assert result.thread_starter is True
    assert result.type == 21


def test_type_0_is_not_thread_starter():
    result = project_message({"id": "105", "content": "regular", "type": 0})
    assert result.thread_starter is False


def test_missing_embeds_and_attachments_default_to_empty():
    result = project_message({"id": "106", "content": "bare"})
    assert result.embeds == []
    assert result.attachments == []


def test_attachments_projected_to_urls():
    result = project_message(
        {
            "id": "107",
            "content": "pic",
            "attachments": [
                {"id": "1", "filename": "a.png", "url": "https://cdn.example/a.png"},
                {"id": "2", "filename": "b.png", "url": "https://cdn.example/b.png"},
            ],
        }
    )
    assert result.attachments == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]


def test_flags_default_to_zero():
    result = project_message({"id": "108", "content": "no flags"})
    assert result.flags == 0


def test_flags_parsed():
    result = project_message({"id": "109", "content": "flagged", "flags": 1 << 6})
    assert result.flags == 64


def test_content_missing_defaults_to_empty_string():
    result = project_message({"id": "110"})
    assert result.content == ""


def test_non_dict_payload_raises_projection_error():
    assert issubclass(MessageProjectionError, ValueError)
    for bad in ("not a dict", None, [1, 2, 3], 42):
        with pytest.raises(MessageProjectionError):
            project_message(bad)
