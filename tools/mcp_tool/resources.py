#!/usr/bin/env python3
"""MCP resource rendering helpers (images, audio, filenames).

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.resources import _render_mcp_resource_block``
continues to work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _cache_mcp_audio_block,
    _cache_mcp_image_block,
    _mcp_image_extension_for_mime_type,
    _mcp_resource_filename,
    _render_mcp_resource_block,
)

__all__ = [
    "_cache_mcp_audio_block",
    "_cache_mcp_image_block",
    "_mcp_image_extension_for_mime_type",
    "_mcp_resource_filename",
    "_render_mcp_resource_block",
]
