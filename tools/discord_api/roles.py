"""Discord role CRUD + assignment REST request builders.

Pure, side-effect-free builders that validate inputs and produce request
descriptors aligned with the Discord REST API v10 role resources:

* ``POST   /guilds/{guild.id}/roles``                       - Create Guild Role
* ``PATCH  /guilds/{guild.id}/roles/{role.id}``             - Modify Guild Role
* ``DELETE /guilds/{guild.id}/roles/{role.id}``             - Delete Guild Role
* ``PUT    /guilds/{guild.id}/members/{user.id}/roles/{role.id}`` - Add Guild Member Role
* ``DELETE /guilds/{guild.id}/members/{user.id}/roles/{role.id}`` - Remove Guild Member Role

Every descriptor is a plain dict of the form ``{"method": ..., "path": ...}``
with an optional ``"json"`` key carrying the validated request body. No
network, token, or base-URL handling lives here.
"""

from __future__ import annotations

__all__ = [
    "RoleError",
    "create_role_request",
    "edit_role_request",
    "delete_role_request",
    "add_member_role_request",
    "remove_member_role_request",
]

#: Discord snowflakes are unsigned 64-bit integers in the range 1..2**64-1.
SNOWFLAKE_MIN = 1
SNOWFLAKE_MAX = (1 << 64) - 1

#: Role ``name`` is limited to 100 characters (REST v10 role object).
NAME_MAX_LENGTH = 100

#: Role ``color`` is an RGB integer in 0..0xFFFFFF (REST v10 role object).
COLOR_MIN = 0
COLOR_MAX = 0xFFFFFF

#: ``permissions`` is a 64-bit permission bitfield (REST v10 permissions topic).
PERMISSIONS_MIN = 0
PERMISSIONS_MAX = (1 << 64) - 1

#: Fields accepted by the role create/modify endpoints.
ROLE_FIELDS = ("name", "permissions", "color", "hoist", "mentionable")


class RoleError(ValueError):
    """Raised when a role request descriptor cannot be validated."""


def _validate_snowflake(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoleError(
            f"{field} must be an integer snowflake, got {value!r}"
        )
    if not SNOWFLAKE_MIN <= value <= SNOWFLAKE_MAX:
        raise RoleError(
            f"{field} must be a snowflake in "
            f"[{SNOWFLAKE_MIN}, {SNOWFLAKE_MAX}], got {value!r}"
        )
    return value


def _validate_name(name):
    if not isinstance(name, str):
        raise RoleError(f"name must be a string, got {name!r}")
    if len(name) > NAME_MAX_LENGTH:
        raise RoleError(
            f"name must be at most {NAME_MAX_LENGTH} characters, "
            f"got {len(name)}"
        )
    return name


def _validate_color(color):
    if isinstance(color, bool) or not isinstance(color, int):
        raise RoleError(f"color must be an integer, got {color!r}")
    if not COLOR_MIN <= color <= COLOR_MAX:
        raise RoleError(
            f"color must be in 0..0x{COLOR_MAX:X}, got {color!r}"
        )
    return color


def _validate_permissions(permissions):
    if isinstance(permissions, bool) or not isinstance(permissions, int):
        raise RoleError(f"permissions must be an integer bitfield, got {permissions!r}")
    if not PERMISSIONS_MIN <= permissions <= PERMISSIONS_MAX:
        raise RoleError(
            f"permissions must be an integer bitfield in "
            f"[{PERMISSIONS_MIN}, {PERMISSIONS_MAX}], got {permissions!r}"
        )
    return permissions


def _validate_flag(value, field):
    if not isinstance(value, bool):
        raise RoleError(f"{field} must be a bool, got {value!r}")
    return value


def _validate_role_field(name, value):
    """Validate one named role field; unknown fields are rejected."""
    if name == "name":
        return _validate_name(value)
    if name == "permissions":
        return _validate_permissions(value)
    if name == "color":
        return _validate_color(value)
    if name in ("hoist", "mentionable"):
        return _validate_flag(value, name)
    raise RoleError(f"unsupported role field {name!r}")


def create_role_request(guild_id, *, name=None, permissions=None, color=None,
                        hoist=False, mentionable=False):
    """Build a ``POST /guilds/{guild}/roles`` create-role request descriptor.

    Optional body fields (``name``, ``permissions``, ``color``) are included
    only when provided (not ``None``); ``hoist``/``mentionable`` are included
    only when ``True``.
    """
    _validate_snowflake(guild_id, "guild_id")
    body = {}
    if name is not None:
        body["name"] = _validate_name(name)
    if permissions is not None:
        body["permissions"] = _validate_permissions(permissions)
    if color is not None:
        body["color"] = _validate_color(color)
    if hoist:
        body["hoist"] = _validate_flag(hoist, "hoist")
    if mentionable:
        body["mentionable"] = _validate_flag(mentionable, "mentionable")
    return {
        "method": "POST",
        "path": f"/guilds/{guild_id}/roles",
        "json": body,
    }


def edit_role_request(guild_id, role_id, **fields):
    """Build a ``PATCH /guilds/{guild}/roles/{role}`` modify-role descriptor.

    Only the fields explicitly passed are included in the body; passing no
    fields is a validation error.
    """
    _validate_snowflake(guild_id, "guild_id")
    _validate_snowflake(role_id, "role_id")
    if not fields:
        raise RoleError("edit_role_request requires at least one field to edit")
    unknown = set(fields) - set(ROLE_FIELDS)
    if unknown:
        raise RoleError(
            f"unsupported role field(s) {sorted(unknown)!r}; "
            f"supported: {sorted(ROLE_FIELDS)!r}"
        )
    body = {key: _validate_role_field(key, value) for key, value in fields.items()}
    return {
        "method": "PATCH",
        "path": f"/guilds/{guild_id}/roles/{role_id}",
        "json": body,
    }


def delete_role_request(guild_id, role_id):
    """Build a ``DELETE /guilds/{guild}/roles/{role}`` delete-role descriptor."""
    _validate_snowflake(guild_id, "guild_id")
    _validate_snowflake(role_id, "role_id")
    return {"method": "DELETE", "path": f"/guilds/{guild_id}/roles/{role_id}"}


def add_member_role_request(guild_id, user_id, role_id):
    """Build a ``PUT /guilds/{guild}/members/{user}/roles/{role}`` descriptor."""
    _validate_snowflake(guild_id, "guild_id")
    _validate_snowflake(user_id, "user_id")
    _validate_snowflake(role_id, "role_id")
    return {
        "method": "PUT",
        "path": f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
    }


def remove_member_role_request(guild_id, user_id, role_id):
    """Build a ``DELETE /guilds/{guild}/members/{user}/roles/{role}`` descriptor."""
    _validate_snowflake(guild_id, "guild_id")
    _validate_snowflake(user_id, "user_id")
    _validate_snowflake(role_id, "role_id")
    return {
        "method": "DELETE",
        "path": f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
    }
