"""Task-scoped file publication for hosted Group Chat Bot turns."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.hosted_room_artifacts import (
    RoomArtifactError,
    RoomArtifactOutbox,
    current_room_artifact_scope,
)
from tools.registry import registry


SHARE_GROUP_FILE_SCHEMA = {
    "name": "share_group_file",
    "description": (
        "Share one local file with the current Group Chat. The file is copied "
        "into private room storage and becomes available to the user and the "
        "other Bots in this Group Chat. Never use it for credentials or secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the file to share.",
            },
            "name": {
                "type": "string",
                "description": "Optional filename shown in the Group Chat.",
            },
        },
        "required": ["path"],
    },
}


def share_group_file(path: str, *, name: str | None = None) -> str:
    """Copy a safe file into the current hosted-room output outbox."""

    scope = current_room_artifact_scope()
    if scope is None:
        return json.dumps({
            "ok": False,
            "error": "File sharing is available only during a hosted Group Chat turn.",
        })
    try:
        from gateway.platforms.base import validate_media_delivery_path
        from gateway.session_context import get_session_env
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from agent.file_safety import get_read_block_error

        candidate = Path(str(path or "")).expanduser()
        if candidate.is_symlink():
            raise RoomArtifactError("Symbolic links cannot be shared.")
        safe_path = validate_media_delivery_path(
            str(path or ""),
            session_key=get_session_env("HERMES_SESSION_KEY", ""),
        )
        if safe_path is None:
            raise RoomArtifactError(
                "That file cannot be shared. Move it to the workspace or a Hermes media folder and try again."
            )
        resolved = Path(safe_path).resolve(strict=True)
        active_home = Path(get_hermes_home()).resolve(strict=False)
        profiles_root = Path(get_default_hermes_root()).resolve(strict=False) / "profiles"
        try:
            resolved.relative_to(profiles_root)
        except ValueError:
            pass
        else:
            try:
                resolved.relative_to(active_home)
            except ValueError as exc:
                raise RoomArtifactError(
                    "Files owned by another Hermes profile cannot be shared."
                ) from exc
            if active_home == profiles_root.parent:
                raise RoomArtifactError(
                    "Files owned by another Hermes profile cannot be shared."
                )
        if get_read_block_error(str(resolved)):
            raise RoomArtifactError("Hermes credential and internal state files cannot be shared.")
        stored = RoomArtifactOutbox(Path(get_hermes_home()) / "state.db").put_path(
            scope=scope,
            path=resolved,
            name=name,
        )
        return json.dumps({
            "ok": True,
            "artifact_id": stored["artifact_id"],
            "name": stored["name"],
            "size": stored["size"],
            "sha256": stored["sha256"],
            "message": f"{stored['name']} will be shared with this Group Chat when your turn completes.",
        })
    except (OSError, RuntimeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})


registry.register(
    name="share_group_file",
    toolset="bot_room",
    schema=SHARE_GROUP_FILE_SCHEMA,
    handler=lambda args, **_kwargs: share_group_file(
        args.get("path", ""),
        name=args.get("name"),
    ),
    emoji="📎",
)


__all__ = ["SHARE_GROUP_FILE_SCHEMA", "share_group_file"]
