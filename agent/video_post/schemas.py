"""OpenAI function-calling schemas for the five video post-production tools.

All media inputs accept any of: an http(s) URL, a file:// URI, a
data:<mime>;base64 URI, or an absolute local path. All tools return a JSON
string; on success ``{"success": true, "video": "<absolute path>", ...}``.
"""

_MEDIA_REF = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Media reference: http(s) URL, file:// URI, data:<mime>;base64 URI, "
        "or absolute local path."
    ),
}

_TIMEOUT = {
    "type": "integer",
    "minimum": 10,
    "maximum": 1800,
    "description": "Max seconds for the ffmpeg run (clamped to the plugin max).",
}


VIDEO_CONCAT_SCHEMA = {
    "name": "video_concat",
    "description": (
        "Concatenate multiple video clips into one MP4. Clips with different "
        "resolution/fps/codec are auto-normalized by default (re-encode); set "
        "normalize=false for a fast lossless concat when all clips already share "
        "codec, resolution and fps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": _MEDIA_REF,
                "minItems": 2,
                "maxItems": 20,
                "description": "Ordered clip references.",
            },
            "normalize": {
                "type": "boolean",
                "default": True,
                "description": (
                    "true: re-encode all clips to a common size/fps/codec (robust). "
                    "false: concat demuxer with stream copy (fast/lossless, requires "
                    "identical codec+resolution+fps)."
                ),
            },
            "width": {"type": "integer", "minimum": 64, "maximum": 7680,
                      "description": "Target width when normalize=true. Default: first clip."},
            "height": {"type": "integer", "minimum": 64, "maximum": 4320,
                       "description": "Target height when normalize=true. Default: first clip."},
            "fps": {"type": "number", "minimum": 1, "maximum": 120,
                    "description": "Target fps when normalize=true. Default: first clip (fallback 30)."},
            "crf": {"type": "integer", "minimum": 0, "maximum": 40, "default": 20},
            "timeout_sec": _TIMEOUT,
        },
        "required": ["clips"],
        "additionalProperties": False,
    },
}


VIDEO_ADD_CAPTIONS_SCHEMA = {
    "name": "video_add_captions",
    "description": (
        "Burn or embed subtitles into a video. Provide subtitle content as SRT/ASS "
        "text (subtitles_text) or as a file reference (subtitles_file). 'burn' "
        "re-encodes visible captions into the MP4; 'soft' embeds a selectable "
        "subtitle track (SRT only)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video": _MEDIA_REF,
            "subtitles_text": {"type": "string", "minLength": 1,
                               "description": "Raw SRT or ASS subtitle content."},
            "subtitles_file": _MEDIA_REF,
            "mode": {"type": "string", "enum": ["burn", "soft"], "default": "burn"},
            "style": {
                "type": "object",
                "properties": {
                    "font": {"type": "string",
                             "description": "System font name (e.g. 'PingFang SC' for CJK)."},
                    "font_size": {"type": "integer", "minimum": 6, "maximum": 200},
                    "outline": {"type": "number", "minimum": 0, "maximum": 10},
                    "margin_v": {"type": "integer", "minimum": 0, "maximum": 500},
                },
                "additionalProperties": False,
                "description": "burn mode only. Assembled into a libass force_style.",
            },
            "force_style": {"type": "string", "maxLength": 500,
                            "description": "burn mode only. Raw libass ForceStyle string; overrides style."},
            "fonts_dir": {"type": "string",
                          "description": "burn mode only. Directory with .ttf/.otf fonts (libass fontsdir)."},
            "timeout_sec": _TIMEOUT,
        },
        "required": ["video"],
        "additionalProperties": False,
    },
}


VIDEO_AUDIO_MIX_SCHEMA = {
    "name": "video_audio_mix",
    "description": (
        "Replace or mix the audio track of a video. 'replace' swaps in a new track "
        "(video stream copied, fast). 'mix' blends new audio over the existing track. "
        "If the base video has no audio track, 'mix' degrades to 'replace' with a note."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video": _MEDIA_REF,
            "audio": dict(_MEDIA_REF, description="Music/narration reference."),
            "mode": {"type": "string", "enum": ["replace", "mix"], "default": "replace"},
            "volume": {"type": "number", "minimum": 0, "maximum": 3, "default": 1.0,
                       "description": "Gain applied to the NEW audio track."},
            "original_volume": {"type": "number", "minimum": 0, "maximum": 3, "default": 1.0,
                                "description": "mix mode only. Gain for the existing base audio."},
            "end": {"type": "string", "enum": ["video", "audio"], "default": "video",
                    "description": "Which input sets output length. video: trim new audio at video end."},
            "timeout_sec": _TIMEOUT,
        },
        "required": ["video", "audio"],
        "additionalProperties": False,
    },
}


VIDEO_PIP_SCHEMA = {
    "name": "video_pip",
    "description": (
        "Overlay a picture-in-picture video on top of a base video. The overlay is "
        "scaled to a fraction of the base width and placed at a preset corner/center. "
        "Base video is re-encoded; base audio is preserved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "base": _MEDIA_REF,
            "overlay": dict(_MEDIA_REF, description="PiP video reference."),
            "position": {"type": "string",
                         "enum": ["top_left", "top_right", "bottom_left", "bottom_right", "center"],
                         "default": "bottom_right"},
            "scale": {"type": "number", "minimum": 0.05, "maximum": 0.95, "default": 0.25,
                      "description": "Overlay width as a fraction of base width."},
            "margin_px": {"type": "integer", "minimum": 0, "maximum": 500, "default": 16},
            "loop_overlay": {"type": "boolean", "default": False,
                             "description": "Loop the overlay if shorter than the base."},
            "timeout_sec": _TIMEOUT,
        },
        "required": ["base", "overlay"],
        "additionalProperties": False,
    },
}


HTML_TO_VIDEO_SCHEMA = {
    "name": "html_to_video",
    "description": (
        "Render an HTML document/animation into an MP4: headless Chromium (Playwright) "
        "captures frames in real time, encoded by ffmpeg. CSS/JS animations run in "
        "wall-clock time. Requires playwright + chromium; returns a clear "
        "install-instruction error when missing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "html": {"type": "string", "minLength": 1,
                     "description": "Full HTML document content to render."},
            "source": dict(_MEDIA_REF,
                           description="Reference to an .html file. Use exactly one of html/source."),
            "width": {"type": "integer", "minimum": 160, "maximum": 3840, "default": 1280},
            "height": {"type": "integer", "minimum": 160, "maximum": 2160, "default": 720},
            "fps": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            "duration_sec": {"type": "number", "minimum": 0.5, "maximum": 300, "default": 5,
                             "description": "Wall-clock capture duration; output length equals this."},
            "settle_sec": {"type": "number", "minimum": 0, "maximum": 30, "default": 1.0,
                           "description": "Wait after page load before capture starts."},
            "device_scale_factor": {"type": "integer", "enum": [1, 2], "default": 1},
            "timeout_sec": _TIMEOUT,
        },
        "required": [],
        "additionalProperties": False,
    },
}


ALL_SCHEMAS = [
    VIDEO_CONCAT_SCHEMA,
    VIDEO_ADD_CAPTIONS_SCHEMA,
    VIDEO_AUDIO_MIX_SCHEMA,
    VIDEO_PIP_SCHEMA,
    HTML_TO_VIDEO_SCHEMA,
]
