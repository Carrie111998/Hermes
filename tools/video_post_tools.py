"""Built-in video post-production tools (local ffmpeg backend).

Registers the ``video_post`` toolset: ``video_concat``, ``video_add_captions``,
``video_audio_mix``, ``video_pip`` and ``html_to_video``. Auto-discovered by
``discover_builtin_tools`` via the top-level ``registry.register`` calls below
(no plugin manifest, no config toggle — the tools ship with hermes).

Implementation lives in ``agent/video_post/``; this module is a thin
registration shim, mirroring how ``tools/video_generation_tool.py`` wraps
``agent/video_gen_provider.py``.

``check_fn`` is attached only to the first tool: the registry keeps a single
check_fn per toolset (see ``tools/registry.py``), and ffmpeg availability
gates all five tools. ``html_to_video``'s browser dependency is deliberately
NOT part of the check_fn — it is resolved at call time so the tool stays
visible and returns a clear install hint instead of disappearing.
"""

from __future__ import annotations

from tools.registry import registry

from agent.video_post.ffmpeg import ffmpeg_available
from agent.video_post.html_video import handle_html_to_video
from agent.video_post.schemas import (
    HTML_TO_VIDEO_SCHEMA,
    VIDEO_ADD_CAPTIONS_SCHEMA,
    VIDEO_AUDIO_MIX_SCHEMA,
    VIDEO_CONCAT_SCHEMA,
    VIDEO_PIP_SCHEMA,
)
from agent.video_post.tools import (
    handle_video_add_captions,
    handle_video_audio_mix,
    handle_video_concat,
    handle_video_pip,
)

registry.register(
    name="video_concat",
    toolset="video_post",
    schema=VIDEO_CONCAT_SCHEMA,
    handler=handle_video_concat,
    check_fn=ffmpeg_available,
    is_async=False,
    emoji="🎞️",
    description="Concatenate video clips into one MP4.",
)

registry.register(
    name="video_add_captions",
    toolset="video_post",
    schema=VIDEO_ADD_CAPTIONS_SCHEMA,
    handler=handle_video_add_captions,
    is_async=False,
    emoji="💬",
    description="Burn or embed subtitles into a video.",
)

registry.register(
    name="video_audio_mix",
    toolset="video_post",
    schema=VIDEO_AUDIO_MIX_SCHEMA,
    handler=handle_video_audio_mix,
    is_async=False,
    emoji="🎵",
    description="Replace or mix a video's audio track.",
)

registry.register(
    name="video_pip",
    toolset="video_post",
    schema=VIDEO_PIP_SCHEMA,
    handler=handle_video_pip,
    is_async=False,
    emoji="🖼️",
    description="Overlay a picture-in-picture video on a base video.",
)

registry.register(
    name="html_to_video",
    toolset="video_post",
    schema=HTML_TO_VIDEO_SCHEMA,
    handler=handle_html_to_video,
    is_async=False,
    emoji="🌐",
    description="Render an HTML document or animation into an MP4.",
)
