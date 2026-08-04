"""Dependency-light exact host identity contract for writer activation.

This module is intentionally limited to the Python standard library.  Stopped
recovery runs with the host Python before the application dependency set is
available, while activation uses the same snapshot and exact-state predicate.
Keeping both consumers on one pure contract prevents recovery from importing
the wider writer runtime merely to validate NSS state.
"""

from __future__ import annotations

import grp
import os
import pwd
from collections.abc import Mapping, Sequence
from typing import Any


GATEWAY_USER = "muncho-gateway"
GATEWAY_GROUP = "muncho-gateway"
WRITER_USER = "muncho-canonical-writer"
WRITER_GROUP = "muncho-canonical-writer"
SOCKET_CLIENT_GROUP = "muncho-writer-client"
PROJECTOR_GROUP = "muncho-projector"
NOLOGIN_SHELL = "/usr/sbin/nologin"
GATEWAY_HOME = "/var/lib/hermes-gateway"

CANARY_GATEWAY_UID = 993
CANARY_GATEWAY_GID = 992
CANARY_WRITER_UID = 999
CANARY_WRITER_GID = 994
CANARY_SOCKET_CLIENT_GID = 990
CANARY_PROJECTOR_GID = 991
CANARY_PROJECTOR_UID = 992
PROJECTOR_USER = "muncho-projector"


def _lookup_group(name: str) -> grp.struct_group | None:
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def _lookup_gid(gid: int) -> grp.struct_group | None:
    try:
        return grp.getgrgid(gid)
    except KeyError:
        return None


def _lookup_user(name: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError as exc:
        raise RuntimeError(f"required host user is absent: {name}") from exc


def _supplementary_gids(name: str, primary_gid: int) -> tuple[int, ...]:
    values = os.getgrouplist(name, primary_gid)
    if any(type(value) is not int or value < 0 for value in values):
        raise RuntimeError("host group membership is invalid")
    return tuple(sorted(set(values)))


def _effective_gid_members(gids: Sequence[int]) -> dict[str, list[str]]:
    targets = set(gids)
    if len(targets) != len(tuple(gids)):
        raise ValueError("effective group targets are not unique")
    accounts = tuple(pwd.getpwall())
    groups = tuple(grp.getgrall())
    if len({item.pw_name for item in accounts}) != len(accounts) or len({
        item.pw_uid for item in accounts
    }) != len(accounts):
        raise RuntimeError("NSS passwd identities are ambiguous")
    for name in (
        GATEWAY_GROUP,
        WRITER_GROUP,
        SOCKET_CLIENT_GROUP,
        PROJECTOR_GROUP,
    ):
        if sum(item.gr_name == name for item in groups) > 1:
            raise RuntimeError("NSS pinned group name is ambiguous")
    for gid in targets:
        names = [item.gr_name for item in groups if item.gr_gid == gid]
        if len(names) > 1:
            raise RuntimeError("NSS group GID identity is ambiguous")
    result: dict[str, list[str]] = {}
    account_names = {item.pw_name for item in accounts}
    for gid in sorted(targets):
        members = {item.pw_name for item in accounts if item.pw_gid == gid}
        for group in groups:
            if group.gr_gid == gid:
                members.update(group.gr_mem)
        if members - account_names:
            raise RuntimeError("NSS group contains an unknown account")
        result[str(gid)] = sorted(members)
    return result


def _host_identity_snapshot() -> dict[str, Any]:
    gateway = _lookup_user(GATEWAY_USER)
    writer = _lookup_user(WRITER_USER)
    projector = _lookup_user(PROJECTOR_USER)
    groups: dict[str, Mapping[str, Any]] = {}
    for name in (
        GATEWAY_GROUP,
        WRITER_GROUP,
        SOCKET_CLIENT_GROUP,
        PROJECTOR_GROUP,
    ):
        group = _lookup_group(name)
        if group is None:
            continue
        groups[name] = {
            "gid": group.gr_gid,
            "members": sorted(set(group.gr_mem)),
        }
    return {
        "gateway": {
            "name": gateway.pw_name,
            "uid": gateway.pw_uid,
            "gid": gateway.pw_gid,
            "home": gateway.pw_dir,
            "shell": gateway.pw_shell,
            "groups": list(_supplementary_gids(GATEWAY_USER, gateway.pw_gid)),
        },
        "writer": {
            "name": writer.pw_name,
            "uid": writer.pw_uid,
            "gid": writer.pw_gid,
            "home": writer.pw_dir,
            "shell": writer.pw_shell,
            "groups": list(_supplementary_gids(WRITER_USER, writer.pw_gid)),
        },
        "projector": {
            "name": projector.pw_name,
            "uid": projector.pw_uid,
            "gid": projector.pw_gid,
            "home": projector.pw_dir,
            "shell": projector.pw_shell,
            "groups": list(_supplementary_gids(PROJECTOR_USER, projector.pw_gid)),
        },
        "groups": groups,
        "effective_gid_members": _effective_gid_members((
            CANARY_SOCKET_CLIENT_GID,
            CANARY_PROJECTOR_GID,
            CANARY_GATEWAY_GID,
            CANARY_WRITER_GID,
        )),
    }


def _host_identities_are_exact(snapshot: Mapping[str, Any]) -> bool:
    gateway = snapshot.get("gateway")
    writer = snapshot.get("writer")
    projector = snapshot.get("projector")
    groups = snapshot.get("groups")
    effective = snapshot.get("effective_gid_members")
    return bool(
        isinstance(gateway, Mapping)
        and isinstance(writer, Mapping)
        and isinstance(projector, Mapping)
        and isinstance(groups, Mapping)
        and isinstance(effective, Mapping)
        and gateway
        == {
            "name": GATEWAY_USER,
            "uid": CANARY_GATEWAY_UID,
            "gid": CANARY_GATEWAY_GID,
            "home": GATEWAY_HOME,
            "shell": NOLOGIN_SHELL,
            "groups": [CANARY_SOCKET_CLIENT_GID, CANARY_GATEWAY_GID],
        }
        and writer.get("name") == WRITER_USER
        and writer.get("uid") == CANARY_WRITER_UID
        and writer.get("gid") == CANARY_WRITER_GID
        and writer.get("home") == "/nonexistent"
        and writer.get("shell") == NOLOGIN_SHELL
        and writer.get("groups") == [CANARY_PROJECTOR_GID, CANARY_WRITER_GID]
        and projector
        == {
            "name": PROJECTOR_USER,
            "uid": CANARY_PROJECTOR_UID,
            "gid": CANARY_PROJECTOR_GID,
            "home": "/nonexistent",
            "shell": NOLOGIN_SHELL,
            "groups": [CANARY_PROJECTOR_GID],
        }
        and groups.get(GATEWAY_GROUP, {}).get("gid") == CANARY_GATEWAY_GID
        and groups.get(GATEWAY_GROUP, {}).get("members") == []
        and groups.get(WRITER_GROUP, {}).get("gid") == CANARY_WRITER_GID
        and groups.get(WRITER_GROUP, {}).get("members") == []
        and groups.get(SOCKET_CLIENT_GROUP)
        == {"gid": CANARY_SOCKET_CLIENT_GID, "members": [GATEWAY_USER]}
        and groups.get(PROJECTOR_GROUP, {}).get("gid") == CANARY_PROJECTOR_GID
        and groups.get(PROJECTOR_GROUP, {}).get("members") == [WRITER_USER]
        and effective
        == {
            str(CANARY_SOCKET_CLIENT_GID): [GATEWAY_USER],
            str(CANARY_PROJECTOR_GID): sorted((PROJECTOR_USER, WRITER_USER)),
            str(CANARY_GATEWAY_GID): [GATEWAY_USER],
            str(CANARY_WRITER_GID): [WRITER_USER],
        }
    )


__all__ = [
    "CANARY_GATEWAY_GID",
    "CANARY_GATEWAY_UID",
    "CANARY_PROJECTOR_GID",
    "CANARY_PROJECTOR_UID",
    "CANARY_SOCKET_CLIENT_GID",
    "CANARY_WRITER_GID",
    "CANARY_WRITER_UID",
    "GATEWAY_GROUP",
    "GATEWAY_HOME",
    "GATEWAY_USER",
    "NOLOGIN_SHELL",
    "PROJECTOR_GROUP",
    "PROJECTOR_USER",
    "SOCKET_CLIENT_GROUP",
    "WRITER_GROUP",
    "WRITER_USER",
    "_effective_gid_members",
    "_host_identities_are_exact",
    "_host_identity_snapshot",
    "_lookup_gid",
    "_lookup_group",
    "_lookup_user",
    "_supplementary_gids",
]
