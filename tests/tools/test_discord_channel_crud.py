"""Behavior tests for Discord channel CRUD actions."""

import json
from unittest.mock import patch

from tools.discord_tool import discord_admin_handler, get_dynamic_schema_admin


class TestCreateChannel:
    @patch("tools.discord_tool._discord_request")
    def test_creates_text_channel_with_explicit_fields(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"discord": {"server_actions": ""}},
        )
        mock_req.return_value = {
            "id": "44", "guild_id": "111", "name": "x-content",
            "type": 0, "parent_id": "22", "topic": "Discuss X strategy",
            "position": 3, "nsfw": False,
        }
        result = json.loads(discord_admin_handler(
            action="create_channel", guild_id="111", name="x-content",
            channel_type="text", parent_id="22", topic="Discuss X strategy",
            nsfw=False, rate_limit_per_user=5,
        ))
        assert result == {
            "success": True,
            "channel": {
                "id": "44", "guild_id": "111", "name": "x-content",
                "type": "text", "parent_id": "22", "topic": "Discuss X strategy",
                "position": 3, "nsfw": False,
            },
        }
        mock_req.assert_called_once_with(
            "POST", "/guilds/111/channels", "test-token",
            body={
                "name": "x-content", "type": 0, "parent_id": "22",
                "topic": "Discuss X strategy", "nsfw": False,
                "rate_limit_per_user": 5,
            },
        )

    @patch("tools.discord_tool._discord_request")
    def test_rejects_unsupported_channel_type_before_api_call(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        result = json.loads(discord_admin_handler(
            action="create_channel", guild_id="111", name="bad", channel_type="stage",
        ))
        assert "error" in result
        assert "channel_type" in result["error"]
        mock_req.assert_not_called()

    @patch("tools.discord_tool._discord_request")
    def test_creates_category_without_text_only_fields(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        mock_req.return_value = {
            "id": "22", "guild_id": "111", "name": "Content", "type": 4,
            "parent_id": None, "position": 1,
        }
        result = json.loads(discord_admin_handler(
            action="create_channel", guild_id="111", name="Content",
            channel_type="category",
        ))
        assert result["channel"]["type"] == "category"
        mock_req.assert_called_once_with(
            "POST", "/guilds/111/channels", "test-token",
            body={"name": "Content", "type": 4},
        )

    @patch("tools.discord_tool._discord_request")
    def test_rejects_text_only_fields_for_category(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        result = json.loads(discord_admin_handler(
            action="create_channel", guild_id="111", name="Content",
            channel_type="category", topic="not valid for a category",
        ))
        assert "error" in result
        assert "category" in result["error"]
        mock_req.assert_not_called()

    @patch("tools.discord_tool._discord_request")
    def test_rejects_out_of_range_slowmode(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        result = json.loads(discord_admin_handler(
            action="create_channel", guild_id="111", name="chat",
            rate_limit_per_user=21601,
        ))
        assert "error" in result
        assert "rate_limit_per_user" in result["error"]
        mock_req.assert_not_called()


class TestEditChannel:
    @patch("tools.discord_tool._discord_request")
    def test_updates_only_explicit_fields(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        mock_req.return_value = {
            "id": "44", "guild_id": "111", "name": "x-strategy",
            "type": 0, "parent_id": None, "topic": "New topic",
            "position": 2, "nsfw": False,
        }
        result = json.loads(discord_admin_handler(
            action="edit_channel", channel_id="44", name="x-strategy",
            topic="New topic", parent_id="null", position=2,
        ))
        assert result["success"] is True
        mock_req.assert_called_once_with(
            "PATCH", "/channels/44", "test-token",
            body={"name": "x-strategy", "topic": "New topic", "parent_id": None, "position": 2},
        )

    @patch("tools.discord_tool._discord_request")
    def test_requires_at_least_one_edit_field(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        result = json.loads(discord_admin_handler(action="edit_channel", channel_id="44"))
        assert "error" in result
        assert "at least one" in result["error"]
        mock_req.assert_not_called()

    @patch("tools.discord_tool._discord_request")
    def test_allows_clearing_topic(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        mock_req.return_value = {"id": "44", "name": "chat", "type": 0, "topic": None}
        result = json.loads(discord_admin_handler(
            action="edit_channel", channel_id="44", topic="",
        ))
        assert result["success"] is True
        mock_req.assert_called_once_with(
            "PATCH", "/channels/44", "test-token", body={"topic": ""},
        )


class TestDeleteChannel:
    @patch("tools.discord_tool._discord_request")
    def test_deletes_channel(self, mock_req, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
        mock_req.return_value = {"id": "44", "name": "old-channel", "type": 0}
        result = json.loads(discord_admin_handler(action="delete_channel", channel_id="44"))
        assert result == {
            "success": True,
            "deleted_channel": {"id": "44", "name": "old-channel", "type": "text"},
        }
        mock_req.assert_called_once_with("DELETE", "/channels/44", "test-token")


def test_admin_schema_exposes_channel_crud(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"discord": {"server_actions": ""}},
    )
    schema = get_dynamic_schema_admin()
    actions = schema["parameters"]["properties"]["action"]["enum"]
    assert "create_channel" in actions
    assert "edit_channel" in actions
    assert "delete_channel" in actions
    props = schema["parameters"]["properties"]
    assert props["channel_type"]["enum"] == ["text", "category"]
    assert "parent_id" in props
    assert "topic" in props
    assert "position" in props
