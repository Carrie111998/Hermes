"""Tests for tools/discord_api.roles request builders (Discord REST v10)."""

import pytest

from tools.discord_api.roles import (
    RoleError,
    add_member_role_request,
    create_role_request,
    delete_role_request,
    edit_role_request,
    remove_member_role_request,
)

GUILD = 123456789012345678
ROLE = 234567890123456789
USER = 345678901234567890


class TestCreateRoleRequest:
    def test_shape(self):
        desc = create_role_request(
            GUILD, name="Moderator", permissions=0x8, color=0x00FF00,
            hoist=True, mentionable=True,
        )
        assert desc["method"] == "POST"
        assert desc["path"] == f"/guilds/{GUILD}/roles"
        assert desc["json"] == {
            "name": "Moderator",
            "permissions": 0x8,
            "color": 0x00FF00,
            "hoist": True,
            "mentionable": True,
        }

    def test_defaults_omit_optional_fields(self):
        desc = create_role_request(GUILD)
        assert desc["method"] == "POST"
        assert desc["json"] == {}

    def test_name_max_length_ok(self):
        name = "x" * 100
        assert create_role_request(GUILD, name=name)["json"]["name"] == name

    @pytest.mark.parametrize("name", ["x" * 101, 123])
    def test_name_invalid(self, name):
        with pytest.raises(RoleError):
            create_role_request(GUILD, name=name)

    def test_name_too_long_raises(self):
        with pytest.raises(RoleError):
            create_role_request(GUILD, name="x" * 101)

    @pytest.mark.parametrize("color", [0, 0xFFFFFF])
    def test_color_bounds_ok(self, color):
        assert create_role_request(GUILD, color=color)["json"]["color"] == color

    @pytest.mark.parametrize("color", [-1, 0x1000000, "0xFF", 1.5, True])
    def test_color_out_of_bounds_raises(self, color):
        with pytest.raises(RoleError):
            create_role_request(GUILD, color=color)

    @pytest.mark.parametrize("permissions", [0, 1, 0x8, (1 << 64) - 1])
    def test_permissions_bitfield_ok(self, permissions):
        assert (
            create_role_request(GUILD, permissions=permissions)["json"]["permissions"]
            == permissions
        )

    @pytest.mark.parametrize("permissions", [-1, 1 << 64, "8", 8.0, True])
    def test_permissions_invalid_raises(self, permissions):
        with pytest.raises(RoleError):
            create_role_request(GUILD, permissions=permissions)

    def test_hoist_mentionable_false_omitted(self):
        desc = create_role_request(GUILD, hoist=False, mentionable=False)
        assert "hoist" not in desc["json"]
        assert "mentionable" not in desc["json"]

    def test_flag_type_validated(self):
        with pytest.raises(RoleError):
            create_role_request(GUILD, hoist="yes")


class TestEditRoleRequest:
    def test_shape(self):
        desc = edit_role_request(GUILD, ROLE, name="Admin", color=0x0000FF)
        assert desc["method"] == "PATCH"
        assert desc["path"] == f"/guilds/{GUILD}/roles/{ROLE}"
        assert desc["json"] == {"name": "Admin", "color": 0x0000FF}

    def test_only_provided_fields(self):
        desc = edit_role_request(GUILD, ROLE, mentionable=True)
        assert desc["json"] == {"mentionable": True}

    def test_no_fields_raises(self):
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE)

    def test_unknown_field_raises(self):
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE, icon="data:image/png;base64,AA==")

    def test_invalid_field_values_raise(self):
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE, name="x" * 101)
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE, color=-1)
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE, permissions=1 << 64)
        with pytest.raises(RoleError):
            edit_role_request(GUILD, ROLE, hoist="true")


class TestDeleteRoleRequest:
    def test_shape(self):
        desc = delete_role_request(GUILD, ROLE)
        assert desc["method"] == "DELETE"
        assert desc["path"] == f"/guilds/{GUILD}/roles/{ROLE}"
        assert "json" not in desc


class TestMemberRoleAssignment:
    def test_add_shape(self):
        desc = add_member_role_request(GUILD, USER, ROLE)
        assert desc["method"] == "PUT"
        assert desc["path"] == f"/guilds/{GUILD}/members/{USER}/roles/{ROLE}"
        assert "json" not in desc

    def test_remove_shape(self):
        desc = remove_member_role_request(GUILD, USER, ROLE)
        assert desc["method"] == "DELETE"
        assert desc["path"] == f"/guilds/{GUILD}/members/{USER}/roles/{ROLE}"
        assert "json" not in desc


INVALID_SNOWFLAKES = [0, -1, 1 << 64, "123", "123456789012345678", None, 1.5, True]


class TestSnowflakeValidation:
    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_create_guild_id(self, bad):
        with pytest.raises(RoleError):
            create_role_request(bad)

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_edit_guild_id(self, bad):
        with pytest.raises(RoleError):
            edit_role_request(bad, ROLE, name="x")

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_edit_role_id(self, bad):
        with pytest.raises(RoleError):
            edit_role_request(GUILD, bad, name="x")

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_delete_role_id(self, bad):
        with pytest.raises(RoleError):
            delete_role_request(GUILD, bad)

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_add_user_id(self, bad):
        with pytest.raises(RoleError):
            add_member_role_request(GUILD, bad, ROLE)

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_remove_role_id(self, bad):
        with pytest.raises(RoleError):
            remove_member_role_request(GUILD, USER, bad)

    def test_role_error_is_value_error(self):
        assert issubclass(RoleError, ValueError)

    def test_max_snowflake_accepted(self):
        top = (1 << 64) - 1
        assert add_member_role_request(top, top, top)["path"] == (
            f"/guilds/{top}/members/{top}/roles/{top}"
        )
