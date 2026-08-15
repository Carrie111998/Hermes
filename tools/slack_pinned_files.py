"""Turn-scoped lazy downloads for files attached to pinned Slack messages."""

import json

from tools.registry import registry


async def download_pinned_slack_file(file_id: str) -> str:
    """Download a pinned Slack document authorised for the current turn."""
    from gateway.config import Platform
    from gateway.session_context import (
        get_session_env,
        get_slack_pinned_file_ids,
    )

    if get_session_env("HERMES_SESSION_PLATFORM", "") != "slack":
        return json.dumps({
            "success": False,
            "error": "This tool is only available in Slack sessions.",
        })
    file_id = str(file_id or "").strip()
    if file_id not in get_slack_pinned_file_ids():
        return json.dumps({
            "success": False,
            "error": "That file ID is not attached to a pinned message in this channel for the current turn.",
        })

    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    adapter = runner.adapters.get(Platform.SLACK) if runner is not None else None
    if adapter is None:
        return json.dumps({
            "success": False,
            "error": "The live Slack adapter is unavailable.",
        })

    try:
        result = await adapter.download_pinned_file(
            file_id=file_id,
            channel_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
            team_id=get_session_env("HERMES_SESSION_SCOPE_ID", ""),
        )
    except Exception as exc:
        return json.dumps({
            "success": False,
            "error": f"Could not download pinned Slack document {file_id}: {exc}",
        })
    return json.dumps({"success": True, **result})


SLACK_DOWNLOAD_PINNED_FILE_SCHEMA = {
    "name": "slack_download_pinned_file",
    "description": (
        "Download one document listed in the current Slack channel's pinned context. "
        "Pass exactly the Slack file ID shown there. Returns a local path. If the "
        "tool reports an error, tell the user; never claim to have read the file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Slack file ID shown in the current pinned context, for example F0BQBQ4MVJR.",
            }
        },
        "required": ["file_id"],
    },
}


registry.register(
    name="slack_download_pinned_file",
    toolset="slack_pinned_files",
    schema=SLACK_DOWNLOAD_PINNED_FILE_SCHEMA,
    handler=lambda args, **kw: download_pinned_slack_file(args.get("file_id", "")),
    is_async=True,
    emoji="📎",
)
